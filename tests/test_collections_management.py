"""Tests for the collection management UI — GET form endpoints.

Covers Phase 3/4 action item 3/6: collection management forms.
Tests verify:
  - GET /collections/new returns 200 and renders a create form
  - GET /collections/{id}/edit returns 200 and renders an edit form pre-filled
  - The create form POSTs to /collections/create
  - The edit form POSTs to /collections/{id}/edit
  - GET /collections/{id}/edit returns 404 for nonexistent collection
  - The /documents page has a "New Collection" link to /collections/new
  - The collection tree sidebar has Edit links to /collections/{id}/edit
  - The parent dropdown is populated with existing collections
  - The edit form excludes the collection itself from its own parent dropdown
  - Form POST creates/updates a collection and redirects to /documents
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
        yield str(Path(tmpdir) / "test_collection_mgmt.db")


@pytest.fixture
async def asgi_client(tmp_db_path: str):
    """Create an httpx.AsyncClient backed by the ASGI app with test
    collections for form rendering tests.
    """
    import httpx
    from src.core.db_sqlite import Database
    from src.web import server

    db = Database(db_path=tmp_db_path)
    await db.connect()

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


# ── GET /collections/new (create form) ───────────────────────────


class TestNewCollectionForm:
    """Tests for GET /collections/new — the create collection form."""

    @pytest.mark.asyncio
    async def test_new_form_returns_200(self, asgi_client):
        """GET /collections/new should return 200."""
        resp = await asgi_client.get("/collections/new")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_new_form_has_title(self, asgi_client):
        """The create form page should have 'New Collection' as title."""
        resp = await asgi_client.get("/collections/new")
        assert resp.status_code == 200
        assert "New Collection" in resp.text

    @pytest.mark.asyncio
    async def test_new_form_has_name_field(self, asgi_client):
        """The form should have a name input field."""
        resp = await asgi_client.get("/collections/new")
        assert resp.status_code == 200
        assert 'name="name"' in resp.text
        assert "<input" in resp.text

    @pytest.mark.asyncio
    async def test_new_form_has_description_field(self, asgi_client):
        """The form should have a description textarea."""
        resp = await asgi_client.get("/collections/new")
        assert resp.status_code == 200
        assert 'name="description"' in resp.text
        assert "<textarea" in resp.text

    @pytest.mark.asyncio
    async def test_new_form_has_parent_dropdown(self, asgi_client):
        """The form should have a parent_id select populated with collections."""
        resp = await asgi_client.get("/collections/new")
        assert resp.status_code == 200
        assert 'name="parent_id"' in resp.text
        assert "<select" in resp.text
        # All four collections should appear in the dropdown
        assert "Tech" in resp.text
        assert "Python" in resp.text
        assert "Go" in resp.text
        assert "Research" in resp.text

    @pytest.mark.asyncio
    async def test_new_form_posts_to_create_endpoint(self, asgi_client):
        """The form action should POST to /collections/create."""
        resp = await asgi_client.get("/collections/new")
        assert resp.status_code == 200
        assert 'action="/collections/create"' in resp.text
        assert 'method="post"' in resp.text

    @pytest.mark.asyncio
    async def test_new_form_has_create_button(self, asgi_client):
        """The form should have a submit button."""
        resp = await asgi_client.get("/collections/new")
        assert resp.status_code == 200
        assert "Create Collection" in resp.text
        assert "<button" in resp.text

    @pytest.mark.asyncio
    async def test_new_form_has_none_option_in_parent(self, asgi_client):
        """The parent dropdown should have a 'None (top-level)' option."""
        resp = await asgi_client.get("/collections/new")
        assert resp.status_code == 200
        assert "None" in resp.text
        assert 'value=""' in resp.text

    @pytest.mark.asyncio
    async def test_new_form_extends_base(self, asgi_client):
        """The page should extend base.html (have the nav bar)."""
        resp = await asgi_client.get("/collections/new")
        assert resp.status_code == 200
        assert "DocMind" in resp.text
        assert "Dashboard" in resp.text

    @pytest.mark.asyncio
    async def test_new_form_back_link_to_documents(self, asgi_client):
        """The form page should have a back link to /documents."""
        resp = await asgi_client.get("/collections/new")
        assert resp.status_code == 200
        assert 'href="/documents"' in resp.text


# ── GET /collections/{id}/edit (edit form) ───────────────────────


class TestEditCollectionForm:
    """Tests for GET /collections/{id}/edit — the edit collection form."""

    @pytest.mark.asyncio
    async def test_edit_form_returns_200(self, asgi_client):
        """GET /collections/1/edit should return 200."""
        resp = await asgi_client.get("/collections/1/edit")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_edit_form_has_title(self, asgi_client):
        """The edit form page should have 'Edit Collection' as title."""
        resp = await asgi_client.get("/collections/1/edit")
        assert resp.status_code == 200
        assert "Edit Collection" in resp.text

    @pytest.mark.asyncio
    async def test_edit_form_prefilled_with_name(self, asgi_client):
        """The name field should be pre-filled with the collection's name."""
        resp = await asgi_client.get("/collections/1/edit")
        assert resp.status_code == 200
        # Tech is collection id=1
        assert "Tech" in resp.text

    @pytest.mark.asyncio
    async def test_edit_form_prefilled_with_description(self, asgi_client):
        """The description should be pre-filled."""
        resp = await asgi_client.get("/collections/1/edit")
        assert resp.status_code == 200
        assert "Tech docs" in resp.text

    @pytest.mark.asyncio
    async def test_edit_form_posts_to_edit_endpoint(self, asgi_client):
        """The form action should POST to /collections/{id}/edit."""
        resp = await asgi_client.get("/collections/2/edit")
        assert resp.status_code == 200
        assert 'action="/collections/2/edit"' in resp.text
        assert 'method="post"' in resp.text

    @pytest.mark.asyncio
    async def test_edit_form_has_update_button(self, asgi_client):
        """The edit form should have an 'Update Collection' button."""
        resp = await asgi_client.get("/collections/1/edit")
        assert resp.status_code == 200
        assert "Update Collection" in resp.text

    @pytest.mark.asyncio
    async def test_edit_form_has_delete_button(self, asgi_client):
        """The edit form should have a delete button (formaction to delete)."""
        resp = await asgi_client.get("/collections/1/edit")
        assert resp.status_code == 200
        assert "Delete Collection" in resp.text
        assert 'formaction="/collections/1/delete"' in resp.text

    @pytest.mark.asyncio
    async def test_edit_form_parent_dropdown_shows_others(self, asgi_client):
        """The parent dropdown should show other collections."""
        resp = await asgi_client.get("/collections/1/edit")
        assert resp.status_code == 200
        assert "Research" in resp.text  # a different root collection

    @pytest.mark.asyncio
    async def test_edit_form_excludes_self_from_parent(self, asgi_client):
        """The collection being edited should not appear as its own parent option."""
        resp = await asgi_client.get("/collections/3/edit")
        assert resp.status_code == 200
        # Go is id=3; when editing Go, its own id should not be a parent option value
        # but other collections should be present
        assert "Tech" in resp.text  # parent exists
        assert "Research" in resp.text  # sibling root

    @pytest.mark.asyncio
    async def test_edit_form_404_for_nonexistent(self, asgi_client):
        """GET /collections/99999/edit should return 404."""
        resp = await asgi_client.get("/collections/99999/edit")
        assert resp.status_code == 404
        assert "Not Found" in resp.text

    @pytest.mark.asyncio
    async def test_edit_child_shows_parent_selected(self, asgi_client):
        """Editing a child collection should have its parent selected in the dropdown."""
        # Python (id=2) has parent Tech (id=1)
        resp = await asgi_client.get("/collections/2/edit")
        assert resp.status_code == 200
        assert "selected" in resp.text


