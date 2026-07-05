"""Tests for the rate limiting middleware.

Covers:
- RateLimiter class: sliding window logic, pruning, retry_after computation
- rate_limit_middleware: passthrough when disabled, 429 when over limit,
  Retry-After header, exempt paths, per-IP isolation
- RateLimitConfig: env var parsing, defaults, wiring into Config
- Integration via ASGI client: end-to-end 429 response shape
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    """Provide a temporary database file path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "test_rate_limit.db")


@pytest.fixture
async def asgi_client_rate_limited(tmp_db_path: str):
    """ASGI client with rate limiting ENABLED at a low threshold.

    Uses 3 requests/minute so tests can exhaust the limit quickly.
    Auth is disabled to isolate rate-limit behaviour.
    """
    import httpx
    from src.core.db_sqlite import Database
    from src.core.config import config
    from src.web import server
    from src.web.rate_limit import _reinit_rate_limiter, get_rate_limiter

    db = Database(db_path=tmp_db_path)
    await db.connect()

    # Save originals.
    orig_rl_enabled = config.rate_limit.enabled
    orig_rl_rpm = config.rate_limit.requests_per_minute
    orig_auth_enabled = config.auth.enabled

    # Configure rate limiting.
    config.rate_limit.enabled = True
    config.rate_limit.requests_per_minute = 3
    config.auth.enabled = False
    _reinit_rate_limiter()
    get_rate_limiter().reset()

    mock_queue = MagicMock()
    mock_queue.enqueue = AsyncMock(return_value=MagicMock(id="test-job-id"))

    original_db = server._db
    original_queue = server._queue
    server._db = db
    server._queue = mock_queue

    app = server.create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await db.disconnect()
    server._db = original_db
    server._queue = original_queue
    config.rate_limit.enabled = orig_rl_enabled
    config.rate_limit.requests_per_minute = orig_rl_rpm
    config.auth.enabled = orig_auth_enabled
    _reinit_rate_limiter()


@pytest.fixture
async def asgi_client_rate_disabled(tmp_db_path: str):
    """ASGI client with rate limiting DISABLED (passthrough)."""
    import httpx
    from src.core.db_sqlite import Database
    from src.core.config import config
    from src.web import server
    from src.web.rate_limit import _reinit_rate_limiter, get_rate_limiter

    db = Database(db_path=tmp_db_path)
    await db.connect()

    orig_rl_enabled = config.rate_limit.enabled
    orig_auth_enabled = config.auth.enabled

    config.rate_limit.enabled = False
    config.auth.enabled = False
    _reinit_rate_limiter()
    get_rate_limiter().reset()

    mock_queue = MagicMock()
    mock_queue.enqueue = AsyncMock(return_value=MagicMock(id="test-job-id"))

    original_db = server._db
    original_queue = server._queue
    server._db = db
    server._queue = mock_queue

    app = server.create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await db.disconnect()
    server._db = original_db
    server._queue = original_queue
    config.rate_limit.enabled = orig_rl_enabled
    config.auth.enabled = orig_auth_enabled
    _reinit_rate_limiter()


# ── RateLimitConfig ──────────────────────────────────────────────


class TestRateLimitConfig:
    """Tests for the RateLimitConfig dataclass and Config wiring."""

    def test_config_has_rate_limit_field(self):
        from src.core.config import config, RateLimitConfig

        assert hasattr(config, "rate_limit")
        assert isinstance(config.rate_limit, RateLimitConfig)

    def test_defaults(self):
        from src.core.config import RateLimitConfig

        rl = RateLimitConfig()
        # Default is False (matching the open self-hosted deployment).
        assert rl.enabled is False
        assert rl.requests_per_minute == 60

    def test_env_var_parsing(self, monkeypatch):
        """RateLimitConfig should read DOCMIND_RATE_LIMIT_* env vars."""
        # We need to re-import to pick up env changes, but since
        # RateLimitConfig uses default_factory lambdas we can test the
        # _env_bool/_env_int helpers directly.
        from src.core.config import _env_bool, _env_int

        monkeypatch.setenv("DOCMIND_RATE_LIMIT_ENABLED", "1")
        assert _env_bool("DOCMIND_RATE_LIMIT_ENABLED", False) is True

        monkeypatch.setenv("DOCMIND_RATE_LIMIT_ENABLED", "0")
        assert _env_bool("DOCMIND_RATE_LIMIT_ENABLED", False) is False

        monkeypatch.setenv("DOCMIND_RATE_LIMIT_REQUESTS_PER_MINUTE", "120")
        assert _env_int("DOCMIND_RATE_LIMIT_REQUESTS_PER_MINUTE", 60) == 120

        monkeypatch.setenv("DOCMIND_RATE_LIMIT_REQUESTS_PER_MINUTE", "not-a-number")
        assert _env_int("DOCMIND_RATE_LIMIT_REQUESTS_PER_MINUTE", 60) == 60


