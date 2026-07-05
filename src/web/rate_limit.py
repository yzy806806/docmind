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

    def __init__(self, max_requests: int, window_seconds: int = _WINDOW_SECONDS) -> None:
        self._max_requests: int = max_requests
        self._window: int = window_seconds
        self._buckets: dict[str, list[float]] = defaultdict(list)

    @property
    def max_requests(self) -> int:
        return self._max_requests

    @property
    def window_seconds(self) -> int:
        return self._window

    def _client_key(self, request: Request) -> str:
        """Extract a client identifier from the request.

        Uses the direct remote IP (``request.client.host``).  If the
        client is unknown (e.g. proxied requests without proper ASGI
        config), falls back to ``"unknown"``.
        """
        client = request.client
        if client and client.host:
            return client.host
        return "unknown"

    def _prune(self, key: str, now: float) -> None:
        """Remove timestamps older than the window from the bucket."""
        cutoff = now - self._window
        bucket = self._buckets[key]
        # Filter in-place to avoid churn on the defaultdict.
        self._buckets[key] = [ts for ts in bucket if ts > cutoff]

    def check(self, request: Request) -> tuple[bool, int]:
        """Check whether the request is allowed.

        Returns a tuple ``(allowed, retry_after)`` where ``allowed`` is
        True if the request should proceed and ``retry_after`` is the
        number of seconds the client should wait before retrying (only
        meaningful when ``allowed`` is False).
        """
        key = self._client_key(request)
        now = time.monotonic()
        self._prune(key, now)
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
