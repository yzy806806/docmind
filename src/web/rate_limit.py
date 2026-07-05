"""In-memory sliding-window rate limiter for the DocMind web server.

This module implements a simple per-client-IP rate limiter using a sliding
window algorithm.  It is designed for the self-hosted single-instance
deployment and requires no external dependencies (no Redis, no slowapi).

* When ``config.rate_limit.enabled`` is False (the default), every request
  passes through unchecked — preserving the existing open behaviour.
* When enabled, each client IP is allowed at most
  ``config.rate_limit.requests_per_minute`` requests per rolling 60-second
  window.  Requests beyond the limit receive a ``429 Too Many Requests``
  response with a ``Retry-After`` header indicating how many seconds to
  wait before retrying.

Public routes (``/login``, ``/health``, ``/docs``, ``/openapi.json``,
``/redoc``, static files) are exempt from rate limiting, matching the
behaviour of the auth middleware.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from ..core.config import config
from .auth import PUBLIC_PATHS, PUBLIC_PREFIXES

logger = logging.getLogger(__name__)

# Sliding window duration in seconds (1 minute).
_WINDOW_SECONDS: int = 60


def _is_exempt(path: str) -> bool:
    """Return True if ``path`` should not be rate limited.

    Mirrors the auth middleware's notion of public paths so that health
    checks, API docs, and static assets are never throttled.
    """
    if path in PUBLIC_PATHS:
        return True
    for prefix in PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


class RateLimiter:
    """Per-client sliding-window rate limiter.

    Maintains a mapping of client IP → list of request timestamps within
    the current window.  Expired timestamps are pruned on each check.

    This class is intentionally lightweight and in-process.  For
    multi-worker deployments a shared backend (Redis) would be needed, but
    that is out of scope for the current single-instance deployment.
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: int = _WINDOW_SECONDS,
        trusted_proxy_ips: set[str] | None = None,
        cleanup_interval: int = 1000,
    ) -> None:
        self._max_requests: int = max_requests
        self._window: int = window_seconds
        self._buckets: dict[str, list[float]] = defaultdict(list)
        self._trusted_proxy_ips: set[str] = trusted_proxy_ips or set()
        self._cleanup_interval: int = cleanup_interval
        self._check_count: int = 0

    @property
    def max_requests(self) -> int:
        return self._max_requests

    @property
    def window_seconds(self) -> int:
        return self._window

    def _client_key(self, request: Request) -> str:
        """Extract a client identifier from the request.

        SECURITY: We use ``request.client.host`` (the direct TCP peer IP)
        by default.  The ``X-Forwarded-For`` header is NOT trusted unless
        the direct peer IP is in ``self._trusted_proxy_ips``.

        Rationale: ``X-Forwarded-For`` is trivially spoofable by any
        client.  If we blindly trusted it, an attacker could send a
        different fake IP in every request to get a fresh rate-limit
        bucket each time, effectively bypassing rate limiting entirely.

        When the direct peer IS a trusted proxy, we use the **rightmost**
        (last) entry in the XFF chain — this is the IP appended by the
        trusted proxy itself and represents the real client as seen by
        the proxy.  The **leftmost** entry is client-controlled and must
        never be used, as an attacker can prepend arbitrary IPs.

        Example::

            X-Forwarded-For: 1.1.1.1, 2.2.2.2, 203.0.113.5
                             ^leftmost         ^rightmost
                             (spoofable)       (proxy-set, trusted)

        The trusted proxy MUST strip or overwrite any incoming
        ``X-Forwarded-For`` header before appending the real client IP.
        If the proxy preserves client-supplied entries, the rightmost
        entry is still safe (it's the one the proxy appended), but the
        chain may contain attacker-controlled entries before it.

        If the client is unknown (e.g. direct connection with no ASGI
        client info), falls back to ``"unknown"``.
        """
        client = request.client
        direct_ip = client.host if client and client.host else ""

        # No trusted proxies configured — use direct IP.
        if not self._trusted_proxy_ips:
            return direct_ip or "unknown"

        # Request is not from a trusted proxy — use direct IP.
        if direct_ip not in self._trusted_proxy_ips:
            return direct_ip or "unknown"

        # Request IS from a trusted proxy — extract the rightmost
        # X-Forwarded-For entry (the one appended by the proxy).
        xff = request.headers.get("x-forwarded-for", "")
        if not xff:
            return direct_ip or "unknown"

        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if not parts:
            return direct_ip or "unknown"

        # Rightmost entry = the real client IP as seen by the proxy.
        return parts[-1]

    def _prune(self, key: str, now: float) -> None:
        """Remove timestamps older than the window from the bucket."""
        cutoff = now - self._window
        bucket = self._buckets[key]
        # Filter in-place to avoid churn on the defaultdict.
        self._buckets[key] = [ts for ts in bucket if ts > cutoff]

    def _cleanup_empty_buckets(self) -> None:
        """Remove buckets with no active timestamps.

        Called periodically (every ``_cleanup_interval`` checks) to
        prevent unbounded growth of ``_buckets`` as new client IPs
        come and go.  Without this, every unique IP that ever made
        a request would leave a permanent (empty) entry in the dict.

        Also prunes stale entries from all buckets during this sweep
        so that buckets whose timestamps have all expired get cleaned
        up even if the IP hasn't made a recent request.
        """
        now = time.monotonic()
        cutoff = now - self._window
        empty_keys = []
        for k, bucket in list(self._buckets.items()):
            # Prune stale entries from this bucket.
            self._buckets[k] = [ts for ts in bucket if ts > cutoff]
            if not self._buckets[k]:
                empty_keys.append(k)
        for k in empty_keys:
            del self._buckets[k]

    def check(self, request: Request) -> tuple[bool, int]:
        """Check whether the request is allowed.

        Returns a tuple ``(allowed, retry_after)`` where ``allowed`` is
        True if the request should proceed and ``retry_after`` is the
        number of seconds the client should wait before retrying (only
        meaningful when ``allowed`` is False).

        Note: ``retry_after`` is an integer count of seconds (delta-seconds),
        not an HTTP-date.  This is valid per RFC 7231 §7.1.3, which allows
        the ``Retry-After`` header to be either an HTTP-date or a
        delta-seconds value.  We use ``time.monotonic()`` for the internal
        sliding window (immune to system clock adjustments) and convert
        the remaining window time to an integer second count for the
        ``Retry-After`` header and JSON body.
        """
        key = self._client_key(request)
        now = time.monotonic()
        self._prune(key, now)

        # Periodically clean up empty buckets to prevent memory growth.
        self._check_count += 1
        if self._check_count >= self._cleanup_interval:
            self._check_count = 0
            self._cleanup_empty_buckets()

        bucket = self._buckets[key]

        if len(bucket) < self._max_requests:
            bucket.append(now)
            return True, 0

        # Window is full — compute how long until the oldest entry expires.
        oldest = bucket[0]
        retry_after = int(oldest + self._window - now) + 1  # ceil to at least 1s
        if retry_after < 1:
            retry_after = 1
        return False, retry_after

    def reset(self) -> None:
        """Clear all stored request timestamps (used in tests)."""
        self._buckets.clear()


