"""Tests for HTTP-layer search filter integration.

Covers the full stack from HTTP request to rendered HTML:
- GET /documents with filter query params (HTML page)
- GET /api/v1/documents with filter query params (JSON API)
- Filter label construction in rendered output
- Filter panel open/closed state
- Pagination link preservation with active filters
- Filter parameter passthrough in pagination links
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

import pytest


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    """Provide a temporary database file path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "test_filter_http.db")


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
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as c:
        yield c

    await db.disconnect()
    server._db = original_db
    server._queue = original_queue


# ── Helper ───────────────────────────────────────────────────────


async def _insert_test_docs(db, count: int = 10) -> None:
    """Insert varied test documents for filter testing."""
    # Mix of PDFs and TXTs with different dates
    types = [".pdf", ".txt", ".pdf", ".txt", ".pdf"]
    dates = [
        "2024-01-15 12:00:00",
        "2024-06-20 08:00:00",
        "2025-03-10 14:00:00",
        "2025-09-05 10:00:00",
        "2026-01-20 16:00:00",
    ]
    sources = ["api", "local", "api", "local", "api"]

    for i in range(count):
        ext = types[i % len(types)]
        date = dates[i % len(dates)]
        src = sources[i % len(sources)]

        doc_id = await db.save_document(
            path=f"/docs/test_{i}{ext}",
            source_type=src,
            source_name=src,
            title=f"Test Document {i}",
            ext=ext,
            mime_type="application/pdf" if ext == ".pdf" else "text/plain",
            body=f"This is test document number {i}.",
            size=100 + i,
            status="indexed" if i % 2 == 0 else "pending",
        )

        # Override created_at for date filter testing
        async with db.connection() as conn:
            await conn.execute(
                "UPDATE documents SET created_at = ? WHERE id = ?",
                (date, doc_id),
            )
            await conn.commit()

        # Add tags to some documents
        if i < 3:
            await db.add_tag(doc_id, "important")
        if i % 3 == 0:
            await db.add_tag(doc_id, "starred")


# ── HTML Page filter tests ──────────────────────────────────────