# ── RateLimiter unit tests ───────────────────────────────────────


class TestRateLimiter:
    """Unit tests for the RateLimiter sliding-window logic."""

    def _make_request(self, ip: str = "1.2.3.4", xff: str | None = None):
        """Create a minimal mock Request with a client.host.

        Args:
            ip: The direct peer IP (request.client.host).
            xff: Optional X-Forwarded-For header value.
        """
        req = MagicMock()
        req.client = MagicMock()
        req.client.host = ip
        req.url.path = "/api/v1/documents"
        # Mock headers dict — supports case-insensitive lookup via .get().
        headers: dict[str, str] = {}
        if xff is not None:
            headers["x-forwarded-for"] = xff
        req.headers = headers
        return req

    def test_allows_requests_under_limit(self):
        from src.web.rate_limit import RateLimiter

        rl = RateLimiter(max_requests=5)
        for i in range(5):
            allowed, retry = rl.check(self._make_request())
            assert allowed is True, f"Request {i+1} should be allowed"

    def test_blocks_request_over_limit(self):
        from src.web.rate_limit import RateLimiter

        rl = RateLimiter(max_requests=3)
        for i in range(3):
            allowed, _ = rl.check(self._make_request())
            assert allowed is True

        allowed, retry_after = rl.check(self._make_request())
        assert allowed is False
        assert retry_after >= 1

    def test_retry_after_is_positive_integer(self):
        from src.web.rate_limit import RateLimiter

        rl = RateLimiter(max_requests=1)
        rl.check(self._make_request())  # exhaust limit

        allowed, retry_after = rl.check(self._make_request())
        assert allowed is False
        assert isinstance(retry_after, int)
        assert retry_after >= 1
        assert retry_after <= 60

    def test_per_ip_isolation(self):
        """Requests from different IPs should have independent limits."""
        from src.web.rate_limit import RateLimiter

        rl = RateLimiter(max_requests=2)

        # IP A exhausts its limit.
        req_a = self._make_request("10.0.0.1")
        rl.check(req_a)
        rl.check(req_a)
        allowed_a, _ = rl.check(req_a)
        assert allowed_a is False

        # IP B should still be allowed.
        req_b = self._make_request("10.0.0.2")
        allowed_b, _ = rl.check(req_b)
        assert allowed_b is True

    def test_reset_clears_state(self):
        from src.web.rate_limit import RateLimiter

        rl = RateLimiter(max_requests=1)
        rl.check(self._make_request())
        allowed, _ = rl.check(self._make_request())
        assert allowed is False

        rl.reset()
        allowed, _ = rl.check(self._make_request())
        assert allowed is True

    def test_window_expiry_frees_slot(self):
        """After the window expires, requests should be allowed again."""
        from src.web.rate_limit import RateLimiter

        rl = RateLimiter(max_requests=1, window_seconds=1)

        # Exhaust the limit.
        allowed, _ = rl.check(self._make_request())
        assert allowed is True
        allowed, _ = rl.check(self._make_request())
        assert allowed is False

        # Wait for the window to expire.
        time.sleep(1.1)

        allowed, _ = rl.check(self._make_request())
        assert allowed is True

    def test_unknown_client_fallback(self):
        """When request.client is None, key falls back to 'unknown'."""
        from src.web.rate_limit import RateLimiter

        rl = RateLimiter(max_requests=1)
        req = MagicMock()
        req.client = None
        req.url.path = "/test"

        allowed, _ = rl.check(req)
        assert allowed is True

    def test_max_requests_property(self):
        from src.web.rate_limit import RateLimiter

        rl = RateLimiter(max_requests=42)
        assert rl.max_requests == 42

    def test_window_seconds_property(self):
        from src.web.rate_limit import RateLimiter

        rl = RateLimiter(max_requests=10, window_seconds=30)
        assert rl.window_seconds == 30

    def test_pruning_removes_old_entries(self):
        """Old timestamps should be pruned on each check."""
        from src.web.rate_limit import RateLimiter

        rl = RateLimiter(max_requests=5, window_seconds=1)

        # Add 3 requests.
        for _ in range(3):
            rl.check(self._make_request())

        # All from same IP, so bucket should have 3 entries.
        key = "1.2.3.4"
        assert len(rl._buckets[key]) == 3

        # Wait for expiry.
        time.sleep(1.1)

        # Next check should prune all old entries and succeed.
        allowed, _ = rl.check(self._make_request())
        assert allowed is True
        # Only the new entry should remain.
        assert len(rl._buckets[key]) == 1