# Module-level singleton, initialised lazily by ``get_rate_limiter``.
_rate_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    """Return the process-wide RateLimiter instance.

    The limiter is created on first access using the current
    ``config.rate_limit.requests_per_minute`` value.  Subsequent calls
    return the same instance so that the window state persists across
    requests.  Tests can call ``reset()`` to clear the state between
    cases.
    """
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(
            max_requests=config.rate_limit.requests_per_minute,
            trusted_proxy_ips=config.rate_limit.trusted_proxy_ip_set,
        )
    return _rate_limiter


def _reinit_rate_limiter() -> None:
    """Discard the cached limiter so the next ``get_rate_limiter`` picks
    up the current config value.

    Called when the rate-limit settings are changed at runtime (e.g. via
    the settings page) or in tests that modify the config.
    """
    global _rate_limiter
    _rate_limiter = None


async def rate_limit_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """FastAPI/Starlette middleware: enforce per-IP rate limiting.

    When ``config.rate_limit.enabled`` is False this is a no-op
    pass-through.  Public paths (docs, health, static) are always exempt.
    """
    if not config.rate_limit.enabled:
        return await call_next(request)

    path = request.url.path
    if _is_exempt(path):
        return await call_next(request)

    limiter = get_rate_limiter()
    allowed, retry_after = limiter.check(request)
    if not allowed:
        logger.warning(
            "Rate limit exceeded for client %s on %s (max %d/min)",
            request.client.host if request.client else "unknown",
            path,
            limiter.max_requests,
        )
        return JSONResponse(
            status_code=429,
            content={
                "error": "RATE_LIMIT",
                "message": "Too many requests. Please slow down.",
                "retry_after": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )

    return await call_next(request)
