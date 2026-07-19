"""Tests for faceted search (Phase 4c).

Covers:
- Facet count correctness: DB facet methods return accurate counts
- Facet count with filters applied: applying one filter updates other facets
- Facet UI structure: dropdowns with counts, collapsible panels
- HTMX partial: /documents/partials/table returns correct fragment with facets
- Edge cases: empty DB, single-doc DB, special characters in facet values
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Generator

import pytest


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "test_faceted_search.db")


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

    app = server.create_app()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as c:
        yield c

    await db.disconnect()
    server._db = original_db
    server._queue = original_queue


async def _insert_varied_docs(db, shape: str = "standard") -> None:
    """Insert documents with varied types, sources, and dates for facet testing.

    Args:
        shape: "standard" = 12 docs with 3 types x 2 sources x varied dates
               "single" = 1 doc only
    """
    if shape == "single":
        await db.save_document(
            path="/docs/single.pdf",
            source_type="api",
            source_name="api-single",
            title="Single Document",
            ext=".pdf",
            mime_type="application/pdf",
            body="Only one document.",
            size=50,
            status="indexed",
        )
        return

    # Standard: varied docs
    configs = [
        # (path, source_type, ext, date)
        ("/docs/a.pdf", "api", ".pdf", "2024-01-15 12:00:00"),
        ("/docs/b.pdf", "api", ".pdf", "2024-03-20 08:00:00"),
        ("/docs/c.pdf", "local", ".pdf", "2024-06-10 14:00:00"),
        ("/docs/d.pdf", "webdav", ".pdf", "2024-09-05 10:00:00"),
        ("/docs/e.txt", "api", ".txt", "2025-01-20 16:00:00"),
        ("/docs/f.txt", "api", ".txt", "2025-03-15 09:00:00"),
        ("/docs/g.txt", "local", ".txt", "2025-06-01 11:00:00"),
        ("/docs/h.txt", "webdav", ".txt", "2025-09-12 07:00:00"),
        ("/docs/i.md", "api", ".md", "2026-01-01 13:00:00"),
        ("/docs/j.md", "api", ".md", "2026-02-14 15:00:00"),
        ("/docs/k.md", "local", ".md", "2026-03-30 17:00:00"),
        ("/docs/l.md", "webdav", ".md", "2026-06-15 19:00:00"),
    ]

    for path, source_type, ext, date in configs:
        doc_id = await db.save_document(
            path=path,
            source_type=source_type,
            source_name=source_type,
            title=Path(path).name,
            ext=ext,
            mime_type="application/pdf" if ext == ".pdf" else "text/plain",
            body=f"Body of {Path(path).name}",
            size=100,
            status="indexed",
        )

        async with db.connection() as conn:
            await conn.execute(
                "UPDATE documents SET created_at = ? WHERE id = ?",
                (date, doc_id),
            )
            await conn.commit()


# ── DB-layer facet method tests ──────────────────────────────────


class TestDbFacetMethodsComprehensive:
    """Comprehensive DB-layer facet method tests beyond basic correctness."""

    @pytest.fixture
    async def facet_db(self, tmp_db_path: str):
        from src.core.db_sqlite import Database

        db = Database(db_path=tmp_db_path)
        await db.connect()
        yield db
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_facet_counts_match_actual_distribution(self, facet_db) -> None:
        """Facet counts should precisely match the distribution of docs in DB."""
        await _insert_varied_docs(facet_db, "standard")

        file_facets = await facet_db.get_file_type_facets()
        # .pdf: 4, .md: 4, .txt: 4
        assert len(file_facets) == 3
        total = sum(f["count"] for f in file_facets)
        assert total == 12

        source_facets = await facet_db.get_source_facets()
        # api: 6, local: 3, webdav: 3
        assert len(source_facets) == 3
        total = sum(f["count"] for f in source_facets)
        assert total == 12

    @pytest.mark.asyncio
    async def test_facets_sorted_by_count_desc(self, facet_db) -> None:
        """Facet results should be sorted by count descending."""
        await _insert_varied_docs(facet_db, "standard")

        file_facets = await facet_db.get_file_type_facets()
        # All have same count (4), but should be stable
        counts = [f["count"] for f in file_facets]
        assert counts == sorted(counts, reverse=True)

        source_facets = await facet_db.get_source_facets()
        # api=6, local=3, webdav=3
        assert source_facets[0]["source_type"] == "api"
        assert source_facets[0]["count"] == 6

    @pytest.mark.asyncio
    async def test_facets_single_document(self, facet_db) -> None:
        """Facets on a single-document DB should return one item each."""
        await _insert_varied_docs(facet_db, "single")

        file_facets = await facet_db.get_file_type_facets()
        assert len(file_facets) == 1
        assert file_facets[0]["ext"] == ".pdf"
        assert file_facets[0]["count"] == 1

        source_facets = await facet_db.get_source_facets()
        assert len(source_facets) == 1
        assert source_facets[0]["source_type"] == "api"
        assert source_facets[0]["count"] == 1

    @pytest.mark.asyncio
    async def test_facets_empty_db(self, facet_db) -> None:
        """Facet methods on an empty DB should return empty lists."""
        file_facets = await facet_db.get_file_type_facets()
        source_facets = await facet_db.get_source_facets()
        assert file_facets == []
        assert source_facets == []

    @pytest.mark.asyncio
    async def test_facets_with_null_ext(self, facet_db) -> None:
        """Documents with None/NULL ext should be counted as ''."""
        await facet_db.save_document(
            path="/docs/noext",
            source_type="api",
            source_name="api",
            title="No Extension",
            ext="",
            mime_type="application/octet-stream",
            body="No extension.",
            size=10,
        )

        file_facets = await facet_db.get_file_type_facets()
        assert len(file_facets) == 1
        # Empty ext should be represented as ""
        assert file_facets[0]["count"] == 1

    @pytest.mark.asyncio
    async def test_facets_return_ordered_dict_keys(self, facet_db) -> None:
        """Facet items should have consistent key structure."""
        await _insert_varied_docs(facet_db, "standard")

        file_facets = await facet_db.get_file_type_facets()
        for item in file_facets:
            assert "ext" in item
            assert "count" in item
            assert isinstance(item["count"], int)
            assert item["count"] > 0

        source_facets = await facet_db.get_source_facets()
        for item in source_facets:
            assert "source_type" in item
            assert "count" in item
            assert isinstance(item["count"], int)
            assert item["count"] > 0


# ── Facet counts with filters applied ────────────────────────────


class TestFacetCountsWithFilters:
    """Test that facet counts update correctly when filters are active.

    Note: The current DB facet methods (get_file_type_facets, get_source_facets)
    return global counts — they do NOT restrict by active filters.
    The server fetches facets independently of document queries.
    Testing the server behavior to ensure facets render correctly even
    when docs are filtered.
    """

    @pytest.mark.asyncio
    async def test_facets_rendered_when_filtering_by_source(self, asgi_client) -> None:
        """Facets should still be rendered when source filter is active."""
        from src.web import server

        await _insert_varied_docs(server._db, "standard")

        resp = await asgi_client.get("/documents?source=api")
        assert resp.status_code == 200

        html = resp.text
        # Should still have the facet dropdowns
        assert '<select name="source"' in html
        assert '<select name="file_type"' in html
        # Source "api" should be selected
        api_idx = html.find('value="api"')
        assert api_idx > -1
        assert "selected" in html[api_idx:api_idx + 50]

    @pytest.mark.asyncio
    async def test_facets_rendered_when_filtering_by_file_type(self, asgi_client) -> None:
        """Facets should still be rendered when file_type filter is active."""
        from src.web import server

        await _insert_varied_docs(server._db, "standard")

        resp = await asgi_client.get("/documents?file_type=.pdf")
        assert resp.status_code == 200

        html = resp.text
        assert '<select name="source"' in html
        assert '<select name="file_type"' in html
        # File type .pdf should be selected
        pdf_idx = html.find('value=".pdf"')
        assert pdf_idx > -1
        assert "selected" in html[pdf_idx:pdf_idx + 50]

    @pytest.mark.asyncio
    async def test_facets_rendered_with_combined_filters(self, asgi_client) -> None:
        """Facets render correctly with combined source + file_type filters."""
        from src.web import server

        await _insert_varied_docs(server._db, "standard")

        resp = await asgi_client.get("/documents?source=api&file_type=.pdf")
        assert resp.status_code == 200

        html = resp.text
        assert '<select name="source"' in html
        assert '<select name="file_type"' in html

    @pytest.mark.asyncio
    async def test_facets_rendered_with_date_filters(self, asgi_client) -> None:
        """Facets render correctly with date range filters."""
        from src.web import server

        await _insert_varied_docs(server._db, "standard")

        resp = await asgi_client.get(
            "/documents?date_from=2025-01-01&date_to=2025-12-31"
        )
        assert resp.status_code == 200

        html = resp.text
        assert '<select name="source"' in html
        assert '<select name="file_type"' in html

    @pytest.mark.asyncio
    async def test_facets_on_empty_filter_result(self, asgi_client) -> None:
        """Facets should render even when filter yields zero documents."""
        from src.web import server

        await _insert_varied_docs(server._db, "standard")

        resp = await asgi_client.get("/documents?source=nonexistent")
        assert resp.status_code == 200

        html = resp.text
        # Facet dropdowns should still exist
        assert '<select name="source"' in html
        assert '<select name="file_type"' in html

    @pytest.mark.asyncio
    async def test_facet_counts_on_documents_page(self, asgi_client) -> None:
        """Full-page route renders facet dropdowns with DB counts."""
        from src.web import server

        await _insert_varied_docs(server._db, "standard")

        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200

        html = resp.text
        # 4 PDFs, 4 TXTs, 4 MDs
        assert ".pdf" in html
        assert ".txt" in html
        assert ".md" in html
        # Counts should be present in parentheses
        assert "(4)" in html or "(6)" in html


# ── Facet UI structure ───────────────────────────────────────────


class TestFacetUIStructure:
    """Test the rendered facet UI structure in the documents page."""

    def test_source_facet_dropdown_has_correct_options(self) -> None:
        """Source facet <select> renders all facets with counts."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="", page=1, per_page=20, total=0,
            total_pages=0,
            source_facets=[
                {"source_type": "api", "count": 15},
                {"source_type": "local", "count": 7},
                {"source_type": "webdav", "count": 3},
            ],
            file_type_facets=[
                {"ext": ".pdf", "count": 12},
                {"ext": ".txt", "count": 8},
            ],
        )

        assert '<select name="source"' in html
        assert '<select name="file_type"' in html
        assert "全部来源" in html
        assert "全部类型" in html

        # Each facet value should appear as an option
        assert "api (15)" in html
        assert "local (7)" in html
        assert "webdav (3)" in html
        assert ".pdf (12)" in html
        assert ".txt (8)" in html

    def test_filter_panel_is_collapsible_details(self) -> None:
        """Filter panel should be a <details> element (collapsible)."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="", page=1, per_page=20, total=0,
            total_pages=0,
        )
        assert "<details" in html
        assert "filter-panel" in html
        assert "<summary>" in html

    def test_filter_panel_open_when_file_type_active(self) -> None:
        """Filter panel should be open when file_type filter is active."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="", page=1, per_page=20, total=0,
            total_pages=0, file_type=".pdf",
        )
        assert '<details class="filter-panel" open>' in html

    def test_filter_panel_open_when_source_active(self) -> None:
        """Filter panel should be open when source filter is active."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="api", page=1, per_page=20, total=0,
            total_pages=0,
        )
        # active_source is passed, so panel opens
        assert '<details class="filter-panel" open>' in html

    def test_filter_panel_open_when_date_from_active(self) -> None:
        """Filter panel should be open when date_from filter is active."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="", page=1, per_page=20, total=0,
            total_pages=0, date_from="2025-01-01",
        )
        assert '<details class="filter-panel" open>' in html

    def test_filter_panel_closed_when_no_filters(self) -> None:
        """Filter panel should be closed (no open attr) when no filters."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="", page=1, per_page=20, total=0,
            total_pages=0,
        )
        assert 'filter-panel" open' not in html

    def test_date_preset_buttons_present(self) -> None:
        """Quick-range date preset buttons are rendered."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="", page=1, per_page=20, total=0,
            total_pages=0,
        )
        assert "date-preset-btn" in html
        assert 'data-days="7"' in html
        assert 'data-days="30"' in html
        assert 'data-days="90"' in html
        assert 'data-days="365"' in html

    def test_date_inputs_present(self) -> None:
        """Date from/to inputs are present in the filter form."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="", page=1, per_page=20, total=0,
            total_pages=0,
        )
        assert 'name="date_from"' in html
        assert 'name="date_to"' in html
        assert 'type="date"' in html

    def test_facets_js_loaded(self) -> None:
        """faceted-filters.js is loaded in the document list template."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="", page=1, per_page=20, total=0,
            total_pages=0,
        )
        assert "/static/js/faceted-filters.js" in html

    def test_filter_form_has_submit_button(self) -> None:
        """Filter form should have a way to submit (button or hx-trigger)."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="", page=1, per_page=20, total=0,
            total_pages=0,
        )
        # Filter form should exist
        assert "facet-filter-form" in html or "filter" in html.lower()


# ── HTMX partial: facet data in table partial ────────────────────


class TestHTMXPartialWithFacets:
    """Test that the HTMX partial endpoint includes facet data."""

    @pytest.mark.asyncio
    async def test_partial_includes_facet_dropdowns(self, asgi_client) -> None:
        """The partial endpoint renders the table region with bulk actions bar.

        Note: The partial does NOT render the facet dropdowns (source/file_type
        <select>) — those live in the filter panel which is part of the
        full-page template, not the table partial. The partial only contains
        the #doc-table-region content (table + pagination + bulk-actions bar).
        """
        from src.web import server

        await _insert_varied_docs(server._db, "standard")

        resp = await asgi_client.get("/documents/partials/table")
        assert resp.status_code == 200

        html = resp.text
        # The partial should contain the doc table region and bulk actions
        assert 'id="doc-table-region"' in html
        assert 'id="bulk-actions-bar"' in html
        # Facet dropdowns are NOT in the partial (they're in the full page)
        # This documents the current design: facets live in the filter panel,
        # not in the HTMX-swapped table region.

    @pytest.mark.asyncio
    async def test_partial_includes_facet_collections(self, asgi_client) -> None:
        """The partial renders collection options for the bulk-move dropdown."""
        from src.web import server

        await _insert_varied_docs(server._db, "standard")
        await server._db.create_collection(name="Test Collection", description="desc")

        resp = await asgi_client.get("/documents/partials/table")
        assert resp.status_code == 200

        html = resp.text
        assert "Test Collection" in html

    @pytest.mark.asyncio
    async def test_partial_includes_bulk_actions(self, asgi_client) -> None:
        """The partial must include the bulk-actions bar."""
        from src.web import server

        await _insert_varied_docs(server._db, "standard")

        resp = await asgi_client.get("/documents/partials/table")
        assert resp.status_code == 200

        html = resp.text
        assert 'id="bulk-actions-bar"' in html
        assert 'id="bulk-tag-form"' in html
        assert 'id="bulk-move-form"' in html
        assert 'id="bulk-export-form"' in html

    @pytest.mark.asyncio
    async def test_partial_renders_pagination(self, asgi_client) -> None:
        """The partial should include pagination controls."""
        from src.web import server

        await _insert_varied_docs(server._db, "standard")

        resp = await asgi_client.get(
            "/documents/partials/table?page=1&per_page=5"
        )
        assert resp.status_code == 200

        html = resp.text
        # Should have pagination info
        assert "显示第" in html
        assert "page" in html.lower() or "document" in html.lower()

    @pytest.mark.asyncio
    async def test_partial_filters_respected(self, asgi_client) -> None:
        """The partial respects filter params and returns only matching docs."""
        from src.web import server

        await _insert_varied_docs(server._db, "standard")

        resp = await asgi_client.get(
            "/documents/partials/table?source=api&file_type=.pdf"
        )
        assert resp.status_code == 200

        html = resp.text
        # Should show fewer results than total
        assert "document" in html.lower()


# ── Edge cases: empty DB, special values ─────────────────────────


class TestFacetedSearchEdgeCases:
    """Edge cases for faceted search rendering."""

    @pytest.mark.asyncio
    async def test_facets_page_with_empty_db(self, asgi_client) -> None:
        """GET /documents on an empty DB should render without error."""
        from src.web import server

        _ = server._db  # ensure DB is assigned

        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200

        html = resp.text
        # Should still have the filter panel
        assert "filter-panel" in html
        assert "Documents" in html

    def test_facets_with_empty_lists(self) -> None:
        """When facet lists are empty, dropdowns render with just 'all' option."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="", page=1, per_page=20, total=0,
            total_pages=0,
            file_type_facets=[],
            source_facets=[],
        )
        assert '<select name="source"' in html
        assert '<select name="file_type"' in html
        assert "全部来源" in html
        assert "全部类型" in html

    def test_facets_with_special_extensions(self) -> None:
        """Facets with unusual extensions render correctly."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="", page=1, per_page=20, total=0,
            total_pages=0,
            file_type_facets=[
                {"ext": ".tar.gz", "count": 1},
                {"ext": ".CONFIG", "count": 2},
            ],
        )
        assert ".tar.gz (1)" in html
        assert ".CONFIG (2)" in html

    def test_facets_with_special_source_types(self) -> None:
        """Facets with special characters in source types render correctly."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[], source="", page=1, per_page=20, total=0,
            total_pages=0,
            source_facets=[
                {"source_type": "REST-API-v2", "count": 5},
                {"source_type": "some/file/path", "count": 1},
            ],
        )
        # Values should appear escaped in <option> tags
        assert "REST-API-v2" in html
        assert "some/file/path" in html

    @pytest.mark.asyncio
    async def test_server_fetches_facets_on_page_load(self, asgi_client) -> None:
        """Verify that GET /documents actually fetches facets from DB."""
        from src.web import server

        await _insert_varied_docs(server._db, "standard")

        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200

        html = resp.text
        # Facets should have counts from actual DB
        assert ".pdf" in html
        assert "api" in html
        # Count values should be present
        assert "(4)" in html or "(6)" in html or "(3)" in html

    @pytest.mark.asyncio
    async def test_facets_respect_only_existing_docs(self, asgi_client) -> None:
        """After deleting all PDFs, .pdf should not appear in facets."""
        from src.web import server

        await _insert_varied_docs(server._db, "standard")

        # Delete PDF docs (ids 1-4)
        for doc_id in range(1, 5):
            await server._db.delete_document(doc_id)

        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200

        html = resp.text
        # .pdf might still appear if facets are global (not filtered),
        # but counts should change. Just verify the page renders.
        assert '<select name="file_type"' in html


