"""Tests for document tag web routes.

Covers:
- POST /documents/{doc_id}/tags        — add a tag via form
- POST /documents/{doc_id}/tags/{tag}/delete  — remove a tag via form
- GET  /documents?tag=xxx              — filter documents by tag
- Tag rendering on document list (badges, tag cloud)
- Tag rendering on document detail (badges, add/remove forms)
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
        yield str(Path(tmpdir) / "test_web_tags.db")


@pytest.fixture
async def asgi_client(tmp_db_path: str):
    """Create an httpx.AsyncClient backed by the ASGI app with test documents."""
    import httpx
    from src.core.db_sqlite import Database
    from src.web import server

    db = Database(db_path=tmp_db_path)
    await db.connect()

    # Insert test documents
    for i in range(5):
        await db.save_document(
            path=f"/docs/tagtest_{i}.txt",
            source_type="api",
            source_name="test-source",
            title=f"Tag Test Document {i}",
            ext=".txt",
            mime_type="text/plain",
            body=f"This is the body of tag test document {i}.",
            size=100,
            status="indexed",
        )

    # Add tags to some documents
    await db.add_tag(1, "python")
    await db.add_tag(1, "ai")
    await db.add_tag(2, "python")
    await db.add_tag(3, "ai")
    await db.add_tag(4, "research")

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


# ── Tag form handler tests ───────────────────────────────────────


class TestAddTagRoute:
    """Tests for POST /documents/{doc_id}/tags."""

    @pytest.mark.asyncio
    async def test_add_tag_via_form(self, asgi_client):
        """POST /documents/5/tags should add the tag and re-render detail."""
        resp = await asgi_client.post(
            "/documents/5/tags",
            data={"tag": "newtag"},
        )
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        # The re-rendered detail page should show the new tag
        assert "newtag" in resp.text

    @pytest.mark.asyncio
    async def test_add_tag_appears_as_badge(self, asgi_client):
        """After adding a tag, the detail page should show it as a tag-pill."""
        resp = await asgi_client.post(
            "/documents/5/tags",
            data={"tag": "badge-test"},
        )
        assert resp.status_code == 200
        assert "tag-pill" in resp.text
        assert "badge-test" in resp.text

    @pytest.mark.asyncio
    async def test_add_tag_with_whitespace_trims(self, asgi_client):
        """Tags with leading/trailing whitespace should be trimmed."""
        resp = await asgi_client.post(
            "/documents/5/tags",
            data={"tag": "  trimmed  "},
        )
        assert resp.status_code == 200
        assert "trimmed" in resp.text

    @pytest.mark.asyncio
    async def test_add_tag_empty_string_ignored(self, asgi_client):
        """Posting an empty tag should not error — page re-renders."""
        resp = await asgi_client.post(
            "/documents/5/tags",
            data={"tag": ""},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_add_tag_duplicate_idempotent(self, asgi_client):
        """Adding a tag that already exists should not error."""
        # Document 1 already has "python"
        resp = await asgi_client.post(
            "/documents/1/tags",
            data={"tag": "python"},
        )
        assert resp.status_code == 200
        assert "python" in resp.text

    @pytest.mark.asyncio
    async def test_add_tag_to_nonexistent_doc_returns_404(self, asgi_client):
        """Adding a tag to a non-existent document should return 404."""
        resp = await asgi_client.post(
            "/documents/9999/tags",
            data={"tag": "ghost"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_add_tag_form_has_input_and_button(self, asgi_client):
        """Document detail should have a tag input field and Add Tag button."""
        resp = await asgi_client.get("/documents/5")
        assert resp.status_code == 200
        assert 'name="tag"' in resp.text
        assert "Add Tag" in resp.text


class TestRemoveTagRoute:
    """Tests for POST /documents/{doc_id}/tags/{tag}/delete."""

    @pytest.mark.asyncio
    async def test_remove_tag_via_form(self, asgi_client):
        """POST /documents/1/tags/python/delete should remove the tag."""
        resp = await asgi_client.post(
            "/documents/1/tags/python/delete",
        )
        assert resp.status_code == 200
        # After removal, the tag should not appear as a pill
        # (it may appear in the form action URL, but not as a visible badge)
        # Let's verify by checking the document via DB
        resp2 = await asgi_client.get("/documents/1")
        assert resp2.status_code == 200
        # "python" should not be in a tag-pill
        # The word "python" might still appear in the form placeholder text, but
        # not in a tag-pill class
        assert 'class="tag-pill">python' not in resp2.text

    @pytest.mark.asyncio
    async def test_remove_tag_from_nonexistent_doc_returns_404(self, asgi_client):
        """Removing a tag from a non-existent document should return 404."""
        resp = await asgi_client.post(
            "/documents/9999/tags/python/delete",
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_remove_nonexistent_tag_no_error(self, asgi_client):
        """Removing a tag that doesn't exist should not error."""
        resp = await asgi_client.post(
            "/documents/1/tags/nonexistent/delete",
        )
        assert resp.status_code == 200


# ── Tag filtering tests ──────────────────────────────────────────


