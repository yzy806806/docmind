"""Tests for src.core.db_sqlite — SQLite-backed async Database.

Covers all CRUD methods, FTS5 full-text search, job queue operations,
and statistics.
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
        yield str(Path(tmpdir) / "test_docmind.db")


@pytest.fixture
async def db(tmp_db_path: str):
    """Create a connected SQLite Database instance."""
    from src.core.db_sqlite import Database

    database = Database(db_path=tmp_db_path)
    await database.connect()
    yield database
    await database.disconnect()


# ── Import / smoke tests ────────────────────────────────────────


def test_import_db_sqlite() -> None:
    """The db_sqlite module should be importable."""
    from src.core.db_sqlite import Database

    assert Database is not None


def test_database_is_sqlite_backend() -> None:
    """Database from db_sqlite should be distinct from the asyncpg version."""
    from src.core.db_sqlite import Database as SqliteDatabase

    # The constructor should accept db_path, not dsn
    db = SqliteDatabase(db_path=":memory:")
    assert db._db_path == ":memory:"


# ── Lifecycle tests ─────────────────────────────────────────────


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_connect_creates_tables(self, tmp_db_path: str) -> None:
        """connect() should create the documents and jobs tables."""
        from src.core.db_sqlite import Database

        db = Database(db_path=tmp_db_path)
        await db.connect()

        # Verify tables exist by querying them
        async with db.connection() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = [row[0] for row in await cursor.fetchall()]

        await db.disconnect()

        assert "documents" in tables
        assert "jobs" in tables
        assert "documents_fts" in tables

    @pytest.mark.asyncio
    async def test_disconnect_sets_conn_none(self, tmp_db_path: str) -> None:
        """disconnect() should clean up the connection."""
        from src.core.db_sqlite import Database

        db = Database(db_path=tmp_db_path)
        await db.connect()
        await db.disconnect()
        assert db._conn is None

    @pytest.mark.asyncio
    async def test_connection_raises_when_not_connected(self) -> None:
        """connection() should raise if not connected."""
        from src.core.db_sqlite import Database

        db = Database(db_path=":memory:")
        with pytest.raises(RuntimeError, match="not connected"):
            async with db.connection() as conn:
                pass

    @pytest.mark.asyncio
    async def test_migrate_is_idempotent(self, tmp_db_path: str) -> None:
        """Running migrate() twice should not error."""
        from src.core.db_sqlite import Database

        db = Database(db_path=tmp_db_path)
        await db.connect()
        await db.migrate()  # second call
        await db.migrate()  # third call
        await db.disconnect()


# ── Document CRUD tests ─────────────────────────────────────────


class TestDocumentCRUD:
    @pytest.mark.asyncio
    async def test_save_document_insert(self, db) -> None:
        """save_document should insert a new document and return its id."""
        doc_id = await db.save_document(
            path="/docs/test.txt",
            source_type="local",
            source_name="test",
            title="Test Document",
            ext=".txt",
            mime_type="text/plain",
            body="This is a test document about machine learning.",
        )
        assert doc_id > 0

    @pytest.mark.asyncio
    async def test_save_document_upsert(self, db) -> None:
        """save_document should update on conflict with same path."""
        doc_id1 = await db.save_document(
            path="/docs/upsert.txt",
            source_type="local",
            source_name="test",
            title="Original Title",
            ext=".txt",
            mime_type="text/plain",
            body="Original body",
        )
        doc_id2 = await db.save_document(
            path="/docs/upsert.txt",
            source_type="local",
            source_name="test",
            title="Updated Title",
            ext=".txt",
            mime_type="text/plain",
            body="Updated body",
        )
        assert doc_id1 == doc_id2

        doc = await db.get_document(doc_id1)
        assert doc["title"] == "Updated Title"
        assert doc["body"] == "Updated body"

    @pytest.mark.asyncio
    async def test_save_document_with_metadata(self, db) -> None:
        """save_document should store and retrieve metadata as JSON."""
        doc_id = await db.save_document(
            path="/docs/meta.txt",
            source_type="api",
            source_name="test",
            title="Meta Doc",
            ext=".txt",
            mime_type="text/plain",
            body="Body text",
            metadata={"author": "alice", "tags": ["python", "testing"]},
        )
        doc = await db.get_document(doc_id)
        assert doc["metadata"]["author"] == "alice"
        assert "python" in doc["metadata"]["tags"]

    @pytest.mark.asyncio
    async def test_save_document_with_all_fields(self, db) -> None:
        """save_document should store all fields correctly."""
        doc_id = await db.save_document(
            path="/docs/full.txt",
            source_type="local",
            source_name="scanner",
            title="Full Document",
            ext=".txt",
            mime_type="text/plain",
            body="Full body content here.",
            file_hash="abc123def456",
            mtime=1234567890.0,
            size=1024,
            metadata={"key": "value"},
            summary="A short summary.",
            status="indexed",
        )
        doc = await db.get_document(doc_id)
        assert doc["file_hash"] == "abc123def456"
        assert doc["mtime"] == 1234567890.0
        assert doc["size"] == 1024
        assert doc["summary"] == "A short summary."
        assert doc["status"] == "indexed"
        assert doc["raw_preview"] == "Full body content here."

    @pytest.mark.asyncio
    async def test_get_document_not_found(self, db) -> None:
        """get_document should return None for non-existent id."""
        doc = await db.get_document(99999)
        assert doc is None

    @pytest.mark.asyncio
    async def test_get_document_by_path(self, db) -> None:
        """get_document_by_path should find by path."""
        await db.save_document(
            path="/docs/by_path.txt",
            source_type="local",
            source_name="test",
            title="Path Test",
            ext=".txt",
            mime_type="text/plain",
            body="Content",
        )
        doc = await db.get_document_by_path("/docs/by_path.txt")
        assert doc is not None
        assert doc["title"] == "Path Test"

    @pytest.mark.asyncio
    async def test_get_document_by_path_not_found(self, db) -> None:
        """get_document_by_path should return None for unknown path."""
        doc = await db.get_document_by_path("/nonexistent/path.txt")
        assert doc is None

    @pytest.mark.asyncio
    async def test_list_documents_empty(self, db) -> None:
        """list_documents on empty db should return empty list."""
        docs = await db.list_documents()
        assert docs == []

    @pytest.mark.asyncio
    async def test_list_documents_multiple(self, db) -> None:
        """list_documents should return all documents."""
        for i in range(5):
            await db.save_document(
                path=f"/docs/doc_{i}.txt",
                source_type="local",
                source_name="test",
                title=f"Document {i}",
                ext=".txt",
                mime_type="text/plain",
                body=f"Body {i}",
            )
        docs = await db.list_documents(limit=100)
        assert len(docs) == 5

    @pytest.mark.asyncio
    async def test_list_documents_with_source_filter(self, db) -> None:
        """list_documents should filter by source name."""
        await db.save_document(
            path="/docs/local.txt",
            source_type="local",
            source_name="local-source",
            title="Local Doc",
            ext=".txt",
            mime_type="text/plain",
            body="Body",
        )
        await db.save_document(
            path="/docs/api.txt",
            source_type="api",
            source_name="api-source",
            title="API Doc",
            ext=".txt",
            mime_type="text/plain",
            body="Body",
        )
        local_docs = await db.list_documents(source="local-source")
        assert len(local_docs) == 1
        assert local_docs[0]["title"] == "Local Doc"

    @pytest.mark.asyncio
    async def test_list_documents_limit(self, db) -> None:
        """list_documents should respect limit."""
        for i in range(10):
            await db.save_document(
                path=f"/docs/limit_{i}.txt",
                source_type="local",
                source_name="test",
                title=f"Doc {i}",
                ext=".txt",
                mime_type="text/plain",
                body=f"Body {i}",
            )
        docs = await db.list_documents(limit=3)
        assert len(docs) == 3

    @pytest.mark.asyncio
    async def test_list_documents_offset(self, db) -> None:
        """list_documents should support offset."""
        for i in range(10):
            await db.save_document(
                path=f"/docs/offset_{i}.txt",
                source_type="local",
                source_name="test",
                title=f"Doc {i}",
                ext=".txt",
                mime_type="text/plain",
                body=f"Body {i}",
            )
        docs_page1 = await db.list_documents(limit=5, offset=0)
        docs_page2 = await db.list_documents(limit=5, offset=5)
        assert len(docs_page1) == 5
        assert len(docs_page2) == 5
        # Ensure no overlap
        ids1 = {d["id"] for d in docs_page1}
        ids2 = {d["id"] for d in docs_page2}
        assert ids1.isdisjoint(ids2)

    @pytest.mark.asyncio
    async def test_delete_document(self, db) -> None:
        """delete_document should remove a document."""
        doc_id = await db.save_document(
            path="/docs/delete_me.txt",
            source_type="local",
            source_name="test",
            title="Delete Me",
            ext=".txt",
            mime_type="text/plain",
            body="To be deleted",
        )
        result = await db.delete_document(doc_id)
        assert result is True

        doc = await db.get_document(doc_id)
        assert doc is None

    @pytest.mark.asyncio
    async def test_delete_document_not_found(self, db) -> None:
        """delete_document should return False for non-existent id."""
        result = await db.delete_document(99999)
        assert result is False

    @pytest.mark.asyncio
    async def test_update_summary(self, db) -> None:
        """update_summary should set summary and change status."""
        doc_id = await db.save_document(
            path="/docs/summary_test.txt",
            source_type="local",
            source_name="test",
            title="Summary Test",
            ext=".txt",
            mime_type="text/plain",
            body="Content to summarize.",
        )
        await db.update_summary(doc_id, "This is the summary.")
        doc = await db.get_document(doc_id)
        assert doc["summary"] == "This is the summary."
        assert doc["status"] == "summarized"

    @pytest.mark.asyncio
    async def test_upsert_document_alias(self, db) -> None:
        """upsert_document should work as an alias for save_document."""
        doc_id = await db.upsert_document(
            path="/docs/alias.txt",
            source_type="local",
            source_name="test",
            title="Alias Test",
            ext=".txt",
            mime_type="text/plain",
            body="Body content",
        )
        assert doc_id > 0
        doc = await db.get_document(doc_id)
        assert doc["title"] == "Alias Test"

    @pytest.mark.asyncio
    async def test_get_document_count(self, db) -> None:
        """get_document_count should return total count."""
        for i in range(3):
            await db.save_document(
                path=f"/docs/count_{i}.txt",
                source_type="local",
                source_name="test",
                title=f"Count {i}",
                ext=".txt",
                mime_type="text/plain",
                body=f"Body {i}",
            )
        count = await db.get_document_count()
        assert count == 3

    @pytest.mark.asyncio
    async def test_get_document_count_with_source(self, db) -> None:
        """get_document_count should filter by source."""
        await db.save_document(
            path="/docs/count_a.txt",
            source_type="local",
            source_name="alpha",
            title="A",
            ext=".txt",
            mime_type="text/plain",
            body="Body A",
        )
        await db.save_document(
            path="/docs/count_b.txt",
            source_type="local",
            source_name="beta",
            title="B",
            ext=".txt",
            mime_type="text/plain",
            body="Body B",
        )
        count_alpha = await db.get_document_count(source="alpha")
        assert count_alpha == 1
        count_all = await db.get_document_count()
        assert count_all == 2

    @pytest.mark.asyncio
    async def test_list_documents_paginated(self, db) -> None:
        """list_documents_paginated should return pagination metadata."""
        for i in range(25):
            await db.save_document(
                path=f"/docs/page_{i}.txt",
                source_type="local",
                source_name="test",
                title=f"Page Doc {i}",
                ext=".txt",
                mime_type="text/plain",
                body=f"Body {i}",
            )
        result = await db.list_documents_paginated(page=1, per_page=10)
        assert result["total"] == 25
        assert result["page"] == 1
        assert result["per_page"] == 10
        assert result["total_pages"] == 3
        assert len(result["documents"]) == 10

    @pytest.mark.asyncio
    async def test_get_pending_summaries(self, db) -> None:
        """get_pending_summaries should return indexed documents."""
        doc_id1 = await db.save_document(
            path="/docs/pending1.txt",
            source_type="local",
            source_name="test",
            title="Pending 1",
            ext=".txt",
            mime_type="text/plain",
            body="Body 1",
        )
        doc_id2 = await db.save_document(
            path="/docs/pending2.txt",
            source_type="local",
            source_name="test",
            title="Pending 2",
            ext=".txt",
            mime_type="text/plain",
            body="Body 2",
        )
        await db.update_summary(doc_id2, "Summarized")

        pending = await db.get_pending_summaries()
        assert len(pending) == 1
        assert pending[0]["id"] == doc_id1


# ── Full-text search tests ──────────────────────────────────────


class TestFullTextSearch:
    @pytest.mark.asyncio
    async def test_search_returns_results(self, db) -> None:
        """search_documents should find matching documents."""
        await db.save_document(
            path="/docs/ml.txt",
            source_type="local",
            source_name="test",
            title="Machine Learning Pipeline",
            ext=".txt",
            mime_type="text/plain",
            body="This document describes a machine learning pipeline for "
            "natural language processing tasks.",
        )
        await db.save_document(
            path="/docs/cooking.txt",
            source_type="local",
            source_name="test",
            title="Cooking Guide",
            ext=".txt",
            mime_type="text/plain",
            body="How to cook pasta and make sauce from scratch.",
        )

        results = await db.search_documents("machine learning")
        assert len(results) >= 1
        titles = [r["title"] for r in results]
        assert "Machine Learning Pipeline" in titles

    @pytest.mark.asyncio
    async def test_search_no_results(self, db) -> None:
        """search_documents should return empty list for no matches."""
        await db.save_document(
            path="/docs/test.txt",
            source_type="local",
            source_name="test",
            title="Test",
            ext=".txt",
            mime_type="text/plain",
            body="Some content here.",
        )
        results = await db.search_documents("nonexistent_xyzzy_12345")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_ranks_by_relevance(self, db) -> None:
        """search_documents should rank results by BM25 relevance."""
        await db.save_document(
            path="/docs/relevant.txt",
            source_type="local",
            source_name="test",
            title="Python Testing",
            ext=".txt",
            mime_type="text/plain",
            body="Python testing pytest fixtures mock assert",
        )
        await db.save_document(
            path="/docs/less_relevant.txt",
            source_type="local",
            source_name="test",
            title="Other Topic",
            ext=".txt",
            mime_type="text/plain",
            body="Python is mentioned once here.",
        )

        results = await db.search_documents("python testing pytest")
        assert len(results) >= 1
        # The more relevant doc should rank first
        assert results[0]["title"] == "Python Testing"

    @pytest.mark.asyncio
    async def test_search_in_body(self, db) -> None:
        """search_documents should search in body text."""
        await db.save_document(
            path="/docs/body_search.txt",
            source_type="local",
            source_name="test",
            title="Generic Title",
            ext=".txt",
            mime_type="text/plain",
            body="The document discusses quantum computing applications "
            "in cryptography and optimization problems.",
        )
        results = await db.search_documents("quantum computing")
        assert len(results) >= 1
        assert results[0]["title"] == "Generic Title"

    @pytest.mark.asyncio
    async def test_search_in_title(self, db) -> None:
        """search_documents should search in title."""
        await db.save_document(
            path="/docs/title_search.txt",
            source_type="local",
            source_name="test",
            title="Kubernetes Deployment Guide",
            ext=".txt",
            mime_type="text/plain",
            body="Some generic content not related to title.",
        )
        results = await db.search_documents("kubernetes")
        assert len(results) >= 1
        assert results[0]["title"] == "Kubernetes Deployment Guide"

    @pytest.mark.asyncio
    async def test_search_in_summary(self, db) -> None:
        """search_documents should search in summary."""
        doc_id = await db.save_document(
            path="/docs/summary_search.txt",
            source_type="local",
            source_name="test",
            title="Generic Doc",
            ext=".txt",
            mime_type="text/plain",
            body="Generic body content.",
            summary="This document covers docker containerization.",
        )
        results = await db.search_documents("docker")
        assert len(results) >= 1
        assert results[0]["id"] == doc_id

    @pytest.mark.asyncio
    async def test_search_rank_field_present(self, db) -> None:
        """search results should include a rank field."""
        await db.save_document(
            path="/docs/rank_test.txt",
            source_type="local",
            source_name="test",
            title="Ranked Document",
            ext=".txt",
            mime_type="text/plain",
            body="Searchable content for ranking test.",
        )
        results = await db.search_documents("ranking")
        assert len(results) >= 1
        assert "rank" in results[0]

    @pytest.mark.asyncio
    async def test_search_limit(self, db) -> None:
        """search_documents should respect limit."""
        for i in range(10):
            await db.save_document(
                path=f"/docs/search_limit_{i}.txt",
                source_type="local",
                source_name="test",
                title=f"Search Limit Doc {i}",
                ext=".txt",
                mime_type="text/plain",
                body=f"Contains the keyword: python",
            )
        results = await db.search_documents("python", limit=3)
        assert len(results) <= 3

    @pytest.mark.asyncio
    async def test_search_after_update(self, db) -> None:
        """FTS index should reflect document updates."""
        doc_id = await db.save_document(
            path="/docs/update_search.txt",
            source_type="local",
            source_name="test",
            title="Original Title",
            ext=".txt",
            mime_type="text/plain",
            body="Original content about cats.",
        )
        # Search should find it
        results1 = await db.search_documents("cats")
        assert len(results1) >= 1

        # Update the document
        await db.save_document(
            path="/docs/update_search.txt",
            source_type="local",
            source_name="test",
            title="Updated Title",
            ext=".txt",
            mime_type="text/plain",
            body="Updated content about dogs.",
        )
        # Search for old content should not find it
        results2 = await db.search_documents("cats")
        assert len(results2) == 0

        # Search for new content should find it
        results3 = await db.search_documents("dogs")
        assert len(results3) >= 1

    @pytest.mark.asyncio
    async def test_search_empty_query(self, db) -> None:
        """search_documents with empty query should return empty list."""
        await db.save_document(
            path="/docs/empty_q.txt",
            source_type="local",
            source_name="test",
            title="Test",
            ext=".txt",
            mime_type="text/plain",
            body="Content",
        )
        results = await db.search_documents("")
        assert results == []

    @pytest.mark.asyncio
    async def test_fulltext_search_alias(self, db) -> None:
        """fulltext_search should be an alias for search_documents."""
        await db.save_document(
            path="/docs/alias_search.txt",
            source_type="local",
            source_name="test",
            title="Alias Search Test",
            ext=".txt",
            mime_type="text/plain",
            body="Searchable content about algorithms.",
        )
        results = await db.fulltext_search("algorithms")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_fts5_fts_table_exists(self, db) -> None:
        """The FTS5 virtual table should exist after migration."""
        async with db.connection() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='documents_fts'"
            )
            row = await cursor.fetchone()
        assert row is not None

    @pytest.mark.asyncio
    async def test_fts5_triggers_exist(self, db) -> None:
        """FTS5 sync triggers should exist after migration."""
        async with db.connection() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
            triggers = [row[0] for row in await cursor.fetchall()]
        assert "documents_ai" in triggers
        assert "documents_ad" in triggers
        assert "documents_au" in triggers

    @pytest.mark.asyncio
    async def test_fts5_fts_index_deleted_on_delete(self, db) -> None:
        """FTS index should be cleaned up when document is deleted."""
        doc_id = await db.save_document(
            path="/docs/fts_delete.txt",
            source_type="local",
            source_name="test",
            title="FTS Delete Test",
            ext=".txt",
            mime_type="text/plain",
            body="Unique searchable keyword: zephyr",
        )
        # Verify it's in the FTS index
        results = await db.search_documents("zephyr")
        assert len(results) >= 1

        # Delete the document
        await db.delete_document(doc_id)

        # FTS index should no longer return the deleted doc
        results2 = await db.search_documents("zephyr")
        assert len(results2) == 0


# ── Job queue tests ─────────────────────────────────────────────


class TestJobQueue:
    @pytest.mark.asyncio
    async def test_create_job(self, db) -> None:
        """create_job should insert a job and return its record."""
        from src.core.models import JobState

        job = await db.create_job(
            "/docs/job1.txt",
            document_title="Job 1",
            source_name="test",
        )
        assert job.id is not None
        assert job.state == JobState.PENDING
        assert job.document_path == "/docs/job1.txt"
        assert job.document_title == "Job 1"
        assert job.source_name == "test"

    @pytest.mark.asyncio
    async def test_enqueue_job(self, db) -> None:
        """enqueue_job should work identically to create_job."""
        from src.core.models import JobState

        job = await db.enqueue_job(
            "/docs/enqueue.txt",
            document_title="Enqueue Test",
            source_name="api",
        )
        assert job.state == JobState.PENDING
        assert job.document_path == "/docs/enqueue.txt"

    @pytest.mark.asyncio
    async def test_dequeue_job_empty(self, db) -> None:
        """dequeue_job should return None when queue is empty."""
        job = await db.dequeue_job()
        assert job is None

    @pytest.mark.asyncio
    async def test_dequeue_job_claims_oldest(self, db) -> None:
        """dequeue_job should claim the oldest pending job."""
        import asyncio

        job1 = await db.enqueue_job("/docs/old.txt", document_title="Old")
        await asyncio.sleep(0.05)  # Ensure different timestamps
        job2 = await db.enqueue_job("/docs/new.txt", document_title="New")

        claimed = await db.dequeue_job()
        assert claimed is not None
        assert claimed.id == job1.id

    @pytest.mark.asyncio
    async def test_dequeue_sets_processing(self, db) -> None:
        """dequeue_job should transition job to processing state."""
        from src.core.models import JobState

        job = await db.enqueue_job("/docs/processing.txt")
        claimed = await db.dequeue_job()
        assert claimed is not None
        assert claimed.state == JobState.PROCESSING

    @pytest.mark.asyncio
    async def test_complete_job(self, db) -> None:
        """complete_job should set state to completed and link document."""
        from src.core.models import JobState

        # Create a document first so the FK is valid
        doc_id = await db.save_document(
            path="/docs/for_complete_job.txt",
            source_type="local",
            source_name="test",
            title="Job Complete Doc",
            ext=".txt",
            mime_type="text/plain",
            body="Body for job completion test.",
        )

        job = await db.enqueue_job("/docs/complete.txt")
        await db.complete_job(job.id, document_id=doc_id)

        fetched = await db.get_job(job.id)
        assert fetched.state == JobState.COMPLETED
        assert fetched.document_id == doc_id

    @pytest.mark.asyncio
    async def test_fail_job(self, db) -> None:
        """fail_job should set state to failed with error message."""
        from src.core.models import JobState

        job = await db.enqueue_job("/docs/fail.txt")
        await db.fail_job(job.id, error="Something went wrong")

        fetched = await db.get_job(job.id)
        assert fetched.state == JobState.FAILED
        assert fetched.error == "Something went wrong"

    @pytest.mark.asyncio
    async def test_get_job_not_found(self, db) -> None:
        """get_job should return None for non-existent job."""
        job = await db.get_job("nonexistent-job-id")
        assert job is None

    @pytest.mark.asyncio
    async def test_list_jobs_empty(self, db) -> None:
        """list_jobs should return empty list when no jobs exist."""
        jobs = await db.list_jobs()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_list_jobs_all(self, db) -> None:
        """list_jobs should return all jobs."""
        for i in range(5):
            await db.enqueue_job(f"/docs/job_list_{i}.txt")
        jobs = await db.list_jobs()
        assert len(jobs) == 5

    @pytest.mark.asyncio
    async def test_list_jobs_by_state(self, db) -> None:
        """list_jobs should filter by state."""
        j1 = await db.enqueue_job("/docs/state_pending.txt")
        j2 = await db.enqueue_job("/docs/state_processing.txt")
        await db.dequeue_job()  # claims j1, sets to processing

        pending = await db.list_jobs(state="pending")
        assert len(pending) == 1
        assert pending[0].id == j2.id

    @pytest.mark.asyncio
    async def test_list_jobs_limit(self, db) -> None:
        """list_jobs should respect limit."""
        for i in range(10):
            await db.enqueue_job(f"/docs/job_limit_{i}.txt")
        jobs = await db.list_jobs(limit=3)
        assert len(jobs) == 3

    @pytest.mark.asyncio
    async def test_update_job_status(self, db) -> None:
        """update_job_status should update state and error."""
        from src.core.models import JobState

        job = await db.enqueue_job("/docs/update_status.txt")
        await db.update_job_status(job.id, "failed", error="Custom error")

        fetched = await db.get_job(job.id)
        assert fetched.state == JobState.FAILED
        assert fetched.error == "Custom error"

    @pytest.mark.asyncio
    async def test_update_job_status_with_document_id(self, db) -> None:
        """update_job_status should set document_id."""
        from src.core.models import JobState

        # Create a document first so the FK is valid
        doc_id = await db.save_document(
            path="/docs/for_update_status.txt",
            source_type="local",
            source_name="test",
            title="Update Status Doc",
            ext=".txt",
            mime_type="text/plain",
            body="Body for update status test.",
        )

        job = await db.enqueue_job("/docs/update_doc_id.txt")
        await db.update_job_status(
            job.id, "completed", document_id=doc_id
        )

        fetched = await db.get_job(job.id)
        assert fetched.state == JobState.COMPLETED
        assert fetched.document_id == doc_id

    @pytest.mark.asyncio
    async def test_dequeue_after_complete(self, db) -> None:
        """After completing a job, dequeue should return the next pending one."""
        from src.core.models import JobState

        # Create a document first so the FK constraint is satisfied
        doc_id = await db.save_document(
            path="/docs/for_dequeue_complete.txt",
            source_type="local",
            source_name="test",
            title="Dequeue Complete Doc",
            ext=".txt",
            mime_type="text/plain",
            body="Body for dequeue complete test.",
        )

        job1 = await db.enqueue_job("/docs/first.txt")
        job2 = await db.enqueue_job("/docs/second.txt")

        # Claim and complete the first
        claimed = await db.dequeue_job()
        assert claimed.id == job1.id
        await db.complete_job(job1.id, document_id=doc_id)

        # Next dequeue should get job2
        claimed2 = await db.dequeue_job()
        assert claimed2.id == job2.id

    @pytest.mark.asyncio
    async def test_job_has_timestamps(self, db) -> None:
        """Jobs should have created_at and updated_at timestamps."""
        job = await db.enqueue_job("/docs/timestamps.txt")
        assert job.created_at is not None
        assert job.updated_at is not None


# ── Statistics tests ────────────────────────────────────────────


class TestStats:
    @pytest.mark.asyncio
    async def test_get_stats_empty(self, db) -> None:
        """get_stats on empty db should return zeros."""
        stats = await db.get_stats()
        assert stats["total"] == 0
        assert stats["pending"] == 0
        assert stats["indexed"] == 0
        assert stats["summarized"] == 0
        assert stats["active_jobs"] == 0

    @pytest.mark.asyncio
    async def test_get_stats_with_documents(self, db) -> None:
        """get_stats should count documents by status."""
        doc1 = await db.save_document(
            path="/docs/stats1.txt",
            source_type="local",
            source_name="test",
            title="Indexed 1",
            ext=".txt",
            mime_type="text/plain",
            body="Body 1",
        )
        doc2 = await db.save_document(
            path="/docs/stats2.txt",
            source_type="local",
            source_name="test",
            title="Indexed 2",
            ext=".txt",
            mime_type="text/plain",
            body="Body 2",
        )
        await db.update_summary(doc2, "Summary")

        stats = await db.get_stats()
        assert stats["total"] == 2
        assert stats["indexed"] == 1
        assert stats["summarized"] == 1

    @pytest.mark.asyncio
    async def test_get_stats_with_jobs(self, db) -> None:
        """get_stats should count active jobs."""
        await db.enqueue_job("/docs/stats_job1.txt")
        await db.enqueue_job("/docs/stats_job2.txt")
        await db.dequeue_job()  # one goes to processing

        stats = await db.get_stats()
        assert stats["active_jobs"] == 2  # pending + processing


# ── Edge cases ──────────────────────────────────────────────────


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_save_document_empty_body(self, db) -> None:
        """save_document should handle empty body."""
        doc_id = await db.save_document(
            path="/docs/empty.txt",
            source_type="local",
            source_name="test",
            title="Empty Body",
            ext=".txt",
            mime_type="text/plain",
            body="",
        )
        doc = await db.get_document(doc_id)
        assert doc["body"] == ""
        assert doc["raw_preview"] == ""

    @pytest.mark.asyncio
    async def test_save_document_unicode(self, db) -> None:
        """save_document should handle Unicode content."""
        doc_id = await db.save_document(
            path="/docs/unicode.txt",
            source_type="local",
            source_name="test",
            title="Unicode Test 你好",
            ext=".txt",
            mime_type="text/plain",
            body="This contains 中文 content and emoji 🎉.",
        )
        doc = await db.get_document(doc_id)
        assert "中文" in doc["body"]
        assert "🎉" in doc["body"]
        assert "你好" in doc["title"]

    @pytest.mark.asyncio
    async def test_save_document_large_body(self, db) -> None:
        """save_document should handle large body text."""
        large_body = "word " * 10000
        doc_id = await db.save_document(
            path="/docs/large.txt",
            source_type="local",
            source_name="test",
            title="Large Doc",
            ext=".txt",
            mime_type="text/plain",
            body=large_body,
        )
        doc = await db.get_document(doc_id)
        assert len(doc["body"]) == len(large_body)
        # raw_preview should be truncated to 500 chars
        assert len(doc["raw_preview"]) == 500

    @pytest.mark.asyncio
    async def test_search_special_characters(self, db) -> None:
        """search_documents should handle special characters in query."""
        await db.save_document(
            path="/docs/special.txt",
            source_type="local",
            source_name="test",
            title="Special Chars",
            ext=".txt",
            mime_type="text/plain",
            body="Content with special characters and keywords.",
        )
        # Query with special chars should not crash
        results = await db.search_documents("special!@#$%characters")
        # May or may not find results, but should not error
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_metadata_default_empty_dict(self, db) -> None:
        """Documents without metadata should have empty dict."""
        doc_id = await db.save_document(
            path="/docs/no_meta.txt",
            source_type="local",
            source_name="test",
            title="No Metadata",
            ext=".txt",
            mime_type="text/plain",
            body="Body",
        )
        doc = await db.get_document(doc_id)
        assert doc["metadata"] == {}

    @pytest.mark.asyncio
    async def test_wal_mode_enabled(self, tmp_db_path: str) -> None:
        """Database should enable WAL journal mode for concurrency."""
        from src.core.db_sqlite import Database

        db = Database(db_path=tmp_db_path)
        await db.connect()

        async with db.connection() as conn:
            cursor = await conn.execute("PRAGMA journal_mode")
            row = await cursor.fetchone()

        await db.disconnect()
        assert row[0].lower() == "wal"

    @pytest.mark.asyncio
    async def test_foreign_keys_enabled(self, tmp_db_path: str) -> None:
        """Database should have foreign key enforcement enabled."""
        from src.core.db_sqlite import Database

        db = Database(db_path=tmp_db_path)
        await db.connect()

        async with db.connection() as conn:
            cursor = await conn.execute("PRAGMA foreign_keys")
            row = await cursor.fetchone()

        await db.disconnect()
        assert row[0] == 1


# ── Chat session & message CRUD tests ────────────────────────────


class TestChatSessionCRUD:
    """Tests for chat_sessions and chat_messages tables and CRUD methods."""

    @pytest.mark.asyncio
    async def test_create_chat_session_returns_dict(self, db) -> None:
        """create_chat_session should return a dict with id, title, timestamps."""
        session = await db.create_chat_session()
        assert "id" in session
        assert isinstance(session["id"], str)
        assert len(session["id"]) > 0
        assert session["title"] == "New Chat"
        assert session["created_at"] is not None
        assert session["updated_at"] is not None

    @pytest.mark.asyncio
    async def test_create_chat_session_with_explicit_id(self, db) -> None:
        """create_chat_session should accept an explicit session_id."""
        sid = "my-custom-session-id-123"
        session = await db.create_chat_session(session_id=sid)
        assert session["id"] == sid

    @pytest.mark.asyncio
    async def test_create_chat_session_with_title(self, db) -> None:
        """create_chat_session should accept a custom title."""
        session = await db.create_chat_session(title="My First Chat")
        assert session["title"] == "My First Chat"

    @pytest.mark.asyncio
    async def test_get_chat_session_existing(self, db) -> None:
        """get_chat_session should return the session dict when it exists."""
        created = await db.create_chat_session(title="Find Me")
        fetched = await db.get_chat_session(created["id"])
        assert fetched is not None
        assert fetched["id"] == created["id"]
        assert fetched["title"] == "Find Me"

    @pytest.mark.asyncio
    async def test_get_chat_session_missing(self, db) -> None:
        """get_chat_session should return None for unknown id."""
        result = await db.get_chat_session("nonexistent-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_chat_sessions_empty(self, db) -> None:
        """list_chat_sessions on empty db returns empty list."""
        sessions = await db.list_chat_sessions()
        assert sessions == []

    @pytest.mark.asyncio
    async def test_list_chat_sessions_returns_newest_first(self, db) -> None:
        """list_chat_sessions should return sessions newest-first."""
        s1 = await db.create_chat_session(title="First")
        s2 = await db.create_chat_session(title="Second")
        sessions = await db.list_chat_sessions()
        assert len(sessions) == 2
        # Newest (s2) should be first
        assert sessions[0]["id"] == s2["id"]
        assert sessions[1]["id"] == s1["id"]

    @pytest.mark.asyncio
    async def test_list_chat_sessions_includes_preview(self, db) -> None:
        """list_chat_sessions should include a preview from last message."""
        session = await db.create_chat_session(title="Preview Test")
        await db.save_chat_message(session["id"], "user", "Hello preview world")
        sessions = await db.list_chat_sessions()
        assert len(sessions) == 1
        assert "Hello preview world" in sessions[0]["preview"]

    @pytest.mark.asyncio
    async def test_list_chat_sessions_respects_limit(self, db) -> None:
        """list_chat_sessions should respect the limit parameter."""
        for i in range(5):
            await db.create_chat_session(title=f"Session {i}")
        sessions = await db.list_chat_sessions(limit=3)
        assert len(sessions) == 3

    @pytest.mark.asyncio
    async def test_delete_chat_session_existing(self, db) -> None:
        """delete_chat_session should return True and remove the session."""
        session = await db.create_chat_session()
        deleted = await db.delete_chat_session(session["id"])
        assert deleted is True
        # Verify gone
        result = await db.get_chat_session(session["id"])
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_chat_session_missing(self, db) -> None:
        """delete_chat_session should return False for unknown id."""
        deleted = await db.delete_chat_session("nonexistent-id")
        assert deleted is False

    @pytest.mark.asyncio
    async def test_update_chat_session_title(self, db) -> None:
        """update_chat_session_title should change the title."""
        session = await db.create_chat_session()
        ok = await db.update_chat_session_title(session["id"], "Updated Title")
        assert ok is True
        fetched = await db.get_chat_session(session["id"])
        assert fetched["title"] == "Updated Title"

    @pytest.mark.asyncio
    async def test_update_chat_session_title_missing(self, db) -> None:
        """update_chat_session_title should return False for unknown id."""
        ok = await db.update_chat_session_title("nonexistent", "Title")
        assert ok is False


class TestChatMessages:
    """Tests for chat_messages persistence."""

    @pytest.mark.asyncio
    async def test_save_chat_message_user(self, db) -> None:
        """save_chat_message should persist a user message."""
        session = await db.create_chat_session()
        msg = await db.save_chat_message(
            session["id"], "user", "What is the API design?"
        )
        assert msg["session_id"] == session["id"]
        assert msg["role"] == "user"
        assert msg["content"] == "What is the API design?"
        assert msg["citations"] == []
        assert msg["id"] > 0

    @pytest.mark.asyncio
    async def test_save_chat_message_assistant_with_citations(self, db) -> None:
        """save_chat_message should persist an assistant message with citations."""
        session = await db.create_chat_session()
        citations = [
            {"ref": 1, "doc_id": 42, "title": "Doc", "snippet": "snip"},
        ]
        msg = await db.save_chat_message(
            session["id"], "assistant", "The API uses REST.", citations=citations
        )
        assert msg["role"] == "assistant"
        assert msg["citations"] == citations

    @pytest.mark.asyncio
    async def test_save_chat_message_invalid_role(self, db) -> None:
        """save_chat_message should reject invalid role."""
        session = await db.create_chat_session()
        with pytest.raises(ValueError, match="role"):
            await db.save_chat_message(session["id"], "system", "content")

    @pytest.mark.asyncio
    async def test_get_chat_history_empty(self, db) -> None:
        """get_chat_history on session with no messages returns empty list."""
        session = await db.create_chat_session()
        history = await db.get_chat_history(session["id"])
        assert history == []

    @pytest.mark.asyncio
    async def test_get_chat_history_ordered_oldest_first(self, db) -> None:
        """get_chat_history should return messages oldest-first."""
        session = await db.create_chat_session()
        await db.save_chat_message(session["id"], "user", "First question")
        await db.save_chat_message(session["id"], "assistant", "First answer")
        await db.save_chat_message(session["id"], "user", "Second question")
        history = await db.get_chat_history(session["id"])
        assert len(history) == 3
        assert history[0]["content"] == "First question"
        assert history[1]["content"] == "First answer"
        assert history[2]["content"] == "Second question"

    @pytest.mark.asyncio
    async def test_get_chat_history_respects_limit(self, db) -> None:
        """get_chat_history should respect the limit parameter."""
        session = await db.create_chat_session()
        for i in range(10):
            await db.save_chat_message(session["id"], "user", f"Q{i}")
        history = await db.get_chat_history(session["id"], limit=5)
        assert len(history) == 5
        # Should be the FIRST 5 (oldest), since we order ASC then limit
        assert history[0]["content"] == "Q0"
        assert history[4]["content"] == "Q4"

    @pytest.mark.asyncio
    async def test_get_chat_history_citations_roundtrip(self, db) -> None:
        """Citations should survive a save -> get roundtrip."""
        session = await db.create_chat_session()
        cites = [{"ref": 1, "doc_id": 5}, {"ref": 2, "doc_id": 6}]
        await db.save_chat_message(
            session["id"], "assistant", "Answer", citations=cites
        )
        history = await db.get_chat_history(session["id"])
        assert len(history) == 1
        assert history[0]["citations"] == cites

    @pytest.mark.asyncio
    async def test_save_message_bumps_session_updated_at(self, db) -> None:
        """Saving a message should update the session's updated_at."""
        session = await db.create_chat_session()
        original_updated = session["updated_at"]
        await db.save_chat_message(session["id"], "user", "Hello")
        fetched = await db.get_chat_session(session["id"])
        assert fetched["updated_at"] >= original_updated

    @pytest.mark.asyncio
    async def test_cascade_delete_removes_messages(self, db) -> None:
        """Deleting a session should cascade-delete its messages."""
        session = await db.create_chat_session()
        await db.save_chat_message(session["id"], "user", "Q1")
        await db.save_chat_message(session["id"], "assistant", "A1")

        # Verify messages exist
        history = await db.get_chat_history(session["id"])
        assert len(history) == 2

        # Delete session
        await db.delete_chat_session(session["id"])

        # Messages should be gone too
        history_after = await db.get_chat_history(session["id"])
        assert history_after == []

    @pytest.mark.asyncio
    async def test_cascade_delete_does_not_affect_other_sessions(self, db) -> None:
        """Deleting one session should not affect messages in another."""
        s1 = await db.create_chat_session()
        s2 = await db.create_chat_session()
        await db.save_chat_message(s1["id"], "user", "S1 msg")
        await db.save_chat_message(s2["id"], "user", "S2 msg")

        await db.delete_chat_session(s1["id"])

        s2_history = await db.get_chat_history(s2["id"])
        assert len(s2_history) == 1
        assert s2_history[0]["content"] == "S2 msg"

    @pytest.mark.asyncio
    async def test_chat_tables_created_on_connect(self, tmp_db_path: str) -> None:
        """connect() should create chat_sessions and chat_messages tables."""
        from src.core.db_sqlite import Database

        db = Database(db_path=tmp_db_path)
        await db.connect()

        async with db.connection() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = [row[0] for row in await cursor.fetchall()]

        await db.disconnect()

        assert "chat_sessions" in tables
        assert "chat_messages" in tables

    @pytest.mark.asyncio
    async def test_chat_messages_index_exists(self, tmp_db_path: str) -> None:
        """An index on chat_messages(session_id) should exist."""
        from src.core.db_sqlite import Database

        db = Database(db_path=tmp_db_path)
        await db.connect()

        async with db.connection() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='chat_messages'"
            )
            indexes = [row[0] for row in await cursor.fetchall()]

        await db.disconnect()
        assert "idx_chat_messages_session" in indexes

