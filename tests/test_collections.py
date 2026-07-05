"""Tests for the collections/folders feature — schema, DB layer, and integration.

Covers all 13 acceptance criteria from the Phase 1 design:
  1. Collection CRUD (create, get, update, delete)
  2. Nesting (parent/child relationships)
  3. Unlimited nesting (5 levels deep)
  4. Documents without collection visible in "All Documents"
  5. Deleting collection preserves documents (moves to All Documents)
  6. Cascade delete (child collections removed when parent deleted)
  7. Cycle detection (cannot move collection under its own descendant)
  8. Sibling name uniqueness (same name allowed under different parents)
  9. Empty/whitespace name raises ValueError
 10. Idempotent migration (re-running migrate does not fail)
 11. Search with collection_id filter
 12. Pagination with collection_id filter
 13. get_collection_path breadcrumbs
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Generator

import pytest


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    """Provide a temporary database file path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "test_collections.db")


@pytest.fixture
async def db(tmp_db_path: str):
    """Create a connected SQLite Database instance."""
    from src.core.db_sqlite import Database

    database = Database(db_path=tmp_db_path)
    await database.connect()
    yield database
    await database.disconnect()


async def _make_doc(db, path: str, title: str = "Doc", body: str = "Body text") -> int:
    """Helper: insert a document and return its id."""
    return await db.save_document(
        path=path,
        source_type="local",
        source_name="test",
        title=title,
        ext=".txt",
        mime_type="text/plain",
        body=body,
    )


# ── 1. Collection CRUD ──────────────────────────────────────────


class TestCollectionCRUD:
    @pytest.mark.asyncio
    async def test_create_collection_returns_id(self, db) -> None:
        """create_collection should return a positive integer id."""
        cid = await db.create_collection("My Collection")
        assert isinstance(cid, int)
        assert cid > 0

    @pytest.mark.asyncio
    async def test_create_collection_with_description_and_parent(self, db) -> None:
        """create_collection should store description and parent_id."""
        parent = await db.create_collection("Parent")
        child = await db.create_collection("Child", description="A child", parent_id=parent)
        col = await db.get_collection(child)
        assert col is not None
        assert col["name"] == "Child"
        assert col["description"] == "A child"
        assert col["parent_id"] == parent

    @pytest.mark.asyncio
    async def test_get_collection_not_found(self, db) -> None:
        """get_collection should return None for non-existent id."""
        assert await db.get_collection(99999) is None

    @pytest.mark.asyncio
    async def test_update_collection_name(self, db) -> None:
        """update_collection should change the name."""
        cid = await db.create_collection("Original")
        result = await db.update_collection(cid, name="Renamed")
        assert result is True
        col = await db.get_collection(cid)
        assert col["name"] == "Renamed"

    @pytest.mark.asyncio
    async def test_update_collection_description(self, db) -> None:
        """update_collection should change the description."""
        cid = await db.create_collection("Col", description="old")
        await db.update_collection(cid, description="new description")
        col = await db.get_collection(cid)
        assert col["description"] == "new description"

    @pytest.mark.asyncio
    async def test_update_collection_not_found(self, db) -> None:
        """update_collection should return False for non-existent id."""
        assert await db.update_collection(99999, name="X") is False

    @pytest.mark.asyncio
    async def test_delete_collection_returns_true(self, db) -> None:
        """delete_collection should return True when deleted."""
        cid = await db.create_collection("To Delete")
        result = await db.delete_collection(cid)
        assert result is True
        assert await db.get_collection(cid) is None

    @pytest.mark.asyncio
    async def test_delete_collection_not_found(self, db) -> None:
        """delete_collection should return False for non-existent id."""
        assert await db.delete_collection(99999) is False


# ── 2. Nesting ──────────────────────────────────────────────────


