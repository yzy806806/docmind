"""Tests for the multi-filter search feature.

Covers list_documents, list_documents_paginated, get_document_count,
and search_documents with combinations of:
  - FTS5 text query (search_documents only)
  - date_from / date_to (created_at range)
  - file_type (ext column)
  - tag (document_tags join)
  - source (existing, now composable with new filters)
  - collection_id (existing, now composable with new filters)
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Generator

import pytest


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    """Provide a temporary database file path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "test_docmind.db")


@pytest.fixture
async def db(tmp_db_path: str):
    """Create a connected SQLite Database instance."""
    from src.core.db_sqlite import Database

    database = Database(db_path=tmp_db_path)
    await database.connect()
    yield database
    await database.disconnect()


# ── Helper ───────────────────────────────────────────────────────


async def _make_doc(
    db, path: str, title: str, ext: str = ".txt", body: str = "",
    source_name: str = "test", source_type: str = "local",
    created_at: str | None = None,
) -> int:
    """Create a document and optionally override created_at."""
    doc_id = await db.save_document(
        path=path,
        source_type=source_type,
        source_name=source_name,
        title=title,
        ext=ext,
        mime_type="text/plain",
        body=body,
    )
    if created_at:
        async with db.connection() as conn:
            await conn.execute(
                "UPDATE documents SET created_at = ? WHERE id = ?",
                (created_at, doc_id),
            )
            await conn.commit()
    return doc_id


# ── list_documents: file_type filter ─────────────────────────────


class TestListDocumentsFileTypeFilter:
    @pytest.mark.asyncio
    async def test_filter_by_file_type_with_dot(self, db) -> None:
        """list_documents(file_type='.pdf') returns only PDF docs."""
        await _make_doc(db, "/docs/a.pdf", "PDF Doc", ext=".pdf")
        await _make_doc(db, "/docs/b.txt", "Text Doc", ext=".txt")

        docs = await db.list_documents(file_type=".pdf")
        assert len(docs) == 1
        assert docs[0]["title"] == "PDF Doc"

    @pytest.mark.asyncio
    async def test_filter_by_file_type_without_dot(self, db) -> None:
        """list_documents(file_type='pdf') normalises to '.pdf'."""
        await _make_doc(db, "/docs/a.pdf", "PDF Doc", ext=".pdf")
        await _make_doc(db, "/docs/b.txt", "Text Doc", ext=".txt")

        docs = await db.list_documents(file_type="pdf")
        assert len(docs) == 1
        assert docs[0]["ext"] == ".pdf"

    @pytest.mark.asyncio
    async def test_filter_by_file_type_no_match(self, db) -> None:
        """list_documents(file_type='.xyz') returns empty when no match."""
        await _make_doc(db, "/docs/a.txt", "Doc", ext=".txt")
        docs = await db.list_documents(file_type=".xyz")
        assert docs == []


# ── list_documents: date range filter ────────────────────────────


class TestListDocumentsDateFilter:
    @pytest.mark.asyncio
    async def test_filter_by_date_from(self, db) -> None:
        """list_documents(date_from=...) excludes older docs."""
        await _make_doc(db, "/docs/old.txt", "Old", created_at="2024-01-01 00:00:00")
        await _make_doc(db, "/docs/new.txt", "New", created_at="2026-06-01 00:00:00")

        docs = await db.list_documents(date_from="2026-01-01")
        assert len(docs) == 1
        assert docs[0]["title"] == "New"

    @pytest.mark.asyncio
    async def test_filter_by_date_to(self, db) -> None:
        """list_documents(date_to=...) excludes newer docs."""
        await _make_doc(db, "/docs/old.txt", "Old", created_at="2024-01-01 00:00:00")
        await _make_doc(db, "/docs/new.txt", "New", created_at="2026-06-01 00:00:00")

        docs = await db.list_documents(date_to="2025-01-01")
        assert len(docs) == 1
        assert docs[0]["title"] == "Old"

    @pytest.mark.asyncio
    async def test_filter_by_date_range(self, db) -> None:
        """list_documents(date_from + date_to) returns docs in range."""
        await _make_doc(db, "/docs/a.txt", "A", created_at="2024-01-01 00:00:00")
        await _make_doc(db, "/docs/b.txt", "B", created_at="2025-06-01 00:00:00")
        await _make_doc(db, "/docs/c.txt", "C", created_at="2026-06-01 00:00:00")

        docs = await db.list_documents(
            date_from="2025-01-01", date_to="2026-01-01"
        )
        assert len(docs) == 1
        assert docs[0]["title"] == "B"

    @pytest.mark.asyncio
    async def test_date_from_inclusive(self, db) -> None:
        """date_from is inclusive — a doc created exactly on that date is returned."""
        await _make_doc(db, "/docs/a.txt", "A", created_at="2025-06-01 12:00:00")
        docs = await db.list_documents(date_from="2025-06-01")
        assert len(docs) == 1

    @pytest.mark.asyncio
    async def test_date_to_inclusive(self, db) -> None:
        """date_to is inclusive — a doc created exactly on that date is returned."""
        await _make_doc(db, "/docs/a.txt", "A", created_at="2025-06-01 12:00:00")
        docs = await db.list_documents(date_to="2025-06-01")
        assert len(docs) == 1


