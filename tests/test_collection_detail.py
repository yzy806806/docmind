"""Tests for the collection detail page — GET /collections/{id}.

Covers Phase 3/4 action item 5/6: collection detail HTML route.
Tests verify:
  - GET /collections/{id} returns 200 for an existing collection
  - The page shows the collection name and description
  - The page shows the breadcrumb navigation
  - The page shows child collections with names and document counts
  - The page shows documents belonging to the collection
  - The page shows edit and delete buttons linking to the form endpoints
  - The page shows the collection-tree sidebar
  - GET /collections/{id} returns 404 for a nonexistent collection
  - The page shows the parent collection link when the collection has a parent
  - The page indicates "root" for a top-level collection
  - Pagination works (page query param)
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
        yield str(Path(tmpdir) / "test_collection_detail.db")


@pytest.fixture
async def asgi_client(tmp_db_path: str):
    """Create an httpx.AsyncClient backed by the ASGI app with test data.

    Sets up:
      Collections:
        - Tech (id=1, root)
          - Python (id=2, parent=Tech)
          - Go (id=3, parent=Tech)
        - Research (id=4, root)

      Documents:
        - "Python Guide"  → assigned to Python (id=2)
        - "Go Tutorial"   → assigned to Go (id=3)
        - "Tech Overview" → assigned to Tech (id=1)
    """
    import httpx
    from src.core.db_sqlite import Database
    from src.web import server

    db = Database(db_path=tmp_db_path)
    await db.connect()

    # Create collections with nesting
    tech_id = await db.create_collection(name="Tech", description="Technology docs")
    python_id = await db.create_collection(
        name="Python", description="Python programming", parent_id=tech_id
    )
    go_id = await db.create_collection(
        name="Go", description="Go programming", parent_id=tech_id
    )
    research_id = await db.create_collection(name="Research")

    # Create documents and assign them to collections
    doc1_id = await db.save_document(
        path="/docs/python-guide.md",
        source_type="file",
        source_name="local",
        title="Python Guide",
        ext=".md",
        mime_type="text/markdown",
        body="A guide to Python programming.",
        status="indexed",
    )
    await db.assign_document_to_collection(doc1_id, python_id)

    doc2_id = await db.save_document(
        path="/docs/go-tutorial.md",
        source_type="file",
        source_name="local",
        title="Go Tutorial",
        ext=".md",
        mime_type="text/markdown",
        body="A tutorial on Go.",
        status="indexed",
    )
    await db.assign_document_to_collection(doc2_id, go_id)

    doc3_id = await db.save_document(
        path="/docs/tech-overview.md",
        source_type="file",
        source_name="local",
        title="Tech Overview",
        ext=".md",
        mime_type="text/markdown",
        body="Overview of technology stack.",
        status="indexed",
    )
    await db.assign_document_to_collection(doc3_id, tech_id)

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


# ── GET /collections/{id} — basic response ───────────────────────


class TestCollectionDetailBasic:
    """Tests for basic GET /collections/{id} response."""

    @pytest.mark.asyncio
    async def test_detail_returns_200(self, asgi_client):
        """GET /collections/1 should return 200."""
        resp = await asgi_client.get("/collections/1")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_detail_404_for_nonexistent(self, asgi_client):
        """GET /collections/99999 should return 404."""
        resp = await asgi_client.get("/collections/99999")
        assert resp.status_code == 404
        assert "Not Found" in resp.text

    @pytest.mark.asyncio
    async def test_detail_extends_base(self, asgi_client):
        """The page should extend base.html (have the nav bar)."""
        resp = await asgi_client.get("/collections/1")
        assert resp.status_code == 200
        assert "DocMind" in resp.text
        assert "Dashboard" in resp.text


# ── GET /collections/{id} — content verification ─────────────────


class TestCollectionDetailContent:
    """Tests that the detail page shows the right content."""

    @pytest.mark.asyncio
    async def test_shows_collection_name(self, asgi_client):
        """The page should show the collection's name."""
        resp = await asgi_client.get("/collections/1")
        assert resp.status_code == 200
        assert "Tech" in resp.text

    @pytest.mark.asyncio
    async def test_shows_collection_description(self, asgi_client):
        """The page should show the collection's description."""
        resp = await asgi_client.get("/collections/1")
        assert resp.status_code == 200
        assert "Technology docs" in resp.text

    @pytest.mark.asyncio
    async def test_shows_breadcrumb(self, asgi_client):
        """The page should show breadcrumb navigation."""
        resp = await asgi_client.get("/collections/2")
        assert resp.status_code == 200
        # Breadcrumb should contain the parent name "Tech"
        assert "collection-breadcrumb" in resp.text
        assert "Tech" in resp.text

    @pytest.mark.asyncio
    async def test_shows_edit_button(self, asgi_client):
        """The page should have an edit button linking to the edit form."""
        resp = await asgi_client.get("/collections/1")
        assert resp.status_code == 200
        assert 'href="/collections/1/edit"' in resp.text
        assert "Edit" in resp.text

    @pytest.mark.asyncio
    async def test_shows_delete_button(self, asgi_client):
        """The page should have a delete form posting to the delete endpoint."""
        resp = await asgi_client.get("/collections/1")
        assert resp.status_code == 200
        assert 'action="/collections/1/delete"' in resp.text
        assert 'method="post"' in resp.text
        assert "Delete" in resp.text

    @pytest.mark.asyncio
    async def test_shows_new_collection_link(self, asgi_client):
        """The page should have a link to create a new collection."""
        resp = await asgi_client.get("/collections/1")
        assert resp.status_code == 200
        assert 'href="/collections/new"' in resp.text

    @pytest.mark.asyncio
    async def test_shows_back_to_documents_link(self, asgi_client):
        """The page should have a back link to /documents."""
        resp = await asgi_client.get("/collections/1")
        assert resp.status_code == 200
        assert 'href="/documents"' in resp.text