# ── get_rate_limiter / _reinit_rate_limiter ──────────────────────


class TestRateLimiterSingleton:
    """Tests for the module-level singleton management."""

    def test_get_rate_limiter_returns_same_instance(self):
        from src.web.rate_limit import get_rate_limiter, _reinit_rate_limiter

        _reinit_rate_limiter()
        rl1 = get_rate_limiter()
        rl2 = get_rate_limiter()
        assert rl1 is rl2

    def test_reinit_creates_new_instance(self):
        from src.web.rate_limit import get_rate_limiter, _reinit_rate_limiter

        rl1 = get_rate_limiter()
        _reinit_rate_limiter()
        rl2 = get_rate_limiter()
        assert rl1 is not rl2


# ── _is_exempt ───────────────────────────────────────────────────


class TestIsExempt:
    """Tests for the path exemption logic."""

    @pytest.mark.parametrize("path", [
        "/login",
        "/logout",
        "/health",
        "/docs",
        "/redoc",
        "/openapi.json",
    ])
    def test_public_paths_exempt(self, path):
        from src.web.rate_limit import _is_exempt

        assert _is_exempt(path) is True

    @pytest.mark.parametrize("path", [
        "/static/css/main.css",
        "/static/js/app.js",
        "/static/img/logo.png",
    ])
    def test_static_prefix_exempt(self, path):
        from src.web.rate_limit import _is_exempt

        assert _is_exempt(path) is True

    @pytest.mark.parametrize("path", [
        "/",
        "/documents",
        "/api/v1/documents",
        "/search",
        "/upload",
        "/jobs",
        "/analytics",
        "/collections/1",
    ])
    def test_api_and_app_paths_not_exempt(self, path):
        from src.web.rate_limit import _is_exempt

        assert _is_exempt(path) is False


# ── Middleware integration tests ─────────────────────────────────


class TestRateLimitMiddlewareDisabled:
    """When rate limiting is disabled, all requests pass through."""

    @pytest.mark.asyncio
    async def test_dashboard_accessible(self, asgi_client_rate_disabled):
        resp = await asgi_client_rate_disabled.get("/")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_429_under_rapid_requests(self, asgi_client_rate_disabled):
        """Even many rapid requests should not trigger 429 when disabled."""
        for _ in range(20):
            resp = await asgi_client_rate_disabled.get("/")
            assert resp.status_code != 429


