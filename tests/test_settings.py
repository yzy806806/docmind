"""Tests for the web UI settings page and DB settings key/value store.

Covers:
- Database settings table CRUD: get_setting, set_setting, get_all_settings,
  delete_setting, upsert behavior, default returns
- Settings page rendering (_render_settings_page): form fields, provider
  dropdown, model input, API key masking, base URL visibility, max_tokens
  slider, temperature slider, chat fallback toggle, save button, nav link
- POST /settings form handler: saves to DB, redirects, reloads config
- Config reload mechanism: _reload_llm_config_from_db updates global config
- API key masking security: _mask_api_key never exposes full key, raw key
  never appears in rendered HTML
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Generator

import pytest


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    """Provide a temporary database file path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "test_settings.db")


@pytest.fixture
async def db(tmp_db_path: str):
    """Create a connected SQLite Database instance."""
    from src.core.db_sqlite import Database

    database = Database(db_path=tmp_db_path)
    await database.connect()
    yield database
    await database.disconnect()


@pytest.fixture
async def asgi_client(tmp_db_path: str):
    """Create an httpx.AsyncClient backed by the ASGI app.

    Uses httpx.AsyncClient + ASGITransport so the async DB connection
    (created in the same event loop) is accessible from route handlers.
    """
    import httpx
    from src.core.db_sqlite import Database
    from src.web import server

    db = Database(db_path=tmp_db_path)
    await db.connect()

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


# ── DB settings CRUD tests ───────────────────────────────────────


class TestSettingsCRUD:
    """Tests for the settings key/value store in db_sqlite.py."""

    @pytest.mark.asyncio
    async def test_get_setting_not_found_returns_default(self, db) -> None:
        """get_setting should return the default when key is absent."""
        result = await db.get_setting("nonexistent", default="fallback")
        assert result == "fallback"

    @pytest.mark.asyncio
    async def test_get_setting_not_found_no_default(self, db) -> None:
        """get_setting should return None when key is absent and no default."""
        result = await db.get_setting("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_and_get_setting(self, db) -> None:
        """set_setting then get_setting should round-trip the value."""
        await db.set_setting("llm_provider", "openai")
        result = await db.get_setting("llm_provider")
        assert result == "openai"

    @pytest.mark.asyncio
    async def test_set_setting_upsert(self, db) -> None:
        """set_setting should update an existing key, not insert a duplicate."""
        await db.set_setting("llm_model", "gpt-4o-mini")
        await db.set_setting("llm_model", "gpt-4o")
        result = await db.get_setting("llm_model")
        assert result == "gpt-4o"

        # Should be only one row for this key
        all_settings = await db.get_all_settings()
        assert list(all_settings.keys()).count("llm_model") == 1

    @pytest.mark.asyncio
    async def test_get_all_settings_empty(self, db) -> None:
        """get_all_settings on empty DB should return only internal keys."""
        result = await db.get_all_settings()
        # The encryption key may be auto-generated on connect; filter it out.
        user_settings = {k: v for k, v in result.items()
                         if k != "email_encryption_key"}
        assert user_settings == {}

    @pytest.mark.asyncio
    async def test_get_all_settings_multiple(self, db) -> None:
        """get_all_settings should return all stored settings."""
        await db.set_setting("llm_provider", "openai")
        await db.set_setting("llm_model", "gpt-4o-mini")
        await db.set_setting("llm_api_key", "sk-test123")

        result = await db.get_all_settings()
        # The encryption key may be auto-generated on connect; filter it out.
        user_settings = {k: v for k, v in result.items()
                         if k != "email_encryption_key"}
        assert len(user_settings) == 3
        assert user_settings["llm_provider"] == "openai"
        assert user_settings["llm_model"] == "gpt-4o-mini"
        assert user_settings["llm_api_key"] == "sk-test123"

    @pytest.mark.asyncio
    async def test_set_setting_empty_string(self, db) -> None:
        """set_setting with empty string should store and retrieve empty."""
        await db.set_setting("llm_base_url", "")
        result = await db.get_setting("llm_base_url")
        assert result == ""

    @pytest.mark.asyncio
    async def test_set_setting_persists_across_reconnect(self, tmp_db_path: str) -> None:
        """Settings should survive a disconnect/reconnect cycle."""
        from src.core.db_sqlite import Database

        db1 = Database(db_path=tmp_db_path)
        await db1.connect()
        await db1.set_setting("llm_provider", "ollama")
        await db1.disconnect()

        db2 = Database(db_path=tmp_db_path)
        await db2.connect()
        result = await db2.get_setting("llm_provider")
        await db2.disconnect()

        assert result == "ollama"

    @pytest.mark.asyncio
    async def test_delete_setting(self, db) -> None:
        """delete_setting should remove a key and return True."""
        await db.set_setting("temp_key", "temp_value")
        deleted = await db.delete_setting("temp_key")
        assert deleted is True

        result = await db.get_setting("temp_key")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_setting_not_found(self, db) -> None:
        """delete_setting should return False for non-existent key."""
        deleted = await db.delete_setting("never_existed")
        assert deleted is False

    @pytest.mark.asyncio
    async def test_settings_table_created_on_migrate(self, tmp_db_path: str) -> None:
        """The settings table should be created by migrate()."""
        from src.core.db_sqlite import Database

        db = Database(db_path=tmp_db_path)
        await db.connect()

        async with db.connection() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='settings'"
            )
            row = await cursor.fetchone()

        await db.disconnect()
        assert row is not None
        assert row["name"] == "settings"


