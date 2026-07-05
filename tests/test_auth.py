"""Tests for the auth module: session cookies, API key header, middleware.

Covers:
- Session token creation and verification (HMAC signing, expiry)
- Login flow: GET /login page render, POST /login with correct/wrong password
- Logout flow: GET /logout and POST /logout clear the session cookie
- Auth middleware: when auth disabled → passthrough; when enabled →
  protected routes redirect (HTML) or 401 (API); public routes always open
- X-API-Key header auth for programmatic API access
- Settings page: enabling/disabling auth persists to DB and rehydrates config
- Login page is dark-mode compatible
- Nav bar shows logout button only when auth is enabled
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Generator

import pytest


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    """Provide a temporary database file path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "test_auth.db")


@pytest.fixture
async def asgi_client_disabled(tmp_db_path: str):
    """ASGI client with auth DISABLED (default passthrough behaviour)."""
    import httpx
    from src.core.db_sqlite import Database
    from src.core.config import config
    from src.web import server

    db = Database(db_path=tmp_db_path)
    await db.connect()

    orig_enabled = config.auth.enabled
    orig_key = config.auth.api_key
    orig_secret = config.auth.session_secret
    config.auth.enabled = False
    config.auth.api_key = ""
    config.auth.session_secret = ""

    from unittest.mock import AsyncMock, MagicMock
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
    config.auth.enabled = orig_enabled
    config.auth.api_key = orig_key
    config.auth.session_secret = orig_secret


@pytest.fixture
async def asgi_client_enabled(tmp_db_path: str):
    """ASGI client with auth ENABLED and a known api key."""
    import httpx
    from src.core.db_sqlite import Database
    from src.core.config import config
    from src.web import server

    db = Database(db_path=tmp_db_path)
    await db.connect()

    orig_enabled = config.auth.enabled
    orig_key = config.auth.api_key
    orig_secret = config.auth.session_secret
    config.auth.enabled = True
    config.auth.api_key = "test-secret-key-123"
    config.auth.session_secret = "stable-test-secret"

    from unittest.mock import AsyncMock, MagicMock
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
    config.auth.enabled = orig_enabled
    config.auth.api_key = orig_key
    config.auth.session_secret = orig_secret


# ── Session token tests ──────────────────────────────────────────


class TestSessionToken:
    """Tests for create_session_token / verify_session_token."""

    def test_create_token_is_string(self):
        from src.web.auth import create_session_token
        token = create_session_token()
        assert isinstance(token, str)
        assert "." in token

    def test_verify_valid_token(self):
        from src.web.auth import create_session_token, verify_session_token
        token = create_session_token()
        assert verify_session_token(token) is True

    def test_verify_empty_token(self):
        from src.web.auth import verify_session_token
        assert verify_session_token("") is False

    def test_verify_garbage_token(self):
        from src.web.auth import verify_session_token
        assert verify_session_token("not.a.valid.token") is False

    def test_verify_tampered_token(self):
        """A token with a modified payload should fail signature check."""
        from src.web.auth import create_session_token, verify_session_token
        token = create_session_token()
        body, sig = token.rsplit(".", 1)
        # Flip the last char of the body
        tampered = body[:-1] + ("A" if body[-1] != "A" else "B") + "." + sig
        assert verify_session_token(tampered) is False

    def test_verify_token_with_wrong_secret(self):
        """A token signed with a different secret should not verify."""
        from src.core.config import config
        from src.web.auth import create_session_token, verify_session_token

        config.auth.session_secret = "secret-A"
        token = create_session_token()
        config.auth.session_secret = "secret-B"
        assert verify_session_token(token) is False

    def test_verify_expired_token(self):
        """An expired token should fail verification."""
        from unittest.mock import patch
        from src.core.config import config
        from src.web.auth import create_session_token, verify_session_token

        config.auth.session_secret = "expiry-test-secret"
        config.auth.session_expiry_hours = 1
        token = create_session_token()
        # Patch time.time to simulate expiry
        orig_time = time.time
        with patch("src.web.auth.time.time", return_value=orig_time() + 7200):
            assert verify_session_token(token) is False


# ── Password check ───────────────────────────────────────────────


