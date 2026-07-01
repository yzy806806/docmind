"""PostgreSQL connection pool and migration utilities for DocMind.

Uses a ``FOR UPDATE SKIP LOCKED`` queue pattern for background job processing.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

import asyncpg  # type: ignore

from .models import JobRecord, JobState

logger = logging.getLogger(__name__)

# ── Schema DDL ─────────────────────────────────────────────────

SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS documents (
    id              SERIAL PRIMARY KEY,
    path            TEXT UNIQUE NOT NULL,
    source_type     TEXT NOT NULL DEFAULT 'api',
    source_name     TEXT NOT NULL DEFAULT 'api',
    file_hash       TEXT,
    mtime           DOUBLE PRECISION DEFAULT 0,
    size            BIGINT DEFAULT 0,
    title           TEXT NOT NULL,
    ext             TEXT DEFAULT '',
    mime_type       TEXT DEFAULT 'application/octet-stream',
    summary         TEXT,
    raw_preview     TEXT DEFAULT '',
    body            TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','indexed','summarized','error')),
    metadata        JSONB DEFAULT '{}'::jsonb,
    search_vector   tsvector GENERATED ALWAYS AS (
                        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                        setweight(to_tsvector('english', coalesce(summary, '')), 'B') ||
                        setweight(to_tsvector('english', coalesce(raw_preview, '')), 'C') ||
                        setweight(to_tsvector('english', coalesce(body, '')), 'D')
                    ) STORED,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_type, source_name);
CREATE INDEX IF NOT EXISTS idx_documents_search ON documents USING GIN(search_vector);

CREATE TABLE IF NOT EXISTS jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    state           TEXT NOT NULL DEFAULT 'pending'
                        CHECK (state IN ('pending','processing','completed','failed')),
    document_path   TEXT NOT NULL,
    document_title  TEXT,
    source_name     TEXT NOT NULL DEFAULT 'api',
    document_id     INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_jobs_state_created ON jobs(state, created_at);
"""


