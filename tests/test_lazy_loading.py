"""Tests for lazy loading implementation (Phase 9).

Covers:
1. Documents list infinite scroll: sentinel element present, rows partial
   endpoint returns <tr> fragments, pagination metadata in headers
2. Search results lazy loading: sentinel + Load More button present, partial
   endpoint returns result <div> fragments, offset/limit slicing correct
3. Document detail excerpt lazy loading: #doc-excerpt-lazy container present,
   /documents/{id}/partials/excerpt endpoint returns <pre> fragment
4. JS file exists and contains IntersectionObserver logic
5. Non-critical scripts have defer attribute
6. Backward compatibility: existing callers of _render_search_results
   without offset/limit/total still work
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Generator

import pytest


# ── Helpers ───────────────────────────────────────────────────────


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _template_path(name: str) -> Path:
    return _project_root() / "src" / "web" / "templates" / name


def _read_template(name: str) -> str:
    return _template_path(name).read_text()


def _js_path(name: str) -> Path:
    return _project_root() / "src" / "web" / "static" / "js" / name


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "test_lazy.db")


@pytest.fixture
async def asgi_client(tmp_db_path: str):
    """Create an httpx.AsyncClient backed by the ASGI app."""
    import httpx
    from src.core.db_sqlite import Database
    from src.web import server

    db = Database(db_path=tmp_db_path)
    await db.connect()

    # Insert 25 test documents — enough for 2 pages at per_page=20
    for i in range(1, 26):
        await db.save_document(
            path=f"/docs/test_{i}.txt",
            source_type="api",
            source_name="test-source",
            title=f"Apple Report {i}",
            ext=".txt",
            mime_type="text/plain",
            body=f"This apple document contains apple-related content. Number {i}. " * 20,
            size=400,
            status="indexed",
        )

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


# ── 1. Documents list infinite scroll ─────────────────────────────


class TestDocumentsListInfiniteScroll:
    """Tests for the documents list infinite scroll feature."""

    def test_sentinel_present_in_list_template(self):
        """The documents list template should contain the load-more sentinel."""
        html = _read_template("documents/list.html")
        assert 'id="load-more-sentinel"' in html, (
            "documents/list.html should contain #load-more-sentinel for infinite scroll"
        )

    def test_sentinel_has_data_attributes(self):
        """The sentinel should have data-page, data-total-pages, etc."""
        html = _read_template("documents/list.html")
        assert 'data-page=' in html, "Sentinel should have data-page attribute"
        assert 'data-total-pages=' in html, "Sentinel should have data-total-pages attribute"
        assert 'data-per-page=' in html, "Sentinel should have data-per-page attribute"

    def test_tbody_id_present(self):
        """The table body should have id='doc-tbody' for row appending."""
        html = _read_template("documents/list.html")
        assert 'id="doc-tbody"' in html, (
            "documents/list.html should have <tbody id='doc-tbody'>"
        )

    def test_sentinel_present_in_table_partial(self):
        """The table partial template should also contain the sentinel."""
        html = _read_template("_partials/documents_table.html")
        assert 'id="load-more-sentinel"' in html

    def test_tbody_id_in_table_partial(self):
        """The table partial template should have <tbody id='doc-tbody'>."""
        html = _read_template("_partials/documents_table.html")
        assert 'id="doc-tbody"' in html

    async def test_rows_partial_endpoint_returns_tr_fragments(self, asgi_client):
        """GET /documents/partials/rows should return <tr> elements."""
        resp = await asgi_client.get("/documents/partials/rows?page=1&per_page=5")
        assert resp.status_code == 200
        body = resp.text
        assert "<tr>" in body, "Rows partial should contain <tr> elements"
        assert "<table" not in body, "Rows partial should NOT contain <table>"
        assert "<tbody" not in body, "Rows partial should NOT contain <tbody>"

    async def test_rows_partial_pagination_headers(self, asgi_client):
        """The rows endpoint should return X-Page, X-Total-Pages headers."""
        resp = await asgi_client.get("/documents/partials/rows?page=1&per_page=20")
        assert resp.status_code == 200
        assert resp.headers.get("X-Page") == "1"
        assert resp.headers.get("X-Total-Pages") == "2"
        assert resp.headers.get("X-Total") == "25"

    async def test_rows_partial_page_2_returns_different_rows(self, asgi_client):
        """Page 2 should return different rows than page 1."""
        resp1 = await asgi_client.get("/documents/partials/rows?page=1&per_page=20")
        resp2 = await asgi_client.get("/documents/partials/rows?page=2&per_page=20")
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        # Page 1 should have 20 rows, page 2 should have 5
        assert resp1.text.count("<tr>") == 20
        assert resp2.text.count("<tr>") == 5
        # The content should be different
        assert resp1.text != resp2.text

    async def test_rows_partial_with_filters(self, asgi_client):
        """The rows endpoint should respect source filter."""
        resp = await asgi_client.get(
            "/documents/partials/rows?page=1&per_page=20&source=api"
        )
        assert resp.status_code == 200
        assert "<tr>" in resp.text

    async def test_rows_partial_empty_page(self, asgi_client):
        """Requesting a page beyond the last should return empty rows."""
        resp = await asgi_client.get("/documents/partials/rows?page=99&per_page=20")
        assert resp.status_code == 200
        assert "<tr>" not in resp.text


# ── 2. Search results lazy loading ────────────────────────────────


class TestSearchResultsLazyLoad:
    """Tests for search results lazy loading."""

    def test_sentinel_present_in_search_results_template(self):
        """The search results template should contain the load-more sentinel."""
        html = _read_template("search_results.html")
        assert 'id="search-load-more-sentinel"' in html, (
            "search_results.html should contain #search-load-more-sentinel"
        )

    def test_load_more_button_present(self):
        """A manual 'Load More' button should be present as fallback."""
        html = _read_template("search_results.html")
        assert 'id="search-load-more-btn"' in html

    def test_results_list_container_present(self):
        """The results should be wrapped in #search-results-list."""
        html = _read_template("search_results.html")
        assert 'id="search-results-list"' in html

    async def test_search_partial_returns_result_divs(self, asgi_client):
        """GET /search?partial=1 should return <div class='result'> fragments."""
        resp = await asgi_client.get("/search?q=apple&partial=1&limit=5")
        assert resp.status_code == 200
        body = resp.text
        assert 'class="result"' in body
        # Should NOT contain the full page chrome
        assert "<html" not in body
        assert "<title>" not in body

    async def test_search_partial_offset_slicing(self, asgi_client):
        """Partial with offset should return different results than offset=0."""
        resp0 = await asgi_client.get("/search?q=apple&partial=1&limit=5&offset=0")
        resp5 = await asgi_client.get("/search?q=apple&partial=1&limit=5&offset=5")
        assert resp0.status_code == 200
        assert resp5.status_code == 200
        # Both should have results
        assert 'class="result"' in resp0.text
        assert 'class="result"' in resp5.text
        # The content should differ
        assert resp0.text != resp5.text

    async def test_search_full_page_has_lazy_elements(self, asgi_client):
        """Full search page should include the sentinel and Load More button
        when total results exceed the page limit."""
        # Use limit=5 — with 25 apple docs, total(25) > limit(5) so sentinel appears
        resp = await asgi_client.get("/search?q=apple&limit=5")
        assert resp.status_code == 200
        html = resp.text
        assert 'id="search-load-more-sentinel"' in html
        assert 'id="search-load-more-btn"' in html

    async def test_search_full_page_shows_count(self, asgi_client):
        """Full search page should show total result count."""
        resp = await asgi_client.get("/search?q=apple")
        assert resp.status_code == 200
        # Should contain "result(s)" text
        assert "result(s)" in resp.text