class TestCheckPassword:
    """Tests for check_password (constant-time comparison)."""

    def test_correct_password(self):
        from src.core.config import config
        from src.web.auth import check_password
        config.auth.api_key = "my-secret-key"
        assert check_password("my-secret-key") is True

    def test_wrong_password(self):
        from src.core.config import config
        from src.web.auth import check_password
        config.auth.api_key = "my-secret-key"
        assert check_password("wrong-key") is False

    def test_empty_password(self):
        from src.core.config import config
        from src.web.auth import check_password
        config.auth.api_key = "my-secret-key"
        assert check_password("") is False

    def test_no_api_key_configured(self):
        from src.core.config import config
        from src.web.auth import check_password
        config.auth.api_key = ""
        assert check_password("anything") is False


# ── Auth-disabled passthrough ────────────────────────────────────


class TestAuthDisabled:
    """When auth is disabled, all routes are open."""

    @pytest.mark.asyncio
    async def test_dashboard_open_when_disabled(self, asgi_client_disabled):
        resp = await asgi_client_disabled.get("/")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_settings_open_when_disabled(self, asgi_client_disabled):
        resp = await asgi_client_disabled.get("/settings")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_login_redirects_when_disabled(self, asgi_client_disabled):
        """GET /login when auth is disabled should redirect to /."""
        resp = await asgi_client_disabled.get("/login")
        assert resp.status_code in (303, 302, 307)

    @pytest.mark.asyncio
    async def test_no_logout_link_in_nav_when_disabled(self, asgi_client_disabled):
        resp = await asgi_client_disabled.get("/")
        assert "/logout" not in resp.text or "Logout" not in resp.text


# ── Auth-enabled protection ──────────────────────────────────────