# ── GET /collections/{id} — child collections ────────────────────


class TestCollectionDetailChildren:
    """Tests that the detail page shows child collections."""

    @pytest.mark.asyncio
    async def test_shows_child_collections_section(self, asgi_client):
        """The page should have a Sub-Collections section."""
        resp = await asgi_client.get("/collections/1")
        assert resp.status_code == 200
        assert "Sub-Collections" in resp.text

    @pytest.mark.asyncio
    async def test_shows_child_collection_names(self, asgi_client):
        """Tech (id=1) should show its children Python and Go."""
        resp = await asgi_client.get("/collections/1")
        assert resp.status_code == 200
        assert "Python" in resp.text
        assert "Go" in resp.text

    @pytest.mark.asyncio
    async def test_shows_child_collection_links(self, asgi_client):
        """Child collections should link to their detail pages."""
        resp = await asgi_client.get("/collections/1")
        assert resp.status_code == 200
        # Python is id=2, Go is id=3
        assert 'href="/collections/2"' in resp.text
        assert 'href="/collections/3"' in resp.text

    @pytest.mark.asyncio
    async def test_no_children_shows_message(self, asgi_client):
        """A collection with no children should show 'No sub-collections.'"""
        # Python (id=2) has no children
        resp = await asgi_client.get("/collections/2")
        assert resp.status_code == 200
        assert "No sub-collections" in resp.text


# ── GET /collections/{id} — documents ────────────────────────────


class TestCollectionDetailDocuments:
    """Tests that the detail page shows documents in the collection."""

    @pytest.mark.asyncio
    async def test_shows_documents_section(self, asgi_client):
        """The page should have a Documents section."""
        resp = await asgi_client.get("/collections/1")
        assert resp.status_code == 200
        assert "Documents" in resp.text

    @pytest.mark.asyncio
    async def test_shows_document_title(self, asgi_client):
        """Tech (id=1) should show the 'Tech Overview' document."""
        resp = await asgi_client.get("/collections/1")
        assert resp.status_code == 200
        assert "Tech Overview" in resp.text

    @pytest.mark.asyncio
    async def test_shows_document_count(self, asgi_client):
        """The page should show the document count."""
        resp = await asgi_client.get("/collections/1")
        assert resp.status_code == 200
        # Tech has 1 document directly assigned
        assert "Documents (1)" in resp.text

    @pytest.mark.asyncio
    async def test_shows_document_in_child_collection(self, asgi_client):
        """Python (id=2) should show the 'Python Guide' document."""
        resp = await asgi_client.get("/collections/2")
        assert resp.status_code == 200
        assert "Python Guide" in resp.text

    @pytest.mark.asyncio
    async def test_document_links_to_detail(self, asgi_client):
        """Documents should link to /documents/{doc_id}."""
        resp = await asgi_client.get("/collections/1")
        assert resp.status_code == 200
        # doc3 (Tech Overview) is assigned to Tech (id=1)
        # The document link should be /documents/{doc_id}
        assert "/documents/" in resp.text


# ── GET /collections/{id} — parent link ──────────────────────────


class TestCollectionDetailParent:
    """Tests that the detail page shows the parent collection link."""

    @pytest.mark.asyncio
    async def test_shows_parent_link_for_child(self, asgi_client):
        """Python (id=2, parent=Tech) should show a link to its parent."""
        resp = await asgi_client.get("/collections/2")
        assert resp.status_code == 200
        # Parent link should point to Tech (id=1)
        assert 'href="/collections/1"' in resp.text
        assert "Parent" in resp.text or "Tech" in resp.text

    @pytest.mark.asyncio
    async def test_shows_root_for_top_level(self, asgi_client):
        """Tech (id=1, root) should indicate it's a root collection."""
        resp = await asgi_client.get("/collections/1")
        assert resp.status_code == 200
        assert "root" in resp.text.lower()


# ── GET /collections/{id} — collection tree sidebar ──────────────


class TestCollectionDetailSidebar:
    """Tests that the detail page shows the collection-tree sidebar."""

    @pytest.mark.asyncio
    async def test_shows_collection_tree(self, asgi_client):
        """The page should include the collection-tree sidebar."""
        resp = await asgi_client.get("/collections/1")
        assert resp.status_code == 200
        assert "collection-tree" in resp.text

    @pytest.mark.asyncio
    async def test_tree_shows_all_collections(self, asgi_client):
        """The sidebar should list all collections."""
        resp = await asgi_client.get("/collections/1")
        assert resp.status_code == 200
        assert "Tech" in resp.text
        assert "Python" in resp.text
        assert "Go" in resp.text
        assert "Research" in resp.text


# ── GET /collections/{id} — pagination ───────────────────────────


class TestCollectionDetailPagination:
    """Tests that pagination works on the collection detail page."""

    @pytest.mark.asyncio
    async def test_pagination_page_param(self, asgi_client):
        """GET /collections/1?page=1 should return 200."""
        resp = await asgi_client.get("/collections/1?page=1")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_pagination_per_page_param(self, asgi_client):
        """GET /collections/1?per_page=5 should return 200."""
        resp = await asgi_client.get("/collections/1?per_page=5")
        assert resp.status_code == 200
