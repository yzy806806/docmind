"""Tests for the collections REST API + HTML form endpoints (Phase 2).

Covers all 9 REST API endpoints, the new GET /api/v1/documents with
collection_id filter, and the 4 HTML form endpoints:

REST API:
  1. POST   /api/v1/collections                   — create
  2. GET    /api/v1/collections                    — list (flat)
  3. GET    /api/v1/collections/tree               — list (tree)
  4. GET    /api/v1/collections/{id}               — get single
  5. PUT    /api/v1/collections/{id}               — update
  6. DELETE /api/v1/collections/{id}               — delete
  7. POST   /api/v1/documents/{doc_id}/collection  — assign
  8. DELETE /api/v1/documents/{doc_id}/collection  — remove
  9. GET    /api/v1/collections/{id}/documents     — list docs in collection
  + GET    /api/v1/documents                       — list with collection_id filter

HTML form endpoints:
  - POST /collections/create
  - POST /collections/{id}/edit
  - POST /collections/{id}/delete
  - POST /documents/{doc_id}/assign-collection

Error cases: 400 (bad input), 404 (not found), 409 (cycle detection).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Generator

import pytest


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    """Provide a temporary database file path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "test_collections_api.db")


@pytest.fixture
async def client(tmp_db_path: str):
    """ASGI test client with auth DISABLED and a real Database instance.

    The server's ``_db`` and ``_queue`` module globals are swapped for the
    duration of the test so that all endpoints operate on the temp DB.
    """
    import httpx
    from src.core.db_sqlite import Database
    from src.core.config import config
    from src.web import server

    db = Database(db_path=tmp_db_path)
    await db.connect()

    orig_enabled = config.auth.enabled
    orig_key = config.auth.api_key
    orig_secret = config.auth.session_secret
    config.auth.enabled = False
    config.auth.api_key = ""
    config.auth.session_secret = ""

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
    config.auth.enabled = orig_enabled
    config.auth.api_key = orig_key
    config.auth.session_secret = orig_secret


async def _make_doc(client, title: str = "Test Doc") -> int:
    """Helper: insert a document via DB and return its id."""
    from src.web import server
    db = server._db
    doc_id = await db.save_document(
        path=f"/docs/{title.lower().replace(' ', '-')}",
        source_type="local",
        source_name="test",
        title=title,
        ext=".txt",
        mime_type="text/plain",
        body=f"Body of {title}",
    )
    return doc_id


async def _make_collection(client, name: str, **kwargs) -> int:
    """Helper: create a collection via the REST API and return its id."""
    resp = await client.post("/api/v1/collections", json={"name": name, **kwargs})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ── 1. POST /api/v1/collections — Create ─────────────────────────


