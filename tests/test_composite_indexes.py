"""Tests for composite index optimization on the documents table.

Verifies via EXPLAIN QUERY PLAN that SQLite's query planner selects
the composite indexes added in Phase 8 for the common filter
combinations in _build_filter_clause(), eliminating full table scans.

Each test:
1. Inserts enough rows that the planner prefers an index scan over
   a full-table scan.
2. Runs EXPLAIN QUERY PLAN on the same SQL that list_documents /
   get_document_count would produce.
3. Asserts that at least one step references a covering index
   (not "SCAN" on the documents table).

See: motion-23756025f605, action item 1/7.
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
        yield str(Path(tmpdir) / "test_indexes.db")


@pytest.fixture
async def db(tmp_db_path: str):
    """Create a connected SQLite Database instance."""
    from src.core.db_sqlite import Database

    database = Database(db_path=tmp_db_path)
    await database.connect()
    yield database
    await database.disconnect()


# ── Helpers ──────────────────────────────────────────────────────


async def _make_doc(
    db, path: str, title: str, ext: str = ".txt", body: str = "",
    source_name: str = "test", source_type: str = "local",
    created_at: str | None = None, collection_id: int | None = None,
    document_type: str = "other", status: str = "indexed",
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
        status=status,
        document_type=document_type,
    )
    if created_at:
        async with db.connection() as conn:
            await conn.execute(
                "UPDATE documents SET created_at = ? WHERE id = ?",
                (created_at, doc_id),
            )
            await conn.commit()
    if collection_id is not None:
        await db.assign_document_to_collection(doc_id, collection_id)
    return doc_id


async def _seed_db(db, n: int = 50) -> None:
    """Seed the database with enough rows to make the planner prefer indexes.

    SQLite's planner may choose a full scan for very small tables because
    reading a handful of pages sequentially is cheaper than random index
    lookups. We insert 50 rows spread across multiple collections,
    extensions, statuses, and dates so that index selection is the
    clearly optimal strategy.
    """
    # Create collections 1, 2, 3 so assign_document_to_collection succeeds.
    for name in ("Col A", "Col B", "Col C"):
        await db.create_collection(name)

    exts = [".pdf", ".docx", ".txt", ".html", ".md"]
    statuses = ["pending", "indexed", "summarized", "error"]
    doc_types = ["text", "spreadsheet", "presentation", "other"]
    source_types = ["local", "api", "email"]

    for i in range(n):
        await _make_doc(
            db,
            path=f"/docs/doc_{i:03d}",
            title=f"Document {i}",
            ext=exts[i % len(exts)],
            source_name=f"src_{i % 3}",
            source_type=source_types[i % len(source_types)],
            created_at=f"2025-{(i % 12) + 1:02d}-{(i % 28) + 1:02d} 10:00:00",
            collection_id=(i % 4) if i % 4 != 0 else None,
            document_type=doc_types[i % len(doc_types)],
            status=statuses[i % len(statuses)],
        )


async def _explain(db, sql: str, params: list | None = None) -> list[str]:
    """Run EXPLAIN QUERY PLAN and return the plan lines as text."""
    if params is None:
        params = []
    async with db.connection() as conn:
        cursor = await conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)
        rows = await cursor.fetchall()
    # Each row: (id, parent, notused, detail)
    return [row[3] for row in rows]


def _uses_index(plan_lines: list[str], index_name: str) -> bool:
    """Check whether any plan line references the given index."""
    return any(index_name in line for line in plan_lines)


def _uses_any_index(plan_lines: list[str], index_names: list[str]) -> bool:
    """Check whether any plan line references any of the given indexes."""
    return any(name in line for line in plan_lines for name in index_names)


def _is_full_table_scan(plan_lines: list[str]) -> bool:
    """Check whether the plan is a full table scan on documents.

    A full scan shows as 'SCAN documents' or 'SCAN TABLE documents'
    without any index reference.
    """
    for line in plan_lines:
        low = line.lower()
        if "scan" in low and "documents" in low and "index" not in low:
            return True
    return False


# ── Index existence ──────────────────────────────────────────────


class TestCompositeIndexesExist:
    """Verify that all composite indexes are created by migrate()."""

    EXPECTED_INDEXES = [
        "idx_documents_collection_created",
        "idx_documents_ext_created",
        "idx_documents_status_created",
        "idx_documents_source_type_created",
        "idx_documents_doc_type_created",
        "idx_documents_created_at",
    ]

    @pytest.mark.asyncio
    async def test_all_composite_indexes_exist(self, db) -> None:
        """All composite indexes should exist after connect/migrate."""
        async with db.connection() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='documents'"
            )
            rows = await cursor.fetchall()
        index_names = {row[0] for row in rows}
        for idx in self.EXPECTED_INDEXES:
            assert idx in index_names, f"Missing index: {idx}"


# ── EXPLAIN QUERY PLAN: single-filter queries ───────────────────


class TestSingleFilterIndexUsage:
    """Verify that single-filter queries use a composite index
    rather than scanning the full documents table."""

    @pytest.mark.asyncio
    async def test_collection_id_filter_uses_index(self, db) -> None:
        """collection_id = ? should use idx_documents_collection_created."""
        await _seed_db(db)
        where, params = db._build_filter_clause(collection_id=1)
        sql = f"SELECT * FROM documents {where} ORDER BY created_at DESC LIMIT 20 OFFSET 0"
        plan = await _explain(db, sql, params)
        assert _uses_index(plan, "idx_documents_collection_created")
        assert not _is_full_table_scan(plan)

    @pytest.mark.asyncio
    async def test_collection_id_is_null_uses_index(self, db) -> None:
        """collection_id IS NULL should use idx_documents_collection_created."""
        await _seed_db(db)
        where, params = db._build_filter_clause(collection_id=0)
        sql = f"SELECT * FROM documents {where} ORDER BY created_at DESC LIMIT 20 OFFSET 0"
        plan = await _explain(db, sql, params)
        assert _uses_index(plan, "idx_documents_collection_created")
        assert not _is_full_table_scan(plan)

    @pytest.mark.asyncio
    async def test_file_type_filter_uses_index(self, db) -> None:
        """file_type (ext = ?) should use idx_documents_ext_created."""
        await _seed_db(db)
        where, params = db._build_filter_clause(file_type=".pdf")
        sql = f"SELECT * FROM documents {where} ORDER BY created_at DESC LIMIT 20 OFFSET 0"
        plan = await _explain(db, sql, params)
        assert _uses_index(plan, "idx_documents_ext_created")
        assert not _is_full_table_scan(plan)

    @pytest.mark.asyncio
    async def test_date_range_filter_uses_index(self, db) -> None:
        """date_from alone should use idx_documents_created_at."""
        await _seed_db(db)
        where, params = db._build_filter_clause(date_from="2025-03-01")
        sql = f"SELECT * FROM documents {where} ORDER BY created_at DESC LIMIT 20 OFFSET 0"
        plan = await _explain(db, sql, params)
        assert _uses_index(plan, "idx_documents_created_at")
        assert not _is_full_table_scan(plan)

    @pytest.mark.asyncio
    async def test_status_filter_uses_index(self, db) -> None:
        """A direct status filter should use an index, not a full scan."""
        await _seed_db(db)
        sql = "SELECT * FROM documents WHERE status = 'indexed' ORDER BY created_at DESC LIMIT 20"
        plan = await _explain(db, sql)
        assert _uses_any_index(plan, [
            "idx_documents_status_created", "idx_documents_status",
        ])
        assert not _is_full_table_scan(plan)

    @pytest.mark.asyncio
    async def test_document_type_filter_uses_index(self, db) -> None:
        """document_type filter should use an index, not a full scan."""
        await _seed_db(db)
        sql = "SELECT * FROM documents WHERE document_type = 'text' ORDER BY created_at DESC LIMIT 20"
        plan = await _explain(db, sql)
        assert _uses_any_index(plan, [
            "idx_documents_doc_type_created", "idx_documents_type",
        ])
        assert not _is_full_table_scan(plan)

    @pytest.mark.asyncio
    async def test_source_type_filter_uses_index(self, db) -> None:
        """source_type filter should use an index, not a full scan."""
        await _seed_db(db)
        sql = "SELECT * FROM documents WHERE source_type = 'api' ORDER BY created_at DESC LIMIT 20"
        plan = await _explain(db, sql)
        assert _uses_any_index(plan, [
            "idx_documents_source_type_created", "idx_documents_source",
        ])
        assert not _is_full_table_scan(plan)


# ── EXPLAIN QUERY PLAN: composite filter queries ────────────────


class TestCompositeFilterIndexUsage:
    """Verify that multi-filter queries use composite indexes
    and avoid full table scans."""

    @pytest.mark.asyncio
    async def test_collection_and_date_range_uses_index(self, db) -> None:
        """collection_id + date range should use idx_documents_collection_created."""
        await _seed_db(db)
        where, params = db._build_filter_clause(
            collection_id=1, date_from="2025-01-01", date_to="2025-12-31",
        )
        sql = f"SELECT * FROM documents {where} ORDER BY created_at DESC LIMIT 20 OFFSET 0"
        plan = await _explain(db, sql, params)
        assert _uses_index(plan, "idx_documents_collection_created")
        assert not _is_full_table_scan(plan)

    @pytest.mark.asyncio
    async def test_file_type_and_date_range_uses_index(self, db) -> None:
        """file_type + date range should use idx_documents_ext_created."""
        await _seed_db(db)
        where, params = db._build_filter_clause(
            file_type=".pdf", date_from="2025-01-01", date_to="2025-12-31",
        )
        sql = f"SELECT * FROM documents {where} ORDER BY created_at DESC LIMIT 20 OFFSET 0"
        plan = await _explain(db, sql, params)
        assert _uses_index(plan, "idx_documents_ext_created")
        assert not _is_full_table_scan(plan)

    @pytest.mark.asyncio
    async def test_count_with_collection_filter_uses_index(self, db) -> None:
        """COUNT(*) with collection_id should use an index, not a full scan."""
        await _seed_db(db)
        where, params = db._build_filter_clause(collection_id=2)
        sql = f"SELECT COUNT(*) as c FROM documents {where}"
        plan = await _explain(db, sql, params)
        assert _uses_index(plan, "idx_documents_collection_created")
        assert not _is_full_table_scan(plan)

    @pytest.mark.asyncio
    async def test_count_with_file_type_filter_uses_index(self, db) -> None:
        """COUNT(*) with file_type should use an index, not a full scan."""
        await _seed_db(db)
        where, params = db._build_filter_clause(file_type=".docx")
        sql = f"SELECT COUNT(*) as c FROM documents {where}"
        plan = await _explain(db, sql, params)
        assert _uses_index(plan, "idx_documents_ext_created")
        assert not _is_full_table_scan(plan)

    @pytest.mark.asyncio
    async def test_count_with_date_range_uses_index(self, db) -> None:
        """COUNT(*) with date range should use idx_documents_created_at."""
        await _seed_db(db)
        where, params = db._build_filter_clause(
            date_from="2025-03-01", date_to="2025-06-01",
        )
        sql = f"SELECT COUNT(*) as c FROM documents {where}"
        plan = await _explain(db, sql, params)
        assert _uses_index(plan, "idx_documents_created_at")
        assert not _is_full_table_scan(plan)

    @pytest.mark.asyncio
    async def test_collection_null_count_uses_index(self, db) -> None:
        """COUNT(*) with collection_id IS NULL should use an index."""
        await _seed_db(db)
        where, params = db._build_filter_clause(collection_id=0)
        sql = f"SELECT COUNT(*) as c FROM documents {where}"
        plan = await _explain(db, sql, params)
        assert _uses_index(plan, "idx_documents_collection_created")
        assert not _is_full_table_scan(plan)


# ── EXPLAIN QUERY PLAN: ORDER BY optimization ───────────────────


class TestOrderByIndexUsage:
    """Verify that ORDER BY created_at DESC is served from an index
    when combined with a filter, avoiding a temporary sort (B-tree)."""

    @pytest.mark.asyncio
    async def test_order_by_with_collection_no_temp_sort(self, db) -> None:
        """ORDER BY created_at DESC with collection filter should not
        need a temporary sort — the index already provides the order."""
        await _seed_db(db)
        where, params = db._build_filter_clause(collection_id=1)
        sql = f"SELECT * FROM documents {where} ORDER BY created_at DESC LIMIT 20 OFFSET 0"
        plan = await _explain(db, sql, params)
        # The plan should not include 'USE TEMP B-TREE FOR ORDER BY'
        assert not any("TEMP B-TREE" in line for line in plan)

    @pytest.mark.asyncio
    async def test_order_by_with_file_type_no_temp_sort(self, db) -> None:
        """ORDER BY created_at DESC with file_type filter should not
        need a temporary sort."""
        await _seed_db(db)
        where, params = db._build_filter_clause(file_type=".pdf")
        sql = f"SELECT * FROM documents {where} ORDER BY created_at DESC LIMIT 20 OFFSET 0"
        plan = await _explain(db, sql, params)
        assert not any("TEMP B-TREE" in line for line in plan)

    @pytest.mark.asyncio
    async def test_order_by_with_date_range_no_temp_sort(self, db) -> None:
        """ORDER BY created_at DESC with date range should not need
        a temporary sort — idx_documents_created_at provides the order."""
        await _seed_db(db)
        where, params = db._build_filter_clause(
            date_from="2025-01-01", date_to="2025-12-31",
        )
        sql = f"SELECT * FROM documents {where} ORDER BY created_at DESC LIMIT 20 OFFSET 0"
        plan = await _explain(db, sql, params)
        assert not any("TEMP B-TREE" in line for line in plan)


# ── Regression: no full table scans on filtered queries ─────────


class TestNoFullTableScans:
    """Regression guard: filtered queries must never fall back to
    a full table scan on the documents table."""

    @pytest.mark.asyncio
    async def test_list_documents_collection_no_scan(self, db) -> None:
        await _seed_db(db)
        where, params = db._build_filter_clause(collection_id=1)
        sql = f"SELECT * FROM documents {where} ORDER BY created_at DESC LIMIT 20 OFFSET 0"
        plan = await _explain(db, sql, params)
        assert not _is_full_table_scan(plan), (
            f"Full table scan detected!\nPlan:\n  " + "\n  ".join(plan)
        )

    @pytest.mark.asyncio
    async def test_list_documents_file_type_no_scan(self, db) -> None:
        await _seed_db(db)
        where, params = db._build_filter_clause(file_type=".pdf")
        sql = f"SELECT * FROM documents {where} ORDER BY created_at DESC LIMIT 20 OFFSET 0"
        plan = await _explain(db, sql, params)
        assert not _is_full_table_scan(plan), (
            f"Full table scan detected!\nPlan:\n  " + "\n  ".join(plan)
        )

    @pytest.mark.asyncio
    async def test_count_collection_no_scan(self, db) -> None:
        await _seed_db(db)
        where, params = db._build_filter_clause(collection_id=2)
        sql = f"SELECT COUNT(*) as c FROM documents {where}"
        plan = await _explain(db, sql, params)
        assert not _is_full_table_scan(plan), (
            f"Full table scan detected!\nPlan:\n  " + "\n  ".join(plan)
        )

    @pytest.mark.asyncio
    async def test_count_file_type_no_scan(self, db) -> None:
        await _seed_db(db)
        where, params = db._build_filter_clause(file_type=".html")
        sql = f"SELECT COUNT(*) as c FROM documents {where}"
        plan = await _explain(db, sql, params)
        assert not _is_full_table_scan(plan), (
            f"Full table scan detected!\nPlan:\n  " + "\n  ".join(plan)
        )


# ── Migration from pre-Phase-5 databases ─────────────────────────


class TestMigrateFromPrePhase5DB:
    """Regression tests for migration ordering.

    Simulates a database created before Phase 5 (no collection_id or
    document_type columns) and verifies that migrate() succeeds and
    creates the dependent indexes.
    """

    @pytest.fixture
    async def old_db(self, tmp_db_path: str):
        """Create a database with a pre-Phase-5 documents table, then migrate."""
        import aiosqlite

        from src.core.db_sqlite import Database

        # Create a documents table WITHOUT collection_id or document_type
        # (mimicking a pre-Phase-5 schema).
        async with aiosqlite.connect(tmp_db_path) as conn:
            await conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS collections (
                    id    INTEGER PRIMARY KEY AUTOINCREMENT,
                    name  TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    parent_id INTEGER REFERENCES collections(id) ON DELETE CASCADE,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS documents (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    path         TEXT UNIQUE NOT NULL,
                    source_type  TEXT NOT NULL DEFAULT 'api',
                    source_name  TEXT NOT NULL DEFAULT 'api',
                    file_hash    TEXT,
                    mtime        REAL DEFAULT 0,
                    size         INTEGER DEFAULT 0,
                    title        TEXT NOT NULL,
                    ext          TEXT DEFAULT '',
                    mime_type    TEXT DEFAULT 'application/octet-stream',
                    summary      TEXT,
                    raw_preview  TEXT DEFAULT '',
                    body         TEXT DEFAULT '',
                    status       TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','indexed','summarized','error')),
                    metadata     TEXT DEFAULT '{}',
                    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id            TEXT PRIMARY KEY,
                    state         TEXT NOT NULL DEFAULT 'pending'
                        CHECK (state IN ('pending','processing','completed','failed')),
                    document_path TEXT NOT NULL,
                    document_title TEXT,
                    source_name   TEXT NOT NULL DEFAULT 'api',
                    document_id   INTEGER REFERENCES documents(id) ON DELETE SET NULL,
                    error         TEXT,
                    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
                );
                """
            )
            await conn.commit()

        # Now connect via Database — migrate() must not raise.
        database = Database(db_path=tmp_db_path)
        await database.connect()
        yield database
        await database.disconnect()

    @pytest.mark.asyncio
    async def test_migrate_succeeds_on_old_db(self, old_db) -> None:
        """migrate() must not raise OperationalError on a pre-Phase-5 DB."""
        # If we got here, migrate() already ran in the fixture without error.
        async with old_db.connection() as conn:
            cursor = await conn.execute("PRAGMA table_info(documents)")
            columns = {row[1] for row in await cursor.fetchall()}
        assert "collection_id" in columns
        assert "document_type" in columns

    @pytest.mark.asyncio
    async def test_indexes_created_on_old_db(self, old_db) -> None:
        """The collection_id and document_type indexes must exist after migrate."""
        async with old_db.connection() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='documents'"
            )
            rows = await cursor.fetchall()
        index_names = {row[0] for row in rows}
        assert "idx_documents_collection_created" in index_names
        assert "idx_documents_doc_type_created" in index_names
        assert "idx_documents_type" in index_names