class TestNesting:
    @pytest.mark.asyncio
    async def test_nested_collections(self, db) -> None:
        """A collection can have a parent."""
        root = await db.create_collection("Root")
        child = await db.create_collection("Child", parent_id=root)
        grandchild = await db.create_collection("Grandchild", parent_id=child)
        gc = await db.get_collection(grandchild)
        assert gc["parent_id"] == child

    @pytest.mark.asyncio
    async def test_list_collections_flat(self, db) -> None:
        """list_collections should return a flat list of all collections."""
        a = await db.create_collection("Alpha")
        b = await db.create_collection("Beta", parent_id=a)
        c = await db.create_collection("Gamma")
        cols = await db.list_collections()
        ids = {col["id"] for col in cols}
        assert ids == {a, b, c}

    @pytest.mark.asyncio
    async def test_list_collections_tree(self, db) -> None:
        """list_collections_tree should return a nested tree."""
        root = await db.create_collection("Root")
        child1 = await db.create_collection("Child1", parent_id=root)
        child2 = await db.create_collection("Child2", parent_id=root)
        grandchild = await db.create_collection("GC", parent_id=child1)

        tree = await db.list_collections_tree()
        assert len(tree) == 1
        assert tree[0]["id"] == root
        assert len(tree[0]["children"]) == 2
        # Child1 should have the grandchild
        c1 = next(c for c in tree[0]["children"] if c["id"] == child1)
        assert len(c1["children"]) == 1
        assert c1["children"][0]["id"] == grandchild
        # Child2 should have no children
        c2 = next(c for c in tree[0]["children"] if c["id"] == child2)
        assert len(c2["children"]) == 0


# ── 3. Unlimited nesting (5 levels) ─────────────────────────────


class TestUnlimitedNesting:
    @pytest.mark.asyncio
    async def test_five_level_nesting(self, db) -> None:
        """Collections should support at least 5 levels of nesting."""
        ids = []
        parent = None
        for i in range(5):
            cid = await db.create_collection(f"Level{i}", parent_id=parent)
            ids.append(cid)
            parent = cid

        # Verify the deepest collection's path
        path = await db.get_collection_path(ids[-1])
        assert len(path) == 5
        assert [p["name"] for p in path] == [
            "Level0", "Level1", "Level2", "Level3", "Level4",
        ]

    @pytest.mark.asyncio
    async def test_tree_reflects_deep_nesting(self, db) -> None:
        """list_collections_tree should show deep nesting correctly."""
        root = await db.create_collection("Root")
        c1 = await db.create_collection("C1", parent_id=root)
        c2 = await db.create_collection("C2", parent_id=c1)
        c3 = await db.create_collection("C3", parent_id=c2)

        tree = await db.list_collections_tree()
        assert tree[0]["id"] == root
        assert tree[0]["children"][0]["id"] == c1
        assert tree[0]["children"][0]["children"][0]["id"] == c2
        assert tree[0]["children"][0]["children"][0]["children"][0]["id"] == c3


# ── 4. Documents without collection visible in All Documents ────


class TestUnassignedDocuments:
    @pytest.mark.asyncio
    async def test_unassigned_documents_in_all(self, db) -> None:
        """Documents without a collection should appear in All Documents."""
        doc1 = await _make_doc(db, "/docs/unassigned1.txt", "Unassigned 1")
        doc2 = await _make_doc(db, "/docs/unassigned2.txt", "Unassigned 2")
        col = await db.create_collection("My Col")
        doc3 = await _make_doc(db, "/docs/assigned.txt", "Assigned")
        await db.assign_document_to_collection(doc3, col)

        # All Documents = collection_id IS NULL
        result = await db.list_documents_paginated(page=1, per_page=50, collection_id=0)
        titles = {d["title"] for d in result["documents"]}
        assert "Unassigned 1" in titles
        assert "Unassigned 2" in titles
        assert "Assigned" not in titles
        assert result["total"] == 2

    @pytest.mark.asyncio
    async def test_get_document_collection_none(self, db) -> None:
        """get_document_collection should return None for unassigned doc."""
        doc = await _make_doc(db, "/docs/free.txt", "Free Doc")
        assert await db.get_document_collection(doc) is None


# ── 5. Deleting collection preserves documents ──────────────────