# ── list_documents: tag filter ───────────────────────────────────


class TestListDocumentsTagFilter:
    @pytest.mark.asyncio
    async def test_filter_by_tag(self, db) -> None:
        """list_documents(tag=...) returns only docs with that tag."""
        d1 = await _make_doc(db, "/docs/a.txt", "A")
        d2 = await _make_doc(db, "/docs/b.txt", "B")
        await db.add_tag(d1, "important")
        await db.add_tag(d2, "trivial")

        docs = await db.list_documents(tag="important")
        assert len(docs) == 1
        assert docs[0]["title"] == "A"

    @pytest.mark.asyncio
    async def test_filter_by_tag_no_match(self, db) -> None:
        """list_documents(tag=...) returns empty when no doc has that tag."""
        await _make_doc(db, "/docs/a.txt", "A")
        docs = await db.list_documents(tag="nonexistent")
        assert docs == []


# ── list_documents: combined filters ─────────────────────────────


class TestListDocumentsCombinedFilters:
    @pytest.mark.asyncio
    async def test_file_type_and_date_from(self, db) -> None:
        """file_type + date_from combine with AND."""
        await _make_doc(db, "/docs/old.pdf", "Old PDF", ext=".pdf",
                        created_at="2024-01-01 00:00:00")
        await _make_doc(db, "/docs/new.pdf", "New PDF", ext=".pdf",
                        created_at="2026-06-01 00:00:00")
        await _make_doc(db, "/docs/new.txt", "New TXT", ext=".txt",
                        created_at="2026-06-01 00:00:00")

        docs = await db.list_documents(file_type=".pdf", date_from="2026-01-01")
        assert len(docs) == 1
        assert docs[0]["title"] == "New PDF"

    @pytest.mark.asyncio
    async def test_tag_and_file_type(self, db) -> None:
        """tag + file_type combine with AND."""
        d1 = await _make_doc(db, "/docs/a.pdf", "A", ext=".pdf")
        d2 = await _make_doc(db, "/docs/b.txt", "B", ext=".txt")
        await db.add_tag(d1, "important")
        await db.add_tag(d2, "important")

        docs = await db.list_documents(tag="important", file_type=".pdf")
        assert len(docs) == 1
        assert docs[0]["title"] == "A"

    @pytest.mark.asyncio
    async def test_source_and_date_range(self, db) -> None:
        """source + date range combine with AND."""
        await _make_doc(db, "/docs/a.txt", "A", source_name="api",
                        created_at="2025-06-01 00:00:00")
        await _make_doc(db, "/docs/b.txt", "B", source_name="local",
                        created_at="2025-06-01 00:00:00")
        await _make_doc(db, "/docs/c.txt", "C", source_name="api",
                        created_at="2024-01-01 00:00:00")

        docs = await db.list_documents(
            source="api", date_from="2025-01-01", date_to="2026-01-01"
        )
        assert len(docs) == 1
        assert docs[0]["title"] == "A"

    @pytest.mark.asyncio
    async def test_all_filters_combined(self, db) -> None:
        """All four new filters + source combine."""
        d1 = await _make_doc(db, "/docs/a.pdf", "A", ext=".pdf",
                             source_name="api",
                             created_at="2025-06-01 00:00:00")
        d2 = await _make_doc(db, "/docs/b.pdf", "B", ext=".pdf",
                             source_name="api",
                             created_at="2025-06-01 00:00:00")
        await _make_doc(db, "/docs/c.txt", "C", ext=".txt",
                        source_name="api",
                        created_at="2025-06-01 00:00:00")
        await db.add_tag(d1, "starred")

        docs = await db.list_documents(
            source="api",
            date_from="2025-01-01",
            date_to="2026-01-01",
            file_type=".pdf",
            tag="starred",
        )
        assert len(docs) == 1
        assert docs[0]["title"] == "A"

    @pytest.mark.asyncio
    async def test_no_filters_returns_all(self, db) -> None:
        """No filters returns all documents (backward compat)."""
        for i in range(5):
            await _make_doc(db, f"/docs/d{i}.txt", f"Doc {i}")
        docs = await db.list_documents()
        assert len(docs) == 5


# ── get_document_count with filters ──────────────────────────────


