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

    def _make_request(self, ip: str = "1.2.3.4"):
        """Create a minimal mock Request with a client.host."""
        req = MagicMock()
        req.client = MagicMock()
        req.client.host = ip
        req.url.path = "/api/v1/documents"
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