class TestRateLimitMiddlewareEnabled:
    """When rate limiting is enabled, over-limit requests get 429."""

    @pytest.mark.asyncio
    async def test_allows_requests_under_limit(self, asgi_client_rate_limited):
        """Requests within the limit should succeed (200, not 429)."""
        for _ in range(3):
            resp = await asgi_client_rate_limited.get("/")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_blocks_request_over_limit(self, asgi_client_rate_limited):
        """The 4th request (limit is 3) should return 429."""
        for _ in range(3):
            resp = await asgi_client_rate_limited.get("/")
            assert resp.status_code == 200

        resp = await asgi_client_rate_limited.get("/")
        assert resp.status_code == 429

    @pytest.mark.asyncio
    async def test_429_has_retry_after_header(self, asgi_client_rate_limited):
        """429 response must include a Retry-After header."""
        for _ in range(3):
            await asgi_client_rate_limited.get("/")

        resp = await asgi_client_rate_limited.get("/")
        assert resp.status_code == 429
        assert "retry-after" in {k.lower() for k in resp.headers.keys()}
        retry_after = int(resp.headers["retry-after"])
        assert retry_after >= 1
        assert retry_after <= 60

    @pytest.mark.asyncio
    async def test_429_response_body_shape(self, asgi_client_rate_limited):
        """429 response should have error, message, and retry_after fields."""
        for _ in range(3):
            await asgi_client_rate_limited.get("/")

        resp = await asgi_client_rate_limited.get("/")
        assert resp.status_code == 429
        body = resp.json()
        assert body["error"] == "RATE_LIMIT"
        assert "message" in body
        assert "retry_after" in body
        assert isinstance(body["retry_after"], int)

    @pytest.mark.asyncio
    async def test_exempt_paths_not_limited(self, asgi_client_rate_limited):
        """Public paths like /health should not be rate limited."""
        # Exhaust the limit on non-exempt paths first.
        for _ in range(3):
            await asgi_client_rate_limited.get("/")

        # /health should still be accessible despite rate limit being exhausted.
        resp = await asgi_client_rate_limited.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_exempt_paths_repeated_not_limited(self, asgi_client_rate_limited):
        """Many requests to exempt paths should never trigger 429."""
        for _ in range(20):
            resp = await asgi_client_rate_limited.get("/health")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_rate_limit_resets_per_test(self, asgi_client_rate_limited):
        """Each fixture instance starts with a fresh limiter state.

        This test simply verifies that we can make 3 requests (the limit)
        without getting 429, proving the state was reset.
        """
        for _ in range(3):
            resp = await asgi_client_rate_limited.get("/")
            assert resp.status_code == 200


# ── _client_key: X-Forwarded-For spoofing protection ─────────────