class TestDeletePreservesDocuments:
    @pytest.mark.asyncio
    async def test_delete_moves_documents_to_all(self, db) -> None:
        """Deleting a collection should move its documents to All Documents."""
        col = await db.create_collection("To Delete")
        doc = await _make_doc(db, "/docs/in_col.txt", "In Collection")
        await db.assign_document_to_collection(doc, col)

        # Verify assignment
        assert (await db.get_document_collection(doc)) is not None

        # Delete the collection
        await db.delete_collection(col)

        # Document should still exist, now unassigned
        d = await db.get_document(doc)
        assert d is not None
        assert d["collection_id"] is None
        assert await db.get_document_collection(doc) is None


# ── 6. Cascade delete ───────────────────────────────────────────


class TestCascadeDelete:
    @pytest.mark.asyncio
    async def test_cascade_delete_children(self, db) -> None:
        """Deleting a parent should cascade-delete child collections."""
        root = await db.create_collection("Root")
        child = await db.create_collection("Child", parent_id=root)
        grandchild = await db.create_collection("GC", parent_id=child)

        await db.delete_collection(root)

        assert await db.get_collection(root) is None
        assert await db.get_collection(child) is None
        assert await db.get_collection(grandchild) is None

    @pytest.mark.asyncio
    async def test_cascade_preserves_documents_in_children(self, db) -> None:
        """When a parent is deleted, documents in child collections should
        also be moved to All Documents, not deleted."""
        root = await db.create_collection("Root")
        child = await db.create_collection("Child", parent_id=root)
        doc1 = await _make_doc(db, "/docs/root_doc.txt", "Root Doc")
        doc2 = await _make_doc(db, "/docs/child_doc.txt", "Child Doc")
        await db.assign_document_to_collection(doc1, root)
        await db.assign_document_to_collection(doc2, child)

        await db.delete_collection(root)

        # Both documents should still exist, unassigned
        d1 = await db.get_document(doc1)
        d2 = await db.get_document(doc2)
        assert d1 is not None and d1["collection_id"] is None
        assert d2 is not None and d2["collection_id"] is None


# ── 7. Cycle detection ──────────────────────────────────────────


class TestCycleDetection:
    @pytest.mark.asyncio
    async def test_cannot_set_parent_to_self(self, db) -> None:
        """A collection cannot be its own parent."""
        cid = await db.create_collection("Self")
        with pytest.raises(ValueError, match="cycle"):
            await db.update_collection(cid, parent_id=cid)

    @pytest.mark.asyncio
    async def test_cannot_move_under_descendant(self, db) -> None:
        """A collection cannot be moved under its own descendant."""
        root = await db.create_collection("Root")
        child = await db.create_collection("Child", parent_id=root)
        # Moving root under child would create a cycle
        with pytest.raises(ValueError, match="cycle"):
            await db.update_collection(root, parent_id=child)

    @pytest.mark.asyncio
    async def test_cannot_move_under_grandchild(self, db) -> None:
        """Cycle detection works for deeper hierarchies."""
        a = await db.create_collection("A")
        b = await db.create_collection("B", parent_id=a)
        c = await db.create_collection("C", parent_id=b)
        # Moving A under C would create a cycle: A -> C -> B -> A
        with pytest.raises(ValueError, match="cycle"):
            await db.update_collection(a, parent_id=c)

    @pytest.mark.asyncio
    async def test_can_move_to_different_branch(self, db) -> None:
        """Moving a collection to a different (non-cyclic) branch is fine."""
        a = await db.create_collection("A")
        b = await db.create_collection("B", parent_id=a)
        c = await db.create_collection("C")
        # Moving B under C is fine — no cycle
        result = await db.update_collection(b, parent_id=c)
        assert result is True
        col = await db.get_collection(b)
        assert col["parent_id"] == c


# ── 8. Sibling name uniqueness ───────────────────────────────────


