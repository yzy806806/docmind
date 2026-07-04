"""Tests for web UI improvements: delete, pagination, dark mode, chat page.

Covers:
- DELETE /api/v1/documents/{doc_id} REST endpoint
- POST /documents/{doc_id}/delete form handler
- GET /documents?page=N&per_page=M paginated document list
- Dark mode CSS variables and toggle button in _base_page()
- GET /chat HTML page route
- Responsive design elements (viewport meta, media queries)
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
        yield str(Path(tmpdir) / "test_web_ui.db")


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

    # Insert test documents
    for i in range(25):
        await db.save_document(
            path=f"/docs/test_{i}.txt",
            source_type="api",
            source_name="test-source",
            title=f"Test Document {i}",
            ext=".txt",
            mime_type="text/plain",
            body=f"This is the body of test document {i}. It contains searchable text.",
            size=100,
            status="indexed" if i % 2 == 0 else "pending",
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


# ── Dark mode CSS tests ──────────────────────────────────────────


class TestDarkModeCSS:
    """Tests for dark mode CSS presence in the base page template."""

    def test_base_page_has_css_variables(self):
        """_base_page output should contain CSS custom properties for theming."""
        from src.web.server import _base_page

        html = _base_page("Test", "<p>content</p>")
        assert ":root {" in html or ":root{{" in html or "--bg" in html
        assert "--surface" in html
        assert "--text" in html

    def test_base_page_has_dark_theme_selector(self):
        """_base_page should contain [data-theme='dark'] selector."""
        from src.web.server import _base_page

        html = _base_page("Test", "<p>content</p>")
        assert 'data-theme="dark"' in html or "data-theme='dark'" in html

    def test_base_page_has_theme_toggle_button(self):
        """_base_page should contain a theme toggle button."""
        from src.web.server import _base_page

        html = _base_page("Test", "<p>content</p>")
        assert "theme-toggle" in html
        assert "toggleTheme" in html

    def test_base_page_has_localstorage_script(self):
        """_base_page should persist theme preference via localStorage."""
        from src.web.server import _base_page

        html = _base_page("Test", "<p>content</p>")
        assert "localStorage" in html
        assert "docmind-theme" in html

    def test_dashboard_page_has_dark_mode_css(self):
        """Dashboard render output should inherit dark mode from _base_page."""
        from src.web.server import _render_dashboard

        html = _render_dashboard(
            {"total": 0, "pending": 0, "indexed": 0, "summarized": 0, "active_jobs": 0},
            [],
        )
        assert "--bg" in html
        assert 'data-theme="dark"' in html

    def test_documents_list_has_dark_mode_css(self):
        """Documents list render should inherit dark mode."""
        from src.web.server import _render_documents_list

        html = _render_documents_list([], "", 1, 20, 0, 0)
        assert "--bg" in html

    def test_document_detail_has_dark_mode_css(self):
        """Document detail render should inherit dark mode."""
        from src.web.server import _render_document_detail

        doc = {"id": 1, "title": "Test", "status": "indexed", "body": "content"}
        html = _render_document_detail(doc)
        assert "--bg" in html

    def test_search_results_has_dark_mode_css(self):
        """Search results render should inherit dark mode."""
        from src.web.server import _render_search_results

        html = _render_search_results("query", [])
        assert "--bg" in html

    def test_chat_page_has_dark_mode_css(self):
        """Chat page render should inherit dark mode."""
        from src.web.server import _render_chat_page

        html = _render_chat_page()
        assert "--bg" in html


# ── Responsive design tests ───────────────────────────────────────


class TestResponsiveDesign:
    """Tests for responsive design elements."""

    def test_base_page_has_viewport_meta(self):
        """_base_page should include viewport meta tag for mobile."""
        from src.web.server import _base_page

        html = _base_page("Test", "<p>content</p>")
        assert "viewport" in html
        assert "width=device-width" in html

    def test_base_page_has_media_query(self):
        """_base_page should include a mobile media query."""
        from src.web.server import _base_page

        html = _base_page("Test", "<p>content</p>")
        assert "@media" in html
        assert "max-width" in html

    def test_base_page_has_nav_toggle(self):
        """_base_page should have a nav toggle button for mobile."""
        from src.web.server import _base_page

        html = _base_page("Test", "<p>content</p>")
        assert "nav-toggle" in html

    def test_base_page_has_chat_nav_link(self):
        """Nav bar should include a link to /chat."""
        from src.web.server import _base_page

        html = _base_page("Test", "<p>content</p>")
        assert 'href="/chat"' in html


# ── Pagination tests ──────────────────────────────────────────────


class TestPaginationRendering:
    """Tests for pagination rendering logic."""

    def test_pagination_single_page_returns_empty(self):
        """When total_pages <= 1, pagination HTML should be empty."""
        from src.web.server import _render_pagination

        result = _render_pagination(1, 20, 5, 1, "")
        assert result == ""

    def test_pagination_multiple_pages_has_nav(self):
        """Pagination should have prev/next links for multiple pages."""
        from src.web.server import _render_pagination

        result = _render_pagination(2, 20, 60, 3, "")
        assert "pagination" in result
        assert "Prev" in result
        assert "Next" in result

    def test_pagination_current_page_marked(self):
        """Current page should be marked with 'current' class."""
        from src.web.server import _render_pagination

        result = _render_pagination(2, 20, 60, 3, "")
        assert "current" in result

    def test_pagination_first_page_no_prev_link(self):
        """On page 1, prev should be disabled."""
        from src.web.server import _render_pagination

        result = _render_pagination(1, 20, 60, 3, "")
        assert "disabled" in result
        # Next should still work
        assert "Next" in result
        assert "page=2" in result

    def test_pagination_last_page_no_next_link(self):
        """On last page, next should be disabled."""
        from src.web.server import _render_pagination

        result = _render_pagination(3, 20, 60, 3, "")
        assert "disabled" in result
        assert "Prev" in result
        assert "page=2" in result

    def test_pagination_many_pages_has_ellipsis(self):
        """With many pages, ellipsis should appear."""
        from src.web.server import _render_pagination

        result = _render_pagination(5, 20, 200, 10, "")
        assert "…" in result

    def test_documents_list_shows_pagination_info(self):
        """Documents list should show pagination info with total count."""
        from src.web.server import _render_documents_list

        html = _render_documents_list([], "", 1, 20, 0, 0)
        assert "document(s)" in html

    def test_documents_list_shows_range(self):
        """Documents list should show the item range."""
        from src.web.server import _render_documents_list

        html = _render_documents_list(
            [{"id": 1, "title": "Doc 1", "status": "indexed"}],
            "",
            2,
            20,
            25,
            2,
        )
        assert "Showing" in html
        assert "25" in html


# ── Delete endpoint tests ─────────────────────────────────────────


class TestDeleteAPIEndpoint:
    """Tests for DELETE /api/v1/documents/{doc_id}."""

    @pytest.mark.asyncio
    async def test_delete_existing_document(self, asgi_client):
        """DELETE should remove a document and return JSON confirmation."""
        resp = await asgi_client.delete("/api/v1/documents/1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 1
        assert data["deleted"] is True

    @pytest.mark.asyncio
    async def test_delete_nonexistent_document(self, asgi_client):
        """DELETE should return 404 for non-existent document."""
        resp = await asgi_client.delete("/api/v1/documents/9999")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_invalid_id(self, asgi_client):
        """DELETE should return 400 or 422 for invalid ID."""
        resp = await asgi_client.delete("/api/v1/documents/-1")
        assert resp.status_code in (400, 422)


class TestDeleteFormHandler:
    """Tests for POST /documents/{doc_id}/delete form handler."""

    @pytest.mark.asyncio
    async def test_delete_form_existing_document(self, asgi_client):
        """POST form delete should return success HTML page."""
        resp = await asgi_client.post("/documents/2/delete")
        assert resp.status_code == 200
        assert "Deleted" in resp.text or "deleted" in resp.text

    @pytest.mark.asyncio
    async def test_delete_form_nonexistent(self, asgi_client):
        """POST form delete for non-existent document should return 404."""
        resp = await asgi_client.post("/documents/9999/delete")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_form_has_back_link(self, asgi_client):
        """Delete success page should have a link back to documents."""
        resp = await asgi_client.post("/documents/3/delete")
        assert resp.status_code == 200
        assert "/documents" in resp.text


class TestDocumentDetailDeleteButton:
    """Tests for delete button presence on document detail page."""

    def test_detail_page_has_delete_button(self):
        """Document detail page should have a delete button."""
        from src.web.server import _render_document_detail

        doc = {"id": 42, "title": "Test Doc", "status": "indexed", "body": "content"}
        html = _render_document_detail(doc)
        assert "btn-delete" in html
        assert "Delete" in html

    def test_detail_page_has_confirm_dialog(self):
        """Delete form should have JavaScript confirm() dialog."""
        from src.web.server import _render_document_detail

        doc = {"id": 42, "title": "Test Doc", "status": "indexed", "body": "content"}
        html = _render_document_detail(doc)
        assert "confirm(" in html

    def test_detail_page_delete_form_action(self):
        """Delete form should POST to /documents/{id}/delete."""
        from src.web.server import _render_document_detail

        doc = {"id": 42, "title": "Test Doc", "status": "indexed", "body": "content"}
        html = _render_document_detail(doc)
        assert 'action="/documents/42/delete"' in html
        assert 'method="post"' in html


# ── Chat page tests ───────────────────────────────────────────────


class TestChatPage:
    """Tests for GET /chat HTML page route."""

    @pytest.mark.asyncio
    async def test_chat_page_returns_html(self, asgi_client):
        """GET /chat should return 200 HTML with chat UI."""
        resp = await asgi_client.get("/chat")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_chat_page_has_websocket_client(self, asgi_client):
        """Chat page should include WebSocket client JavaScript."""
        resp = await asgi_client.get("/chat")
        assert "WebSocket" in resp.text
        assert "'ws:'" in resp.text or "'wss:'" in resp.text

    @pytest.mark.asyncio
    async def test_chat_page_has_input_and_send_button(self, asgi_client):
        """Chat page should have input field and send button."""
        resp = await asgi_client.get("/chat")
        assert "chat-input" in resp.text
        assert "sendChat" in resp.text

    @pytest.mark.asyncio
    async def test_chat_page_has_citations_panel(self, asgi_client):
        """Chat page should have a citations panel."""
        resp = await asgi_client.get("/chat")
        assert "citations-panel" in resp.text


# ── Pagination route integration tests ────────────────────────────


class TestPaginationRoute:
    """Tests for GET /documents with pagination parameters."""

    @pytest.mark.asyncio
    async def test_documents_default_pagination(self, asgi_client):
        """GET /documents should default to page 1, 20 per page."""
        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200
        assert "Showing" in resp.text
        assert "20" in resp.text  # per_page=20

    @pytest.mark.asyncio
    async def test_documents_custom_per_page(self, asgi_client):
        """GET /documents?per_page=5 should return 5 items per page."""
        resp = await asgi_client.get("/documents?per_page=5")
        assert resp.status_code == 200
        assert "5" in resp.text

    @pytest.mark.asyncio
    async def test_documents_page_2(self, asgi_client):
        """GET /documents?page=2 should show second page."""
        resp = await asgi_client.get("/documents?page=2&per_page=10")
        assert resp.status_code == 200
        # Should have pagination nav
        assert "pagination" in resp.text

    @pytest.mark.asyncio
    async def test_documents_pagination_has_total(self, asgi_client):
        """Documents page should show total document count."""
        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200
        assert "25" in resp.text  # We inserted 25 documents