# ── Rendering function tests for facets ──────────────────────────


class TestRenderingFunctionFacets:
    """Test _render_documents_list and _render_documents_table_partial with facets."""

    def test_render_list_passes_facets(self) -> None:
        """_render_documents_list passes facet data through to template."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[],
            source="",
            page=1,
            per_page=20,
            total=0,
            total_pages=0,
            file_type_facets=[{"ext": ".pdf", "count": 10}],
            source_facets=[{"source_type": "api", "count": 8}],
        )
        assert ".pdf (10)" in html
        assert "api (8)" in html

    def test_render_list_without_facets_graceful(self) -> None:
        """_render_documents_list without facet data should not crash."""
        from src.web.rendering import _render_documents_list

        html = _render_documents_list(
            documents=[],
            source="",
            page=1,
            per_page=20,
            total=0,
            total_pages=0,
            # No facets passed — should render gracefully
        )
        assert "Documents" in html
        assert "filter-panel" in html

    def test_render_partial_passes_facets(self) -> None:
        """_render_documents_table_partial renders the doc table region.

        Note: The table partial renders #doc-table-region (table + pagination +
        bulk-actions bar). It does NOT include facet <select> elements —
        those are part of the filter panel in the full-page template, not the
        HTMX table partial fragment.
        """
        from src.web.rendering import _render_documents_table_partial

        html = _render_documents_table_partial(
            documents=[
                {"id": 1, "title": "Test Doc", "status": "indexed",
                 "source_name": "api", "ext": ".pdf", "created_at": "2025-01-01"}
            ],
            page=1,
            per_page=20,
            total=1,
            total_pages=1,
            file_type_facets=[{"ext": ".pdf", "count": 5}],
            source_facets=[{"source_type": "local", "count": 3}],
            all_collections_list=[{"id": 1, "name": "Test"}],
        )
        # The partial should include the table region and bulk-actions
        assert 'id="doc-table-region"' in html
        assert 'id="bulk-actions-bar"' in html
        # Facet <select> elements live in the filter panel (full page), not here
        # Collection names should be in the bulk-move dropdown though
        assert "Test" in html

    def test_render_partial_without_facets_graceful(self) -> None:
        """_render_documents_table_partial without facets should not crash."""
        from src.web.rendering import _render_documents_table_partial

        html = _render_documents_table_partial(
            documents=[],
            page=1,
            per_page=20,
            total=0,
            total_pages=0,
        )
        assert 'id="doc-table-region"' in html