class TestSiblingNameUniqueness:
    @pytest.mark.asyncio
    async def test_same_name_under_different_parents(self, db) -> None:
        """Two collections with the same name under different parents are OK."""
        parent_a = await db.create_collection("ParentA")
        parent_b = await db.create_collection("ParentB")
        child_a = await db.create_collection("Shared", parent_id=parent_a)
        child_b = await db.create_collection("Shared", parent_id=parent_b)
        assert child_a != child_b

    @pytest.mark.asyncio
    async def test_duplicate_name_same_parent_raises(self, db) -> None:
        """Two collections with the same name under the same parent should fail."""
        parent = await db.create_collection("Parent")
        await db.create_collection("Dup", parent_id=parent)
        with pytest.raises(Exception):  # sqlite3.IntegrityError
            await db.create_collection("Dup", parent_id=parent)

    @pytest.mark.asyncio
    async def test_duplicate_root_name_raises(self, db) -> None:
        """Two root-level collections with the same name should fail."""
        await db.create_collection("RootName")
        with pytest.raises(Exception):  # sqlite3.IntegrityError
            await db.create_collection("RootName")


# ── 9. Empty/whitespace name raises ValueError ──────────────────


class TestNameValidation:
    @pytest.mark.asyncio
    async def test_empty_name_raises(self, db) -> None:
        """Empty string name should raise ValueError."""
        with pytest.raises(ValueError, match="empty"):
            await db.create_collection("")

    @pytest.mark.asyncio
    async def test_whitespace_name_raises(self, db) -> None:
        """Whitespace-only name should raise ValueError."""
        with pytest.raises(ValueError, match="empty"):
            await db.create_collection("   ")

    @pytest.mark.asyncio
    async def test_none_name_raises(self, db) -> None:
        """None name should raise ValueError."""
        with pytest.raises(ValueError, match="empty"):
            await db.create_collection(None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_update_to_empty_name_raises(self, db) -> None:
        """Updating name to empty should raise ValueError."""
        cid = await db.create_collection("Valid")
        with pytest.raises(ValueError, match="empty"):
            await db.update_collection(cid, name="")

    @pytest.mark.asyncio
    async def test_name_is_trimmed(self, db) -> None:
        """Whitespace around the name should be trimmed."""
        cid = await db.create_collection("  Padded  ")
        col = await db.get_collection(cid)
        assert col["name"] == "Padded"


# ── 10. Idempotent migration ────────────────────────────────────


class TestIdempotentMigration:
    @pytest.mark.asyncio
    async def test_migrate_twice_no_error(self, tmp_db_path: str) -> None:
        """Running migrate() multiple times should not fail."""
        from src.core.db_sqlite import Database

        db = Database(db_path=tmp_db_path)
        await db.connect()
        await db.migrate()  # second call
        await db.migrate()  # third call
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_collection_id_column_exists(self, db) -> None:
        """The documents table should have a collection_id column after migrate."""
        async with db.connection() as conn:
            cursor = await conn.execute("PRAGMA table_info(documents)")
            columns = {row[1] for row in await cursor.fetchall()}
        assert "collection_id" in columns

    @pytest.mark.asyncio
    async def test_collections_table_exists(self, db) -> None:
        """The collections table should exist after migrate."""
        async with db.connection() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='collections'"
            )
            row = await cursor.fetchone()
        assert row is not None

    @pytest.mark.asyncio
    async def test_unique_index_exists(self, db) -> None:
        """The unique index on (parent_id, name) should exist."""
        async with db.connection() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='uq_collections_name_parent'"
            )
            row = await cursor.fetchone()
        assert row is not None


# ── 11. Search with collection_id filter ────────────────────────


class TestSearchWithCollectionFilter:
    @pytest.mark.asyncio
    async def test_search_within_collection(self, db) -> None:
        """search_documents with collection_id should only search within that collection."""
        col = await db.create_collection("ML Papers")
        doc_in = await _make_doc(
            db, "/docs/ml_paper.txt", "ML Paper",
            body="This paper discusses machine learning models.",
        )
        doc_out = await _make_doc(
            db, "/docs/cooking.txt", "Cooking",
            body="Machine learning can help with cooking too.",
        )
        await db.assign_document_to_collection(doc_in, col)

        results = await db.search_documents("machine learning", collection_id=col)
        titles = [r["title"] for r in results]
        assert "ML Paper" in titles
        assert "Cooking" not in titles

    @pytest.mark.asyncio
    async def test_search_without_collection_filter(self, db) -> None:
        """search_documents without collection_id should search all documents."""
        col = await db.create_collection("Col")
        doc_in = await _make_doc(
            db, "/docs/in.txt", "In Col",
            body="Quantum computing research paper.",
        )
        doc_out = await _make_doc(
            db, "/docs/out.txt", "Out Col",
            body="Quantum computing blog post.",
        )
        await db.assign_document_to_collection(doc_in, col)

        results = await db.search_documents("quantum computing")
        titles = [r["title"] for r in results]
        assert "In Col" in titles
        assert "Out Col" in titles