class TestGetDocumentCountWithFilters:
    @pytest.mark.asyncio
    async def test_count_with_file_type(self, db) -> None:
        """get_document_count(file_type=...) counts only matching docs."""
        await _make_doc(db, "/docs/a.pdf", "A", ext=".pdf")
        await _make_doc(db, "/docs/b.txt", "B", ext=".txt")
        await _make_doc(db, "/docs/c.pdf", "C", ext=".pdf")

        count = await db.get_document_count(file_type=".pdf")
        assert count == 2

    @pytest.mark.asyncio
    async def test_count_with_date_range(self, db) -> None:
        """get_document_count(date_from, date_to) counts in range."""
        await _make_doc(db, "/docs/a.txt", "A", created_at="2024-01-01 00:00:00")
        await _make_doc(db, "/docs/b.txt", "B", created_at="2025-06-01 00:00:00")
        await _make_doc(db, "/docs/c.txt", "C", created_at="2026-06-01 00:00:00")

        count = await db.get_document_count(
            date_from="2025-01-01", date_to="2026-01-01"
        )
        assert count == 1

    @pytest.mark.asyncio
    async def test_count_with_tag(self, db) -> None:
        """get_document_count(tag=...) counts tagged docs."""
        d1 = await _make_doc(db, "/docs/a.txt", "A")
        d2 = await _make_doc(db, "/docs/b.txt", "B")
        await db.add_tag(d1, "x")
        await db.add_tag(d2, "x")

        count = await db.get_document_count(tag="x")
        assert count == 2

    @pytest.mark.asyncio
    async def test_count_with_combined_filters(self, db) -> None:
        """get_document_count with multiple filters."""
        d1 = await _make_doc(db, "/docs/a.pdf", "A", ext=".pdf",
                             created_at="2025-06-01 00:00:00")
        await _make_doc(db, "/docs/b.pdf", "B", ext=".pdf",
                        created_at="2024-01-01 00:00:00")
        await _make_doc(db, "/docs/c.txt", "C", ext=".txt",
                        created_at="2025-06-01 00:00:00")
        await db.add_tag(d1, "starred")

        count = await db.get_document_count(
            file_type=".pdf", date_from="2025-01-01", tag="starred"
        )
        assert count == 1


# ── list_documents_paginated with filters ────────────────────────


class TestListDocumentsPaginatedWithFilters:
    @pytest.mark.asyncio
    async def test_paginated_with_file_type(self, db) -> None:
        """list_documents_paginated with file_type filter."""
        for i in range(10):
            await _make_doc(db, f"/docs/a{i}.pdf", f"PDF {i}", ext=".pdf")
        for i in range(5):
            await _make_doc(db, f"/docs/b{i}.txt", f"TXT {i}", ext=".txt")

        result = await db.list_documents_paginated(
            page=1, per_page=5, file_type=".pdf"
        )
        assert result["total"] == 10
        assert len(result["documents"]) == 5
        assert all(d["ext"] == ".pdf" for d in result["documents"])

    @pytest.mark.asyncio
    async def test_paginated_with_tag(self, db) -> None:
        """list_documents_paginated with tag filter."""
        for i in range(8):
            doc_id = await _make_doc(db, f"/docs/d{i}.txt", f"Doc {i}")
            if i < 3:
                await db.add_tag(doc_id, "special")

        result = await db.list_documents_paginated(
            page=1, per_page=10, tag="special"
        )
        assert result["total"] == 3
        assert len(result["documents"]) == 3

    @pytest.mark.asyncio
    async def test_paginated_with_date_range(self, db) -> None:
        """list_documents_paginated with date range filter."""
        for i in range(5):
            await _make_doc(db, f"/docs/old{i}.txt", f"Old {i}",
                            created_at="2024-01-01 00:00:00")
        for i in range(5):
            await _make_doc(db, f"/docs/new{i}.txt", f"New {i}",
                            created_at="2026-06-01 00:00:00")

        result = await db.list_documents_paginated(
            page=1, per_page=10, date_from="2026-01-01"
        )
        assert result["total"] == 5
        assert all("New" in d["title"] for d in result["documents"])


# ── search_documents with filters ────────────────────────────────


class TestSearchDocumentsWithFilters:
    @pytest.mark.asyncio
    async def test_search_with_file_type(self, db) -> None:
        """search_documents(file_type=...) narrows FTS results by ext."""
        await _make_doc(db, "/docs/ml.pdf", "ML Guide", ext=".pdf",
                        body="machine learning concepts")
        await _make_doc(db, "/docs/ml.txt", "ML Notes", ext=".txt",
                        body="machine learning notes")

        results = await db.search_documents("machine learning", file_type=".pdf")
        assert len(results) == 1
        assert results[0]["title"] == "ML Guide"

    @pytest.mark.asyncio
    async def test_search_with_date_range(self, db) -> None:
        """search_documents(date_from, date_to) narrows FTS results by date."""
        await _make_doc(db, "/docs/old.txt", "Old ML", ext=".txt",
                        body="machine learning old",
                        created_at="2024-01-01 00:00:00")
        await _make_doc(db, "/docs/new.txt", "New ML", ext=".txt",
                        body="machine learning new",
                        created_at="2026-06-01 00:00:00")

        results = await db.search_documents(
            "machine learning", date_from="2026-01-01"
        )
        assert len(results) == 1
        assert results[0]["title"] == "New ML"

    @pytest.mark.asyncio
    async def test_search_with_tag(self, db) -> None:
        """search_documents(tag=...) narrows FTS results by tag."""
        d1 = await _make_doc(db, "/docs/a.txt", "ML Guide A", ext=".txt",
                             body="machine learning guide a")
        d2 = await _make_doc(db, "/docs/b.txt", "ML Guide B", ext=".txt",
                             body="machine learning guide b")
        await db.add_tag(d1, "starred")

        results = await db.search_documents("machine learning", tag="starred")
        assert len(results) == 1
        assert results[0]["title"] == "ML Guide A"

    @pytest.mark.asyncio
    async def test_search_with_all_filters(self, db) -> None:
        """search_documents with FTS + date + type + tag combined."""
        d1 = await _make_doc(db, "/docs/a.pdf", "ML Alpha", ext=".pdf",
                             body="machine learning alpha",
                             created_at="2025-06-01 00:00:00")
        await _make_doc(db, "/docs/b.txt", "ML Beta", ext=".txt",
                        body="machine learning beta",
                        created_at="2025-06-01 00:00:00")
        await _make_doc(db, "/docs/c.pdf", "ML Gamma", ext=".pdf",
                        body="machine learning gamma",
                        created_at="2024-01-01 00:00:00")
        await db.add_tag(d1, "starred")

        results = await db.search_documents(
            "machine learning",
            file_type=".pdf",
            date_from="2025-01-01",
            date_to="2026-01-01",
            tag="starred",
        )
        assert len(results) == 1
        assert results[0]["title"] == "ML Alpha"

    @pytest.mark.asyncio
    async def test_search_without_filters_still_works(self, db) -> None:
        """search_documents with no new filters works as before."""
        await _make_doc(db, "/docs/a.txt", "ML", ext=".txt",
                        body="machine learning")
        results = await db.search_documents("machine learning")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_with_collection_and_file_type(self, db) -> None:
        """search_documents with collection_id + file_type."""
        d1 = await _make_doc(db, "/docs/a.pdf", "ML A", ext=".pdf",
                             body="machine learning a")
        await _make_doc(db, "/docs/b.pdf", "ML B", ext=".pdf",
                        body="machine learning b")
        col = await db.create_collection("TestCol")
        await db.assign_document_to_collection(d1, col)

        results = await db.search_documents(
            "machine learning", collection_id=col, file_type=".pdf"
        )
        assert len(results) == 1
        assert results[0]["title"] == "ML A"