# ── API key masking tests ────────────────────────────────────────


class TestMaskApiKey:
    """Tests for _mask_api_key — the security-critical masking function."""

    def test_mask_empty_key(self):
        """Empty key should mask to empty string."""
        from src.web.server import _mask_api_key

        assert _mask_api_key("") == ""

    def test_mask_short_key(self):
        """Keys <= 4 chars should show **** + the full key."""
        from src.web.server import _mask_api_key

        assert _mask_api_key("ab") == "****ab"
        assert _mask_api_key("1234") == "****1234"

    def test_mask_long_key_shows_last_4(self):
        """Long keys should show only the last 4 characters."""
        from src.web.server import _mask_api_key

        result = _mask_api_key("sk-abcd1234")
        assert result == "****1234"
        assert "sk-abcd" not in result
        assert "abcd" not in result

    def test_mask_never_exposes_full_key(self):
        """The masked output should never contain the full original key."""
        from src.web.server import _mask_api_key

        key = "sk-proj-abc123XYZ789"
        masked = _mask_api_key(key)
        assert key not in masked
        # Only last 4 chars should be visible
        assert masked.endswith(key[-4:])

    def test_mask_output_always_starts_with_stars(self):
        """Non-empty keys should always produce a **** prefix."""
        from src.web.server import _mask_api_key

        assert _mask_api_key("x").startswith("****")
        assert _mask_api_key("sk-very-long-key-12345").startswith("****")


# ── Settings page rendering tests ────────────────────────────────