class TestClientKeyForwardedFor:
    """Tests for _client_key() X-Forwarded-For handling.

    When deployed behind a reverse proxy with --proxy-headers, the
    rate limiter must use the rightmost XFF entry (set by the trusted
    proxy) rather than the leftmost (client-controlled) to prevent
    IP spoofing bypass.
    """

    def _make_request_with_headers(self, client_ip: str, headers: dict | None = None):
        """Create a mock Request with client.host and optional headers."""
        req = MagicMock()
        req.client = MagicMock()
        req.client.host = client_ip
        req.url.path = "/api/v1/documents"
        # Starlette headers are case-insensitive; mock .get() and .getlist().
        headers = headers or {}
        req.headers = MagicMock()
        req.headers.get = MagicMock(
            side_effect=lambda key, default="": headers.get(key.lower(), default)
        )
        # .getlist returns a list of values for a given key.
        xff = headers.get("x-forwarded-for", "")
        xff_list = [v.strip() for v in xff.split(",")] if xff else []
        req.headers.getlist = MagicMock(
            side_effect=lambda key: xff_list if key.lower() == "x-forwarded-for" else []
        )
        return req

    def test_no_xff_uses_client_host(self):
        """Without X-Forwarded-For, _client_key uses request.client.host."""
        from src.web.rate_limit import RateLimiter

        rl = RateLimiter(max_requests=5)
        req = self._make_request_with_headers("1.2.3.4")
        assert rl._client_key(req) == "1.2.3.4"

    def test_xff_ignored_without_trusted_proxies(self):
        """When no trusted_proxy_ips configured, XFF is ignored entirely."""
        from src.web.rate_limit import RateLimiter

        rl = RateLimiter(max_requests=5, trusted_proxy_ips=set())
        req = self._make_request_with_headers(
            "10.0.0.1",
            headers={"x-forwarded-for": "99.99.99.99"},
        )
        # Should use direct connection IP, not XFF.
        assert rl._client_key(req) == "10.0.0.1"

    def test_xff_used_when_client_is_trusted_proxy(self):
        """When request comes from a trusted proxy, use rightmost XFF entry."""
        from src.web.rate_limit import RateLimiter

        rl = RateLimiter(
            max_requests=5,
            trusted_proxy_ips={"10.0.0.1"},
        )
        req = self._make_request_with_headers(
            "10.0.0.1",
            headers={"x-forwarded-for": "203.0.113.5"},
        )
        assert rl._client_key(req) == "203.0.113.5"

    def test_xff_rightmost_used_not_leftmost(self):
        """Use the rightmost XFF entry (proxy-set), not leftmost (client-controlled).

        This is the core anti-spoofing test: a malicious client sends
        X-Forwarded-For: 1.1.1.1, 2.2.2.2, 203.0.113.5
        The trusted proxy (10.0.0.1) appends the real client IP.
        We should use the rightmost (203.0.113.5), not the leftmost (1.1.1.1).
        """
        from src.web.rate_limit import RateLimiter

        rl = RateLimiter(
            max_requests=5,
            trusted_proxy_ips={"10.0.0.1"},
        )
        req = self._make_request_with_headers(
            "10.0.0.1",
            headers={"x-forwarded-for": "1.1.1.1, 2.2.2.2, 203.0.113.5"},
        )
        assert rl._client_key(req) == "203.0.113.5"

    def test_xff_untrusted_proxy_ignored(self):
        """XFF from a non-trusted proxy IP should be ignored."""
        from src.web.rate_limit import RateLimiter

        rl = RateLimiter(
            max_requests=5,
            trusted_proxy_ips={"10.0.0.1"},
        )
        req = self._make_request_with_headers(
            "10.0.0.2",  # NOT a trusted proxy
            headers={"x-forwarded-for": "99.99.99.99"},
        )
        assert rl._client_key(req) == "10.0.0.2"

    def test_xff_empty_string_falls_back_to_client_host(self):
        """Empty XFF header falls back to request.client.host."""
        from src.web.rate_limit import RateLimiter

        rl = RateLimiter(
            max_requests=5,
            trusted_proxy_ips={"10.0.0.1"},
        )
        req = self._make_request_with_headers(
            "10.0.0.1",
            headers={"x-forwarded-for": ""},
        )
        assert rl._client_key(req) == "10.0.0.1"

    def test_xff_only_spaces_falls_back_to_client_host(self):
        """XFF with only commas/spaces falls back to request.client.host."""
        from src.web.rate_limit import RateLimiter

        rl = RateLimiter(
            max_requests=5,
            trusted_proxy_ips={"10.0.0.1"},
        )
        req = self._make_request_with_headers(
            "10.0.0.1",
            headers={"x-forwarded-for": " ,  ,  "},
        )
        assert rl._client_key(req) == "10.0.0.1"

    def test_multiple_trusted_proxies(self):
        """Multiple trusted proxy IPs are all accepted."""
        from src.web.rate_limit import RateLimiter

        rl = RateLimiter(
            max_requests=5,
            trusted_proxy_ips={"10.0.0.1", "10.0.0.2", "192.168.1.1"},
        )
        req = self._make_request_with_headers(
            "192.168.1.1",
            headers={"x-forwarded-for": "203.0.113.10"},
        )
        assert rl._client_key(req) == "203.0.113.10"

    def test_spoofed_xff_does_not_create_separate_buckets(self):
        """A client cannot bypass rate limiting by rotating XFF values.

        Without trusted proxy validation, each spoofed IP would get its
        own bucket. With the fix, the rightmost entry from a trusted
        proxy is used consistently.
        """
        from src.web.rate_limit import RateLimiter

        rl = RateLimiter(
            max_requests=2,
            trusted_proxy_ips={"10.0.0.1"},
        )

        # Client sends different spoofed leftmost XFF values each time.
        spoofed_ips = ["1.1.1.1", "2.2.2.2", "3.3.3.3"]
        for spoofed in spoofed_ips:
            req = self._make_request_with_headers(
                "10.0.0.1",
                headers={"x-forwarded-for": f"{spoofed}, 203.0.113.5"},
            )
            allowed, _ = rl.check(req)

        # After 2 requests from real IP 203.0.113.5, the 3rd should be blocked.
        req4 = self._make_request_with_headers(
            "10.0.0.1",
            headers={"x-forwarded-for": "4.4.4.4, 203.0.113.5"},
        )
        allowed, _ = rl.check(req4)
        assert allowed is False, "Spoofed XFF should not create separate buckets"