# ── _build_filter_clause unit test ───────────────────────────────


class TestBuildFilterClause:
    @pytest.mark.asyncio
    async def test_no_filters_returns_empty(self, db) -> None:
        """_build_filter_clause with no args returns empty clause."""
        where, params = db._build_filter_clause()
        assert where == ""
        assert params == []

    @pytest.mark.asyncio
    async def test_single_filter(self, db) -> None:
        """_build_filter_clause with one filter."""
        where, params = db._build_filter_clause(file_type=".pdf")
        assert "ext = ?" in where
        assert ".pdf" in params

    @pytest.mark.asyncio
    async def test_multiple_filters_joined_with_and(self, db) -> None:
        """_build_filter_clause joins conditions with AND."""
        where, params = db._build_filter_clause(
            file_type=".pdf", date_from="2025-01-01", tag="x"
        )
        assert "AND" in where
        assert where.count("AND") == 2
        assert len(params) == 3

    @pytest.mark.asyncio
    async def test_file_type_normalization(self, db) -> None:
        """_build_filter_clause normalises file_type to have leading dot."""
        where, params = db._build_filter_clause(file_type="pdf")
        assert params[0] == ".pdf"

    @pytest.mark.asyncio
    async def test_collection_id_zero_is_null(self, db) -> None:
        """_build_filter_clause with collection_id=0 uses IS NULL."""
        where, params = db._build_filter_clause(collection_id=0)
        assert "IS NULL" in where
        assert params == []

    @pytest.mark.asyncio
    async def test_all_six_params(self, db) -> None:
        """_build_filter_clause with all six filter parameters at once."""
        where, params = db._build_filter_clause(
            source="api", collection_id=1,
            date_from="2025-01-01", date_to="2025-12-31",
            file_type=".pdf", tag="important",
        )
        assert "AND" in where
        # 6 conditions, so 5 ANDs
        assert where.count("AND") == 5
        # source=2, collection_id=1, date_from=1, date_to=1, file_type=1, tag=1 = 7
        assert len(params) == 7

    @pytest.mark.asyncio
    async def test_date_to_with_time_component_not_extended(self, db) -> None:
        """date_to with explicit time is NOT auto-extended."""
        where, params = db._build_filter_clause(
            date_to="2025-06-15 14:30:00"
        )
        assert params == ["2025-06-15 14:30:00"]

    @pytest.mark.asyncio
    async def test_date_to_iso8601_with_t_preserved(self, db) -> None:
        """date_to with ISO 8601 'T' separator is preserved as-is."""
        where, params = db._build_filter_clause(
            date_to="2025-06-15T18:00:00"
        )
        assert params == ["2025-06-15T18:00:00"]

    @pytest.mark.asyncio
    async def test_date_to_date_only_auto_extended(self, db) -> None:
        """date_to as pure date (10 chars) gets ' 23:59:59' appended."""
        where, params = db._build_filter_clause(date_to="2025-06-15")
        assert params == ["2025-06-15 23:59:59"]

    @pytest.mark.asyncio
    async def test_file_type_with_whitespace_trimmed(self, db) -> None:
        """_build_filter_clause trims whitespace from file_type."""
        where, params = db._build_filter_clause(file_type="  pdf  ")
        assert params == [".pdf"]

    @pytest.mark.asyncio
    async def test_source_uses_two_params(self, db) -> None:
        """source adds two bound parameters (name OR type)."""
        where, params = db._build_filter_clause(source="api")
        assert params == ["api", "api"]
        assert "source_name = ?" in where
        assert "source_type = ?" in where

    @pytest.mark.asyncio
    async def test_collection_id_positive(self, db) -> None:
        """_build_filter_clause with positive collection_id uses = ?."""
        where, params = db._build_filter_clause(collection_id=42)
        assert "collection_id = ?" in where
        assert params == [42]

    @pytest.mark.asyncio
    async def test_tag_subquery_present(self, db) -> None:
        """tag filter includes subquery on document_tags."""
        where, params = db._build_filter_clause(tag="starred")
        assert "id IN (SELECT doc_id FROM document_tags WHERE tag = ?)" in where
        assert params == ["starred"]

    @pytest.mark.asyncio
    async def test_file_type_already_dotted_unchanged(self, db) -> None:
        """_build_filter_clause preserves dot if already present."""
        where, params = db._build_filter_clause(file_type=".csv")
        assert params == [".csv"]


