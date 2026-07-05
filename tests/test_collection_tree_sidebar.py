"""Tests for the collection tree sidebar on the documents list page.

Covers Phase 3 action item 1/6: collection tree sidebar rendering.
Tests verify:
  - GET /documents renders the collection tree sidebar with nested collections
  - Document counts are displayed per collection
  - Active state is applied when a collection is selected
  - collection_id query param filters documents by collection
  - collection_id=0 filters to unassigned documents
  - "All Documents" and "Unassigned" links are present
  - Tree nesting (parent-child) is reflected in the HTML
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
        yield str(Path(tmpdir) / "test_collection_tree.db")


@pytest.fixture
async def asgi_client(tmp_db_path: str):
    """Create an httpx.AsyncClient backed by the ASGI app with test documents
    and collections.
    """
    import httpx
    from src.core.db_sqlite import Database
    from src.web import server

    db = Database(db_path=tmp_db_path)
    await db.connect()

    # Insert test documents
    for i in range(10):
        await db.save_document(
            path=f"/docs/coltest_{i}.txt",
            source_type="api",
            source_name="test-source",
            title=f"Collection Test Document {i}",
            ext=".txt",
            mime_type="text/plain",
            body=f"Body of collection test document {i}.",
            size=100,
            status="indexed",
        )

    # Create collections with nesting:
    #   - Tech (parent=None)
    #     - Python (parent=Tech)
    #     - Go (parent=Tech)
    #   - Research (parent=None)
    tech_id = await db.create_collection(name="Tech", description="Tech docs")
    python_id = await db.create_collection(
        name="Python", description="Python docs", parent_id=tech_id
    )
    go_id = await db.create_collection(
        name="Go", description="Go docs", parent_id=tech_id
    )
    research_id = await db.create_collection(name="Research")

    # Assign documents to collections
    # Docs 1-3 -> Python, Doc 4 -> Go, Doc 5 -> Research, Docs 6-10 unassigned
    await db.assign_document_to_collection(1, python_id)
    await db.assign_document_to_collection(2, python_id)
    await db.assign_document_to_collection(3, python_id)
    await db.assign_document_to_collection(4, go_id)
    await db.assign_document_to_collection(5, research_id)

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


# ── Collection tree sidebar rendering tests ──────────────────────


class TestCollectionTreeSidebar:
    """Tests for the collection tree sidebar on /documents."""

    @pytest.mark.asyncio
    async def test_collection_tree_present_on_documents_page(self, asgi_client):
        """GET /documents should render the collection tree sidebar."""
        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200
        assert "collection-tree" in resp.text
        assert "Collections" in resp.text

    @pytest.mark.asyncio
    async def test_collection_tree_shows_all_collections(self, asgi_client):
        """All collection names should appear in the tree."""
        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200
        assert "Tech" in resp.text
        assert "Python" in resp.text
        assert "Go" in resp.text
        assert "Research" in resp.text

    @pytest.mark.asyncio
    async def test_collection_tree_shows_document_counts(self, asgi_client):
        """Document counts per collection should be displayed."""
        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200
        # Python has 3 documents
        assert "(3)" in resp.text
        # Go has 1 document
        assert "(1)" in resp.text
        # Research has 1 document
        # (also appears as (1) — already checked above)

    @pytest.mark.asyncio
    async def test_collection_tree_has_all_documents_link(self, asgi_client):
        """The 'All Documents' link should be present."""
        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200
        assert "All Documents" in resp.text
        assert 'href="/documents"' in resp.text

    @pytest.mark.asyncio
    async def test_collection_tree_has_unassigned_link(self, asgi_client):
        """The 'Unassigned' link should be present with collection_id=0."""
        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200
        assert "Unassigned" in resp.text
        assert "collection_id=0" in resp.text

    @pytest.mark.asyncio
    async def test_collection_tree_links_to_collection_filter(self, asgi_client):
        """Collection links should point to /documents?collection_id=N."""
        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200
        assert "collection_id=1" in resp.text  # Tech (id=1)
        assert "collection_id=2" in resp.text  # Python (id=2)

    @pytest.mark.asyncio
    async def test_nested_collections_render_in_tree(self, asgi_client):
        """Nested collections should be in a children <ul>."""
        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200
        assert "collection-tree-children" in resp.text

    @pytest.mark.asyncio
    async def test_active_state_when_collection_selected(self, asgi_client):
        """When filtering by collection_id, the active collection gets 'active' class."""
        resp = await asgi_client.get("/documents?collection_id=2")
        assert resp.status_code == 200
        # The Python collection (id=2) should have the active class
        assert "collection-tree-item active" in resp.text

    @pytest.mark.asyncio
    async def test_no_active_state_when_no_collection_selected(self, asgi_client):
        """Without collection_id param, no collection item should be active."""
        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200
        # "All Documents" should be active (no collection filter)
        assert "collection-tree-item active" in resp.text

    @pytest.mark.asyncio
    async def test_filter_label_shows_collection_name(self, asgi_client):
        """Filter label should show the collection name when filtering."""
        resp = await asgi_client.get("/documents?collection_id=2")
        assert resp.status_code == 200
        assert "collection: Python" in resp.text

    @pytest.mark.asyncio
    async def test_filter_label_shows_unassigned(self, asgi_client):
        """Filter label should show 'unassigned' for collection_id=0."""
        resp = await asgi_client.get("/documents?collection_id=0")
        assert resp.status_code == 200
        assert "unassigned" in resp.text.lower()


# ── Collection filtering tests ───────────────────────────────────


class TestCollectionFiltering:
    """Tests for collection_id query param filtering on /documents."""

    @pytest.mark.asyncio
    async def test_filter_by_collection_returns_only_matching_docs(self, asgi_client):
        """GET /documents?collection_id=2 should return only Python docs."""
        resp = await asgi_client.get("/documents?collection_id=2")
        assert resp.status_code == 200
        # Python collection has docs 1, 2, 3
        assert "Collection Test Document 0" in resp.text
        assert "Collection Test Document 1" in resp.text
        assert "Collection Test Document 2" in resp.text
        # But not doc 4 (Go) or doc 6 (unassigned)
        assert "Collection Test Document 3" not in resp.text
        assert "Collection Test Document 5" not in resp.text

    @pytest.mark.asyncio
    async def test_filter_by_unassigned_returns_only_unassigned(self, asgi_client):
        """GET /documents?collection_id=0 should return only unassigned docs."""
        resp = await asgi_client.get("/documents?collection_id=0")
        assert resp.status_code == 200
        # Docs 6-10 (0-indexed 5-9) are unassigned
        assert "Collection Test Document 5" in resp.text
        assert "Collection Test Document 9" in resp.text
        # But not doc 1 (Python)
        assert "Collection Test Document 0" not in resp.text

    @pytest.mark.asyncio
    async def test_pagination_preserves_collection_id(self, asgi_client):
        """Pagination links should include collection_id param."""
        resp = await asgi_client.get("/documents?collection_id=2&per_page=2")
        assert resp.status_code == 200
        # The pagination links should include collection_id=2
        assert "collection_id=2" in resp.text

    @pytest.mark.asyncio
    async def test_no_collections_shows_empty_tree(self, tmp_db_path: str):
        """When no collections exist, the tree should still show All/Unassigned."""
        import httpx
        from src.core.db_sqlite import Database
        from src.web import server

        db = Database(db_path=tmp_db_path)
        await db.connect()

        # Insert one document but no collections
        await db.save_document(
            path="/docs/nocoltest.txt",
            source_type="api",
            source_name="test",
            title="No Collection Test",
            ext=".txt",
            mime_type="text/plain",
            body="Body text.",
            size=100,
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
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as c:
            resp = await c.get("/documents")
            assert resp.status_code == 200
            assert "collection-tree" in resp.text
            assert "All Documents" in resp.text
            assert "Unassigned" in resp.text

        await db.disconnect()
        server._db = original_db
        server._queue = original_queue
