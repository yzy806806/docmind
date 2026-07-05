"""Tests for bulk operations UI: bulk tag, bulk move to collection, bulk export.

Covers:
- POST /documents/bulk-tag (HTML form) — add a tag to multiple documents
- POST /api/v1/documents/bulk-tag (JSON API) — add a tag to multiple documents
- POST /documents/bulk-move-collection (HTML form) — assign docs to a collection
- POST /api/v1/documents/bulk-assign (JSON API) — assign docs to a collection
- GET /documents/bulk-export?doc_ids=1,2&format=csv (HTML form / GET)
- GET /api/v1/documents/bulk-export?doc_ids=1,2&format=json (JSON API)
- UI: documents list template has bulk action controls
"""

from __future__ import annotations

import json as _json
import tempfile
from pathlib import Path
from typing import Generator

import pytest


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "test_bulk_ops.db")


@pytest.fixture
async def asgi_client(tmp_db_path: str):
    """Create an httpx.AsyncClient backed by the ASGI app with test documents."""
    import httpx
    from src.core.db_sqlite import Database
    from src.web import server

    db = Database(db_path=tmp_db_path)
    await db.connect()

    # Insert test documents
    for i in range(15):
        await db.save_document(
            path=f"/docs/bulk_{i}.txt",
            source_type="api",
            source_name="bulk-source",
            title=f"Bulk Test Document {i}",
            ext=".txt",
            mime_type="text/plain",
            body=f"This is the body of bulk test document {i}. It has searchable text.",
            size=100,
            status="indexed" if i % 2 == 0 else "pending",
        )

    # Create test collections
    await db.create_collection(name="Collection A", description="First collection")
    await db.create_collection(name="Collection B", description="Second collection")

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


# ── Bulk Tag: API endpoint ───────────────────────────────────────