# ── Combined filter intersections — deep coverage ─────────────

class TestCombinedFilterIntersections:
    """Deep coverage of multi-filter intersection scenarios across
    list_documents, get_document_count, and search_documents."""

    # ── list_documents 3-way intersections ────────────────────

    @pytest.mark.asyncio
    async def test_tag_and_date_range(self, db) -> None:
        """tag + date_range intersection: only the tagged doc in range."""
        d1 = await _make_doc(db, "/docs/a.txt", "A",
                             created_at="2025-06-01 00:00:00")
        d2 = await _make_doc(db, "/docs/b.txt", "B",
                             created_at="2024-01-01 00:00:00")
        d3 = await _make_doc(db, "/docs/c.txt", "C",
                             created_at="2025-08-01 00:00:00")
        await db.add_tag(d1, "x")
        await db.add_tag(d2, "x")
        await db.add_tag(d3, "x")

        docs = await db.list_documents(
            tag="x", date_from="2025-01-01", date_to="2025-07-01"
        )
        assert len(docs) == 1
        assert docs[0]["title"] == "A"

    @pytest.mark.asyncio
    async def test_tag_and_date_and_type(self, db) -> None:
        """tag + date + type: all three must match."""
        d1 = await _make_doc(db, "/docs/a.pdf", "A", ext=".pdf",
                             created_at="2025-06-01 00:00:00")
        d2 = await _make_doc(db, "/docs/b.pdf", "B", ext=".pdf",
                             created_at="2025-06-01 00:00:00")
        await _make_doc(db, "/docs/c.txt", "C", ext=".txt",
                        created_at="2025-06-01 00:00:00")
        await _make_doc(db, "/docs/d.pdf", "D", ext=".pdf",
                        created_at="2024-01-01 00:00:00")
        await db.add_tag(d1, "starred")

        docs = await db.list_documents(
            tag="starred", date_from="2025-01-01",
            date_to="2025-12-31", file_type=".pdf"
        )
        assert len(docs) == 1
        assert docs[0]["title"] == "A"

    @pytest.mark.asyncio
    async def test_source_tag_date_type(self, db) -> None:
        """source + tag + date + type: four filter intersection."""
        d1 = await _make_doc(db, "/docs/a.pdf", "A", ext=".pdf",
                             source_name="api",
                             created_at="2025-06-01 00:00:00")
        await _make_doc(db, "/docs/b.pdf", "B", ext=".pdf",
                        source_name="local",
                        created_at="2025-06-01 00:00:00")
        await db.add_tag(d1, "starred")

        docs = await db.list_documents(
            source="api", tag="starred",
            date_from="2025-01-01", file_type=".pdf"
        )
        assert len(docs) == 1
        assert docs[0]["title"] == "A"

    @pytest.mark.asyncio
    async def test_collection_with_new_filters(self, db) -> None:
        """collection_id combined with date + type filters."""
        d1 = await _make_doc(db, "/docs/a.pdf", "A", ext=".pdf",
                             created_at="2025-06-01 00:00:00")
        d2 = await _make_doc(db, "/docs/b.pdf", "B", ext=".pdf",
                             created_at="2025-06-01 00:00:00")
        d3 = await _make_doc(db, "/docs/c.txt", "C", ext=".txt",
                             created_at="2025-06-01 00:00:00")
        col = await db.create_collection("Test")
        await db.assign_document_to_collection(d1, col)
        await db.assign_document_to_collection(d2, col)

        docs = await db.list_documents(
            collection_id=col, file_type=".pdf",
            date_from="2025-01-01"
        )
        assert len(docs) == 2
        titles = {d["title"] for d in docs}
        assert titles == {"A", "B"}

    @pytest.mark.asyncio
    async def test_collection_unassigned_with_filters(self, db) -> None:
        """collection_id=0 (unassigned) combined with type filter."""
        d1 = await _make_doc(db, "/docs/a.pdf", "A", ext=".pdf")
        d2 = await _make_doc(db, "/docs/b.txt", "B", ext=".txt")
        col = await db.create_collection("Test")
        await db.assign_document_to_collection(d1, col)

        docs = await db.list_documents(collection_id=0, file_type=".pdf")
        # d1 is in collection, so only d2's .txt should be excluded.
        # Actually d1 is .pdf but assigned, d2 is .txt. No .pdf unassigned.
        assert len(docs) == 0

        docs2 = await db.list_documents(collection_id=0, file_type=".txt")
        assert len(docs2) == 1
        assert docs2[0]["title"] == "B"

    # ── Empty result sets for intersections ─────────────────────

    @pytest.mark.asyncio
    async def test_combined_filters_no_match(self, db) -> None:
        """Multiple filters that each match some docs, but no doc matches all."""
        d1 = await _make_doc(db, "/docs/a.pdf", "A", ext=".pdf",
                             created_at="2024-01-01 00:00:00")
        d2 = await _make_doc(db, "/docs/b.txt", "B", ext=".txt",
                             created_at="2026-06-01 00:00:00")
        await db.add_tag(d1, "important")

        # tag=important is on d1 (old, .pdf)
        # date_from=2026 excludes d1
        # file_type=.txt excludes d1
        docs = await db.list_documents(
            tag="important", date_from="2026-01-01", file_type=".txt"
        )
        assert docs == []

    @pytest.mark.asyncio
    async def test_date_range_empty_intersection(self, db) -> None:
        """date_from after date_to produces empty set."""
        await _make_doc(db, "/docs/a.txt", "A",
                        created_at="2025-06-01 00:00:00")
        docs = await db.list_documents(
            date_from="2025-12-01", date_to="2025-01-01"
        )
        assert docs == []

    @pytest.mark.asyncio
    async def test_tag_match_but_date_excludes(self, db) -> None:
        """tag matches but date range filters it out."""
        d1 = await _make_doc(db, "/docs/a.txt", "A",
                             created_at="2024-01-01 00:00:00")
        await db.add_tag(d1, "old")
        docs = await db.list_documents(
            tag="old", date_from="2026-01-01"
        )
        assert docs == []

    @pytest.mark.asyncio
    async def test_date_match_but_tag_excludes(self, db) -> None:
        """date range matches but tag filters it out."""
        await _make_doc(db, "/docs/a.txt", "A",
                        created_at="2025-06-01 00:00:00")
        docs = await db.list_documents(
            tag="nonexistent", date_from="2025-01-01"
        )
        assert docs == []

    # ── get_document_count intersections ───────────────────────

    @pytest.mark.asyncio
    async def test_count_tag_and_date(self, db) -> None:
        """get_document_count with tag + date range."""
        d1 = await _make_doc(db, "/docs/a.txt", "A",
                             created_at="2025-06-01 00:00:00")
        d2 = await _make_doc(db, "/docs/b.txt", "B",
                             created_at="2024-01-01 00:00:00")
        await db.add_tag(d1, "x")
        await db.add_tag(d2, "x")

        count = await db.get_document_count(
            tag="x", date_from="2025-01-01"
        )
        assert count == 1

    @pytest.mark.asyncio
    async def test_count_all_five_filters(self, db) -> None:
        """get_document_count with source + collection + date + type + tag."""
        d1 = await _make_doc(db, "/docs/a.pdf", "A", ext=".pdf",
                             source_name="api",
                             created_at="2025-06-01 00:00:00")
        await _make_doc(db, "/docs/b.pdf", "B", ext=".pdf",
                        source_name="local",
                        created_at="2025-06-01 00:00:00")
        await db.add_tag(d1, "starred")
        col = await db.create_collection("Test")
        await db.assign_document_to_collection(d1, col)

        count = await db.get_document_count(
            source="api", collection_id=col,
            date_from="2025-01-01", file_type=".pdf", tag="starred",
        )
        assert count == 1

    @pytest.mark.asyncio
    async def test_count_no_match_returns_zero(self, db) -> None:
        """get_document_count returns 0 when no doc matches all filters."""
        await _make_doc(db, "/docs/a.txt", "A")
        count = await db.get_document_count(
            tag="nonexistent", file_type=".pdf"
        )
        assert count == 0

    @pytest.mark.asyncio
    async def test_count_with_collection_and_date(self, db) -> None:
        """get_document_count with collection_id + date filters."""
        d1 = await _make_doc(db, "/docs/a.txt", "A",
                             created_at="2025-06-01 00:00:00")
        d2 = await _make_doc(db, "/docs/b.txt", "B",
                             created_at="2024-01-01 00:00:00")
        col = await db.create_collection("Test")
        await db.assign_document_to_collection(d1, col)
        await db.assign_document_to_collection(d2, col)

        count = await db.get_document_count(
            collection_id=col, date_from="2025-01-01"
        )
        assert count == 1

    @pytest.mark.asyncio
    async def test_count_unassigned_with_filters(self, db) -> None:
        """get_document_count with collection_id=0 + filters."""
        d1 = await _make_doc(db, "/docs/a.pdf", "A", ext=".pdf")
        await _make_doc(db, "/docs/b.txt", "B", ext=".txt")
        col = await db.create_collection("Test")
        await db.assign_document_to_collection(d1, col)

        count = await db.get_document_count(
            collection_id=0, file_type=".pdf"
        )
        assert count == 0