# ── 3. Document detail excerpt lazy loading ──────────────────────


class TestDocumentDetailLazyExcerpt:
    """Tests for document detail excerpt lazy loading."""

    def test_lazy_container_present(self):
        """The detail template should contain #doc-excerpt-lazy."""
        html = _read_template("documents/detail.html")
        assert 'id="doc-excerpt-lazy"' in html

    def test_lazy_container_has_data_doc_id(self):
        """The lazy container should have data-doc-id attribute."""
        html = _read_template("documents/detail.html")
        assert 'data-doc-id=' in html

    async def test_excerpt_partial_endpoint(self, asgi_client):
        """GET /documents/{id}/partials/excerpt should return <pre> fragment."""
        resp = await asgi_client.get("/documents/1/partials/excerpt")
        assert resp.status_code == 200
        assert '<pre class="doc-excerpt">' in resp.text
        # Should NOT contain full page chrome
        assert "<html" not in resp.text
        assert "<title>" not in resp.text

    async def test_excerpt_partial_404_for_missing_doc(self, asgi_client):
        """Excerpt endpoint should return 404 for non-existent document."""
        resp = await asgi_client.get("/documents/99999/partials/excerpt")
        assert resp.status_code == 404

    async def test_excerpt_partial_content_matches(self, asgi_client):
        """The excerpt partial should return the same content as the full page."""
        # Get the full detail page
        resp_full = await asgi_client.get("/documents/1")
        assert resp_full.status_code == 200
        # Get the excerpt partial
        resp_partial = await asgi_client.get("/documents/1/partials/excerpt")
        assert resp_partial.status_code == 200
        # Both should contain the excerpt text
        assert "apple" in resp_full.text.lower()
        assert "apple" in resp_partial.text.lower()


# ── 4. JS file existence and IntersectionObserver logic ──────────


