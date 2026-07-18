"""Tests for hx-push-url on search interactions (browser back-button support).

Context: Agora Phase 2, motion-69159b7de5f1, action item 4/7.
Add hx-push-url to all filter/search interactions so browser back button
works correctly after navigation.

The search forms (search_form.html, search_results.html) use HTMX with
hx-trigger="submit, keyup ... delay:250ms, change" for live debounced
search.  Naively adding hx-push-url="true" would push a history entry
on every keystroke — flooding browser history.  Instead, the server-side
HX-Push-Url response header is used, gated on the submit_search param
which is only present when the form was submitted via the Search button.

These tests verify:
1. Submit-triggered HTMX search: HX-Push-Url header present, points to /search?q=...
2. Keyup-triggered HTMX search: NO HX-Push-Url header (no history flood)
3. Non-HTMX full-page search: NO HX-Push-Url header (native history works)
4. Pushed URL is canonical /search (not a partial endpoint)
5. Pushed URL excludes the internal submit_search param
6. Pushed URL includes vector_weight when provided
7. Search form templates have name="submit_search" on submit buttons
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Generator

import pytest


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "test_search_push_url.db")


@pytest.fixture
async def asgi_client(tmp_db_path: str):
    """Create an httpx.AsyncClient backed by the ASGI app with test documents."""
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

    # Insert a searchable document so /search?q=... returns results
    await db.save_document(
        path="/docs/test.txt",
        source_type="api",
        source_name="test",
        title="Machine Learning Guide",
        ext=".txt",
        mime_type="text/plain",
        body="This document covers machine learning concepts and revenue.",
        size=100,
        status="indexed",
    )

    app = server.create_app()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as c:
        yield c

    await db.disconnect()
    server._db = original_db
    server._queue = original_queue


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _read(path: str) -> str:
    return (_project_root() / path).read_text()


# ── 1. Submit-triggered HTMX search pushes URL ───────────────────


class TestSubmitPushesUrl:
    """When the search form is submitted (submit_search=1 present),
    the HTMX response must include HX-Push-Url so the browser back
    button works."""

    @pytest.mark.asyncio
    async def test_submit_htmx_has_push_url(self, asgi_client) -> None:
        """HTMX request with submit_search=1 must have HX-Push-Url header."""
        resp = await asgi_client.get(
            "/search?q=machine&submit_search=1",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        push_url = resp.headers.get("hx-push-url") or resp.headers.get("HX-Push-Url")
        assert push_url is not None, (
            "HX-Push-Url header missing on submit-triggered HTMX search"
        )
        assert push_url.startswith("/search?"), (
            f"HX-Push-Url should point to /search?q=..., got: {push_url}"
        )

    @pytest.mark.asyncio
    async def test_submit_push_url_includes_query(self, asgi_client) -> None:
        """The pushed URL must include the search query param."""
        resp = await asgi_client.get(
            "/search?q=machine&submit_search=1",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        push_url = resp.headers.get("hx-push-url") or resp.headers.get("HX-Push-Url")
        assert push_url is not None
        assert "q=machine" in push_url, (
            f"Pushed URL must contain q=machine: {push_url}"
        )

    @pytest.mark.asyncio
    async def test_submit_push_url_excludes_submit_search(self, asgi_client) -> None:
        """The pushed URL must NOT contain the internal submit_search param."""
        resp = await asgi_client.get(
            "/search?q=machine&submit_search=1",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        push_url = resp.headers.get("hx-push-url") or resp.headers.get("HX-Push-Url")
        assert push_url is not None
        assert "submit_search" not in push_url, (
            f"Pushed URL must not contain submit_search: {push_url}"
        )

    @pytest.mark.asyncio
    async def test_submit_push_url_includes_vector_weight(self, asgi_client) -> None:
        """The pushed URL must include vector_weight when provided."""
        resp = await asgi_client.get(
            "/search?q=machine&vector_weight=0.8&submit_search=1",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        push_url = resp.headers.get("hx-push-url") or resp.headers.get("HX-Push-Url")
        assert push_url is not None
        assert "vector_weight=0.80" in push_url, (
            f"Pushed URL must contain vector_weight=0.80: {push_url}"
        )

    @pytest.mark.asyncio
    async def test_submit_push_url_without_vector_weight(self, asgi_client) -> None:
        """When no vector_weight is provided, the pushed URL omits it."""
        resp = await asgi_client.get(
            "/search?q=machine&submit_search=1",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        push_url = resp.headers.get("hx-push-url") or resp.headers.get("HX-Push-Url")
        assert push_url is not None
        assert "vector_weight" not in push_url, (
            f"Pushed URL should not contain vector_weight: {push_url}"
        )

    @pytest.mark.asyncio
    async def test_submit_push_url_url_encoded(self, asgi_client) -> None:
        """Query with special characters is URL-encoded in the pushed URL."""
        resp = await asgi_client.get(
            "/search?q=machine+learning&submit_search=1",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        push_url = resp.headers.get("hx-push-url") or resp.headers.get("HX-Push-Url")
        assert push_url is not None
        # "machine learning" should be encoded as machine+learning or machine%20learning
        assert "machine" in push_url
        assert "learning" in push_url


# ── 2. Keyup-triggered HTMX search does NOT push URL ─────────────


class TestKeyupDoesNotPushUrl:
    """When the search is triggered by keyup (live search, no
    submit_search param), the response must NOT include HX-Push-Url —
    otherwise every keystroke would flood the browser history."""

    @pytest.mark.asyncio
    async def test_keyup_htmx_no_push_url(self, asgi_client) -> None:
        """HTMX request WITHOUT submit_search must NOT have HX-Push-Url."""
        resp = await asgi_client.get(
            "/search?q=machine",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        push_url = resp.headers.get("hx-push-url") or resp.headers.get("HX-Push-Url")
        assert push_url is None, (
            f"HX-Push-Url should be absent on keyup-triggered search, got: {push_url}"
        )

    @pytest.mark.asyncio
    async def test_keyup_htmx_still_returns_fragment(self, asgi_client) -> None:
        """Keyup-triggered HTMX request still returns a valid HTML fragment."""
        resp = await asgi_client.get(
            "/search?q=machine",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # Should be a fragment (live search), not a full page
        assert "<form" not in resp.text or "search-live-region" not in resp.text, (
            "Live HTMX fragment should not include the full form wrapper"
        )
        # Should contain search results
        assert "result" in resp.text.lower() or "no results" in resp.text.lower()


# ── 3. Non-HTMX full-page search does NOT push URL ───────────────


class TestFullPageNoPushUrl:
    """Non-HTMX requests (full page load) don't need HX-Push-Url —
    the browser handles history natively on full page loads."""

    @pytest.mark.asyncio
    async def test_full_page_search_no_push_url(self, asgi_client) -> None:
        """Regular (non-HTMX) GET /search?q=... must NOT have HX-Push-Url."""
        resp = await asgi_client.get("/search?q=machine")
        assert resp.status_code == 200
        push_url = resp.headers.get("hx-push-url") or resp.headers.get("HX-Push-Url")
        assert push_url is None, (
            f"Full-page search should not have HX-Push-Url: {push_url}"
        )

    @pytest.mark.asyncio
    async def test_full_page_search_returns_full_page(self, asgi_client) -> None:
        """Regular GET /search?q=... returns a full HTML page with form."""
        resp = await asgi_client.get("/search?q=machine")
        assert resp.status_code == 200
        assert "<form" in resp.text
        assert "search-live-region" in resp.text


# ── 4. Template: submit buttons have name="submit_search" ────────


class TestSearchFormSubmitButton:
    """Verify both search templates have name="submit_search" on the
    submit button so the server can distinguish submit from keyup."""

    def test_search_form_html_has_submit_search_name(self):
        """search_form.html submit button has name='submit_search'."""
        html = _read("src/web/templates/search_form.html")
        button_match = re.search(
            r'<button[^>]*type="submit"[^>]*>Search</button>', html
        )
        assert button_match, "Submit button not found in search_form.html"
        button_tag = button_match.group(0)
        assert 'name="submit_search"' in button_tag, (
            f"Submit button must have name='submit_search': {button_tag}"
        )

    def test_search_results_html_has_submit_search_name(self):
        """search_results.html submit button has name='submit_search'."""
        html = _read("src/web/templates/search_results.html")
        button_match = re.search(
            r'<button[^>]*type="submit"[^>]*>Search</button>', html
        )
        assert button_match, "Submit button not found in search_results.html"
        button_tag = button_match.group(0)
        assert 'name="submit_search"' in button_tag, (
            f"Submit button must have name='submit_search': {button_tag}"
        )

    def test_search_form_submit_button_has_value(self):
        """search_form.html submit button has value='1'."""
        html = _read("src/web/templates/search_form.html")
        button_match = re.search(
            r'<button[^>]*type="submit"[^>]*>Search</button>', html
        )
        assert button_match
        assert 'value="1"' in button_match.group(0), (
            f"Submit button must have value='1': {button_match.group(0)}"
        )

    def test_search_results_submit_button_has_value(self):
        """search_results.html submit button has value='1'."""
        html = _read("src/web/templates/search_results.html")
        button_match = re.search(
            r'<button[^>]*type="submit"[^>]*>Search</button>', html
        )
        assert button_match
        assert 'value="1"' in button_match.group(0), (
            f"Submit button must have value='1': {button_match.group(0)}"
        )


# ── 5. Documents filter form already has hx-push-url ─────────────


class TestDocumentsFilterPushUrl:
    """Verify the documents filter form still has hx-push-url (regression guard)."""

    def test_documents_list_has_hx_push_url(self):
        """documents/list.html filter form has hx-push-url='true'."""
        html = _read("src/web/templates/documents/list.html")
        # The filter form should have hx-push-url
        form_match = re.search(
            r'<form[^>]*id="facet-filter-form"[^>]*>', html, re.DOTALL
        )
        assert form_match, "Filter form not found in documents/list.html"
        form_tag = form_match.group(0)
        assert 'hx-push-url' in form_tag, (
            f"Filter form must have hx-push-url: {form_tag}"
        )