# ── search_documents deep edge cases ─────────────────────────

class TestSearchDocumentsDeepEdgeCases:
    """Edge cases for FTS5 search with filter combinations."""

    @pytest.mark.asyncio
    async def test_search_with_date_to_only(self, db) -> None:
        """search_documents with date_to (not date_from)."""
        await _make_doc(db, "/docs/old.txt", "Old ML",
                        body="machine learning old",
                        created_at="2024-01-01 00:00:00")
        await _make_doc(db, "/docs/new.txt", "New ML",
                        body="machine learning new",
                        created_at="2026-06-01 00:00:00")

        results = await db.search_documents(
            "machine learning", date_to="2025-01-01"
        )
        assert len(results) == 1
        assert results[0]["title"] == "Old ML"

    @pytest.mark.asyncio
    async def test_search_with_type_and_tag_no_fts_match(self, db) -> None:
        """search_documents with type + tag, but no FTS match exists."""
        d1 = await _make_doc(db, "/docs/a.pdf", "A", ext=".pdf",
                             body="some content")
        await db.add_tag(d1, "starred")

        results = await db.search_documents(
            "machine learning", file_type=".pdf", tag="starred"
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_search_all_filters_no_results(self, db) -> None:
        """search_documents with all filters but no doc matches all."""
        d1 = await _make_doc(db, "/docs/a.pdf", "A", ext=".pdf",
                             body="machine learning alpha",
                             created_at="2025-06-01 00:00:00")
        d2 = await _make_doc(db, "/docs/b.txt", "B", ext=".txt",
                             body="machine learning beta",
                             created_at="2025-06-01 00:00:00")
        await db.add_tag(d1, "starred")
        await db.add_tag(d2, "starred")

        # type=.txt + tag=starred matches d2, but date_from=2026 excludes it
        results = await db.search_documents(
            "machine learning",
            file_type=".txt", tag="starred", date_from="2026-01-01"
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_search_with_tag_and_date_range(self, db) -> None:
        """search_documents with FTS + tag + date_range."""
        d1 = await _make_doc(db, "/docs/a.txt", "A",
                             body="machine learning alpha",
                             created_at="2025-06-01 00:00:00")
        d2 = await _make_doc(db, "/docs/b.txt", "B",
                             body="machine learning beta",
                             created_at="2024-01-01 00:00:00")
        await db.add_tag(d1, "starred")
        await db.add_tag(d2, "starred")

        results = await db.search_documents(
            "machine learning", tag="starred", date_from="2025-01-01"
        )
        assert len(results) == 1
        assert results[0]["title"] == "A"

    @pytest.mark.asyncio
    async def test_search_with_tag_and_type_and_date(self, db) -> None:
        """search_documents with FTS + tag + type + date (4 filters)."""
        d1 = await _make_doc(db, "/docs/a.pdf", "A", ext=".pdf",
                             body="machine learning alpha",
                             created_at="2025-06-01 00:00:00")
        await _make_doc(db, "/docs/b.pdf", "B", ext=".pdf",
                        body="machine learning beta",
                        created_at="2025-06-01 00:00:00")
        await _make_doc(db, "/docs/c.txt", "C", ext=".txt",
                        body="machine learning gamma",
                        created_at="2025-06-01 00:00:00")
        await db.add_tag(d1, "starred")

        results = await db.search_documents(
            "machine learning",
            tag="starred", file_type=".pdf", date_from="2025-01-01"
        )
        assert len(results) == 1
        assert results[0]["title"] == "A"

    @pytest.mark.asyncio
    async def test_search_with_collection_and_tag_and_type(self, db) -> None:
        """search_documents with collection_id + tag + type."""
        d1 = await _make_doc(db, "/docs/a.pdf", "A", ext=".pdf",
                             body="machine learning alpha")
        d2 = await _make_doc(db, "/docs/b.pdf", "B", ext=".pdf",
                             body="machine learning beta")
        await db.add_tag(d1, "starred")
        col = await db.create_collection("Test")
        await db.assign_document_to_collection(d1, col)
        await db.assign_document_to_collection(d2, col)

        results = await db.search_documents(
            "machine learning",
            collection_id=col, tag="starred", file_type=".pdf"
        )
        assert len(results) == 1
        assert results[0]["title"] == "A"

    @pytest.mark.asyncio
    async def test_search_empty_query_returns_empty(self, db) -> None:
        """search_documents with empty/whitespace query returns []."""
        await _make_doc(db, "/docs/a.txt", "A", body="some content")
        results = await db.search_documents("")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_special_characters_sanitized(self, db) -> None:
        """search_documents strips special FTS5 chars from query."""
        await _make_doc(db, "/docs/a.txt", "A", body="machine learning")
        results = await db.search_documents("(machine) OR learning*")
        # Should still match via sanitized tokens
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_very_short_terms_filtered(self, db) -> None:
        """search_documents filters tokens shorter than 2 chars."""
        await _make_doc(db, "/docs/a.txt", "A", body="a machine learning b")
        # "a" and "b" should be filtered out, only "machine" and "learning" remain
        results = await db.search_documents("a b machine learning")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_with_limit_respected(self, db) -> None:
        """search_documents respects the limit parameter."""
        for i in range(10):
            await _make_doc(db, f"/docs/d{i}.txt", f"Doc {i}",
                            body="machine learning common text")

        results = await db.search_documents("machine learning", limit=3)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_search_fts_ranks_by_relevance(self, db) -> None:
        """search_documents returns results ranked by BM25 relevance."""
        # doc with title+body match should rank higher than body-only
        d1 = await _make_doc(db, "/docs/a.txt", "Machine Learning Guide",
                             body="irrelevant text here")
        d2 = await _make_doc(db, "/docs/b.txt", "Other Doc",
                             body="machine learning is the topic of this text")

        results = await db.search_documents("machine learning")
        assert len(results) >= 2
        # d1 has "machine learning" in title (weight A=10), d2 only in body (D=1)
        assert results[0]["id"] == d1


# ── list_documents_paginated deep combined filters ────────────

class TestPaginatedDeepCombinedFilters:
    """Paginated queries with complex filter combinations."""

    @pytest.mark.asyncio
    async def test_paginated_tag_and_date(self, db) -> None:
        """Paginated with tag + date range combined."""
        for i in range(5):
            d = await _make_doc(db, f"/docs/new{i}.txt", f"New {i}",
                                created_at="2026-06-01 00:00:00")
            if i < 2:
                await db.add_tag(d, "special")

        result = await db.list_documents_paginated(
            page=1, per_page=10,
            tag="special", date_from="2026-01-01"
        )
        assert result["total"] == 2
        assert len(result["documents"]) == 2

    @pytest.mark.asyncio
    async def test_paginated_type_and_tag(self, db) -> None:
        """Paginated with file_type + tag combined."""
        d1 = await _make_doc(db, "/docs/a.pdf", "A", ext=".pdf")
        d2 = await _make_doc(db, "/docs/b.pdf", "B", ext=".pdf")
        d3 = await _make_doc(db, "/docs/c.txt", "C", ext=".txt")
        await db.add_tag(d1, "starred")
        await db.add_tag(d2, "starred")
        await db.add_tag(d3, "starred")

        result = await db.list_documents_paginated(
            page=1, per_page=10,
            tag="starred", file_type=".pdf"
        )
        assert result["total"] == 2
        assert all(d["ext"] == ".pdf" for d in result["documents"])

    @pytest.mark.asyncio
    async def test_paginated_second_page_with_filters(self, db) -> None:
        """Paginated page 2 with filters — offset works correctly."""
        for i in range(15):
            await _make_doc(db, f"/docs/pdf{i}.pdf", f"PDF {i}",
                            ext=".pdf")

        page1 = await db.list_documents_paginated(
            page=1, per_page=5, file_type=".pdf"
        )
        page2 = await db.list_documents_paginated(
            page=2, per_page=5, file_type=".pdf"
        )
        assert page1["total"] == 15
        assert page1["page"] == 1
        assert len(page1["documents"]) == 5
        assert page2["page"] == 2
        assert len(page2["documents"]) == 5
        # Page 1 and page 2 should have different docs
        p1_ids = {d["id"] for d in page1["documents"]}
        p2_ids = {d["id"] for d in page2["documents"]}
        assert p1_ids.isdisjoint(p2_ids)

    @pytest.mark.asyncio
    async def test_paginated_last_page_partial(self, db) -> None:
        """Paginated last page with fewer items than per_page."""
        for i in range(7):
            await _make_doc(db, f"/docs/d{i}.txt", f"Doc {i}")

        result = await db.list_documents_paginated(
            page=2, per_page=5
        )
        assert result["total"] == 7
        assert result["page"] == 2
        assert result["total_pages"] == 2
        assert len(result["documents"]) == 2

    @pytest.mark.asyncio
    async def test_paginated_beyond_last_page_empty(self, db) -> None:
        """Paginated beyond total pages returns empty list."""
        for i in range(3):
            await _make_doc(db, f"/docs/d{i}.txt", f"Doc {i}")

        result = await db.list_documents_paginated(
            page=5, per_page=10
        )
        assert result["total"] == 3
        assert result["documents"] == []

    @pytest.mark.asyncio
    async def test_paginated_source_and_date(self, db) -> None:
        """Paginated with source + date combined."""
        await _make_doc(db, "/docs/a.txt", "A", source_name="api",
                        created_at="2025-06-01 00:00:00")
        await _make_doc(db, "/docs/b.txt", "B", source_name="local",
                        created_at="2025-06-01 00:00:00")

        result = await db.list_documents_paginated(
            page=1, per_page=10,
            source="api", date_from="2025-01-01"
        )
        assert result["total"] == 1
        assert result["documents"][0]["title"] == "A"

    @pytest.mark.asyncio
    async def test_paginated_all_five_filters(self, db) -> None:
        """Paginated with source + date + type + tag + collection."""
        d1 = await _make_doc(db, "/docs/a.pdf", "A", ext=".pdf",
                             source_name="api",
                             created_at="2025-06-01 00:00:00")
        await _make_doc(db, "/docs/b.pdf", "B", ext=".pdf",
                        source_name="api",
                        created_at="2025-06-01 00:00:00")
        await db.add_tag(d1, "starred")
        col = await db.create_collection("Test")
        await db.assign_document_to_collection(d1, col)

        result = await db.list_documents_paginated(
            page=1, per_page=10,
            source="api", collection_id=col,
            date_from="2025-01-01", file_type=".pdf", tag="starred",
        )
        assert result["total"] == 1
        assert result["documents"][0]["title"] == "A"