# ── 12. Pagination with collection_id filter ────────────────────


class TestPaginationWithCollectionFilter:
    @pytest.mark.asyncio
    async def test_paginate_within_collection(self, db) -> None:
        """list_documents_paginated with collection_id should paginate correctly."""
        col = await db.create_collection("My Col")
        for i in range(15):
            doc_id = await _make_doc(db, f"/docs/col_{i}.txt", f"Doc {i}")
            await db.assign_document_to_collection(doc_id, col)
        # Add some unassigned docs
        for i in range(5):
            await _make_doc(db, f"/docs/unassigned_{i}.txt", f"Unassigned {i}")

        result = await db.list_documents_paginated(
            page=1, per_page=10, collection_id=col,
        )
        assert result["total"] == 15
        assert len(result["documents"]) == 10
        assert result["total_pages"] == 2

        page2 = await db.list_documents_paginated(
            page=2, per_page=10, collection_id=col,
        )
        assert len(page2["documents"]) == 5

    @pytest.mark.asyncio
    async def test_paginate_unassigned(self, db) -> None:
        """list_documents_paginated with collection_id=0 should list unassigned."""
        col = await db.create_collection("Col")
        for i in range(3):
            doc_id = await _make_doc(db, f"/docs/in_col_{i}.txt", f"InCol {i}")
            await db.assign_document_to_collection(doc_id, col)
        for i in range(7):
            await _make_doc(db, f"/docs/free_{i}.txt", f"Free {i}")

        result = await db.list_documents_paginated(
            page=1, per_page=50, collection_id=0,
        )
        assert result["total"] == 7
        for doc in result["documents"]:
            assert doc["collection_id"] is None

    @pytest.mark.asyncio
    async def test_list_documents_by_collection(self, db) -> None:
        """list_documents_by_collection should be a convenience wrapper."""
        col = await db.create_collection("Col")
        doc1 = await _make_doc(db, "/docs/a.txt", "A")
        doc2 = await _make_doc(db, "/docs/b.txt", "B")
        await db.assign_document_to_collection(doc1, col)
        await db.assign_document_to_collection(doc2, col)

        result = await db.list_documents_by_collection(col, page=1, per_page=10)
        assert result["total"] == 2
        assert len(result["documents"]) == 2


# ── 13. get_collection_path breadcrumbs ─────────────────────────


class TestCollectionPath:
    @pytest.mark.asyncio
    async def test_breadcrumb_chain(self, db) -> None:
        """get_collection_path should return root-first breadcrumb chain."""
        a = await db.create_collection("A")
        b = await db.create_collection("B", parent_id=a)
        c = await db.create_collection("C", parent_id=b)

        path = await db.get_collection_path(c)
        assert len(path) == 3
        assert [p["name"] for p in path] == ["A", "B", "C"]

    @pytest.mark.asyncio
    async def test_breadcrumb_root_collection(self, db) -> None:
        """get_collection_path on a root collection returns just itself."""
        root = await db.create_collection("Root")
        path = await db.get_collection_path(root)
        assert len(path) == 1
        assert path[0]["id"] == root

    @pytest.mark.asyncio
    async def test_breadcrumb_nonexistent(self, db) -> None:
        """get_collection_path on a non-existent id returns empty list."""
        path = await db.get_collection_path(99999)
        assert path == []


# ── Extra: Document assignment and counts ───────────────────────