class TestBulkTagAPIEndpoint:
    """Tests for POST /api/v1/documents/bulk-tag (JSON API)."""

    @pytest.mark.asyncio
    async def test_bulk_tag_adds_tag_to_multiple_docs(self, asgi_client):
        """POST /api/v1/documents/bulk-tag should add tag to multiple docs."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-tag",
            content=_json.dumps({"doc_ids": [1, 2, 3], "tag": "important"}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["tagged_count"] == 3
        assert set(data["tagged"]) == {1, 2, 3}
        assert data["not_found_count"] == 0

    @pytest.mark.asyncio
    async def test_bulk_tag_with_not_found_ids(self, asgi_client):
        """Non-existent IDs should be reported in not_found."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-tag",
            content=_json.dumps({"doc_ids": [1, 9999], "tag": "test"}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["tagged_count"] == 1
        assert 1 in data["tagged"]
        assert 9999 in data["not_found"]

    @pytest.mark.asyncio
    async def test_bulk_tag_empty_tag_string(self, asgi_client):
        """Empty tag string should return 400."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-tag",
            content=_json.dumps({"doc_ids": [1, 2], "tag": ""}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_tag_missing_tag_key(self, asgi_client):
        """Missing 'tag' key should return 400."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-tag",
            content=_json.dumps({"doc_ids": [1, 2]}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_tag_missing_doc_ids_key(self, asgi_client):
        """Missing 'doc_ids' key should return 400."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-tag",
            content=_json.dumps({"tag": "test"}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_tag_empty_doc_ids(self, asgi_client):
        """Empty doc_ids array should return 400."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-tag",
            content=_json.dumps({"doc_ids": [], "tag": "test"}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_tag_invalid_json(self, asgi_client):
        """Malformed JSON body should return 400."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-tag",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_tag_actually_tags(self, asgi_client):
        """Verify the tag is actually applied to documents."""
        await asgi_client.post(
            "/api/v1/documents/bulk-tag",
            content=_json.dumps({"doc_ids": [4, 5], "tag": "verified"}).encode(),
            headers={"content-type": "application/json"},
        )
        # Check tags were added via the documents list with tag filter
        resp = await asgi_client.get("/api/v1/documents?tag=verified&per_page=100")
        assert resp.status_code == 200
        doc_ids = [d["id"] for d in resp.json().get("documents", [])]
        assert 4 in doc_ids
        assert 5 in doc_ids

    @pytest.mark.asyncio
    async def test_bulk_tag_idempotent(self, asgi_client):
        """Adding the same tag twice should not error (idempotent)."""
        # First add
        await asgi_client.post(
            "/api/v1/documents/bulk-tag",
            content=_json.dumps({"doc_ids": [6], "tag": "dup"}).encode(),
            headers={"content-type": "application/json"},
        )
        # Second add — same tag
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-tag",
            content=_json.dumps({"doc_ids": [6], "tag": "dup"}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["tagged_count"] == 1


# ── Bulk Tag: Form endpoint ──────────────────────────────────────


class TestBulkTagFormHandler:
    """Tests for POST /documents/bulk-tag (HTML form)."""

    @pytest.mark.asyncio
    async def test_bulk_tag_form_multiple(self, asgi_client):
        """POST form with multiple doc_ids should tag them and show success."""
        resp = await asgi_client.post(
            "/documents/bulk-tag",
            data={"doc_ids": ["1", "2", "3"], "tag": "formtag"},
        )
        assert resp.status_code == 200
        assert "Bulk Tag" in resp.text or "Tagged" in resp.text
        assert "formtag" in resp.text

    @pytest.mark.asyncio
    async def test_bulk_tag_form_no_selection(self, asgi_client):
        """POST form with no doc_ids should return 400."""
        resp = await asgi_client.post(
            "/documents/bulk-tag",
            data={"tag": "test"},
        )
        assert resp.status_code == 400
        assert "selected" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_bulk_tag_form_empty_tag(self, asgi_client):
        """POST form with empty tag should return 400."""
        resp = await asgi_client.post(
            "/documents/bulk-tag",
            data={"doc_ids": ["1", "2"], "tag": ""},
        )
        assert resp.status_code == 400
        assert "tag" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_bulk_tag_form_has_back_link(self, asgi_client):
        """Bulk tag success page should link back to documents."""
        resp = await asgi_client.post(
            "/documents/bulk-tag",
            data={"doc_ids": ["7"], "tag": "backtest"},
        )
        assert resp.status_code == 200
        assert "/documents" in resp.text

    @pytest.mark.asyncio
    async def test_bulk_tag_form_actually_tags(self, asgi_client):
        """Verify documents are actually tagged after form bulk tag."""
        resp = await asgi_client.post(
            "/documents/bulk-tag",
            data={"doc_ids": ["8", "9"], "tag": "formverified"},
        )
        assert resp.status_code == 200
        # Check via the documents list with tag filter
        resp_docs = await asgi_client.get(
            "/api/v1/documents?tag=formverified&per_page=100"
        )
        doc_ids = [d["id"] for d in resp_docs.json().get("documents", [])]
        assert 8 in doc_ids
        assert 9 in doc_ids


# ── Bulk Move to Collection: API endpoint ────────────────────────


class TestBulkAssignAPIEndpoint:
    """Tests for POST /api/v1/documents/bulk-assign (JSON API)."""

    @pytest.mark.asyncio
    async def test_bulk_assign_moves_docs_to_collection(self, asgi_client):
        """POST /api/v1/documents/bulk-assign should assign docs to a collection."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-assign",
            content=_json.dumps(
                {"doc_ids": [1, 2, 3], "collection_id": 1}
            ).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["assigned_count"] == 3
        assert set(data["assigned"]) == {1, 2, 3}
        assert data["not_found_count"] == 0

    @pytest.mark.asyncio
    async def test_bulk_assign_with_not_found_docs(self, asgi_client):
        """Non-existent doc IDs should be reported in not_found."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-assign",
            content=_json.dumps(
                {"doc_ids": [1, 9999], "collection_id": 1}
            ).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["assigned_count"] == 1
        assert 9999 in data["not_found"]

    @pytest.mark.asyncio
    async def test_bulk_assign_invalid_collection_id(self, asgi_client):
        """Non-existent collection_id should return 404."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-assign",
            content=_json.dumps(
                {"doc_ids": [1, 2], "collection_id": 9999}
            ).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_bulk_assign_missing_collection_id(self, asgi_client):
        """Missing collection_id key should return 400."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-assign",
            content=_json.dumps({"doc_ids": [1, 2]}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_assign_missing_doc_ids(self, asgi_client):
        """Missing doc_ids key should return 400."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-assign",
            content=_json.dumps({"collection_id": 1}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_assign_empty_doc_ids(self, asgi_client):
        """Empty doc_ids array should return 400."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-assign",
            content=_json.dumps({"doc_ids": [], "collection_id": 1}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_assign_invalid_json(self, asgi_client):
        """Malformed JSON body should return 400."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-assign",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_assign_actually_assigns(self, asgi_client):
        """Verify documents are actually assigned to the collection."""
        await asgi_client.post(
            "/api/v1/documents/bulk-assign",
            content=_json.dumps(
                {"doc_ids": [4, 5], "collection_id": 2}
            ).encode(),
            headers={"content-type": "application/json"},
        )
        # Check docs are in collection 2
        resp = await asgi_client.get("/api/v1/collections/2/documents")
        assert resp.status_code == 200
        doc_ids = [d["id"] for d in resp.json().get("documents", [])]
        assert 4 in doc_ids
        assert 5 in doc_ids


# ── Bulk Move to Collection: Form endpoint ───────────────────────


class TestBulkAssignFormHandler:
    """Tests for POST /documents/bulk-move-collection (HTML form)."""

    @pytest.mark.asyncio
    async def test_bulk_move_form_multiple(self, asgi_client):
        """POST form should assign docs to collection and show success."""
        resp = await asgi_client.post(
            "/documents/bulk-move-collection",
            data={"doc_ids": ["1", "2"], "collection_id": "1"},
        )
        assert resp.status_code == 200
        assert "Bulk Move" in resp.text or "Assigned" in resp.text or "Moved" in resp.text
        assert "Collection A" in resp.text

    @pytest.mark.asyncio
    async def test_bulk_move_form_no_selection(self, asgi_client):
        """POST form with no doc_ids should return 400."""
        resp = await asgi_client.post(
            "/documents/bulk-move-collection",
            data={"collection_id": "1"},
        )
        assert resp.status_code == 400
        assert "selected" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_bulk_move_form_invalid_collection(self, asgi_client):
        """POST form with non-existent collection should return 404."""
        resp = await asgi_client.post(
            "/documents/bulk-move-collection",
            data={"doc_ids": ["1", "2"], "collection_id": "9999"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_bulk_move_form_has_back_link(self, asgi_client):
        """Bulk move success page should link back to documents."""
        resp = await asgi_client.post(
            "/documents/bulk-move-collection",
            data={"doc_ids": ["3"], "collection_id": "1"},
        )
        assert resp.status_code == 200
        assert "/documents" in resp.text

    @pytest.mark.asyncio
    async def test_bulk_move_form_actually_moves(self, asgi_client):
        """Verify documents are actually moved after form bulk assign."""
        resp = await asgi_client.post(
            "/documents/bulk-move-collection",
            data={"doc_ids": ["6", "7"], "collection_id": "2"},
        )
        assert resp.status_code == 200
        # Check via API
        resp_docs = await asgi_client.get("/api/v1/collections/2/documents")
        doc_ids = [d["id"] for d in resp_docs.json().get("documents", [])]
        assert 6 in doc_ids
        assert 7 in doc_ids


# ── Bulk Export ──────────────────────────────────────────────────


class TestBulkExportAPIEndpoint:
    """Tests for GET /api/v1/documents/bulk-export (JSON API)."""

    @pytest.mark.asyncio
    async def test_bulk_export_csv(self, asgi_client):
        """GET /api/v1/documents/bulk-export?format=csv should return CSV."""
        resp = await asgi_client.get(
            "/api/v1/documents/bulk-export?doc_ids=1,2,3&format=csv"
        )
        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("content-type", "")
        assert "attachment" in resp.headers.get("content-disposition", "")
        # CSV should have header row + 3 data rows
        lines = resp.text.strip().split("\n")
        assert len(lines) >= 4  # header + 3 data rows
        assert "id" in lines[0].lower()

    @pytest.mark.asyncio
    async def test_bulk_export_json(self, asgi_client):
        """GET /api/v1/documents/bulk-export?format=json should return JSON."""
        resp = await asgi_client.get(
            "/api/v1/documents/bulk-export?doc_ids=1,2&format=json"
        )
        assert resp.status_code == 200
        assert "application/json" in resp.headers.get("content-type", "")
        assert "attachment" in resp.headers.get("content-disposition", "")
        data = resp.json()
        assert data["exported_count"] == 2
        assert len(data["documents"]) == 2

    @pytest.mark.asyncio
    async def test_bulk_export_with_not_found_ids(self, asgi_client):
        """Non-existent IDs should be skipped, not cause error."""
        resp = await asgi_client.get(
            "/api/v1/documents/bulk-export?doc_ids=1,9999&format=json"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["exported_count"] == 1
        assert 9999 in data.get("not_found", [])

    @pytest.mark.asyncio
    async def test_bulk_export_no_doc_ids(self, asgi_client):
        """Missing doc_ids should return 400."""
        resp = await asgi_client.get(
            "/api/v1/documents/bulk-export?format=csv"
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_export_invalid_format(self, asgi_client):
        """Invalid format should return 400."""
        resp = await asgi_client.get(
            "/api/v1/documents/bulk-export?doc_ids=1,2&format=xml"
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_export_default_format_is_csv(self, asgi_client):
        """When format is not specified, should default to CSV."""
        resp = await asgi_client.get(
            "/api/v1/documents/bulk-export?doc_ids=1,2"
        )
        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("content-type", "")


class TestBulkExportFormEndpoint:
    """Tests for GET /documents/bulk-export (HTML form / GET)."""

    @pytest.mark.asyncio
    async def test_bulk_export_form_csv(self, asgi_client):
        """GET /documents/bulk-export?doc_ids=1,2&format=csv should return CSV."""
        resp = await asgi_client.get(
            "/documents/bulk-export?doc_ids=1,2&format=csv"
        )
        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("content-type", "")
        assert "attachment" in resp.headers.get("content-disposition", "")

    @pytest.mark.asyncio
    async def test_bulk_export_form_json(self, asgi_client):
        """GET /documents/bulk-export?doc_ids=1&format=json should return JSON."""
        resp = await asgi_client.get(
            "/documents/bulk-export?doc_ids=1&format=json"
        )
        assert resp.status_code == 200
        assert "application/json" in resp.headers.get("content-type", "")


# ── UI: Documents list template ──────────────────────────────────


class TestBulkOperationsListUI:
    """Tests for bulk operation controls in the documents list template."""

    def test_list_template_has_bulk_tag_button(self):
        """Documents list should have a bulk tag button/control."""
        from src.web.server import _render_documents_list

        docs = [{"id": 1, "title": "A", "status": "indexed", "source_name": "s",
                 "ext": ".txt", "created_at": "2025-01-01"}]
        html = _render_documents_list(docs, "", 1, 20, 1, 1, tags_map={1: []})
        assert "bulk-tag" in html.lower() or "tag-selected" in html.lower()

    def test_list_template_has_bulk_move_button(self):
        """Documents list should have a bulk move to collection control."""
        from src.web.server import _render_documents_list

        docs = [{"id": 1, "title": "A", "status": "indexed", "source_name": "s",
                 "ext": ".txt", "created_at": "2025-01-01"}]
        html = _render_documents_list(docs, "", 1, 20, 1, 1, tags_map={1: []})
        assert "bulk-move" in html.lower() or "move-selected" in html.lower()

    def test_list_template_has_bulk_export_button(self):
        """Documents list should have a bulk export button/control."""
        from src.web.server import _render_documents_list

        docs = [{"id": 1, "title": "A", "status": "indexed", "source_name": "s",
                 "ext": ".txt", "created_at": "2025-01-01"}]
        html = _render_documents_list(docs, "", 1, 20, 1, 1, tags_map={1: []})
        assert "bulk-export" in html.lower() or "export-selected" in html.lower()

    def test_list_template_has_bulk_action_controls(self):
        """Documents list should have a bulk actions area with multiple controls."""
        from src.web.server import _render_documents_list

        docs = [{"id": 1, "title": "A", "status": "indexed", "source_name": "s",
                 "ext": ".txt", "created_at": "2025-01-01"}]
        html = _render_documents_list(docs, "", 1, 20, 1, 1, tags_map={1: []})
        # Should have a bulk actions container
        assert "bulk-actions" in html.lower() or "bulk-action" in html.lower()