class TestSettingsPageRendering:
    """Tests for _render_settings_page HTML output."""

    def test_render_has_form(self):
        """The settings page should contain a form posting to /settings."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({})
        assert "<form" in html
        assert 'action="/settings"' in html
        assert 'method="post"' in html

    def test_render_has_provider_dropdown(self):
        """The page should have a provider select with all options."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({})
        assert "<select" in html
        assert 'name="provider"' in html
        assert 'value="openai"' in html
        assert 'value="openai-compat"' in html
        assert 'value="ollama"' in html
        assert 'value=""' in html  # empty/none option

    def test_render_has_model_input(self):
        """The page should have a model name text input."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({})
        assert 'name="model"' in html
        assert 'type="text"' in html

    def test_render_has_api_key_input_password_type(self):
        """The API key input should be type=password."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({})
        assert 'name="api_key"' in html
        assert 'type="password"' in html

    def test_render_has_base_url_input(self):
        """The page should have a base URL input."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({})
        assert 'name="base_url"' in html

    def test_render_has_max_tokens_slider(self):
        """The page should have a max_tokens range input (4000-64000)."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({})
        assert 'name="max_tokens"' in html
        assert 'type="range"' in html
        assert 'min="4000"' in html
        assert 'max="64000"' in html

    def test_render_has_temperature_slider(self):
        """The page should have a temperature range input (0.0-1.0)."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({})
        assert 'name="temperature"' in html
        assert 'type="range"' in html
        assert 'min="0.0"' in html
        assert 'max="1.0"' in html

    def test_render_has_chat_fallback_toggle(self):
        """The page should have a chat_fallback checkbox."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({})
        assert 'name="chat_fallback"' in html
        assert 'type="checkbox"' in html

    def test_render_has_save_button(self):
        """The page should have a save/submit button."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({})
        assert 'type="submit"' in html
        assert "Save" in html

    def test_render_success_banner_when_saved(self):
        """When success=True, a success banner should appear."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({}, success=True)
        assert "success" in html.lower()
        assert "saved" in html.lower()

    def test_render_no_success_banner_by_default(self):
        """When success=False (default), no success banner."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({}, success=False)
        # The success div class should not contain the saved message
        assert "Settings saved" not in html

    def test_render_provider_selected(self):
        """The selected provider should be marked in the dropdown."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({"llm_provider": "ollama"})
        # The ollama option should have 'selected'
        assert 'value="ollama"' in html
        assert "selected" in html

    def test_render_model_value_populated(self):
        """The model input should show the stored model value."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({"llm_model": "gpt-4o-mini"})
        assert "gpt-4o-mini" in html

    def test_render_api_key_masked_in_html(self):
        """The rendered page should show the masked key, never the raw key."""
        from src.web.server import _render_settings_page

        raw_key = "sk-super-secret-key-12345"
        html = _render_settings_page({"llm_api_key": raw_key})
        # Raw key must never appear in the HTML
        assert raw_key not in html
        # Masked version (last 4 chars) should appear
        assert "****2345" in html

    def test_render_api_key_not_set_shows_hint(self):
        """When no API key is set, the hint should say 'not set'."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({})
        assert "not set" in html

    def test_render_has_settings_nav_link(self):
        """The base page nav should include a Settings link."""
        from src.web.server import _base_page

        html = _base_page("Test", "<p>content</p>")
        assert 'href="/settings"' in html

    def test_render_has_dark_mode_css(self):
        """Settings page should link external stylesheet with dark mode."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({})
        assert "/static/css/styles.css" in html
        assert "/static/js/theme.js" in html

    def test_render_base_url_hidden_for_openai(self):
        """Base URL row should be hidden (display:none) when provider is openai."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({"llm_provider": "openai"})
        assert 'display:none' in html or 'display: none' in html

    def test_render_base_url_visible_for_ollama(self):
        """Base URL row should be visible when provider is ollama."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({"llm_provider": "ollama"})
        assert 'display:block' in html or 'display: block' in html

    def test_render_base_url_visible_for_openai_compat(self):
        """Base URL row should be visible when provider is openai-compat."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({"llm_provider": "openai-compat"})
        assert 'display:block' in html or 'display: block' in html

    def test_render_max_tokens_value_populated(self):
        """The max_tokens slider should reflect the stored value."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({"llm_max_tokens": "3000"})
        assert 'value="3000"' in html

    def test_render_temperature_value_populated(self):
        """The temperature slider should reflect the stored value."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({"llm_temperature": "0.70"})
        assert 'value="0.70"' in html or "0.7" in html

    def test_render_chat_fallback_checked_when_enabled(self):
        """The chat fallback checkbox should be checked when set to 1."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({"llm_chat_fallback": "1"})
        assert "checked" in html

    def test_render_chat_fallback_unchecked_when_disabled(self):
        """The chat fallback checkbox should NOT be checked when set to 0."""
        from src.web.server import _render_settings_page

        html = _render_settings_page({"llm_chat_fallback": "0"})
        # The checkbox input should not have 'checked'
        # Find the checkbox line
        import re

        checkbox_match = re.search(
            r'<input[^>]*name="chat_fallback"[^>]*>', html
        )
        assert checkbox_match is not None
        assert "checked" not in checkbox_match.group()


# ── Settings page route tests ────────────────────────────────────


class TestSettingsRoute:
    """Tests for GET /settings HTTP route."""

    @pytest.mark.asyncio
    async def test_get_settings_returns_html(self, asgi_client):
        """GET /settings should return 200 HTML."""
        resp = await asgi_client.get("/settings")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_get_settings_has_form(self, asgi_client):
        """GET /settings should contain the settings form."""
        resp = await asgi_client.get("/settings")
        assert "<form" in resp.text
        assert 'action="/settings"' in resp.text

    @pytest.mark.asyncio
    async def test_get_settings_has_provider_dropdown(self, asgi_client):
        """GET /settings should contain the provider dropdown."""
        resp = await asgi_client.get("/settings")
        assert 'name="provider"' in resp.text
        assert "openai" in resp.text
        assert "ollama" in resp.text

    @pytest.mark.asyncio
    async def test_get_settings_success_banner_with_query_param(self, asgi_client):
        """GET /settings?saved=1 should show the success banner."""
        resp = await asgi_client.get("/settings?saved=1")
        assert resp.status_code == 200
        assert "saved" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_get_settings_no_success_banner_without_query_param(self, asgi_client):
        """GET /settings (no saved param) should NOT show the success banner."""
        resp = await asgi_client.get("/settings")
        assert "Settings saved" not in resp.text

    @pytest.mark.asyncio
    async def test_get_settings_has_nav_link(self, asgi_client):
        """The settings page should have the Settings nav link."""
        resp = await asgi_client.get("/settings")
        assert 'href="/settings"' in resp.text


# ── POST form handler tests ──────────────────────────────────────


class TestSettingsPostHandler:
    """Tests for POST /settings form handler."""

    @pytest.mark.asyncio
    async def test_post_settings_redirects(self, asgi_client):
        """POST /settings should return a 302 redirect to /settings?saved=1."""
        resp = await asgi_client.post(
            "/settings",
            data={
                "provider": "openai",
                "model": "gpt-4o-mini",
                "api_key": "sk-test123",
                "base_url": "",
                "max_tokens": "1000",
                "temperature": "0.3",
                "chat_fallback": "1",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers.get("location") == "/settings?saved=1"

    @pytest.mark.asyncio
    async def test_post_settings_saves_to_db(self, asgi_client):
        """POST /settings should persist settings to the DB."""
        await asgi_client.post(
            "/settings",
            data={
                "provider": "openai",
                "model": "gpt-4o-mini",
                "api_key": "sk-mysecret",
                "base_url": "",
                "max_tokens": "2000",
                "temperature": "0.50",
                "chat_fallback": "1",
            },
            follow_redirects=False,
        )

        # Verify settings were saved to the DB
        from src.web import server

        db = server._db
        assert await db.get_setting("llm_provider") == "openai"
        assert await db.get_setting("llm_model") == "gpt-4o-mini"
        assert await db.get_setting("llm_api_key") == "sk-mysecret"
        assert await db.get_setting("llm_max_tokens") == "2000"
        assert await db.get_setting("llm_temperature") == "0.50"
        assert await db.get_setting("llm_chat_fallback") == "1"

    @pytest.mark.asyncio
    async def test_post_settings_ollama_provider(self, asgi_client):
        """POST /settings with ollama provider should save correctly."""
        await asgi_client.post(
            "/settings",
            data={
                "provider": "ollama",
                "model": "llama3",
                "api_key": "",
                "base_url": "http://localhost:11434",
                "max_tokens": "500",
                "temperature": "0.8",
                "chat_fallback": "1",
            },
            follow_redirects=False,
        )

        from src.web import server

        db = server._db
        assert await db.get_setting("llm_provider") == "ollama"
        assert await db.get_setting("llm_model") == "llama3"
        assert await db.get_setting("llm_base_url") == "http://localhost:11434"

    @pytest.mark.asyncio
    async def test_post_settings_chat_fallback_unchecked(self, asgi_client):
        """When chat_fallback checkbox is unchecked, it should save as '0'."""
        await asgi_client.post(
            "/settings",
            data={
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "sk-test",
                "base_url": "",
                "max_tokens": "1000",
                "temperature": "0.3",
                # chat_fallback intentionally omitted (unchecked checkbox)
            },
            follow_redirects=False,
        )

        from src.web import server

        db = server._db
        assert await db.get_setting("llm_chat_fallback") == "0"

    @pytest.mark.asyncio
    async def test_post_settings_max_tokens_bounds_clamped(self, asgi_client):
        """max_tokens above 4000 should be clamped to 4000."""
        await asgi_client.post(
            "/settings",
            data={
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "sk-test",
                "base_url": "",
                "max_tokens": "99999",
                "temperature": "0.3",
                "chat_fallback": "1",
            },
            follow_redirects=False,
        )

        from src.web import server

        db = server._db
        assert await db.get_setting("llm_max_tokens") == "4000"

    @pytest.mark.asyncio
    async def test_post_settings_max_tokens_below_min_clamped(self, asgi_client):
        """max_tokens below 100 should be clamped to 100."""
        await asgi_client.post(
            "/settings",
            data={
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "sk-test",
                "base_url": "",
                "max_tokens": "10",
                "temperature": "0.3",
                "chat_fallback": "1",
            },
            follow_redirects=False,
        )

        from src.web import server

        db = server._db
        assert await db.get_setting("llm_max_tokens") == "100"

    @pytest.mark.asyncio
    async def test_post_settings_temperature_bounds_clamped(self, asgi_client):
        """temperature above 1.0 should be clamped to 1.0."""
        await asgi_client.post(
            "/settings",
            data={
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "sk-test",
                "base_url": "",
                "max_tokens": "1000",
                "temperature": "5.0",
                "chat_fallback": "1",
            },
            follow_redirects=False,
        )

        from src.web import server

        db = server._db
        temp = float(await db.get_setting("llm_temperature"))
        assert temp == 1.0

    @pytest.mark.asyncio
    async def test_post_settings_invalid_max_tokens_defaults(self, asgi_client):
        """Non-numeric max_tokens should fall back to 1000."""
        await asgi_client.post(
            "/settings",
            data={
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "sk-test",
                "base_url": "",
                "max_tokens": "not-a-number",
                "temperature": "0.3",
                "chat_fallback": "1",
            },
            follow_redirects=False,
        )

        from src.web import server

        db = server._db
        assert await db.get_setting("llm_max_tokens") == "1000"

    @pytest.mark.asyncio
    async def test_post_settings_masked_api_key_keeps_existing(self, asgi_client):
        """Submitting the masked placeholder should NOT overwrite the stored key."""
        from src.web import server

        db = server._db
        # First, save a real key
        await db.set_setting("llm_api_key", "sk-real-key-9999")

        # Now POST with the masked placeholder (what the form shows)
        await asgi_client.post(
            "/settings",
            data={
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "****9999",  # masked placeholder
                "base_url": "",
                "max_tokens": "1000",
                "temperature": "0.3",
                "chat_fallback": "1",
            },
            follow_redirects=False,
        )

        # The original key should still be there
        assert await db.get_setting("llm_api_key") == "sk-real-key-9999"

    @pytest.mark.asyncio
    async def test_post_settings_empty_api_key_keeps_existing(self, asgi_client):
        """Submitting an empty api_key should NOT clear the stored key."""
        from src.web import server

        db = server._db
        await db.set_setting("llm_api_key", "sk-original-key")

        await asgi_client.post(
            "/settings",
            data={
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "",  # empty — user didn't change it
                "base_url": "",
                "max_tokens": "1000",
                "temperature": "0.3",
                "chat_fallback": "1",
            },
            follow_redirects=False,
        )

        # The original key should still be there
        assert await db.get_setting("llm_api_key") == "sk-original-key"

    @pytest.mark.asyncio
    async def test_post_settings_new_api_key_overwrites(self, asgi_client):
        """Submitting a new (non-masked) api_key should overwrite the stored key."""
        from src.web import server

        db = server._db
        await db.set_setting("llm_api_key", "sk-old-key")

        await asgi_client.post(
            "/settings",
            data={
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "sk-new-key-1234",  # new real key
                "base_url": "",
                "max_tokens": "1000",
                "temperature": "0.3",
                "chat_fallback": "1",
            },
            follow_redirects=False,
        )

        assert await db.get_setting("llm_api_key") == "sk-new-key-1234"

    @pytest.mark.asyncio
    async def test_post_settings_followed_shows_success(self, asgi_client):
        """Following the redirect should show the success banner."""
        resp = await asgi_client.post(
            "/settings",
            data={
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "sk-test",
                "base_url": "",
                "max_tokens": "1000",
                "temperature": "0.3",
                "chat_fallback": "1",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "saved" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_post_settings_persists_across_new_request(self, asgi_client):
        """After saving, a subsequent GET /settings should show the saved values."""
        await asgi_client.post(
            "/settings",
            data={
                "provider": "ollama",
                "model": "llama3",
                "api_key": "",
                "base_url": "http://localhost:11434",
                "max_tokens": "500",
                "temperature": "0.7",
                "chat_fallback": "1",
            },
            follow_redirects=False,
        )

        resp = await asgi_client.get("/settings")
        assert resp.status_code == 200
        assert "llama3" in resp.text
        assert "http://localhost:11434" in resp.text


# ── Config reload tests ──────────────────────────────────────────


class TestConfigReload:
    """Tests for _reload_llm_config_from_db — updating in-memory config."""

    def test_reload_updates_provider(self):
        """_reload_llm_config_from_db should update config.llm.provider."""
        from src.core.config import config, LLMConfig
        from src.web.server import _reload_llm_config_from_db

        original = config.llm.provider
        try:
            _reload_llm_config_from_db({"llm_provider": "ollama"})
            assert config.llm.provider == "ollama"
        finally:
            config.llm = LLMConfig()
            config.llm.provider = original

    def test_reload_updates_model(self):
        """_reload_llm_config_from_db should update config.llm.model."""
        from src.core.config import config, LLMConfig
        from src.web.server import _reload_llm_config_from_db

        original = config.llm.model
        try:
            _reload_llm_config_from_db({"llm_model": "llama3"})
            assert config.llm.model == "llama3"
        finally:
            config.llm = LLMConfig()
            config.llm.model = original

    def test_reload_updates_api_key(self):
        """_reload_llm_config_from_db should update config.llm.api_key."""
        from src.core.config import config, LLMConfig
        from src.web.server import _reload_llm_config_from_db

        original = config.llm.api_key
        try:
            _reload_llm_config_from_db({"llm_api_key": "sk-new-key-123"})
            assert config.llm.api_key == "sk-new-key-123"
        finally:
            config.llm = LLMConfig()
            config.llm.api_key = original

    def test_reload_ignores_masked_api_key(self):
        """_reload_llm_config_from_db should NOT apply masked keys to config."""
        from src.core.config import config, LLMConfig
        from src.web.server import _reload_llm_config_from_db

        original = config.llm.api_key
        try:
            _reload_llm_config_from_db({"llm_api_key": "****1234"})
            # Should remain unchanged (masked values are never applied)
            assert config.llm.api_key == original
        finally:
            config.llm = LLMConfig()
            config.llm.api_key = original

    def test_reload_updates_base_url(self):
        """_reload_llm_config_from_db should update config.llm.base_url."""
        from src.core.config import config, LLMConfig
        from src.web.server import _reload_llm_config_from_db

        original = config.llm.base_url
        try:
            _reload_llm_config_from_db({"llm_base_url": "http://localhost:11434"})
            assert config.llm.base_url == "http://localhost:11434"
        finally:
            config.llm = LLMConfig()
            config.llm.base_url = original

    def test_reload_updates_max_tokens(self):
        """_reload_llm_config_from_db should update config.llm.max_tokens."""
        from src.core.config import config, LLMConfig
        from src.web.server import _reload_llm_config_from_db

        original = config.llm.max_tokens
        try:
            _reload_llm_config_from_db({"llm_max_tokens": "2500"})
            assert config.llm.max_tokens == 2500
        finally:
            config.llm = LLMConfig()
            config.llm.max_tokens = original

    def test_reload_updates_temperature(self):
        """_reload_llm_config_from_db should update config.llm.temperature."""
        from src.core.config import config, LLMConfig
        from src.web.server import _reload_llm_config_from_db

        original = config.llm.temperature
        try:
            _reload_llm_config_from_db({"llm_temperature": "0.80"})
            assert config.llm.temperature == 0.80
        finally:
            config.llm = LLMConfig()
            config.llm.temperature = original

    def test_reload_empty_dict_is_noop(self):
        """_reload_llm_config_from_db with empty dict should not change config."""
        from src.core.config import config, LLMConfig
        from src.web.server import _reload_llm_config_from_db

        original_provider = config.llm.provider
        original_model = config.llm.model
        original_key = config.llm.api_key
        original_url = config.llm.base_url
        original_tokens = config.llm.max_tokens
        original_temp = config.llm.temperature

        try:
            _reload_llm_config_from_db({})
            assert config.llm.provider == original_provider
            assert config.llm.model == original_model
            assert config.llm.api_key == original_key
            assert config.llm.base_url == original_url
            assert config.llm.max_tokens == original_tokens
            assert config.llm.temperature == original_temp
        finally:
            config.llm = LLMConfig()
            config.llm.provider = original_provider
            config.llm.model = original_model
            config.llm.api_key = original_key
            config.llm.base_url = original_url
            config.llm.max_tokens = original_tokens
            config.llm.temperature = original_temp

    def test_reload_invalid_max_tokens_ignored(self):
        """Invalid max_tokens value should be silently ignored."""
        from src.core.config import config, LLMConfig
        from src.web.server import _reload_llm_config_from_db

        original = config.llm.max_tokens
        try:
            _reload_llm_config_from_db({"llm_max_tokens": "not-a-number"})
            assert config.llm.max_tokens == original
        finally:
            config.llm = LLMConfig()
            config.llm.max_tokens = original

    def test_reload_invalid_temperature_ignored(self):
        """Invalid temperature value should be silently ignored."""
        from src.core.config import config, LLMConfig
        from src.web.server import _reload_llm_config_from_db

        original = config.llm.temperature
        try:
            _reload_llm_config_from_db({"llm_temperature": "not-a-number"})
            assert config.llm.temperature == original
        finally:
            config.llm = LLMConfig()
            config.llm.temperature = original

    @pytest.mark.asyncio
    async def test_post_settings_reloads_config(self, asgi_client):
        """POST /settings should update the global config.llm from saved values."""
        from src.core.config import config, LLMConfig
        from src.web import server

        # Save original config values
        orig_provider = config.llm.provider
        orig_model = config.llm.model
        orig_key = config.llm.api_key

        try:
            await asgi_client.post(
                "/settings",
                data={
                    "provider": "ollama",
                    "model": "llama3",
                    "api_key": "",
                    "base_url": "http://localhost:11434",
                    "max_tokens": "500",
                    "temperature": "0.7",
                    "chat_fallback": "1",
                },
                follow_redirects=False,
            )

            # Config should now reflect the saved values
            assert config.llm.provider == "ollama"
            assert config.llm.model == "llama3"
            assert config.llm.base_url == "http://localhost:11434"
            assert config.llm.max_tokens == 500
            assert config.llm.temperature == 0.7
        finally:
            # Restore original config
            config.llm = LLMConfig()
            config.llm.provider = orig_provider
            config.llm.model = orig_model
            config.llm.api_key = orig_key


# ── Redirect page tests ──────────────────────────────────────────


class TestSettingsRedirect:
    """Tests for _render_settings_redirect helper."""

    def test_redirect_has_meta_refresh(self):
        """The redirect page should have a meta refresh tag."""
        from src.web.server import _render_settings_redirect

        html = _render_settings_redirect()
        assert "http-equiv" in html
        assert "refresh" in html.lower()
        assert "/settings?saved=1" in html

    def test_redirect_has_fallback_link(self):
        """The redirect page should have a manual link as fallback."""
        from src.web.server import _render_settings_redirect

        html = _render_settings_redirect()
        assert 'href="/settings?saved=1"' in html