class TestLazyLoadJS:
    """Tests for the lazy-load.js file."""

    def test_js_file_exists(self):
        """lazy-load.js should exist in the static/js directory."""
        assert _js_path("lazy-load.js").exists()

    def test_js_file_non_empty(self):
        """lazy-load.js should contain actual code."""
        js = _js_path("lazy-load.js").read_text()
        assert len(js) > 500

    def test_js_contains_intersection_observer(self):
        """lazy-load.js should use IntersectionObserver."""
        js = _js_path("lazy-load.js").read_text()
        assert "IntersectionObserver" in js

    def test_js_contains_documents_list_init(self):
        """lazy-load.js should have the documents list infinite scroll init."""
        js = _js_path("lazy-load.js").read_text()
        assert "initDocumentsListInfiniteScroll" in js
        assert "load-more-sentinel" in js

    def test_js_contains_search_results_init(self):
        """lazy-load.js should have the search results lazy load init."""
        js = _js_path("lazy-load.js").read_text()
        assert "initSearchResultsLazyLoad" in js
        assert "search-load-more-sentinel" in js

    def test_js_contains_excerpt_lazy_init(self):
        """lazy-load.js should have the document detail excerpt lazy init."""
        js = _js_path("lazy-load.js").read_text()
        assert "initDocumentDetailLazyPreview" in js
        assert "doc-excerpt-lazy" in js

    def test_js_has_progressive_enhancement(self):
        """lazy-load.js should gracefully degrade without IntersectionObserver."""
        js = _js_path("lazy-load.js").read_text()
        # Should check for IntersectionObserver existence
        assert "'IntersectionObserver' in window" in js

    def test_js_has_root_margin_for_preloading(self):
        """lazy-load.js should use rootMargin to preload before visible."""
        js = _js_path("lazy-load.js").read_text()
        assert "rootMargin" in js
        assert "200px" in js


# ── 5. Script defer attributes ────────────────────────────────────


class TestScriptDefer:
    """Tests for defer attributes on non-critical scripts."""

    def test_lazy_load_js_has_defer_in_base(self):
        """base.html should load lazy-load.js with defer."""
        html = _read_template("base.html")
        assert 'lazy-load.js" defer' in html

    def test_documents_list_js_has_defer(self):
        """documents/list.html should load its JS with defer."""
        html = _read_template("documents/list.html")
        assert 'documents-list.js" defer' in html
        assert 'faceted-filters.js" defer' in html

    def test_viewer_js_has_defer(self):
        """viewer.html should load viewer.js with defer."""
        html = _read_template("viewer.html")
        assert 'viewer.js" defer' in html


# ── 6. Backward compatibility ────────────────────────────────────


class TestBackwardCompatibility:
    """Tests for backward compatibility of modified rendering functions."""

    def test_render_search_results_without_pagination_params(self):
        """_render_search_results should work without offset/limit/total."""
        from src.web.rendering import _render_search_results

        results = [
            {"id": 1, "title": "Test", "snippet": "snip", "status": "indexed"},
        ]
        html = _render_search_results("test", results)
        assert "Test" in html
        assert "1 result(s)" in html

    def test_render_search_results_with_pagination_params(self):
        """_render_search_results should accept offset/limit/total."""
        from src.web.rendering import _render_search_results

        results = [
            {"id": 1, "title": "Test", "snippet": "snip", "status": "indexed"},
        ]
        html = _render_search_results(
            "test", results, offset=0, limit=20, total=50
        )
        assert "50 result(s)" in html
        assert "search-load-more-sentinel" in html

    def test_render_search_result_row_returns_div(self):
        """_render_search_result_row should return a <div class='result'>."""
        from src.web.rendering import _render_search_result_row

        r = {"id": 42, "title": "My Doc", "snippet": "hello", "status": "indexed", "rank": 0.85}
        html = _render_search_result_row(r)
        assert '<div class="result">' in html
        assert "My Doc" in html
        assert "42" in html

    def test_render_document_rows_partial_returns_tr(self):
        """_render_document_rows_partial should return <tr> elements only."""
        from src.web.rendering import _render_document_rows_partial

        documents = [
            {"id": 1, "title": "Doc 1", "status": "indexed", "ext": ".txt",
             "source_name": "test", "created_at": "2024-01-01"},
        ]
        html = _render_document_rows_partial(documents)
        assert "<tr>" in html
        assert "<table" not in html
        assert "<tbody" not in html
        assert "Doc 1" in html


# ── 7. Rows partial template existence ────────────────────────────


class TestRowsPartialTemplate:
    """Tests for the document_rows.html partial template."""

    def test_template_exists(self):
        """The _partials/document_rows.html template should exist."""
        assert _template_path("_partials/document_rows.html").exists()

    def test_template_contains_tr_loop(self):
        """The template should loop over documents and render <tr> elements."""
        html = _read_template("_partials/document_rows.html")
        assert "{% for doc in documents %}" in html
        assert "<tr>" in html
        assert "{{ doc.id }}" in html

    def test_template_no_page_chrome(self):
        """The template should not contain page chrome elements."""
        html = _read_template("_partials/document_rows.html")
        # Strip comments to avoid false positives from documentation text
        import re
        code_only = re.sub(r'\{#.*?#\}', '', html, flags=re.DOTALL)
        assert "<html" not in code_only
        assert "<body" not in code_only
        assert "<table" not in code_only
        assert "<tbody" not in code_only
