"""Tests for collection breadcrumb navigation on the documents list page.

Covers Phase 3 action item 3/6: breadcrumb navigation showing the
collection path (root > parent > child) above the documents list using
get_collection_path(), with clickable links to navigate up the tree.

Tests verify:
  - Breadcrumbs appear when filtering by a nested collection
  - Breadcrumb chain shows root > parent > child path
  - Each breadcrumb link navigates to the corresponding collection_id
  - The current (deepest) collection is shown but not clickable
  - No breadcrumbs when no collection_id is selected
  - No breadcrumbs for collection_id=0 (unassigned)
  - Breadcrumbs do not appear when the collection does not exist
  - Breadcrumb links are above the documents list
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
        yield str(Path(tmpdir) / "test_breadcrumb.db")


@pytest.fixture
async def asgi_client(tmp_db_path: str):
    """Create an httpx.AsyncClient backed by the ASGI app with test
    documents and a nested collection hierarchy:

        - Tech (id=1, root)
          - Python (id=2, child of Tech)
            - Django (id=3, child of Python)
          - Go (id=4, child of Tech)
        - Research (id=5, root)
    """
    import httpx
    from src.core.db_sqlite import Database
    from src.web import server

    db = Database(db_path=tmp_db_path)
    await db.connect()

    # Insert test documents
    for i in range(10):
        await db.save_document(
            path=f"/docs/bctest_{i}.txt",
            source_type="api",
            source_name="test-source",
            title=f"Breadcrumb Test Document {i}",
            ext=".txt",
            mime_type="text/plain",
            body=f"Body of breadcrumb test document {i}.",
            size=100,
            status="indexed",
        )

    # Create nested collection hierarchy
    tech_id = await db.create_collection(name="Tech", description="Tech docs")
    python_id = await db.create_collection(
        name="Python", description="Python docs", parent_id=tech_id
    )
    django_id = await db.create_collection(
        name="Django", description="Django docs", parent_id=python_id
    )
    go_id = await db.create_collection(
        name="Go", description="Go docs", parent_id=tech_id
    )
    research_id = await db.create_collection(name="Research")

    # Assign documents
    await db.assign_document_to_collection(1, django_id)
    await db.assign_document_to_collection(2, python_id)
    await db.assign_document_to_collection(3, go_id)
    await db.assign_document_to_collection(4, research_id)

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


# ── Breadcrumb rendering tests ───────────────────────────────────


class TestCollectionBreadcrumbs:
    """Tests for the collection breadcrumb navigation on /documents."""

    @pytest.mark.asyncio
    async def test_breadcrumbs_present_when_collection_selected(self, asgi_client):
        """GET /documents?collection_id=3 (Django) should render breadcrumbs."""
        resp = await asgi_client.get("/documents?collection_id=3")
        assert resp.status_code == 200
        assert '<div class="collection-breadcrumb">' in resp.text

    @pytest.mark.asyncio
    async def test_breadcrumbs_absent_when_no_collection(self, asgi_client):
        """GET /documents (no collection_id) should NOT render breadcrumbs."""
        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200
        assert '<div class="collection-breadcrumb">' not in resp.text

    @pytest.mark.asyncio
    async def test_breadcrumbs_absent_for_unassigned(self, asgi_client):
        """GET /documents?collection_id=0 (unassigned) should NOT render breadcrumbs."""
        resp = await asgi_client.get("/documents?collection_id=0")
        assert resp.status_code == 200
        assert '<div class="collection-breadcrumb">' not in resp.text

    @pytest.mark.asyncio
    async def test_breadcrumb_chain_shows_full_path(self, asgi_client):
        """Breadcrumbs for Django (id=3) should show Tech > Python > Django."""
        resp = await asgi_client.get("/documents?collection_id=3")
        assert resp.status_code == 200
        text = resp.text
        # All three collection names should appear in the breadcrumb
        assert "Tech" in text
        assert "Python" in text
        assert "Django" in text
        # The breadcrumb should appear in order: Tech before Python before Django
        tech_pos = text.find("Tech")
        python_pos = text.find("Python")
        django_pos = text.find("Django")
        assert tech_pos < python_pos < django_pos

    @pytest.mark.asyncio
    async def test_breadcrumb_chain_for_root_collection(self, asgi_client):
        """Breadcrumbs for a root collection (id=1, Tech) should show just Tech."""
        resp = await asgi_client.get("/documents?collection_id=1")
        assert resp.status_code == 200
        assert '<div class="collection-breadcrumb">' in resp.text
        assert "Tech" in resp.text

    @pytest.mark.asyncio
    async def test_breadcrumb_links_navigate_up_tree(self, asgi_client):
        """Breadcrumb links should point to /documents?collection_id=N for ancestors."""
        resp = await asgi_client.get("/documents?collection_id=3")
        assert resp.status_code == 200
        text = resp.text
        # Tech (id=1) and Python (id=2) should be clickable links
        assert 'href="/documents?collection_id=1"' in text
        assert 'href="/documents?collection_id=2"' in text

    @pytest.mark.asyncio
    async def test_current_collection_not_clickable_in_breadcrumb(self, asgi_client):
        """The current collection (id=3, Django) should not be a link in the breadcrumb.

        The sidebar tree will still link to collection_id=3, but within the
        breadcrumb nav the current item should be a <span>, not an <a>.
        """
        resp = await asgi_client.get("/documents?collection_id=3")
        assert resp.status_code == 200
        text = resp.text
        # Find the actual breadcrumb div (not the CSS definition)
        bc_start = text.find('<div class="collection-breadcrumb">')
        assert bc_start != -1
        # Find the end of the breadcrumb div
        bc_end = text.find("</div>", bc_start)
        assert bc_end != -1
        breadcrumb_html = text[bc_start:bc_end]
        # The current collection should not have a self-link in the breadcrumb
        assert "collection_id=3" not in breadcrumb_html
        # Django should appear as a span (non-clickable) in the breadcrumb
        assert "Django" in breadcrumb_html

    @pytest.mark.asyncio
    async def test_breadcrumbs_above_documents_list(self, asgi_client):
        """Breadcrumbs should appear before the documents table."""
        resp = await asgi_client.get("/documents?collection_id=3")
        assert resp.status_code == 200
        text = resp.text
        breadcrumb_pos = text.find('<div class="collection-breadcrumb">')
        table_pos = text.find("<table>")
        assert breadcrumb_pos != -1
        assert table_pos != -1
        assert breadcrumb_pos < table_pos

    @pytest.mark.asyncio
    async def test_breadcrumb_root_link_present(self, asgi_client):
        """Breadcrumbs should include a link back to all documents (root)."""
        resp = await asgi_client.get("/documents?collection_id=3")
        assert resp.status_code == 200
        text = resp.text
        # There should be a link to /documents (clearing the collection filter)
        assert 'href="/documents"' in text

    @pytest.mark.asyncio
    async def test_breadcrumbs_absent_for_nonexistent_collection(self, asgi_client):
        """GET /documents?collection_id=99999 should not show breadcrumbs."""
        resp = await asgi_client.get("/documents?collection_id=99999")
        assert resp.status_code == 200
        assert '<div class="collection-breadcrumb">' not in resp.text

    @pytest.mark.asyncio
    async def test_breadcrumb_for_two_level_nesting(self, asgi_client):
        """Breadcrumbs for Python (id=2) should show Tech > Python."""
        resp = await asgi_client.get("/documents?collection_id=2")
        assert resp.status_code == 200
        text = resp.text
        assert "Tech" in text
        assert "Python" in text
        # Tech should be a clickable link (ancestor) in the breadcrumb
        assert 'href="/documents?collection_id=1"' in text
        # Within the breadcrumb section, Python (id=2) is the current
        # collection — should not link to itself
        bc_start = text.find('<div class="collection-breadcrumb">')
        bc_end = text.find("</div>", bc_start)
        breadcrumb_html = text[bc_start:bc_end]
        assert "collection_id=2" not in breadcrumb_html

    @pytest.mark.asyncio
    async def test_breadcrumb_has_separator_between_items(self, asgi_client):
        """Breadcrumb items should be separated by / (in separator spans)."""
        resp = await asgi_client.get("/documents?collection_id=3")
        assert resp.status_code == 200
        text = resp.text
        assert "collection-breadcrumb-sep" in text
        assert "/" in text

    @pytest.mark.asyncio
    async def test_breadcrumb_all_link_is_first_element(self, asgi_client):
        """The 'All' link should be the first element in the breadcrumb."""
        resp = await asgi_client.get("/documents?collection_id=3")
        assert resp.status_code == 200
        text = resp.text
        bc_start = text.find('<div class="collection-breadcrumb">')
        bc_end = text.find("</div>", bc_start)
        breadcrumb_html = text[bc_start:bc_end]
        # "All" link should appear before any collection name
        all_pos = breadcrumb_html.find("All")
        assert all_pos != -1
        # "All" should be a link to /documents
        assert 'href="/documents"' in breadcrumb_html
        # "All" should come before the first separator
        sep_pos = breadcrumb_html.find("collection-breadcrumb-sep")
        assert all_pos < sep_pos

    @pytest.mark.asyncio
    async def test_breadcrumb_current_has_span_class(self, asgi_client):
        """The current collection should have the 'collection-breadcrumb-current' class."""
        resp = await asgi_client.get("/documents?collection_id=3")
        assert resp.status_code == 200
        text = resp.text
        assert "collection-breadcrumb-current" in text
        # Django should be the current collection
        assert "Django" in text