# ── Bucket cleanup ───────────────────────────────────────────────


class TestBucketCleanup:
    """Tests for periodic pruning of empty bucket entries.

    The _buckets dict can grow unboundedly as new IPs are seen. After
    pruning, entries that become empty should be periodically removed
    to prevent memory growth.
    """

    def _make_request(self, ip: str = "1.2.3.4"):
        req = MagicMock()
        req.client = MagicMock()
        req.client.host = ip
        req.url.path = "/api/v1/documents"
        req.headers = MagicMock()
        req.headers.get = MagicMock(return_value="")
        req.headers.getlist = MagicMock(return_value=[])
        return req

    def test_empty_bucket_removed_after_cleanup(self):
        """After cleanup, empty bucket entries are removed from _buckets."""
        from src.web.rate_limit import RateLimiter

        rl = RateLimiter(max_requests=5, window_seconds=1, cleanup_interval=3)

        # Make requests from a few IPs.
        for ip in ["1.1.1.1", "2.2.2.2", "3.3.3.3"]:
            rl.check(self._make_request(ip))

        assert len(rl._buckets) == 3

        # Wait for window to expire.
        time.sleep(1.1)

        # Next check triggers pruning; after cleanup_interval checks,
        # empty entries should be removed.
        rl.check(self._make_request("4.4.4.4"))  # check 1 (prunes, doesn't clean)
        rl.check(self._make_request("4.4.4.4"))  # check 2
        rl.check(self._make_request("4.4.4.4"))  # check 3 — triggers cleanup

        # The expired IPs should have been cleaned up.
        assert "1.1.1.1" not in rl._buckets
        assert "2.2.2.2" not in rl._buckets
        assert "3.3.3.3" not in rl._buckets
        # Current IP should still be there.
        assert "4.4.4.4" in rl._buckets

    def test_non_empty_buckets_not_removed(self):
        """Buckets with active entries are not removed during cleanup."""
        from src.web.rate_limit import RateLimiter

        rl = RateLimiter(max_requests=5, cleanup_interval=2)

        rl.check(self._make_request("1.1.1.1"))

        # Trigger cleanup (2 checks).
        rl.check(self._make_request("1.1.1.1"))
        rl.check(self._make_request("1.1.1.1"))

        # Bucket should still exist (has active entries).
        assert "1.1.1.1" in rl._buckets
        assert len(rl._buckets["1.1.1.1"]) > 0


# ── Config: trusted_proxy_ip_set property ────────────────────────


class TestTrustedProxyIpSet:
    """Tests for RateLimitConfig.trusted_proxy_ip_set parsing."""

    def test_empty_string_returns_empty_set(self):
        from src.core.config import RateLimitConfig

        rc = RateLimitConfig()
        assert rc.trusted_proxy_ip_set == set()

    def test_single_ip(self):
        from src.core.config import RateLimitConfig

        rc = RateLimitConfig(trusted_proxy_ips="10.0.0.1")
        assert rc.trusted_proxy_ip_set == {"10.0.0.1"}

    def test_multiple_ips_comma_separated(self):
        from src.core.config import RateLimitConfig

        rc = RateLimitConfig(trusted_proxy_ips="10.0.0.1, 10.0.0.2, 192.168.1.1")
        assert rc.trusted_proxy_ip_set == {"10.0.0.1", "10.0.0.2", "192.168.1.1"}

    def test_whitespace_stripped(self):
        from src.core.config import RateLimitConfig

        rc = RateLimitConfig(trusted_proxy_ips="  10.0.0.1  ,  10.0.0.2  ")
        assert rc.trusted_proxy_ip_set == {"10.0.0.1", "10.0.0.2"}

    def test_empty_entries_ignored(self):
        from src.core.config import RateLimitConfig

        rc = RateLimitConfig(trusted_proxy_ips="10.0.0.1,,  ,10.0.0.2")
        assert rc.trusted_proxy_ip_set == {"10.0.0.1", "10.0.0.2"}