class TestCreateCollection:
    @pytest.mark.asyncio
    async def test_create_collection_basic(self, client) -> None:
        resp = await client.post("/api/v1/collections", json={"name": "My Col"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "My Col"
        assert data["description"] == ""
        assert data["parent_id"] is None
        assert isinstance(data["id"], int)

    @pytest.mark.asyncio
    async def test_create_collection_with_description_and_parent(self, client) -> None:
        parent_id = await _make_collection(client, "Parent")
        resp = await client.post(
            "/api/v1/collections",
            json={"name": "Child", "description": "A child col", "parent_id": parent_id},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Child"
        assert data["description"] == "A child col"
        assert data["parent_id"] == parent_id

    @pytest.mark.asyncio
    async def test_create_collection_missing_name(self, client) -> None:
        resp = await client.post("/api/v1/collections", json={"description": "no name"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_collection_empty_name(self, client) -> None:
        resp = await client.post("/api/v1/collections", json={"name": "  "})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_collection_nonexistent_parent(self, client) -> None:
        resp = await client.post(
            "/api/v1/collections", json={"name": "Orphan", "parent_id": 99999}
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_create_collection_invalid_parent_type(self, client) -> None:
        resp = await client.post(
            "/api/v1/collections", json={"name": "Bad", "parent_id": -1}
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_collection_invalid_json(self, client) -> None:
        resp = await client.post(
            "/api/v1/collections",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_collection_not_a_dict(self, client) -> None:
        resp = await client.post(
            "/api/v1/collections", content=b"[]", headers={"content-type": "application/json"}
        )
        assert resp.status_code == 400


# ── 2. GET /api/v1/collections — List (flat) ─────────────────────


class TestListCollections:
    @pytest.mark.asyncio
    async def test_list_empty(self, client) -> None:
        resp = await client.get("/api/v1/collections")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["collections"] == []

    @pytest.mark.asyncio
    async def test_list_multiple(self, client) -> None:
        await _make_collection(client, "Alpha")
        await _make_collection(client, "Beta")
        resp = await client.get("/api/v1/collections")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        # Ordered by name
        assert data["collections"][0]["name"] == "Alpha"
        assert data["collections"][1]["name"] == "Beta"


# ── 3. GET /api/v1/collections/tree — Tree ───────────────────────


class TestListCollectionsTree:
    @pytest.mark.asyncio
    async def test_tree_empty(self, client) -> None:
        resp = await client.get("/api/v1/collections/tree")
        assert resp.status_code == 200
        assert resp.json()["tree"] == []

    @pytest.mark.asyncio
    async def test_tree_nested(self, client) -> None:
        parent_id = await _make_collection(client, "Parent")
        await _make_collection(client, "Child", parent_id=parent_id)
        resp = await client.get("/api/v1/collections/tree")
        assert resp.status_code == 200
        tree = resp.json()["tree"]
        assert len(tree) == 1
        assert tree[0]["name"] == "Parent"
        assert len(tree[0]["children"]) == 1
        assert tree[0]["children"][0]["name"] == "Child"


# ── 4. GET /api/v1/collections/{id} — Get single ─────────────────


class TestGetCollection:
    @pytest.mark.asyncio
    async def test_get_existing(self, client) -> None:
        cid = await _make_collection(client, "Find Me", description="desc")
        resp = await client.get(f"/api/v1/collections/{cid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == cid
        assert data["name"] == "Find Me"
        assert data["description"] == "desc"

    @pytest.mark.asyncio
    async def test_get_not_found(self, client) -> None:
        resp = await client.get("/api/v1/collections/99999")
        assert resp.status_code == 404


# ── 5. PUT /api/v1/collections/{id} — Update ─────────────────────


class TestUpdateCollection:
    @pytest.mark.asyncio
    async def test_update_name(self, client) -> None:
        cid = await _make_collection(client, "Original")
        resp = await client.put(
            f"/api/v1/collections/{cid}", json={"name": "Renamed"}
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Renamed"

    @pytest.mark.asyncio
    async def test_update_description(self, client) -> None:
        cid = await _make_collection(client, "Col", description="old")
        resp = await client.put(
            f"/api/v1/collections/{cid}", json={"description": "new desc"}
        )
        assert resp.status_code == 200
        assert resp.json()["description"] == "new desc"

    @pytest.mark.asyncio
    async def test_update_parent(self, client) -> None:
        parent_id = await _make_collection(client, "Parent")
        child_id = await _make_collection(client, "Child")
        resp = await client.put(
            f"/api/v1/collections/{child_id}", json={"parent_id": parent_id}
        )
        assert resp.status_code == 200
        assert resp.json()["parent_id"] == parent_id

    @pytest.mark.asyncio
    async def test_update_not_found(self, client) -> None:
        resp = await client.put(
            "/api/v1/collections/99999", json={"name": "X"}
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_empty_name(self, client) -> None:
        cid = await _make_collection(client, "Col")
        resp = await client.put(
            f"/api/v1/collections/{cid}", json={"name": "  "}
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_update_self_parent_cycle(self, client) -> None:
        """Setting parent_id to the collection's own id → 409."""
        cid = await _make_collection(client, "Self")
        resp = await client.put(
            f"/api/v1/collections/{cid}", json={"parent_id": cid}
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_update_descendant_parent_cycle(self, client) -> None:
        """Moving a parent under its own child → 409."""
        parent_id = await _make_collection(client, "Parent")
        child_id = await _make_collection(client, "Child", parent_id=parent_id)
        # Try to move Parent under Child — cycle
        resp = await client.put(
            f"/api/v1/collections/{parent_id}", json={"parent_id": child_id}
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_update_nonexistent_parent(self, client) -> None:
        cid = await _make_collection(client, "Col")
        resp = await client.put(
            f"/api/v1/collections/{cid}", json={"parent_id": 99999}
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_invalid_json(self, client) -> None:
        cid = await _make_collection(client, "Col")
        resp = await client.put(
            f"/api/v1/collections/{cid}",
            content=b"bad",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400


# ── 6. DELETE /api/v1/collections/{id} — Delete ──────────────────


class TestDeleteCollection:
    @pytest.mark.asyncio
    async def test_delete_existing(self, client) -> None:
        cid = await _make_collection(client, "Delete Me")
        resp = await client.delete(f"/api/v1/collections/{cid}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    @pytest.mark.asyncio
    async def test_delete_not_found(self, client) -> None:
        resp = await client.delete("/api/v1/collections/99999")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_moves_documents_to_unassigned(self, client) -> None:
        """Deleting a collection should move its documents to unassigned."""
        cid = await _make_collection(client, "With Docs")
        doc_id = await _make_doc(client, "Doc1")

        # Assign doc to collection
        assign_resp = await client.post(
            f"/api/v1/documents/{doc_id}/collection", json={"collection_id": cid}
        )
        assert assign_resp.status_code == 200

        # Delete collection
        resp = await client.delete(f"/api/v1/collections/{cid}")
        assert resp.status_code == 200

        # Document should still exist but be unassigned
        from src.web import server
        col = await server._db.get_document_collection(doc_id)
        assert col is None


# ── 7. POST /api/v1/documents/{doc_id}/collection — Assign ───────


class TestAssignDocument:
    @pytest.mark.asyncio
    async def test_assign_success(self, client) -> None:
        cid = await _make_collection(client, "Target")
        doc_id = await _make_doc(client, "Doc")
        resp = await client.post(
            f"/api/v1/documents/{doc_id}/collection", json={"collection_id": cid}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["assigned"] is True
        assert data["collection_id"] == cid

    @pytest.mark.asyncio
    async def test_assign_doc_not_found(self, client) -> None:
        cid = await _make_collection(client, "Col")
        resp = await client.post(
            "/api/v1/documents/99999/collection", json={"collection_id": cid}
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_assign_collection_not_found(self, client) -> None:
        doc_id = await _make_doc(client, "Doc")
        resp = await client.post(
            f"/api/v1/documents/{doc_id}/collection", json={"collection_id": 99999}
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_assign_missing_collection_id(self, client) -> None:
        doc_id = await _make_doc(client, "Doc")
        resp = await client.post(
            f"/api/v1/documents/{doc_id}/collection", json={}
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_assign_invalid_collection_id(self, client) -> None:
        doc_id = await _make_doc(client, "Doc")
        resp = await client.post(
            f"/api/v1/documents/{doc_id}/collection",
            json={"collection_id": -5},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_assign_invalid_json(self, client) -> None:
        doc_id = await _make_doc(client, "Doc")
        resp = await client.post(
            f"/api/v1/documents/{doc_id}/collection",
            content=b"bad",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400


# ── 8. DELETE /api/v1/documents/{doc_id}/collection — Remove ─────


class TestRemoveDocument:
    @pytest.mark.asyncio
    async def test_remove_after_assign(self, client) -> None:
        cid = await _make_collection(client, "Col")
        doc_id = await _make_doc(client, "Doc")
        await client.post(
            f"/api/v1/documents/{doc_id}/collection", json={"collection_id": cid}
        )
        resp = await client.delete(f"/api/v1/documents/{doc_id}/collection")
        assert resp.status_code == 200
        assert resp.json()["removed"] is True

    @pytest.mark.asyncio
    async def test_remove_already_unassigned(self, client) -> None:
        doc_id = await _make_doc(client, "Doc")
        resp = await client.delete(f"/api/v1/documents/{doc_id}/collection")
        assert resp.status_code == 200
        assert resp.json()["removed"] is False

    @pytest.mark.asyncio
    async def test_remove_doc_not_found(self, client) -> None:
        resp = await client.delete("/api/v1/documents/99999/collection")
        assert resp.status_code == 404


# ── 9. GET /api/v1/collections/{id}/documents — List docs ────────


class TestListDocumentsInCollection:
    @pytest.mark.asyncio
    async def test_list_empty_collection(self, client) -> None:
        cid = await _make_collection(client, "Empty")
        resp = await client.get(f"/api/v1/collections/{cid}/documents")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["documents"] == []

    @pytest.mark.asyncio
    async def test_list_with_documents(self, client) -> None:
        cid = await _make_collection(client, "With Docs")
        doc1 = await _make_doc(client, "Doc1")
        doc2 = await _make_doc(client, "Doc2")
        await client.post(
            f"/api/v1/documents/{doc1}/collection", json={"collection_id": cid}
        )
        await client.post(
            f"/api/v1/documents/{doc2}/collection", json={"collection_id": cid}
        )
        resp = await client.get(f"/api/v1/collections/{cid}/documents")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2

    @pytest.mark.asyncio
    async def test_list_collection_not_found(self, client) -> None:
        resp = await client.get("/api/v1/collections/99999/documents")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_pagination(self, client) -> None:
        cid = await _make_collection(client, "Paginated")
        for i in range(5):
            doc_id = await _make_doc(client, f"Doc{i}")
            await client.post(
                f"/api/v1/documents/{doc_id}/collection",
                json={"collection_id": cid},
            )
        resp = await client.get(
            f"/api/v1/collections/{cid}/documents?page=1&per_page=2"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert data["page"] == 1
        assert data["per_page"] == 2
        assert data["total_pages"] == 3
        assert len(data["documents"]) == 2


# ── 10. GET /api/v1/documents — List with collection_id filter ───


class TestListDocumentsApi:
    @pytest.mark.asyncio
    async def test_list_all_documents(self, client) -> None:
        await _make_doc(client, "Doc1")
        await _make_doc(client, "Doc2")
        resp = await client.get("/api/v1/documents")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2

    @pytest.mark.asyncio
    async def test_list_filter_by_collection(self, client) -> None:
        cid = await _make_collection(client, "Col")
        doc1 = await _make_doc(client, "InCol")
        doc2 = await _make_doc(client, "NotInCol")
        await client.post(
            f"/api/v1/documents/{doc1}/collection", json={"collection_id": cid}
        )
        resp = await client.get(f"/api/v1/documents?collection_id={cid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["documents"][0]["title"] == "InCol"

    @pytest.mark.asyncio
    async def test_list_unassigned_documents(self, client) -> None:
        """collection_id=0 lists unassigned documents."""
        cid = await _make_collection(client, "Col")
        doc1 = await _make_doc(client, "Assigned")
        doc2 = await _make_doc(client, "Unassigned")
        await client.post(
            f"/api/v1/documents/{doc1}/collection", json={"collection_id": cid}
        )
        resp = await client.get("/api/v1/documents?collection_id=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["documents"][0]["title"] == "Unassigned"

    @pytest.mark.asyncio
    async def test_list_pagination(self, client) -> None:
        for i in range(5):
            await _make_doc(client, f"Doc{i}")
        resp = await client.get("/api/v1/documents?page=1&per_page=2")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert data["total_pages"] == 3
        assert len(data["documents"]) == 2


# ── HTML form endpoints ──────────────────────────────────────────


class TestHtmlFormEndpoints:
    @pytest.mark.asyncio
    async def test_create_collection_form(self, client) -> None:
        resp = await client.post(
            "/collections/create",
            data={"name": "Form Col", "description": "via form"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/documents"

        # Verify it was created
        list_resp = await client.get("/api/v1/collections")
        names = [c["name"] for c in list_resp.json()["collections"]]
        assert "Form Col" in names

    @pytest.mark.asyncio
    async def test_create_collection_form_empty_name(self, client) -> None:
        resp = await client.post(
            "/collections/create", data={"name": "", "description": ""}
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_collection_form_with_parent(self, client) -> None:
        parent_id = await _make_collection(client, "Parent")
        resp = await client.post(
            "/collections/create",
            data={"name": "Child", "description": "", "parent_id": str(parent_id)},
        )
        assert resp.status_code == 303

    @pytest.mark.asyncio
    async def test_create_collection_form_bad_parent(self, client) -> None:
        resp = await client.post(
            "/collections/create",
            data={"name": "Orphan", "description": "", "parent_id": "not-a-number"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_collection_form_nonexistent_parent(self, client) -> None:
        resp = await client.post(
            "/collections/create",
            data={"name": "Orphan", "description": "", "parent_id": "99999"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_edit_collection_form(self, client) -> None:
        cid = await _make_collection(client, "Original")
        resp = await client.post(
            f"/collections/{cid}/edit",
            data={"name": "Edited", "description": "updated"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/documents"

        # Verify the update
        col = await client.get(f"/api/v1/collections/{cid}")
        assert col.json()["name"] == "Edited"

    @pytest.mark.asyncio
    async def test_edit_collection_form_not_found(self, client) -> None:
        resp = await client.post(
            "/collections/99999/edit", data={"name": "X"}
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_edit_collection_form_empty_name(self, client) -> None:
        cid = await _make_collection(client, "Col")
        resp = await client.post(
            f"/collections/{cid}/edit", data={"name": ""}
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_delete_collection_form(self, client) -> None:
        cid = await _make_collection(client, "To Delete")
        resp = await client.post(f"/collections/{cid}/delete")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/documents"

        # Verify it's gone
        get_resp = await client.get(f"/api/v1/collections/{cid}")
        assert get_resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_collection_form_not_found(self, client) -> None:
        resp = await client.post("/collections/99999/delete")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_assign_collection_form(self, client) -> None:
        cid = await _make_collection(client, "Target")
        doc_id = await _make_doc(client, "Doc")
        resp = await client.post(
            f"/documents/{doc_id}/assign-collection",
            data={"collection_id": str(cid)},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/documents/{doc_id}"

        # Verify assignment
        col = await client.get(f"/api/v1/documents/{doc_id}/collection")
        # There's no direct GET for doc's collection via API, check via DB
        from src.web import server
        result = await server._db.get_document_collection(doc_id)
        assert result is not None
        assert result["id"] == cid

    @pytest.mark.asyncio
    async def test_assign_collection_form_empty_unassigns(self, client) -> None:
        """Empty collection_id in form removes the document from its collection."""
        cid = await _make_collection(client, "Col")
        doc_id = await _make_doc(client, "Doc")
        await client.post(
            f"/documents/{doc_id}/assign-collection",
            data={"collection_id": str(cid)},
        )
        # Now unassign with empty
        resp = await client.post(
            f"/documents/{doc_id}/assign-collection",
            data={"collection_id": ""},
        )
        assert resp.status_code == 303
        from src.web import server
        result = await server._db.get_document_collection(doc_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_assign_collection_form_doc_not_found(self, client) -> None:
        resp = await client.post(
            "/documents/99999/assign-collection",
            data={"collection_id": ""},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_assign_collection_form_bad_collection_id(self, client) -> None:
        doc_id = await _make_doc(client, "Doc")
        resp = await client.post(
            f"/documents/{doc_id}/assign-collection",
            data={"collection_id": "not-a-number"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_assign_collection_form_nonexistent_collection(self, client) -> None:
        doc_id = await _make_doc(client, "Doc")
        resp = await client.post(
            f"/documents/{doc_id}/assign-collection",
            data={"collection_id": "99999"},
        )
        assert resp.status_code == 404


# ── Integration: full workflow ───────────────────────────────────


class TestCollectionWorkflow:
    @pytest.mark.asyncio
    async def test_full_lifecycle(self, client) -> None:
        """Create → get → update → assign doc → list docs → delete → verify."""
        # Create
        cid = await _make_collection(client, "Lifecycle", description="test")

        # Get
        resp = await client.get(f"/api/v1/collections/{cid}")
        assert resp.json()["name"] == "Lifecycle"

        # Update
        resp = await client.put(
            f"/api/v1/collections/{cid}",
            json={"name": "Renamed", "description": "updated"},
        )
        assert resp.json()["name"] == "Renamed"

        # Create a doc and assign
        doc_id = await _make_doc(client, "Workflow Doc")
        resp = await client.post(
            f"/api/v1/documents/{doc_id}/collection",
            json={"collection_id": cid},
        )
        assert resp.status_code == 200

        # List docs in collection
        resp = await client.get(f"/api/v1/collections/{cid}/documents")
        assert resp.json()["total"] == 1

        # Remove doc from collection
        resp = await client.delete(f"/api/v1/documents/{doc_id}/collection")
        assert resp.json()["removed"] is True

        # List docs again — should be empty
        resp = await client.get(f"/api/v1/collections/{cid}/documents")
        assert resp.json()["total"] == 0

        # Delete collection
        resp = await client.delete(f"/api/v1/collections/{cid}")
        assert resp.status_code == 200

        # Verify gone
        resp = await client.get(f"/api/v1/collections/{cid}")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_nested_collections_tree_and_breadcrumbs(self, client) -> None:
        """Create 3-level nesting, verify tree structure."""
        root = await _make_collection(client, "Root")
        child = await _make_collection(client, "Child", parent_id=root)
        grandchild = await _make_collection(client, "Grandchild", parent_id=child)

        resp = await client.get("/api/v1/collections/tree")
        tree = resp.json()["tree"]
        assert len(tree) == 1
        assert tree[0]["name"] == "Root"
        assert len(tree[0]["children"]) == 1
        assert tree[0]["children"][0]["name"] == "Child"
        assert len(tree[0]["children"][0]["children"]) == 1
        assert tree[0]["children"][0]["children"][0]["name"] == "Grandchild"
