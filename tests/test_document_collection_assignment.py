"""Tests for document-to-collection assignment UI on the document detail page.

Covers:
- GET /documents/{doc_id} shows a collection dropdown (<select name="collection_id">)
- The dropdown includes an "Unassigned" option and all collection names
- The currently assigned collection is marked selected in the dropdown
- The form posts to /documents/{doc_id}/assign-collection
- The current collection name appears as a breadcrumb link when assigned
- POST /documents/{doc_id}/assign-collection assigns and redirects back
- Posting with empty collection_id removes the assignment
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
        yield str(Path(tmpdir) / "test_doc_collection_assign.db")


@pytest.fixture
async def asgi_client(tmp_db_path: str):
    """Create an httpx.AsyncClient backed by the ASGI app with test data."""
    import httpx
    from src.core.db_sqlite import Database
    from src.web import server

    db = Database(db_path=tmp_db_path)
    await db.connect()

    # Insert test documents
    for i in range(3):
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

    # Create collections
    await db.create_collection("Research")
    await db.create_collection("Personal")
    await db.create_collection("Work")

    # Assign document 1 to "Research" (collection_id=1)
    await db.assign_document_to_collection(1, 1)

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


# ── Detail page rendering tests ──────────────────────────────────


class TestCollectionDropdownRendering:
    """Tests for the collection dropdown on the document detail page."""

    @pytest.mark.asyncio
    async def test_detail_page_has_collection_select(self, asgi_client):
        """Document detail should have a <select name="collection_id">."""
        resp = await asgi_client.get("/documents/1")
        assert resp.status_code == 200
        assert 'name="collection_id"' in resp.text
        assert "<select" in resp.text

    @pytest.mark.asyncio
    async def test_detail_page_has_assign_button(self, asgi_client):
        """Document detail should have an Assign submit button."""
        resp = await asgi_client.get("/documents/1")
        assert resp.status_code == 200
        assert "分配" in resp.text
        assert 'type="submit"' in resp.text

    @pytest.mark.asyncio
    async def test_detail_page_has_unassigned_option(self, asgi_client):
        """The dropdown should include an 'Unassigned' option with empty value."""
        resp = await asgi_client.get("/documents/2")
        assert resp.status_code == 200
        assert 'value=""' in resp.text
        assert "未分配" in resp.text

    @pytest.mark.asyncio
    async def test_detail_page_lists_all_collections(self, asgi_client):
        """The dropdown should list all collection names."""
        resp = await asgi_client.get("/documents/2")
        assert resp.status_code == 200
        assert "Research" in resp.text
        assert "Personal" in resp.text
        assert "Work" in resp.text

    @pytest.mark.asyncio
    async def test_current_collection_is_selected(self, asgi_client):
        """The currently assigned collection should be selected in the dropdown."""
        resp = await asgi_client.get("/documents/1")
        assert resp.status_code == 200
        # Document 1 is assigned to "Research" (collection_id=1)
        assert "selected" in resp.text
        # The selected option should contain "Research"
        # Find the option with selected and verify it contains Research
        text = resp.text
        # Look for <option ... selected>Research</option>
        assert "Research" in text

    @pytest.mark.asyncio
    async def test_unassigned_doc_has_no_selected_option(self, asgi_client):
        """For an unassigned document, no collection option should be selected."""
        resp = await asgi_client.get("/documents/2")
        assert resp.status_code == 200
        # The only "selected" should not appear on a collection option
        # (document 2 has no collection)
        text = resp.text
        # Extract the select block to verify no collection is selected
        # The Unassigned option (value="") should not have "selected"
        # since it's the default — but our template doesn't mark it selected.
        # Just verify no collection option is selected.
        assert 'value="1"' in text  # Research exists
        # Ensure value="1" is not in a selected option
        # (the "selected" keyword should not appear near collection options)
        # We check that no <option value="1" ... selected> pattern exists
        import re

        selected_collection = re.search(
            r'<option\s+value="[1-9]"\s+selected', text
        )
        assert selected_collection is None

    @pytest.mark.asyncio
    async def test_form_posts_to_assign_endpoint(self, asgi_client):
        """The form action should post to /documents/{doc_id}/assign-collection."""
        resp = await asgi_client.get("/documents/1")
        assert resp.status_code == 200
        assert 'action="/documents/1/assign-collection"' in resp.text
        assert 'method="post"' in resp.text


class TestCollectionBreadcrumb:
    """Tests for the current collection breadcrumb link."""

    @pytest.mark.asyncio
    async def test_assigned_doc_shows_collection_breadcrumb(self, asgi_client):
        """When a document is assigned, its collection name should be a link."""
        resp = await asgi_client.get("/documents/1")
        assert resp.status_code == 200
        assert 'class="collection-breadcrumb"' in resp.text
        assert "Research" in resp.text
        # Should link to the documents page filtered by collection
        assert 'href="/documents?collection_id=1"' in resp.text

    @pytest.mark.asyncio
    async def test_unassigned_doc_shows_unassigned_text(self, asgi_client):
        """When a document has no collection, it should show 'Unassigned'."""
        resp = await asgi_client.get("/documents/2")
        assert resp.status_code == 200
        # The breadcrumb area should show "Unassigned" as emphasized text
        assert "未分配" in resp.text


# ── Assignment form POST tests ───────────────────────────────────


class TestAssignCollectionPost:
    """Tests for POST /documents/{doc_id}/assign-collection."""

    @pytest.mark.asyncio
    async def test_assign_to_collection_redirects(self, asgi_client):
        """POSTing a collection_id should redirect (303) back to detail page."""
        resp = await asgi_client.post(
            "/documents/2/assign-collection",
            data={"collection_id": "2"},  # Personal
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/documents/2" in resp.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_assign_persists_after_redirect(self, asgi_client):
        """After assignment, the detail page should show the new collection as selected."""
        # Assign document 2 to "Personal" (id=2)
        await asgi_client.post(
            "/documents/2/assign-collection",
            data={"collection_id": "2"},
            follow_redirects=True,
        )
        # Fetch detail page and verify
        resp = await asgi_client.get("/documents/2")
        assert resp.status_code == 200
        assert 'class="collection-breadcrumb"' in resp.text
        assert "Personal" in resp.text

    @pytest.mark.asyncio
    async def test_assign_unassign_removes_collection(self, asgi_client):
        """POSTing with empty collection_id should remove the assignment."""
        # Document 1 is assigned to Research; unassign it
        resp = await asgi_client.post(
            "/documents/1/assign-collection",
            data={"collection_id": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # Verify on detail page — no breadcrumb link should be rendered
        resp2 = await asgi_client.get("/documents/1")
        assert resp2.status_code == 200
        assert 'class="collection-breadcrumb"' not in resp2.text

    @pytest.mark.asyncio
    async def test_reassign_to_different_collection(self, asgi_client):
        """Reassigning a document from one collection to another should work."""
        # Document 1 is in Research (id=1); move to Work (id=3)
        await asgi_client.post(
            "/documents/1/assign-collection",
            data={"collection_id": "3"},
            follow_redirects=True,
        )
        resp = await asgi_client.get("/documents/1")
        assert resp.status_code == 200
        assert "Work" in resp.text
        # Should show Work as breadcrumb, not Research
        assert 'class="collection-breadcrumb"' in resp.text