class Database:
    """Thin async wrapper around an asyncpg connection pool."""

    def __init__(self, dsn: str, *, min_size: int = 2, max_size: int = 10):
        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None
        self._min_size = min_size
        self._max_size = max_size

    # ── Lifecycle ───────────────────────────────────────────

    async def connect(self) -> None:
        """Create the connection pool and run migrations."""
        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
        )
        await self.migrate()
        logger.info("Database connected (pool size %d–%d)", self._min_size, self._max_size)

    async def disconnect(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def migrate(self) -> None:
        """Apply schema DDL idempotently."""
        async with self._pool.acquire() as conn:  # type: asyncpg.Connection
            await conn.execute(SCHEMA_SQL)

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[asyncpg.Connection]:
        """Acquire a connection from the pool for the duration of a context."""
        if self._pool is None:
            raise RuntimeError("Database not connected — call db.connect() first")
        async with self._pool.acquire() as conn:
            yield conn

    # ── Job Queue (SKIP LOCKED) ─────────────────────────────

    async def enqueue_job(
        self,
        document_path: str,
        *,
        document_title: Optional[str] = None,
        source_name: str = "api",
    ) -> JobRecord:
        """Insert a new job and return its record."""
        async with self.connection() as conn:
            row = await conn.fetchrow(
                """INSERT INTO jobs (document_path, document_title, source_name)
                   VALUES ($1, $2, $3)
                   RETURNING id, state, document_path, document_title, source_name,
                             document_id, error, created_at, updated_at""",
                document_path, document_title, source_name,
            )
        return JobRecord(
            id=str(row["id"]),
            state=JobState(row["state"]),
            document_path=row["document_path"],
            document_title=row["document_title"],
            source_name=row["source_name"],
            document_id=row["document_id"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def dequeue_job(self) -> Optional[JobRecord]:
        """Claim the oldest pending job using SKIP LOCKED.

        Returns ``None`` when the queue is empty.
        """
        async with self.connection() as conn:
            row = await conn.fetchrow(
                """WITH next_job AS (
                       SELECT id FROM jobs
                       WHERE state = 'pending'
                       ORDER BY created_at
                       LIMIT 1
                       FOR UPDATE SKIP LOCKED
                   )
                   UPDATE jobs SET state = 'processing', updated_at = now()
                   FROM next_job
                   WHERE jobs.id = next_job.id
                   RETURNING jobs.id, jobs.state, jobs.document_path,
                             jobs.document_title, jobs.source_name,
                             jobs.document_id, jobs.error,
                             jobs.created_at, jobs.updated_at"""
            )
        if row is None:
            return None
        return JobRecord(
            id=str(row["id"]),
            state=JobState(row["state"]),
            document_path=row["document_path"],
            document_title=row["document_title"],
            source_name=row["source_name"],
            document_id=row["document_id"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def complete_job(self, job_id: str, document_id: int) -> None:
        """Mark a job as completed, linking it to the created document."""
        async with self.connection() as conn:
            await conn.execute(
                """UPDATE jobs
                   SET state = 'completed', document_id = $2, updated_at = now()
                   WHERE id = $1::uuid""",
                job_id, document_id,
            )

    async def fail_job(self, job_id: str, error: str) -> None:
        """Mark a job as failed with an error message."""
        async with self.connection() as conn:
            await conn.execute(
                """UPDATE jobs
                   SET state = 'failed', error = $2, updated_at = now()
                   WHERE id = $1::uuid""",
                job_id, error,
            )

    async def get_job(self, job_id: str) -> Optional[JobRecord]:
        """Fetch a single job by ID."""
        async with self.connection() as conn:
            row = await conn.fetchrow(
                """SELECT id, state, document_path, document_title, source_name,
                          document_id, error, created_at, updated_at
                   FROM jobs WHERE id = $1::uuid""",
                job_id,
            )
        if row is None:
            return None
        return JobRecord(
            id=str(row["id"]),
            state=JobState(row["state"]),
            document_path=row["document_path"],
            document_title=row["document_title"],
            source_name=row["source_name"],
            document_id=row["document_id"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ── Document CRUD ───────────────────────────────────────

    async def upsert_document(
        self,
        path: str,
        source_type: str,
        source_name: str,
        title: str,
        ext: str,
        mime_type: str,
        body: str,
        file_hash: Optional[str] = None,
        mtime: float = 0.0,
        size: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Insert or update a document, returning its id."""
        async with self.connection() as conn:
            row = await conn.fetchrow(
                """INSERT INTO documents
                      (path, source_type, source_name, file_hash, mtime, size,
                       title, ext, mime_type, body, raw_preview, status, metadata)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                           left($10, 500), 'indexed', $11::jsonb)
                   ON CONFLICT (path) DO UPDATE SET
                       file_hash   = excluded.file_hash,
                       mtime       = excluded.mtime,
                       size        = excluded.size,
                       title       = excluded.title,
                       ext         = excluded.ext,
                       mime_type   = excluded.mime_type,
                       body        = excluded.body,
                       raw_preview = excluded.raw_preview,
                       status      = 'indexed',
                       metadata    = excluded.metadata,
                       updated_at  = now()
                   RETURNING id""",
                path,
                source_type,
                source_name,
                file_hash,
                mtime,
                size,
                title,
                ext,
                mime_type,
                body,
                metadata or {},
            )
        return row["id"]

    async def get_document(self, doc_id: int) -> Optional[dict[str, Any]]:
        """Fetch a document by its internal ID."""
        async with self.connection() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM documents WHERE id = $1", doc_id
            )
        return dict(row) if row else None

    async def update_summary(self, doc_id: int, summary: str) -> None:
        """Store an LLM-generated summary for a document."""
        async with self.connection() as conn:
            await conn.execute(
                """UPDATE documents
                   SET summary = $2, status = 'summarized', updated_at = now()
                   WHERE id = $1""",
                doc_id, summary,
            )

    async def fulltext_search(
        self, query: str, limit: int = 30
    ) -> list[dict[str, Any]]:
        """Full-text search using PostgreSQL tsvector."""
        async with self.connection() as conn:
            rows = await conn.fetch(
                """SELECT *, ts_rank(search_vector, plainto_tsquery('english', $1)) AS rank
                   FROM documents
                   WHERE search_vector @@ plainto_tsquery('english', $1)
                   ORDER BY rank DESC
                   LIMIT $2""",
                query, limit,
            )
        return [dict(r) for r in rows]