class TestDocumentAssignment:
    @pytest.mark.asyncio
    async def test_assign_document(self, db) -> None:
        """assign_document_to_collection should set collection_id."""
        col = await db.create_collection("Col")
        doc = await _make_doc(db, "/docs/assign.txt", "Assign Me")
        result = await db.assign_document_to_collection(doc, col)
        assert result is True
        d = await db.get_document(doc)
        assert d["collection_id"] == col

    @pytest.mark.asyncio
    async def test_assign_nonexistent_collection(self, db) -> None:
        """assign_document_to_collection should fail for non-existent collection."""
        doc = await _make_doc(db, "/docs/x.txt", "X")
        assert await db.assign_document_to_collection(doc, 99999) is False

    @pytest.mark.asyncio
    async def test_assign_nonexistent_document(self, db) -> None:
        """assign_document_to_collection should fail for non-existent doc."""
        col = await db.create_collection("Col")
        assert await db.assign_document_to_collection(99999, col) is False

    @pytest.mark.asyncio
    async def test_remove_document_from_collection(self, db) -> None:
        """remove_document_from_collection should set collection_id to NULL."""
        col = await db.create_collection("Col")
        doc = await _make_doc(db, "/docs/remove.txt", "Remove Me")
        await db.assign_document_to_collection(doc, col)
        assert await db.get_document_collection(doc) is not None

        result = await db.remove_document_from_collection(doc)
        assert result is True
        d = await db.get_document(doc)
        assert d["collection_id"] is None

    @pytest.mark.asyncio
    async def test_remove_already_unassigned(self, db) -> None:
        """remove_document_from_collection should return False if already unassigned."""
        doc = await _make_doc(db, "/docs/never_assigned.txt", "Never")
        assert await db.remove_document_from_collection(doc) is False

    @pytest.mark.asyncio
    async def test_reassign_document(self, db) -> None:
        """Reassigning a document to a new collection should work."""
        col1 = await db.create_collection("Col1")
        col2 = await db.create_collection("Col2")
        doc = await _make_doc(db, "/docs/reassign.txt", "Reassign")
        await db.assign_document_to_collection(doc, col1)
        await db.assign_document_to_collection(doc, col2)
        col = await db.get_document_collection(doc)
        assert col is not None
        assert col["id"] == col2

    @pytest.mark.asyncio
    async def test_get_document_collection(self, db) -> None:
        """get_document_collection should return the collection dict."""
        col = await db.create_collection("My Col", description="desc")
        doc = await _make_doc(db, "/docs/col_doc.txt", "CDoc")
        await db.assign_document_to_collection(doc, col)
        result = await db.get_document_collection(doc)
        assert result is not None
        assert result["id"] == col
        assert result["name"] == "My Col"
        assert result["description"] == "desc"


class TestCollectionCounts:
    @pytest.mark.asyncio
    async def test_counts_basic(self, db) -> None:
        """get_collection_counts should return correct doc counts per collection."""
        col1 = await db.create_collection("Col1")
        col2 = await db.create_collection("Col2")
        for i in range(3):
            doc = await _make_doc(db, f"/docs/c1_{i}.txt", f"C1-{i}")
            await db.assign_document_to_collection(doc, col1)
        for i in range(2):
            doc = await _make_doc(db, f"/docs/c2_{i}.txt", f"C2-{i}")
            await db.assign_document_to_collection(doc, col2)
        # Unassigned docs
        await _make_doc(db, "/docs/unassigned.txt", "U")

        counts = await db.get_collection_counts()
        assert counts[col1] == 3
        assert counts[col2] == 2
        # Unassigned docs should not appear
        assert len(counts) == 2

    @pytest.mark.asyncio
    async def test_counts_empty(self, db) -> None:
        """get_collection_counts on empty db should return empty dict."""
        assert await db.get_collection_counts() == {}

    @pytest.mark.asyncio
    async def test_counts_after_delete(self, db) -> None:
        """Counts should update after a collection is deleted."""
        col = await db.create_collection("Col")
        doc = await _make_doc(db, "/docs/d.txt", "D")
        await db.assign_document_to_collection(doc, col)
        assert (await db.get_collection_counts()).get(col) == 1

        await db.delete_collection(col)
        assert col not in (await db.get_collection_counts())
        # Document should still exist, unassigned
        d = await db.get_document(doc)
        assert d is not None
        assert d["collection_id"] is None