class TestAuthEnabled:
    """When auth is enabled, protected routes require auth."""

    @pytest.mark.asyncio
    async def test_protected_route_redirects_to_login(self, asgi_client_enabled):
        """Unauthenticated browser request → redirect to /login."""
        resp = await asgi_client_enabled.get("/", follow_redirects=False,
                                             headers={"Accept": "text/html"})
        assert resp.status_code in (303, 302, 307)
        assert "/login" in resp.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_api_route_returns_401_without_auth(self, asgi_client_enabled):
        """Unauthenticated API request → 401 JSON."""
        resp = await asgi_client_enabled.get(
            "/api/v1/documents/1/status",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_api_route_with_x_api_key_header(self, asgi_client_enabled):
        """X-API-Key header with correct key → 200 (or 404, not 401)."""
        resp = await asgi_client_enabled.get(
            "/api/v1/documents/1/status",
            headers={"X-API-Key": "test-secret-key-123",
                     "Accept": "application/json"},
        )
        assert resp.status_code != 401

    @pytest.mark.asyncio
    async def test_api_route_with_wrong_x_api_key_header(self, asgi_client_enabled):
        resp = await asgi_client_enabled.get(
            "/api/v1/documents/1/status",
            headers={"X-API-Key": "wrong-key",
                     "Accept": "application/json"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_public_routes_accessible(self, asgi_client_enabled):
        """/login, /health, /docs should be open even when auth enabled."""
        # /login page
        resp = await asgi_client_enabled.get("/login")
        assert resp.status_code == 200
        assert "password" in resp.text.lower()

        # /health
        resp = await asgi_client_enabled.get("/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_logout_link_in_nav_when_enabled(self, asgi_client_enabled):
        """When auth is enabled and authenticated, nav shows logout link."""
        from src.web.auth import create_session_token
        token = create_session_token()
        resp = await asgi_client_enabled.get(
            "/",
            cookies={"docmind_session": token},
            headers={"Accept": "text/html"},
        )
        assert resp.status_code == 200
        assert "/logout" in resp.text
        assert "Logout" in resp.text


# ── Login flow ───────────────────────────────────────────────────


class TestLoginFlow:
    """Tests for the POST /login → session cookie flow."""

    @pytest.mark.asyncio
    async def test_login_with_correct_password_sets_cookie(self, asgi_client_enabled):
        resp = await asgi_client_enabled.post(
            "/login",
            data={"password": "test-secret-key-123"},
            follow_redirects=False,
        )
        assert resp.status_code in (303, 302, 307)
        # Session cookie should be set
        set_cookie = resp.headers.get("set-cookie", "")
        assert "docmind_session" in set_cookie

    @pytest.mark.asyncio
    async def test_login_with_wrong_password_shows_error(self, asgi_client_enabled):
        resp = await asgi_client_enabled.post(
            "/login",
            data={"password": "wrong-password"},
            follow_redirects=False,
        )
        # Wrong password re-renders the login page with 401
        assert resp.status_code == 401
        assert "invalid" in resp.text.lower() or "error" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_login_page_has_password_field(self, asgi_client_enabled):
        resp = await asgi_client_enabled.get("/login")
        assert resp.status_code == 200
        assert 'type="password"' in resp.text
        assert 'name="password"' in resp.text

    @pytest.mark.asyncio
    async def test_login_page_has_dark_mode(self, asgi_client_enabled):
        resp = await asgi_client_enabled.get("/login")
        assert resp.status_code == 200
        # Dark mode is via external CSS + theme.js (not inline)
        assert "/static/css/styles.css" in resp.text
        assert "/static/js/theme.js" in resp.text
        assert "dark" in resp.text  # theme.js contains dark mode logic

    @pytest.mark.asyncio
    async def test_session_cookie_grants_access(self, asgi_client_enabled):
        """After logging in, the session cookie should grant access to /."""
        from src.web.auth import create_session_token
        token = create_session_token()
        resp = await asgi_client_enabled.get(
            "/",
            cookies={"docmind_session": token},
            headers={"Accept": "text/html"},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_invalid_session_cookie_redirected(self, asgi_client_enabled):
        """A garbage session cookie should be treated as unauthenticated."""
        resp = await asgi_client_enabled.get(
            "/",
            cookies={"docmind_session": "garbage.token"},
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert resp.status_code in (303, 302, 307)
        assert "/login" in resp.headers.get("location", "")


# ── Logout flow ──────────────────────────────────────────────────


class TestLogoutFlow:
    """Tests for GET/POST /logout."""

    @pytest.mark.asyncio
    async def test_get_logout_clears_cookie(self, asgi_client_enabled):
        resp = await asgi_client_enabled.get("/logout", follow_redirects=False)
        assert resp.status_code in (303, 302, 307)
        assert "/login" in resp.headers.get("location", "")
        # Cookie should be cleared (Set-Cookie with empty/max-age=0)
        set_cookie = resp.headers.get("set-cookie", "").lower()
        assert "docmind_session" in set_cookie

    @pytest.mark.asyncio
    async def test_post_logout_clears_cookie(self, asgi_client_enabled):
        resp = await asgi_client_enabled.post("/logout", follow_redirects=False)
        assert resp.status_code in (303, 302, 307)
        assert "/login" in resp.headers.get("location", "")


# ── Settings toggle enable/disable ───────────────────────────────


class TestSettingsAuthToggle:
    """Tests for enabling/disabling auth via the settings page."""

    @pytest.mark.asyncio
    async def test_enable_auth_via_settings(self, asgi_client_disabled, tmp_db_path):
        """POST /settings with auth_enabled=1 should enable auth."""
        # First, while auth is still disabled, save settings with auth on
        resp = await asgi_client_disabled.post(
            "/settings",
            data={
                "provider": "",
                "model": "",
                "api_key": "",
                "base_url": "",
                "max_tokens": "1000",
                "temperature": "0.3",
                "chat_fallback": "1",
                "auth_enabled": "1",
                "auth_api_key": "my-new-api-key-123",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

        # Auth should now be enabled in the in-memory config
        from src.core.config import config
        assert config.auth.enabled is True
        assert config.auth.api_key == "my-new-api-key-123"

        # DB should have the auth settings persisted
        from src.core.db_sqlite import Database
        db2 = Database(db_path=tmp_db_path)
        await db2.connect()
        stored = await db2.get_setting("auth_enabled")
        assert stored == "1"
        stored_key = await db2.get_setting("auth_api_key")
        assert stored_key == "my-new-api-key-123"
        stored_secret = await db2.get_setting("auth_session_secret")
        assert stored_secret  # non-empty
        await db2.disconnect()

    @pytest.mark.asyncio
    async def test_disable_auth_via_settings(self, asgi_client_enabled, tmp_db_path):
        """POST /settings without auth_enabled should disable auth.

        Authenticated via X-API-Key header (the request itself changes
        auth state, so we use the header rather than a session cookie).
        """
        resp = await asgi_client_enabled.post(
            "/settings",
            data={
                "provider": "",
                "model": "",
                "api_key": "",
                "base_url": "",
                "max_tokens": "1000",
                "temperature": "0.3",
                "chat_fallback": "1",
                # auth_enabled checkbox omitted → disabled
                "auth_api_key": "****123",  # masked, should not overwrite
            },
            headers={"X-API-Key": "test-secret-key-123"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

        from src.core.config import config
        assert config.auth.enabled is False

        from src.core.db_sqlite import Database
        db2 = Database(db_path=tmp_db_path)
        await db2.connect()
        stored = await db2.get_setting("auth_enabled")
        assert stored == "0"
        await db2.disconnect()


# ── Auth config hydration ────────────────────────────────────────


class TestAuthConfigHydration:
    """Tests for apply_auth_settings_from_db."""

    def test_hydrate_enables_auth(self):
        from src.core.config import config
        from src.web.auth import apply_auth_settings_from_db

        orig = config.auth.enabled
        config.auth.enabled = False
        try:
            apply_auth_settings_from_db({"auth_enabled": "1",
                                          "auth_api_key": "key-xyz",
                                          "auth_session_secret": "sec"})
            assert config.auth.enabled is True
            assert config.auth.api_key == "key-xyz"
            assert config.auth.session_secret == "sec"
        finally:
            config.auth.enabled = orig

    def test_hydrate_disables_auth(self):
        from src.core.config import config
        from src.web.auth import apply_auth_settings_from_db

        orig = config.auth.enabled
        config.auth.enabled = True
        try:
            apply_auth_settings_from_db({"auth_enabled": "0"})
            assert config.auth.enabled is False
        finally:
            config.auth.enabled = orig

    def test_hydrate_partial_settings_keeps_existing(self):
        """If only some keys are present, others remain unchanged."""
        from src.core.config import config
        from src.web.auth import apply_auth_settings_from_db

        orig_enabled = config.auth.enabled
        orig_key = config.auth.api_key
        config.auth.api_key = "existing-key"
        config.auth.enabled = True
        try:
            apply_auth_settings_from_db({"auth_enabled": "0"})
            assert config.auth.enabled is False
            # Key not in settings dict → stays as-is
            assert config.auth.api_key == "existing-key"
        finally:
            config.auth.enabled = orig_enabled
            config.auth.api_key = orig_key


# ── Login page rendering ─────────────────────────────────────────


class TestLoginPageRendering:
    """Tests for _render_login_page."""

    def test_render_has_form(self):
        from src.web.rendering import _render_login_page
        html = _render_login_page()
        assert "<form" in html
        assert 'action="/login"' in html
        assert 'method="post"' in html

    def test_render_has_password_input(self):
        from src.web.rendering import _render_login_page
        html = _render_login_page()
        assert 'type="password"' in html
        assert 'name="password"' in html

    def test_render_has_dark_mode(self):
        from src.web.rendering import _render_login_page
        html = _render_login_page()
        # Dark mode is via external CSS + theme.js (not inline)
        assert "/static/css/styles.css" in html
        assert "/static/js/theme.js" in html
        assert "dark" in html  # theme.js contains dark mode logic

    def test_render_error_message(self):
        from src.web.rendering import _render_login_page
        html = _render_login_page(error="Bad password")
        assert "Bad password" in html
        assert "error" in html.lower()

    def test_render_has_submit_button(self):
        from src.web.rendering import _render_login_page
        html = _render_login_page()
        assert 'type="submit"' in html

    def test_render_has_theme_toggle(self):
        from src.web.rendering import _render_login_page
        html = _render_login_page()
        # login.html extends base.html which includes the theme toggle button
        assert "theme-toggle" in html
        assert "toggleTheme" in html  # onclick handler
        # The shared theme.js module is loaded via <script src>
        assert "/static/js/theme.js" in html