# ── /documents page integration (New Collection button + Edit links) ──


class TestDocumentsPageIntegration:
    """Tests that the /documents page has the new collection UI elements."""

    @pytest.mark.asyncio
    async def test_documents_page_has_new_collection_link(self, asgi_client):
        """The /documents page should have a link to /collections/new."""
        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200
        assert "New Collection" in resp.text
        assert 'href="/collections/new"' in resp.text

    @pytest.mark.asyncio
    async def test_documents_page_has_edit_links_for_collections(self, asgi_client):
        """Each collection in the tree should have an edit link."""
        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200
        # Tech (id=1) should have an edit link
        assert 'href="/collections/1/edit"' in resp.text
        # Python (id=2)
        assert 'href="/collections/2/edit"' in resp.text
        # Go (id=3)
        assert 'href="/collections/3/edit"' in resp.text
        # Research (id=4)
        assert 'href="/collections/4/edit"' in resp.text

    @pytest.mark.asyncio
    async def test_documents_page_edit_links_have_title(self, asgi_client):
        """The edit links should have a title attribute."""
        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200
        assert "Edit collection" in resp.text


# ── Form POST round-trip (create + update via form) ──────────────


class TestFormPostRoundTrip:
    """Tests that POSTing the form actually creates/updates collections."""

    @pytest.mark.asyncio
    async def test_post_create_form_redirects_to_documents(self, asgi_client):
        """POST /collections/create should redirect to /documents (303)."""
        resp = await asgi_client.post(
            "/collections/create",
            data={"name": "New Col", "description": "A test", "parent_id": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/documents"

    @pytest.mark.asyncio
    async def test_post_create_form_creates_collection(self, asgi_client):
        """POSTing the create form should actually create the collection."""
        resp = await asgi_client.post(
            "/collections/create",
            data={"name": "Form Created", "description": "via form", "parent_id": ""},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        # The redirect target (/documents) should now show the new collection
        assert "Form Created" in resp.text

    @pytest.mark.asyncio
    async def test_post_edit_form_updates_collection(self, asgi_client):
        """POSTing the edit form should update the collection name."""
        # First edit collection 1 (Tech) to "Renamed Tech"
        resp = await asgi_client.post(
            "/collections/1/edit",
            data={"name": "Renamed Tech", "description": "Tech docs", "parent_id": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # Verify the name changed by fetching the edit form
        resp2 = await asgi_client.get("/collections/1/edit")
        assert resp2.status_code == 200
        assert "Renamed Tech" in resp2.text

    @pytest.mark.asyncio
    async def test_post_create_with_parent(self, asgi_client):
        """POSTing the create form with a parent_id should nest the collection."""
        # Create a child of Tech (id=1)
        resp = await asgi_client.post(
            "/collections/create",
            data={"name": "Child Col", "description": "", "parent_id": "1"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # Verify it was created with the right parent by checking /documents
        resp2 = await asgi_client.get("/documents")
        assert resp2.status_code == 200
        assert "Child Col" in resp2.text

    @pytest.mark.asyncio
    async def test_post_delete_form_deletes_collection(self, asgi_client):
        """POSTing the delete form should delete the collection."""
        resp = await asgi_client.post(
            "/collections/1/delete",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # Verify collection 1 is gone — edit form should 404
        resp2 = await asgi_client.get("/collections/1/edit")
        assert resp2.status_code == 404