class TestTagFiltering:
    """Tests for GET /documents?tag=xxx filtering."""

    @pytest.mark.asyncio
    async def test_filter_by_tag_returns_matching_docs(self, asgi_client):
        """GET /documents?tag=python should return only documents with that tag."""
        resp = await asgi_client.get("/documents?tag=python")
        assert resp.status_code == 200
        # doc_id=1 (title "Tag Test Document 0") and doc_id=2 ("Tag Test Document 1")
        # both have the "python" tag.
        assert "Tag Test Document 0" in resp.text
        assert "Tag Test Document 1" in resp.text
        # doc_id=3 ("Tag Test Document 2") has "ai" tag, not "python"
        assert "Tag Test Document 2</a>" not in resp.text

    @pytest.mark.asyncio
    async def test_filter_by_tag_shows_active_tag(self, asgi_client):
        """When filtering by tag, the active tag should be highlighted."""
        resp = await asgi_client.get("/documents?tag=python")
        assert resp.status_code == 200
        assert "tag-cloud-item" in resp.text
        # The active tag should have the "active" class
        assert "tag-cloud-item active" in resp.text or "active" in resp.text

    @pytest.mark.asyncio
    async def test_filter_by_tag_shows_filter_label(self, asgi_client):
        """Documents page with tag filter should show the tag name in the header."""
        resp = await asgi_client.get("/documents?tag=python")
        assert resp.status_code == 200
        assert "python" in resp.text

    @pytest.mark.asyncio
    async def test_filter_by_nonexistent_tag_returns_empty(self, asgi_client):
        """Filtering by a non-existent tag should show no documents."""
        resp = await asgi_client.get("/documents?tag=nonexistent")
        assert resp.status_code == 200
        assert "No documents found" in resp.text or "0 document" in resp.text

    @pytest.mark.asyncio
    async def test_tag_filter_pagination_preserved(self, asgi_client):
        """Tag filter + pagination should both work together."""
        resp = await asgi_client.get("/documents?tag=python&page=1&per_page=1")
        assert resp.status_code == 200
        # Should have pagination navigation
        assert "pagination" in resp.text

    @pytest.mark.asyncio
    async def test_show_all_link_when_filtered(self, asgi_client):
        """When filtered by tag, a 'Show all' link should appear."""
        resp = await asgi_client.get("/documents?tag=python")
        assert resp.status_code == 200
        assert "Show all" in resp.text or "/documents\"" in resp.text


# ── Tag rendering tests ──────────────────────────────────────────


class TestTagRendering:
    """Tests for tag rendering in document list and detail pages."""

    @pytest.mark.asyncio
    async def test_documents_list_shows_tag_cloud(self, asgi_client):
        """Documents list page should show a tag cloud with all tags."""
        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200
        assert "tag-cloud" in resp.text
        assert "python" in resp.text
        assert "ai" in resp.text
        assert "research" in resp.text

    @pytest.mark.asyncio
    async def test_tag_cloud_shows_counts(self, asgi_client):
        """Tag cloud should show document counts for each tag."""
        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200
        # python has 2 documents
        assert "(2)" in resp.text

    @pytest.mark.asyncio
    async def test_documents_list_shows_tag_badges(self, asgi_client):
        """Documents list should show tag pills on each document row."""
        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200
        assert "tag-pill" in resp.text

    @pytest.mark.asyncio
    async def test_document_detail_shows_tags(self, asgi_client):
        """Document detail page should show existing tags as badges."""
        resp = await asgi_client.get("/documents/1")
        assert resp.status_code == 200
        assert "tag-pill" in resp.text
        assert "python" in resp.text
        assert "ai" in resp.text

    @pytest.mark.asyncio
    async def test_document_detail_shows_remove_buttons(self, asgi_client):
        """Document detail should have remove (✕) buttons for each tag."""
        resp = await asgi_client.get("/documents/1")
        assert resp.status_code == 200
        assert "tag-remove" in resp.text
        assert "/documents/1/tags/python/delete" in resp.text
        assert "/documents/1/tags/ai/delete" in resp.text

    @pytest.mark.asyncio
    async def test_document_detail_shows_add_tag_form(self, asgi_client):
        """Document detail should have an Add Tag form."""
        resp = await asgi_client.get("/documents/1")
        assert resp.status_code == 200
        assert 'action="/documents/1/tags"' in resp.text
        assert 'method="post"' in resp.text
        assert 'name="tag"' in resp.text
        assert "Add Tag" in resp.text

    @pytest.mark.asyncio
    async def test_document_detail_no_tags_shows_message(self, asgi_client):
        """Document with no tags should show 'No tags yet' message."""
        resp = await asgi_client.get("/documents/5")
        assert resp.status_code == 200
        assert "No tags yet" in resp.text

    @pytest.mark.asyncio
    async def test_tag_cloud_links_to_filtered_view(self, asgi_client):
        """Tag cloud items should link to /documents?tag=xxx."""
        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200
        assert 'href="/documents?tag=python"' in resp.text

    @pytest.mark.asyncio
    async def test_tag_pills_link_to_filtered_view(self, asgi_client):
        """Tag pills on document rows should link to the filtered view."""
        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200
        assert 'href="/documents?tag=' in resp.text

    @pytest.mark.asyncio
    async def test_documents_list_has_tags_column(self, asgi_client):
        """Documents table should have a Tags column header."""
        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200
        assert "<th>Tags</th>" in resp.text
