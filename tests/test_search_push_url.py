"""Tests for hx-push-url on filter/search interactions (browser back-button support).

Context: Agora Phase 2, motion-69159b7de5f1, action item 4/7.
Add hx-push-url to all filter/search interactions so browser back button
works correctly after navigation.

Two HTMX filter/search interactions exist:
1. Documents filter form (/documents): HTMX swaps #doc-table-region on
   submit/change. The partial endpoint returns HX-Push-Url header pointing
   to the canonical /documents URL.
2. Search form (/search): When submitted via the Search button
   (submit_search param present) and the request is HTMX, the response
   includes HX-Push-Url header pointing to /search?q=... URL.

Keyup-triggered (live search) requests do NOT push a URL — otherwise every
keystroke would flood the browser history.

These tests verify:
1. Documents partial: HX-Push-Url header present, points to /documents?params
2. Documents partial: pushed URL includes filter params, excludes partial path
3. Documents filter form template: has hx-push-url="true"
4. Search submit (HTMX): HX-Push-Url header present, points to /search?q=...
5. Search keyup (HTMX, no submit_search): NO HX-Push-Url header
6. Search non-HTMX: NO HX-Push-Url header (native history)
7. Pushed search URL excludes internal submit_search param
8. Pushed search URL includes vector_weight when provided
9. Search form templates have name="submit_search" on submit buttons
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

    # Also insert varied docs for filter testing
    await db.save_document(
        path="/docs/a.pdf",
        source_type="api",
        source_name="api",
        title="PDF Doc",
        ext=".pdf",
        mime_type="application/pdf",
        body="PDF content",
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


# ── 1. Documents partial: HX-Push-Url header ─────────────────────


class TestDocumentsPartialPushUrl:
    """The /documents/partials/table endpoint must include HX-Push-Url
    so the browser back button works after an HTMX filter swap."""

    @pytest.mark.asyncio
    async def test_partial_has_push_url_header(self, asgi_client) -> None:
        """The partial response must include HX-Push-Url header."""
        resp = await asgi_client.get("/documents/partials/table")
        assert resp.status_code == 200
        push_url = resp.headers.get("hx-push-url") or resp.headers.get("HX-Push-Url")
        assert push_url is not None, (
            "HX-Push-Url header missing on documents partial response"
        )
        assert push_url.startswith("/documents?"), (
            f"HX-Push-Url should point to /documents?..., got: {push_url}"
        )
        # Must NOT push the partial endpoint URL
        assert "/partials/" not in push_url, (
            f"HX-Push-Url must not contain /partials/: {push_url}"
        )

    @pytest.mark.asyncio
    async def test_partial_push_url_includes_filter_params(self, asgi_client) -> None:
        """The HX-Push-Url header must include all applied filter params."""
        resp = await asgi_client.get(
            "/documents/partials/table?source=api&file_type=.pdf&page=2&per_page=5"
        )
        assert resp.status_code == 200
        push_url = resp.headers.get("hx-push-url") or resp.headers.get("HX-Push-Url")
        assert push_url is not None
        assert "source=api" in push_url
        assert "page=2" in push_url
        assert "per_page=5" in push_url

    @pytest.mark.asyncio
    async def test_partial_push_url_url_encoded(self, asgi_client) -> None:
        """Filter values with special characters are URL-encoded in push URL."""
        resp = await asgi_client.get(
            "/documents/partials/table?tag=web%2Fdev"
        )
        assert resp.status_code == 200
        push_url = resp.headers.get("hx-push-url") or resp.headers.get("HX-Push-Url")
        assert push_url is not None
        assert "tag=web" in push_url


# ── 2. Documents filter form template: hx-push-url attribute ─────


class TestDocumentsFilterFormTemplate:
    """Verify the documents filter form template has hx-push-url."""

    def test_filter_form_has_hx_push_url(self):
        """documents/list.html filter form has hx-push-url attribute."""
        html = _read("src/web/templates/documents/list.html")
        form_match = re.search(
            r'<form[^>]*id="facet-filter-form"[^>]*>', html, re.DOTALL
        )
        assert form_match, "Filter form not found in documents/list.html"
        form_tag = form_match.group(0)
        assert 'hx-push-url' in form_tag, (
            f"Filter form must have hx-push-url: {form_tag}"
        )


# ── 3. Search submit (HTMX): HX-Push-Url header ──────────────────


class TestSearchSubmitPushesUrl:
    """When the search form is submitted (submit_search=1 present)
    and the request is HTMX, the response must include HX-Push-Url."""

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
            f"HX-Push-Url should point to /search?..., got: {push_url}"
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


# ── 4. Search keyup (HTMX, no submit_search): NO push URL ────────


class TestSearchKeyupNoPushUrl:
    """When the search is triggered by keyup (live search, no
    submit_search param), the response must NOT include HX-Push-Url."""

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


# ── 5. Non-HTMX search: NO push URL ──────────────────────────────


class TestSearchNonHtmxNoPushUrl:
    """Non-HTMX requests (full page load) don't need HX-Push-Url."""

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
    async def test_full_page_search_returns_html(self, asgi_client) -> None:
        """Regular GET /search?q=... returns HTML."""
        resp = await asgi_client.get("/search?q=machine")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


# ── 6. Search form templates: submit button has name="submit_search" ──


class TestSearchFormSubmitButton:
    """Verify search templates have name='submit_search' on submit button."""

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