class TestDocumentsPageWithFilters:
    """Test GET /documents with filter query parameters."""

    @pytest.mark.asyncio
    async def test_page_with_file_type_filter(self, asgi_client) -> None:
        """GET /documents?file_type=.pdf returns HTML with only PDF docs."""
        from src.web import server

        await _insert_test_docs(server._db)

        resp = await asgi_client.get("/documents?file_type=.pdf")
        assert resp.status_code == 200

        html = resp.text
        # Should show only PDFs in the table (5 PDFs out of 10 docs)
        assert ".pdf" in html
        # Filter panel should be open
        assert 'class="filter-panel"' in html
        # Filter label should mention type
        assert "type:" in html.lower()

    @pytest.mark.asyncio
    async def test_page_with_date_from_filter(self, asgi_client) -> None:
        """GET /documents?date_from=2025-01-01 excludes older docs."""
        from src.web import server

        await _insert_test_docs(server._db)

        resp = await asgi_client.get("/documents?date_from=2025-01-01")
        assert resp.status_code == 200

        html = resp.text
        # The label should mention "from"
        assert "from 2025-01-01" in html

    @pytest.mark.asyncio
    async def test_page_with_date_to_filter(self, asgi_client) -> None:
        """GET /documents?date_to=2024-12-31 excludes newer docs."""
        from src.web import server

        await _insert_test_docs(server._db)

        resp = await asgi_client.get("/documents?date_to=2024-12-31")
        assert resp.status_code == 200

        html = resp.text
        assert "to 2024-12-31" in html

    @pytest.mark.asyncio
    async def test_page_with_tag_and_file_type(self, asgi_client) -> None:
        """GET /documents?tag=important&file_type=.pdf — combined filters."""
        from src.web import server

        await _insert_test_docs(server._db)

        resp = await asgi_client.get(
            "/documents?tag=important&file_type=.pdf"
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_page_with_date_range(self, asgi_client) -> None:
        """GET /documents?date_from=2025-01-01&date_to=2025-12-31."""
        from src.web import server

        await _insert_test_docs(server._db)

        resp = await asgi_client.get(
            "/documents?date_from=2025-01-01&date_to=2025-12-31"
        )
        assert resp.status_code == 200

        html = resp.text
        assert "from 2025-01-01" in html
        assert "to 2025-12-31" in html

    @pytest.mark.asyncio
    async def test_page_with_all_filters(self, asgi_client) -> None:
        """GET /documents with all filter params combined."""
        from src.web import server

        await _insert_test_docs(server._db)

        resp = await asgi_client.get(
            "/documents?source=api&tag=important&file_type=.pdf"
            "&date_from=2024-01-01&date_to=2026-12-31"
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_page_with_empty_filters_returns_all(self, asgi_client) -> None:
        """GET /documents with no filter params returns all docs."""
        from src.web import server

        await _insert_test_docs(server._db)

        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200

        html = resp.text
        # Should show document count
        assert "Showing" in html

    @pytest.mark.asyncio
    async def test_page_preserves_filters_in_pagination_links(self, asgi_client) -> None:
        """Pagination links include active filter params."""
        from src.web import server

        await _insert_test_docs(server._db, count=25)

        resp = await asgi_client.get("/documents?file_type=.pdf&page=1&per_page=5")
        assert resp.status_code == 200

        html = resp.text
        # Pagination links should preserve file_type filter
        assert "file_type=.pdf" in html

    @pytest.mark.asyncio
    async def test_page_filter_panel_open_when_filters_active(self, asgi_client) -> None:
        """Filter panel has 'open' attribute when filters are active."""
        from src.web import server

        await _insert_test_docs(server._db)

        resp = await asgi_client.get("/documents?file_type=.pdf")
        assert resp.status_code == 200

        html = resp.text
        # Filter panel should be open
        assert '<details class="filter-panel" open>' in html

    @pytest.mark.asyncio
    async def test_page_filter_panel_closed_when_no_filters(self, asgi_client) -> None:
        """Filter panel is closed when no filters are active."""
        from src.web import server

        await _insert_test_docs(server._db)

        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200

        html = resp.text
        # Filter panel should NOT have 'open' attribute when no filters active
        # Jinja2 may render <details class="filter-panel"> or
        # <details  class="filter-panel"> — both are closed
        assert "filter-panel" in html
        # When no filters, the 'open' attribute must not be present
        assert 'filter-panel" open' not in html
        assert "filter-panel' open" not in html


# ── JSON API filter tests ───────────────────────────────────────


class TestDocumentsApiWithFilters:
    """Test GET /api/v1/documents with filter query parameters."""

    API_KEY = "test-api-key-docmind"

    @pytest.mark.asyncio
    async def test_api_file_type_filter(self, asgi_client) -> None:
        """GET /api/v1/documents?file_type=.pdf returns JSON with only PDFs."""
        from src.web import server

        await _insert_test_docs(server._db)

        resp = await asgi_client.get(
            "/api/v1/documents?file_type=.pdf",
            headers={"X-API-Key": self.API_KEY},
        )
        assert resp.status_code == 200

        data = resp.json()
        assert "total" in data
        assert "documents" in data
        # All returned docs should have ext=.pdf
        for doc in data["documents"]:
            assert doc["ext"] == ".pdf"

    @pytest.mark.asyncio
    async def test_api_date_range_filter(self, asgi_client) -> None:
        """GET /api/v1/documents?date_from=2025-01-01&date_to=2025-12-31."""
        from src.web import server

        await _insert_test_docs(server._db)

        resp = await asgi_client.get(
            "/api/v1/documents?date_from=2025-01-01&date_to=2025-12-31",
            headers={"X-API-Key": self.API_KEY},
        )
        assert resp.status_code == 200

        data = resp.json()
        for doc in data["documents"]:
            assert doc["created_at"] >= "2025-01-01"
            assert doc["created_at"] <= "2025-12-31 23:59:59"

    @pytest.mark.asyncio
    async def test_api_tag_filter(self, asgi_client) -> None:
        """GET /api/v1/documents?tag=important returns tagged docs."""
        from src.web import server

        await _insert_test_docs(server._db)

        resp = await asgi_client.get(
            "/api/v1/documents?tag=important",
            headers={"X-API-Key": self.API_KEY},
        )
        assert resp.status_code == 200

        data = resp.json()
        # At least the first 3 docs got "important" tag
        assert data["total"] >= 3

    @pytest.mark.asyncio
    async def test_api_combined_filters(self, asgi_client) -> None:
        """GET /api/v1/documents with tag + file_type + date_range."""
        from src.web import server

        await _insert_test_docs(server._db)

        resp = await asgi_client.get(
            "/api/v1/documents?tag=important&file_type=.pdf"
            "&date_from=2024-01-01&date_to=2026-12-31",
            headers={"X-API-Key": self.API_KEY},
        )
        assert resp.status_code == 200

        data = resp.json()
        for doc in data["documents"]:
            assert doc["ext"] == ".pdf"

    @pytest.mark.asyncio
    async def test_api_pagination_with_filters(self, asgi_client) -> None:
        """GET /api/v1/documents?file_type=.pdf&page=1&per_page=2."""
        from src.web import server

        await _insert_test_docs(server._db, count=10)

        resp = await asgi_client.get(
            "/api/v1/documents?file_type=.pdf&page=1&per_page=2",
            headers={"X-API-Key": self.API_KEY},
        )
        assert resp.status_code == 200

        data = resp.json()
        assert data["page"] == 1
        assert data["per_page"] == 2
        assert len(data["documents"]) <= 2

    @pytest.mark.asyncio
    async def test_api_source_filter(self, asgi_client) -> None:
        """GET /api/v1/documents?source=api filters by source."""
        from src.web import server

        await _insert_test_docs(server._db)

        resp = await asgi_client.get(
            "/api/v1/documents?source=api",
            headers={"X-API-Key": self.API_KEY},
        )
        assert resp.status_code == 200

        data = resp.json()
        for doc in data["documents"]:
            assert doc["source_name"] == "api" or doc["source_type"] == "api"

    @pytest.mark.asyncio
    async def test_api_no_filters_returns_all(self, asgi_client) -> None:
        """GET /api/v1/documents with no filters returns all docs."""
        from src.web import server

        await _insert_test_docs(server._db)

        resp = await asgi_client.get(
            "/api/v1/documents",
            headers={"X-API-Key": self.API_KEY},
        )
        assert resp.status_code == 200

        data = resp.json()
        assert data["total"] >= 10


# ── Rendering function tests ────────────────────────────────────


class TestRenderDocumentsListWithFilters:
    """Test _render_documents_list filter label construction."""

    def test_filter_label_date_from_only(self) -> None:
        """_render_documents_list shows date_from in filter label."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="", page=1, per_page=20, total=0,
            total_pages=0, date_from="2025-06-01",
        )
        assert "from 2025-06-01" in html

    def test_filter_label_date_to_only(self) -> None:
        """_render_documents_list shows date_to in filter label."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="", page=1, per_page=20, total=0,
            total_pages=0, date_to="2025-12-31",
        )
        assert "to 2025-12-31" in html

    def test_filter_label_file_type_only(self) -> None:
        """_render_documents_list shows file_type in filter label."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="", page=1, per_page=20, total=0,
            total_pages=0, file_type=".pdf",
        )
        assert "type:" in html
        assert ".pdf" in html

    def test_filter_label_date_and_type(self) -> None:
        """_render_documents_list combines date + type in filter label."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="", page=1, per_page=20, total=0,
            total_pages=0, date_from="2025-01-01", file_type=".pdf",
        )
        assert "from 2025-01-01" in html
        assert "type:" in html

    def test_filter_label_with_tag(self) -> None:
        """_render_documents_list with active_tag shows tag in label."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="", page=1, per_page=20, total=0,
            total_pages=0, active_tag="important",
        )
        assert "tag:" in html
        assert "important" in html

    def test_filter_label_tag_with_date(self) -> None:
        """_render_documents_list with tag + date shows both in label."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="", page=1, per_page=20, total=0,
            total_pages=0, active_tag="important", date_from="2025-01-01",
        )
        assert "tag:" in html
        assert "important" in html
        assert "from 2025-01-01" in html

    def test_filter_label_source_with_type(self) -> None:
        """_render_documents_list with source + file_type shows both."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="api", page=1, per_page=20, total=0,
            total_pages=0, file_type=".pdf",
        )
        assert "api" in html
        assert "type:" in html

    def test_filter_panel_open_when_filters_active(self) -> None:
        """Template receives date_from/date_to/file_type for filter panel state."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="", page=1, per_page=20, total=0,
            total_pages=0, date_from="2025-01-01",
        )
        # The rendered template should contain the filter form
        assert "filter-panel" in html
        assert 'name="date_from"' in html

    def test_filter_form_has_all_inputs(self) -> None:
        """Filter form contains source, tag, file_type, date_from, date_to inputs."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="", page=1, per_page=20, total=0,
            total_pages=0,
        )
        assert 'name="source"' in html
        assert 'name="tag"' in html
        assert 'name="file_type"' in html
        assert 'name="date_from"' in html
        assert 'name="date_to"' in html

    def test_filter_inputs_preserve_values(self) -> None:
        """Active filter values are preserved in form inputs."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="api", page=1, per_page=20, total=0,
            total_pages=0, date_from="2025-06-01", date_to="2025-12-31",
            file_type=".pdf",
        )
        # Input values should be preserved
        assert 'value="api"' in html
        assert 'value="2025-06-01"' in html
        assert 'value="2025-12-31"' in html
        assert 'value=".pdf"' in html

    def test_pagination_preserves_filter_params(self) -> None:
        """Pagination links contain active filter parameters."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="api", page=1, per_page=20, total=50,
            total_pages=3, date_from="2025-01-01", file_type=".pdf",
        )
        # Pagination should include filter params
        assert "date_from=2025-01-01" in html or "date_from%3D2025-01-01" in html
        assert "file_type=.pdf" in html or "file_type%3D.pdf" in html

    def test_empty_filter_label_when_no_filters(self) -> None:
        """Filter label is empty/absent when no filters active."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="", page=1, per_page=20, total=0,
            total_pages=0,
        )
        # When no filters, the label should just show "Documents"
        assert "Documents" in html
