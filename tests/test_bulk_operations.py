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


# ══════════════════════════════════════════════════════════════════
# Additional comprehensive tests — edge cases and error paths
# ══════════════════════════════════════════════════════════════════


# ── Bulk Tag: Advanced edge cases ────────────────────────────────


class TestBulkTagAPIAdvanced:
    """Advanced edge cases and error paths for POST /api/v1/documents/bulk-tag."""

    @pytest.mark.asyncio
    async def test_bulk_tag_whitespace_only_tag(self, asgi_client):
        """Tag that is only whitespace after stripping should return 400."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-tag",
            content=_json.dumps({"doc_ids": [1, 2], "tag": "   "}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_tag_special_characters(self, asgi_client):
        """Tags with special characters (unicode, spaces) should work."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-tag",
            content=_json.dumps({"doc_ids": [10, 11], "tag": "café résumé 中文"}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["tagged_count"] == 2
        assert data["tag"] == "café résumé 中文"

    @pytest.mark.asyncio
    async def test_bulk_tag_non_list_doc_ids(self, asgi_client):
        """doc_ids that is not a list should return 400."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-tag",
            content=_json.dumps({"doc_ids": "not-a-list", "tag": "test"}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_tag_non_integer_ids(self, asgi_client):
        """doc_ids containing non-integer values should return 400."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-tag",
            content=_json.dumps({"doc_ids": ["abc", "def"], "tag": "test"}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_tag_negative_ids(self, asgi_client):
        """Negative document IDs should return 400."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-tag",
            content=_json.dumps({"doc_ids": [-1, -2], "tag": "test"}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_tag_all_not_found(self, asgi_client):
        """All IDs not found should still return 200 with not_found populated."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-tag",
            content=_json.dumps({"doc_ids": [9000, 9001, 9002], "tag": "ghost"}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["tagged_count"] == 0
        assert data["not_found_count"] == 3
        assert 9000 in data["not_found"]

    @pytest.mark.asyncio
    async def test_bulk_tag_doc_ids_as_integers(self, asgi_client):
        """doc_ids as raw integers (not strings) should work."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-tag",
            content=_json.dumps({"doc_ids": [1, 2], "tag": "rawint"}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_bulk_tag_doc_ids_as_strings(self, asgi_client):
        """doc_ids as string representations of integers should work."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-tag",
            content=_json.dumps({"doc_ids": ["3", "4"], "tag": "strint"}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["tagged_count"] == 2


# ── Bulk Tag Form: Advanced edge cases ───────────────────────────


class TestBulkTagFormAdvanced:
    """Advanced edge cases for POST /documents/bulk-tag (HTML form)."""

    @pytest.mark.asyncio
    async def test_bulk_tag_form_invalid_doc_ids(self, asgi_client):
        """Form with invalid (non-numeric) doc_ids should report them in invalid_ids."""
        resp = await asgi_client.post(
            "/documents/bulk-tag",
            data={"doc_ids": ["abc", "def"], "tag": "testtag"},
        )
        assert resp.status_code == 200
        assert "invalid" in resp.text.lower() or "Invalid IDs" in resp.text

    @pytest.mark.asyncio
    async def test_bulk_tag_form_mixed_valid_invalid_ids(self, asgi_client):
        """Form with mix of valid and invalid IDs should tag valid, report invalid."""
        resp = await asgi_client.post(
            "/documents/bulk-tag",
            data={"doc_ids": ["1", "abc", "3"], "tag": "mixed"},
        )
        assert resp.status_code == 200
        assert "mixed" in resp.text
        # Should have tagged IDs 1 and 3
        # Check via API
        resp_api = await asgi_client.get("/api/v1/documents?tag=mixed&per_page=100")
        doc_ids = [d["id"] for d in resp_api.json().get("documents", [])]
        assert 1 in doc_ids
        assert 3 in doc_ids

    @pytest.mark.asyncio
    async def test_bulk_tag_form_all_invalid(self, asgi_client):
        """Form with all invalid IDs should still render success page with errors."""
        resp = await asgi_client.post(
            "/documents/bulk-tag",
            data={"doc_ids": ["x", "y", "z"], "tag": "never"},
        )
        assert resp.status_code == 200
        assert "invalid" in resp.text.lower() or "Invalid IDs" in resp.text
        assert "never" in resp.text

    @pytest.mark.asyncio
    async def test_bulk_tag_form_special_characters_tag(self, asgi_client):
        """Form with special characters in tag should work."""
        resp = await asgi_client.post(
            "/documents/bulk-tag",
            data={"doc_ids": ["10"], "tag": "résumé 日本語"},
        )
        assert resp.status_code == 200
        assert "résumé 日本語" in resp.text

    @pytest.mark.asyncio
    async def test_bulk_tag_form_multiple_same_value_ids(self, asgi_client):
        """Form with duplicate doc_ids should be idempotent."""
        resp = await asgi_client.post(
            "/documents/bulk-tag",
            data={"doc_ids": ["5", "5", "5"], "tag": "duplicate"},
        )
        assert resp.status_code == 200


# ── Bulk Assign API: Advanced edge cases ─────────────────────────


class TestBulkAssignAPIAdvanced:
    """Advanced edge cases for POST /api/v1/documents/bulk-assign."""

    @pytest.mark.asyncio
    async def test_bulk_assign_non_integer_collection_id(self, asgi_client):
        """collection_id as a non-integer value should return 400."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-assign",
            content=_json.dumps({"doc_ids": [1, 2], "collection_id": "abc"}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_assign_non_list_doc_ids(self, asgi_client):
        """doc_ids that is not a list should return 400."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-assign",
            content=_json.dumps({"doc_ids": "not-list", "collection_id": 1}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_assign_negative_collection_id(self, asgi_client):
        """Negative collection_id should return 404 (no collection with that id)."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-assign",
            content=_json.dumps({"doc_ids": [1, 2], "collection_id": -1}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_bulk_assign_all_not_found_docs(self, asgi_client):
        """All doc_ids not found should still return 200 with not_found."""
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-assign",
            content=_json.dumps({"doc_ids": [9000, 9001], "collection_id": 1}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["assigned_count"] == 0
        assert data["not_found_count"] == 2

    @pytest.mark.asyncio
    async def test_bulk_assign_reassign_same_collection(self, asgi_client):
        """Reassigning docs to the same collection should be idempotent."""
        # First assign
        await asgi_client.post(
            "/api/v1/documents/bulk-assign",
            content=_json.dumps({"doc_ids": [12, 13], "collection_id": 1}).encode(),
            headers={"content-type": "application/json"},
        )
        # Second assign — same docs, same collection
        resp = await asgi_client.post(
            "/api/v1/documents/bulk-assign",
            content=_json.dumps({"doc_ids": [12, 13], "collection_id": 1}).encode(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["assigned_count"] == 2  # still reports as assigned


# ── Bulk Move Form: Advanced edge cases ──────────────────────────


class TestBulkMoveFormAdvanced:
    """Advanced edge cases for POST /documents/bulk-move-collection."""

    @pytest.mark.asyncio
    async def test_bulk_move_form_no_collection_selected(self, asgi_client):
        """Form with no collection_id should return 400."""
        resp = await asgi_client.post(
            "/documents/bulk-move-collection",
            data={"doc_ids": ["1", "2"], "collection_id": ""},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_move_form_non_numeric_collection(self, asgi_client):
        """Form with non-numeric collection_id should return 400."""
        resp = await asgi_client.post(
            "/documents/bulk-move-collection",
            data={"doc_ids": ["1", "2"], "collection_id": "not-a-number"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_move_form_invalid_doc_ids(self, asgi_client):
        """Form with invalid doc_ids should report them in invalid_ids."""
        resp = await asgi_client.post(
            "/documents/bulk-move-collection",
            data={"doc_ids": ["xyz", "abc"], "collection_id": "1"},
        )
        assert resp.status_code == 200
        assert "invalid" in resp.text.lower() or "Invalid IDs" in resp.text

    @pytest.mark.asyncio
    async def test_bulk_move_form_mixed_valid_invalid(self, asgi_client):
        """Form with mix of valid/invalid IDs should move valid, report invalid."""
        resp = await asgi_client.post(
            "/documents/bulk-move-collection",
            data={"doc_ids": ["10", "xyz", "11"], "collection_id": "2"},
        )
        assert resp.status_code == 200
        # Verify docs 10 and 11 actually moved to collection 2
        resp_api = await asgi_client.get("/api/v1/collections/2/documents")
        doc_ids = [d["id"] for d in resp_api.json().get("documents", [])]
        assert 10 in doc_ids
        assert 11 in doc_ids

    @pytest.mark.asyncio
    async def test_bulk_move_form_has_view_collection_link(self, asgi_client):
        """Success page should have a link to view the target collection."""
        resp = await asgi_client.post(
            "/documents/bulk-move-collection",
            data={"doc_ids": ["14"], "collection_id": "1"},
        )
        assert resp.status_code == 200
        assert "/collections/1" in resp.text


# ── Bulk Export: Advanced edge cases ─────────────────────────────


class TestBulkExportAdvanced:
    """Advanced edge cases for bulk export endpoints."""

    @pytest.mark.asyncio
    async def test_bulk_export_form_no_doc_ids(self, asgi_client):
        """GET /documents/bulk-export without doc_ids should return 400."""
        resp = await asgi_client.get("/documents/bulk-export?format=csv")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_export_form_empty_doc_ids(self, asgi_client):
        """GET /documents/bulk-export with empty doc_ids should return 400."""
        resp = await asgi_client.get("/documents/bulk-export?doc_ids=&format=csv")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_export_api_empty_doc_ids(self, asgi_client):
        """GET /api/v1/documents/bulk-export with empty doc_ids should return 400."""
        resp = await asgi_client.get("/api/v1/documents/bulk-export?doc_ids=&format=json")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bulk_export_all_not_found(self, asgi_client):
        """All export IDs not found should return 200 with not_found."""
        resp = await asgi_client.get(
            "/api/v1/documents/bulk-export?doc_ids=9000,9001&format=json"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["exported_count"] == 0
        assert 9000 in data.get("not_found", [])

    @pytest.mark.asyncio
    async def test_bulk_export_large_selection(self, asgi_client):
        """Export of all existing docs should work."""
        resp = await asgi_client.get(
            "/api/v1/documents/bulk-export?doc_ids=1,2,3,4,5,6,7,8,9,10,11,12,13,14,15&format=json"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["exported_count"] == 15

    @pytest.mark.asyncio
    async def test_bulk_export_csv_content(self, asgi_client):
        """CSV export should contain actual document data."""
        resp = await asgi_client.get(
            "/api/v1/documents/bulk-export?doc_ids=1,2&format=csv"
        )
        assert resp.status_code == 200
        # CSV should contain document titles or paths
        csv_text = resp.text
        assert "bulk" in csv_text.lower() or "test" in csv_text.lower()

    @pytest.mark.asyncio
    async def test_bulk_export_json_content(self, asgi_client):
        """JSON export should contain document objects."""
        resp = await asgi_client.get(
            "/api/v1/documents/bulk-export?doc_ids=1,2&format=json"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["documents"]) == 2
        assert "title" in data["documents"][0]
        assert "id" in data["documents"][0]

    @pytest.mark.asyncio
    async def test_bulk_export_form_default_format(self, asgi_client):
        """GET /documents/bulk-export without format defaults to CSV."""
        resp = await asgi_client.get(
            "/documents/bulk-export?doc_ids=1,2"
        )
        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_bulk_export_form_invalid_format(self, asgi_client):
        """GET /documents/bulk-export with invalid format should return 400."""
        resp = await asgi_client.get(
            "/documents/bulk-export?doc_ids=1,2&format=xml"
        )
        assert resp.status_code == 400


# ── UI: Bulk actions toolbar comprehensive ───────────────────────


class TestBulkActionsToolbarUI:
    """Comprehensive tests for the bulk actions toolbar in the UI template."""

    def test_toolbar_has_select_all(self):
        """Template should include a select-all checkbox."""
        from src.web.rendering import _render_documents_list

        docs = [{"id": 1, "title": "Test", "status": "indexed",
                 "source_name": "api", "ext": ".txt", "created_at": "2025-01-01"}]
        html = _render_documents_list(docs, "", 1, 20, 1, 1, tags_map={1: []})
        assert "select-all" in html.lower() or "select all" in html.lower()

    def test_toolbar_has_checkbox_column(self):
        """Each document row should have a checkbox."""
        from src.web.rendering import _render_documents_list

        docs = [
            {"id": 1, "title": "Test A", "status": "indexed",
             "source_name": "api", "ext": ".txt", "created_at": "2025-01-01"},
            {"id": 2, "title": "Test B", "status": "indexed",
             "source_name": "api", "ext": ".pdf", "created_at": "2025-01-02"},
        ]
        html = _render_documents_list(docs, "", 1, 20, 2, 1, tags_map={1: [], 2: []})
        assert "doc-checkbox" in html

    def test_toolbar_has_delete_button(self):
        """Bulk actions bar should include a delete button."""
        from src.web.rendering import _render_documents_list

        docs = [{"id": 1, "title": "Test", "status": "indexed",
                 "source_name": "api", "ext": ".txt", "created_at": "2025-01-01"}]
        html = _render_documents_list(docs, "", 1, 20, 1, 1, tags_map={1: []})
        assert "delete-selected" in html.lower() or "bulk-delete" in html.lower()

    def test_toolbar_has_tag_form(self):
        """Bulk actions bar should include a tag form."""
        from src.web.rendering import _render_documents_list

        docs = [{"id": 1, "title": "Test", "status": "indexed",
                 "source_name": "api", "ext": ".txt", "created_at": "2025-01-01"}]
        html = _render_documents_list(docs, "", 1, 20, 1, 1, tags_map={1: []})
        assert "bulk-tag" in html.lower()

    def test_toolbar_has_move_form(self):
        """Bulk actions bar should include a move-to-collection form."""
        from src.web.rendering import _render_documents_list

        docs = [{"id": 1, "title": "Test", "status": "indexed",
                 "source_name": "api", "ext": ".txt", "created_at": "2025-01-01"}]
        html = _render_documents_list(docs, "", 1, 20, 1, 1, tags_map={1: []})
        assert "bulk-move" in html.lower()

    def test_toolbar_has_export_form(self):
        """Bulk actions bar should include an export form."""
        from src.web.rendering import _render_documents_list

        docs = [{"id": 1, "title": "Test", "status": "indexed",
                 "source_name": "api", "ext": ".txt", "created_at": "2025-01-01"}]
        html = _render_documents_list(docs, "", 1, 20, 1, 1, tags_map={1: []})
        assert "bulk-export" in html.lower()

    def test_toolbar_shows_selected_count(self):
        """Bulk actions bar should have a selected count element."""
        from src.web.rendering import _render_documents_list

        docs = [{"id": 1, "title": "Test", "status": "indexed",
                 "source_name": "api", "ext": ".txt", "created_at": "2025-01-01"}]
        html = _render_documents_list(docs, "", 1, 20, 1, 1, tags_map={1: []})
        assert "selected-count" in html

    def test_toolbar_buttons_initially_disabled(self):
        """Bulk action buttons should start disabled (no selection)."""
        from src.web.rendering import _render_documents_list

        docs = [{"id": 1, "title": "Test", "status": "indexed",
                 "source_name": "api", "ext": ".txt", "created_at": "2025-01-01"}]
        html = _render_documents_list(docs, "", 1, 20, 1, 1, tags_map={1: []})
        assert "disabled" in html

    def test_toolbar_loads_documents_list_js(self):
        """Template should load documents-list.js for bulk action logic."""
        from src.web.rendering import _render_documents_list

        docs = [{"id": 1, "title": "Test", "status": "indexed",
                 "source_name": "api", "ext": ".txt", "created_at": "2025-01-01"}]
        html = _render_documents_list(docs, "", 1, 20, 1, 1, tags_map={1: []})
        assert "/static/js/documents-list.js" in html
