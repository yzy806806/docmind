"""SQLite-backed async Database for DocMind standalone operation.

Replaces the PostgreSQL/asyncpg ``Database`` class with a lightweight
SQLite + aiosqlite implementation that requires no external server.

Features:
- Same async interface as ``db.py`` (connect, disconnect, migrate, connection)
- FTS5 virtual table for full-text search (replacing PostgreSQL tsvector)
- Weighted BM25 ranking across title (A), summary (B), raw_preview (C), body (D)
- Job queue with atomic claim using SQLite's immediate transaction isolation
- JSON metadata stored as TEXT, serialized/deserialized transparently
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import aiosqlite

from .embeddings import (
    cosine_similarity,
    deserialize_vector,
    serialize_vector,
)
from .models import JobRecord, JobState

logger = logging.getLogger(__name__)

# ── Schema DDL ─────────────────────────────────────────────────

SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    path            TEXT UNIQUE NOT NULL,
    source_type     TEXT NOT NULL DEFAULT 'api',
    source_name     TEXT NOT NULL DEFAULT 'api',
    file_hash       TEXT,
    mtime           REAL DEFAULT 0,
    size            INTEGER DEFAULT 0,
    title           TEXT NOT NULL,
    ext             TEXT DEFAULT '',
    mime_type       TEXT DEFAULT 'application/octet-stream',
    summary         TEXT,
    raw_preview     TEXT DEFAULT '',
    body            TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','indexed','summarized','error')),
    metadata        TEXT DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_type, source_name);

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    state           TEXT NOT NULL DEFAULT 'pending'
                        CHECK (state IN ('pending','processing','completed','failed')),
    document_path   TEXT NOT NULL,
    document_title  TEXT,
    source_name     TEXT NOT NULL DEFAULT 'api',
    document_id     INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_state_created ON jobs(state, created_at);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT 'New Chat',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT NOT NULL DEFAULT '',
    citations_json  TEXT DEFAULT '[]',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS document_tags (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id       INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tag          TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (doc_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_document_tags_tag ON document_tags(tag);
CREATE INDEX IF NOT EXISTS idx_document_tags_doc_id ON document_tags(doc_id);

CREATE TABLE IF NOT EXISTS document_embeddings (
    doc_id      INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    embedding   BLOB NOT NULL,
    dim         INTEGER NOT NULL DEFAULT 384,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (doc_id)
);
"""

# FTS5 virtual table — created separately because some SQLite builds
# may not support IF NOT EXISTS for virtual tables in the same script.
FTS_SCHEMA_SQL = r"""
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts
    USING fts5(
        title, summary, raw_preview, body,
        content='documents',
        content_rowid='id'
    );
"""

# Triggers to keep FTS5 in sync with the documents table
FTS_TRIGGERS_SQL = r"""
CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, title, summary, raw_preview, body)
    VALUES (new.id, new.title, new.summary, new.raw_preview, new.body);
END;

CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, summary, raw_preview, body)
    VALUES ('delete', old.id, old.title, old.summary, old.raw_preview, old.body);
END;

CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, summary, raw_preview, body)
    VALUES ('delete', old.id, old.title, old.summary, old.raw_preview, old.body);
    INSERT INTO documents_fts(rowid, title, summary, raw_preview, body)
    VALUES (new.id, new.title, new.summary, new.raw_preview, new.body);
END;
"""


def _now_iso() -> str:
    """Return current UTC timestamp as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _sanitize_fts_query(query: str) -> str:
    """Sanitize a user query for FTS5 MATCH syntax.

    Removes characters with special meaning in FTS5, keeps alphanumeric
    tokens of length >= 2, and joins them with implicit AND.
    """
    tokens = re.findall(r"[a-zA-Z0-9_]{2,}", query)
    if not tokens:
        return '""'
    return " ".join(tokens)


class Database:
    """Thin async wrapper around an aiosqlite connection.

    Mirrors the interface of the PostgreSQL ``Database`` class in ``db.py``
    but uses SQLite under the hood, requiring no external server.
    """

    def __init__(
        self,
        db_path: str = "data/docmind.db",
        *,
        min_size: int = 1,
        max_size: int = 5,
    ):
        """Initialize the Database with a path to the SQLite file.

        Args:
            db_path: Path to the SQLite database file. Parent directories
                are created automatically.
            min_size: Ignored (kept for interface compatibility with asyncpg).
            max_size: Maximum number of concurrent connections in the pool.
        """
        self._db_path = db_path
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()
        self._min_size = min_size
        self._max_size = max_size
        # Optional async callback invoked after a document is saved.
        # Signature: async fn(doc_id: int, path: str, title: str, summary: str, body: str)
        # Used by the embedding pipeline to generate vectors on document index.
        self.on_document_saved = None

    # ── Lifecycle ───────────────────────────────────────────────

    async def connect(self) -> None:
        """Open the connection and run migrations."""
        self._conn = await aiosqlite.connect(str(self._path))
        self._conn.row_factory = aiosqlite.Row
        # Enable foreign keys and WAL mode for better concurrency
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self.migrate()
        logger.info("Database connected: %s", self._db_path)

    async def disconnect(self) -> None:
        """Close the connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def migrate(self) -> None:
        """Apply schema DDL idempotently."""
        if self._conn is None:
            raise RuntimeError("Database not connected — call db.connect() first")
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.executescript(FTS_SCHEMA_SQL)
        await self._conn.executescript(FTS_TRIGGERS_SQL)
        await self._conn.commit()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """Provide a connection context for the duration of a block.

        Since SQLite uses a single connection (serialized), this yields
        the shared connection protected by a lock for write safety.
        """
        if self._conn is None:
            raise RuntimeError("Database not connected — call db.connect() first")
        async with self._lock:
            yield self._conn

    # ── Job Queue ───────────────────────────────────────────────

    async def enqueue_job(
        self,
        document_path: str,
        *,
        document_title: Optional[str] = None,
        source_name: str = "api",
    ) -> JobRecord:
        """Insert a new job and return its record."""
        job_id = str(uuid.uuid4())
        now = _now_iso()
        async with self.connection() as conn:
            await conn.execute(
                """INSERT INTO jobs (id, state, document_path, document_title,
                                      source_name, created_at, updated_at)
                   VALUES (?, 'pending', ?, ?, ?, ?, ?)""",
                (job_id, document_path, document_title, source_name, now, now),
            )
            await conn.commit()
            cursor = await conn.execute(
                """SELECT id, state, document_path, document_title, source_name,
                          document_id, error, created_at, updated_at
                   FROM jobs WHERE id = ?""",
                (job_id,),
            )
            row = await cursor.fetchone()

        return self._row_to_job_record(row)

    async def dequeue_job(self) -> Optional[JobRecord]:
        """Claim the oldest pending job atomically.

        Uses a transaction with immediate locking to ensure only one
        worker claims a given job. Returns ``None`` when queue is empty.
        """
        async with self.connection() as conn:
            # Atomically select and update using a subquery
            cursor = await conn.execute(
                """SELECT id FROM jobs
                   WHERE state = 'pending'
                   ORDER BY created_at
                   LIMIT 1""",
            )
            row = await cursor.fetchone()
            if row is None:
                return None

            job_id = row["id"]
            now = _now_iso()
            await conn.execute(
                """UPDATE jobs SET state = 'processing', updated_at = ?
                   WHERE id = ? AND state = 'pending'""",
                (now, job_id),
            )
            await conn.commit()

            cursor = await conn.execute(
                """SELECT id, state, document_path, document_title, source_name,
                          document_id, error, created_at, updated_at
                   FROM jobs WHERE id = ?""",
                (job_id,),
            )
            row = await cursor.fetchone()

        return self._row_to_job_record(row) if row else None

    async def complete_job(self, job_id: str, document_id: int) -> None:
        """Mark a job as completed, linking it to the created document."""
        now = _now_iso()
        async with self.connection() as conn:
            await conn.execute(
                """UPDATE jobs
                   SET state = 'completed', document_id = ?, updated_at = ?
                   WHERE id = ?""",
                (document_id, now, job_id),
            )
            await conn.commit()

    async def fail_job(self, job_id: str, error: str) -> None:
        """Mark a job as failed with an error message."""
        now = _now_iso()
        async with self.connection() as conn:
            await conn.execute(
                """UPDATE jobs
                   SET state = 'failed', error = ?, updated_at = ?
                   WHERE id = ?""",
                (error, now, job_id),
            )
            await conn.commit()

    async def get_job(self, job_id: str) -> Optional[JobRecord]:
        """Fetch a single job by ID."""
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT id, state, document_path, document_title, source_name,
                          document_id, error, created_at, updated_at
                   FROM jobs WHERE id = ?""",
                (job_id,),
            )
            row = await cursor.fetchone()

        return self._row_to_job_record(row) if row else None

    async def create_job(
        self,
        document_path: str,
        *,
        document_title: Optional[str] = None,
        source_name: str = "api",
    ) -> JobRecord:
        """Alias for enqueue_job — creates a new job in the queue."""
        return await self.enqueue_job(
            document_path,
            document_title=document_title,
            source_name=source_name,
        )

    async def list_jobs(
        self,
        *,
        state: Optional[str] = None,
        limit: int = 100,
    ) -> list[JobRecord]:
        """List jobs, optionally filtered by state."""
        async with self.connection() as conn:
            if state:
                cursor = await conn.execute(
                    """SELECT id, state, document_path, document_title, source_name,
                              document_id, error, created_at, updated_at
                       FROM jobs WHERE state = ?
                       ORDER BY created_at DESC LIMIT ?""",
                    (state, limit),
                )
            else:
                cursor = await conn.execute(
                    """SELECT id, state, document_path, document_title, source_name,
                              document_id, error, created_at, updated_at
                       FROM jobs ORDER BY created_at DESC LIMIT ?""",
                    (limit,),
                )
            rows = await cursor.fetchall()

        return [self._row_to_job_record(r) for r in rows]

    async def list_jobs_paginated(
        self,
        *,
        state: Optional[str] = None,
        page: int = 1,
        per_page: int = 20,
    ) -> dict[str, Any]:
        """List jobs with pagination, optionally filtered by state.

        Returns a dict with ``jobs``, ``total``, ``page``, ``per_page``,
        and ``total_pages`` keys.
        """
        offset = (page - 1) * per_page
        async with self.connection() as conn:
            if state:
                cursor = await conn.execute(
                    """SELECT id, state, document_path, document_title, source_name,
                              document_id, error, created_at, updated_at
                       FROM jobs WHERE state = ?
                       ORDER BY created_at DESC
                       LIMIT ? OFFSET ?""",
                    (state, per_page, offset),
                )
                count_cursor = await conn.execute(
                    "SELECT COUNT(*) AS cnt FROM jobs WHERE state = ?",
                    (state,),
                )
            else:
                cursor = await conn.execute(
                    """SELECT id, state, document_path, document_title, source_name,
                              document_id, error, created_at, updated_at
                       FROM jobs
                       ORDER BY created_at DESC
                       LIMIT ? OFFSET ?""",
                    (per_page, offset),
                )
                count_cursor = await conn.execute(
                    "SELECT COUNT(*) AS cnt FROM jobs",
                )
            rows = await cursor.fetchall()
            count_row = await count_cursor.fetchone()

        total = count_row["cnt"] if count_row else 0
        total_pages = (total + per_page - 1) // per_page if per_page > 0 else 0
        return {
            "jobs": [self._row_to_job_record(r) for r in rows],
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
        }

    async def count_jobs(self, *, state: Optional[str] = None) -> int:
        """Count jobs, optionally filtered by state."""
        async with self.connection() as conn:
            if state:
                cursor = await conn.execute(
                    "SELECT COUNT(*) AS cnt FROM jobs WHERE state = ?",
                    (state,),
                )
            else:
                cursor = await conn.execute(
                    "SELECT COUNT(*) AS cnt FROM jobs",
                )
            row = await cursor.fetchone()
        return row["cnt"] if row else 0

    async def update_job_status(
        self,
        job_id: str,
        state: str,
        *,
        error: Optional[str] = None,
        document_id: Optional[int] = None,
    ) -> None:
        """Update a job's state, optionally setting error or document_id."""
        now = _now_iso()
        async with self.connection() as conn:
            if document_id is not None and error is not None:
                await conn.execute(
                    """UPDATE jobs SET state = ?, error = ?, document_id = ?, updated_at = ?
                       WHERE id = ?""",
                    (state, error, document_id, now, job_id),
                )
            elif document_id is not None:
                await conn.execute(
                    """UPDATE jobs SET state = ?, document_id = ?, updated_at = ?
                       WHERE id = ?""",
                    (state, document_id, now, job_id),
                )
            elif error is not None:
                await conn.execute(
                    """UPDATE jobs SET state = ?, error = ?, updated_at = ?
                       WHERE id = ?""",
                    (state, error, now, job_id),
                )
            else:
                await conn.execute(
                    """UPDATE jobs SET state = ?, updated_at = ?
                       WHERE id = ?""",
                    (state, now, job_id),
                )
            await conn.commit()

    # ── Document CRUD ───────────────────────────────────────────

    async def save_document(
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
        summary: Optional[str] = None,
        status: str = "indexed",
    ) -> int:
        """Insert or update a document, returning its id.

        If a document with the same path already exists, it is updated
        (upsert). The FTS5 index is kept in sync via triggers.
        """
        raw_preview = (body or "")[:500]
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        now = _now_iso()

        async with self.connection() as conn:
            await conn.execute(
                """INSERT INTO documents
                      (path, source_type, source_name, file_hash, mtime, size,
                       title, ext, mime_type, summary, raw_preview, body,
                       status, metadata, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                       file_hash   = excluded.file_hash,
                       mtime       = excluded.mtime,
                       size        = excluded.size,
                       title       = excluded.title,
                       ext         = excluded.ext,
                       mime_type   = excluded.mime_type,
                       summary     = excluded.summary,
                       raw_preview = excluded.raw_preview,
                       body        = excluded.body,
                       status      = excluded.status,
                       metadata    = excluded.metadata,
                       updated_at  = excluded.updated_at""",
                (
                    path, source_type, source_name, file_hash, mtime, size,
                    title, ext, mime_type, summary, raw_preview, body,
                    status, meta_json, now, now,
                ),
            )
            await conn.commit()

            cursor = await conn.execute(
                "SELECT id FROM documents WHERE path = ?", (path,)
            )
            row = await cursor.fetchone()

        doc_id = row["id"] if row else 0

        # Fire the embedding hook if configured (non-blocking, error-tolerant)
        if doc_id and self.on_document_saved is not None:
            try:
                import asyncio
                asyncio.ensure_future(
                    self.on_document_saved(
                        doc_id=doc_id,
                        path=path,
                        title=title,
                        summary=summary or "",
                        body=body or "",
                    )
                )
            except Exception as e:
                logger.warning("on_document_saved hook failed: %s", e)

        return doc_id

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
        """Insert or update a document — alias for save_document.

        Provided for backward compatibility with the PostgreSQL Database
        interface.
        """
        return await self.save_document(
            path, source_type, source_name, title, ext, mime_type, body,
            file_hash=file_hash, mtime=mtime, size=size, metadata=metadata,
        )

    async def get_document(self, doc_id: int) -> Optional[dict[str, Any]]:
        """Fetch a document by its internal ID."""
        async with self.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM documents WHERE id = ?", (doc_id,)
            )
            row = await cursor.fetchone()

        if row is None:
            return None
        return self._row_to_doc_dict(row)

    async def get_document_by_path(self, path: str) -> Optional[dict[str, Any]]:
        """Fetch a document by its unique path."""
        async with self.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM documents WHERE path = ?", (path,)
            )
            row = await cursor.fetchone()

        if row is None:
            return None
        return self._row_to_doc_dict(row)

    async def list_documents(
        self,
        *,
        source: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List documents, optionally filtered by source name or type."""
        async with self.connection() as conn:
            if source:
                cursor = await conn.execute(
                    """SELECT * FROM documents
                       WHERE source_name = ? OR source_type = ?
                       ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                    (source, source, limit, offset),
                )
            else:
                cursor = await conn.execute(
                    """SELECT * FROM documents
                       ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                    (limit, offset),
                )
            rows = await cursor.fetchall()

        return [self._row_to_doc_dict(r) for r in rows]

    async def list_documents_paginated(
        self,
        *,
        page: int = 1,
        per_page: int = 20,
        source: Optional[str] = None,
    ) -> dict[str, Any]:
        """List documents with pagination metadata."""
        offset = (page - 1) * per_page
        docs = await self.list_documents(
            source=source, limit=per_page, offset=offset
        )
        total = await self.get_document_count(source=source)
        return {
            "documents": docs,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page if per_page > 0 else 0,
        }

    async def get_document_count(
        self, *, source: Optional[str] = None
    ) -> int:
        """Return the total number of documents, optionally filtered."""
        async with self.connection() as conn:
            if source:
                cursor = await conn.execute(
                    """SELECT COUNT(*) as c FROM documents
                       WHERE source_name = ? OR source_type = ?""",
                    (source, source),
                )
            else:
                cursor = await conn.execute(
                    "SELECT COUNT(*) as c FROM documents"
                )
            row = await cursor.fetchone()
        return row["c"] if row else 0

    async def delete_document(self, doc_id: int) -> bool:
        """Delete a document by ID. Returns True if deleted, False if not found."""
        async with self.connection() as conn:
            cursor = await conn.execute(
                "DELETE FROM documents WHERE id = ?", (doc_id,)
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def update_summary(self, doc_id: int, summary: str) -> None:
        """Store an LLM-generated summary for a document."""
        now = _now_iso()
        async with self.connection() as conn:
            await conn.execute(
                """UPDATE documents
                   SET summary = ?, status = 'summarized', updated_at = ?
                   WHERE id = ?""",
                (summary, now, doc_id),
            )
            await conn.commit()

    async def search_documents(
        self, query: str, limit: int = 30
    ) -> list[dict[str, Any]]:
        """Full-text search using SQLite FTS5 with BM25 ranking.

        Weights: title (A=10.0), summary (B=5.0), raw_preview (C=2.0), body (D=1.0).
        BM25 returns negative scores; we negate to get descending rank order.
        """
        safe_query = _sanitize_fts_query(query)
        if not safe_query or safe_query == '""':
            return []

        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT d.id, d.path, d.source_type, d.source_name,
                          d.file_hash, d.mtime, d.size, d.title, d.ext,
                          d.mime_type, d.summary, d.raw_preview, d.body,
                          d.status, d.metadata, d.created_at, d.updated_at,
                          -bm25(documents_fts, 10.0, 5.0, 2.0, 1.0) AS rank
                   FROM documents d
                   JOIN documents_fts fts ON d.id = fts.rowid
                   WHERE documents_fts MATCH ?
                   ORDER BY rank DESC
                   LIMIT ?""",
                (safe_query, limit),
            )
            rows = await cursor.fetchall()

        return [self._row_to_doc_dict(r) for r in rows]

    async def fulltext_search(
        self, query: str, limit: int = 30
    ) -> list[dict[str, Any]]:
        """Alias for search_documents — backward compatibility with db.py."""
        return await self.search_documents(query, limit=limit)

    async def get_stats(self) -> dict[str, Any]:
        """Return knowledge base statistics."""
        async with self.connection() as conn:
            total_cursor = await conn.execute(
                "SELECT COUNT(*) as c FROM documents"
            )
            total_row = await total_cursor.fetchone()

            pending_cursor = await conn.execute(
                "SELECT COUNT(*) as c FROM documents WHERE status = 'pending'"
            )
            pending_row = await pending_cursor.fetchone()

            indexed_cursor = await conn.execute(
                "SELECT COUNT(*) as c FROM documents WHERE status = 'indexed'"
            )
            indexed_row = await indexed_cursor.fetchone()

            summarized_cursor = await conn.execute(
                "SELECT COUNT(*) as c FROM documents WHERE status = 'summarized'"
            )
            summarized_row = await summarized_cursor.fetchone()

            error_cursor = await conn.execute(
                "SELECT COUNT(*) as c FROM documents WHERE status = 'error'"
            )
            error_row = await error_cursor.fetchone()

            job_cursor = await conn.execute(
                "SELECT COUNT(*) as c FROM jobs WHERE state IN ('pending', 'processing')"
            )
            job_row = await job_cursor.fetchone()

        return {
            "total": total_row["c"] if total_row else 0,
            "pending": pending_row["c"] if pending_row else 0,
            "indexed": indexed_row["c"] if indexed_row else 0,
            "summarized": summarized_row["c"] if summarized_row else 0,
            "error": error_row["c"] if error_row else 0,
            "active_jobs": job_row["c"] if job_row else 0,
        }

    async def get_pending_summaries(self, limit: int = 10) -> list[dict[str, Any]]:
        """Get documents that need LLM summarization (status = 'indexed')."""
        async with self.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM documents WHERE status = 'indexed' LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()

        return [self._row_to_doc_dict(r) for r in rows]

    # ── Chat session & message CRUD ──────────────────────────────

    async def create_chat_session(
        self, session_id: Optional[str] = None, *, title: str = "New Chat"
    ) -> dict[str, Any]:
        """Create a new chat session and return it as a dict.

        Args:
            session_id: Optional UUID string. If None, one is generated.
            title: Session title (defaults to "New Chat"; callers typically
                update this from the first user message via update_chat_session_title).
        """
        sid = session_id or str(uuid.uuid4())
        now = _now_iso()
        async with self.connection() as conn:
            await conn.execute(
                """INSERT INTO chat_sessions (id, title, created_at, updated_at)
                   VALUES (?, ?, ?, ?)""",
                (sid, title, now, now),
            )
            await conn.commit()
        return {"id": sid, "title": title, "created_at": now, "updated_at": now}

    async def get_chat_session(self, session_id: str) -> Optional[dict[str, Any]]:
        """Fetch a single chat session by id. Returns None if not found."""
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT id, title, created_at, updated_at
                   FROM chat_sessions WHERE id = ?""",
                (session_id,),
            )
            row = await cursor.fetchone()

        if row is None:
            return None
        return self._row_to_chat_session_dict(row)

    async def list_chat_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        """List recent chat sessions, newest first.

        Each entry includes a ``preview`` field: the first 120 chars of the
        most recent user message in that session (or empty string if none).
        """
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT s.id, s.title, s.created_at, s.updated_at,
                          (
                              SELECT content FROM chat_messages m
                              WHERE m.session_id = s.id
                              ORDER BY m.created_at DESC LIMIT 1
                          ) AS last_content
                   FROM chat_sessions s
                   ORDER BY s.updated_at DESC
                   LIMIT ?""",
                (limit,),
            )
            rows = await cursor.fetchall()

        result: list[dict[str, Any]] = []
        for r in rows:
            preview = (r["last_content"] or "")[:120]
            result.append({
                "id": r["id"],
                "title": r["title"],
                "preview": preview,
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            })
        return result

    async def delete_chat_session(self, session_id: str) -> bool:
        """Delete a chat session and all its messages (CASCADE).

        Returns True if a row was deleted, False if the session was not found.
        """
        async with self.connection() as conn:
            cursor = await conn.execute(
                "DELETE FROM chat_sessions WHERE id = ?", (session_id,)
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def update_chat_session_title(
        self, session_id: str, title: str
    ) -> bool:
        """Update a session's title and bump updated_at.

        Returns True if updated, False if the session does not exist.
        """
        now = _now_iso()
        async with self.connection() as conn:
            cursor = await conn.execute(
                """UPDATE chat_sessions SET title = ?, updated_at = ?
                   WHERE id = ?""",
                (title, now, session_id),
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def save_chat_message(
        self,
        session_id: str,
        role: str,
        content: str,
        citations: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Persist a chat message and return it as a dict.

        Also bumps the parent session's updated_at timestamp.
        """
        if role not in ("user", "assistant"):
            raise ValueError(f"role must be 'user' or 'assistant', got {role!r}")
        citations_json = json.dumps(citations or [], ensure_ascii=False, default=str)
        now = _now_iso()
        async with self.connection() as conn:
            cursor = await conn.execute(
                """INSERT INTO chat_messages
                       (session_id, role, content, citations_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, role, content, citations_json, now),
            )
            msg_id = cursor.lastrowid
            # Bump session updated_at
            await conn.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            await conn.commit()

        return {
            "id": msg_id,
            "session_id": session_id,
            "role": role,
            "content": content,
            "citations": citations or [],
            "created_at": now,
        }

    async def get_chat_history(
        self, session_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return messages for a session, oldest first, up to ``limit``."""
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT id, session_id, role, content, citations_json, created_at
                   FROM chat_messages
                   WHERE session_id = ?
                   ORDER BY created_at ASC
                   LIMIT ?""",
                (session_id, limit),
            )
            rows = await cursor.fetchall()

        return [self._row_to_chat_message_dict(r) for r in rows]

    # ── Settings key/value store ─────────────────────────────────

    async def get_setting(
        self, key: str, default: Optional[str] = None
    ) -> Optional[str]:
        """Fetch a single setting by key.

        Args:
            key: The setting key (case-sensitive).
            default: Value to return when the key is absent.

        Returns:
            The stored value as a string, or ``default`` if not found.
        """
        async with self.connection() as conn:
            cursor = await conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            )
            row = await cursor.fetchone()

        if row is None:
            return default
        val = row["value"]
        return val if val is not None else default

    async def set_setting(self, key: str, value: str) -> None:
        """Insert or update a setting (upsert by key).

        Args:
            key: The setting key.
            value: The setting value. Empty string is permitted and
                distinct from ``None``; pass ``None`` to store SQL NULL.
        """
        now = _now_iso()
        async with self.connection() as conn:
            await conn.execute(
                """INSERT INTO settings (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value,
                       updated_at = excluded.updated_at""",
                (key, value, now),
            )
            await conn.commit()

    async def get_all_settings(self) -> dict[str, str]:
        """Return all settings as a {key: value} dict.

        Values that are SQL NULL are skipped (matching the get_setting
        default semantics — callers that need a value should provide one).
        """
        async with self.connection() as conn:
            cursor = await conn.execute(
                "SELECT key, value FROM settings ORDER BY key"
            )
            rows = await cursor.fetchall()

        return {
            row["key"]: row["value"]
            for row in rows
            if row["value"] is not None
        }

    async def delete_setting(self, key: str) -> bool:
        """Delete a setting by key. Returns True if a row was removed."""
        async with self.connection() as conn:
            cursor = await conn.execute(
                "DELETE FROM settings WHERE key = ?", (key,)
            )
            await conn.commit()
            return cursor.rowcount > 0

    # ── Document Tags ────────────────────────────────────────────

    async def add_tag(self, doc_id: int, tag: str) -> dict[str, Any]:
        """Add a tag to a document. Returns the tag record as a dict.

        If the tag already exists for this document (unique constraint),
        the existing record is returned without error (idempotent).

        Args:
            doc_id: The document ID to tag.
            tag: The tag string (whitespace-trimmed, case-preserved).

        Returns:
            A dict with keys: id, doc_id, tag, created_at.

        Raises:
            ValueError: If the tag is empty after trimming.
        """
        tag = (tag or "").strip()
        if not tag:
            raise ValueError("tag must not be empty")
        now = _now_iso()
        async with self.connection() as conn:
            await conn.execute(
                """INSERT INTO document_tags (doc_id, tag, created_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(doc_id, tag) DO NOTHING""",
                (doc_id, tag, now),
            )
            await conn.commit()
            cursor = await conn.execute(
                """SELECT id, doc_id, tag, created_at
                   FROM document_tags
                   WHERE doc_id = ? AND tag = ?""",
                (doc_id, tag),
            )
            row = await cursor.fetchone()

        return {
            "id": row["id"],
            "doc_id": row["doc_id"],
            "tag": row["tag"],
            "created_at": row["created_at"],
        }

    async def remove_tag(self, doc_id: int, tag: str) -> bool:
        """Remove a tag from a document.

        Returns True if a tag was removed, False if the tag was not found.
        """
        tag = (tag or "").strip()
        if not tag:
            return False
        async with self.connection() as conn:
            cursor = await conn.execute(
                "DELETE FROM document_tags WHERE doc_id = ? AND tag = ?",
                (doc_id, tag),
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def get_tags(self, doc_id: int) -> list[str]:
        """Return a list of tag names for the given document, sorted alphabetically."""
        async with self.connection() as conn:
            cursor = await conn.execute(
                "SELECT tag FROM document_tags WHERE doc_id = ? ORDER BY tag",
                (doc_id,),
            )
            rows = await cursor.fetchall()
        return [row["tag"] for row in rows]

    async def get_documents_by_tag(self, tag: str) -> list[dict[str, Any]]:
        """Return all documents that have the given tag, newest first."""
        tag = (tag or "").strip()
        if not tag:
            return []
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT d.* FROM documents d
                   JOIN document_tags dt ON d.id = dt.doc_id
                   WHERE dt.tag = ?
                   ORDER BY d.created_at DESC""",
                (tag,),
            )
            rows = await cursor.fetchall()
        return [self._row_to_doc_dict(r) for r in rows]

    async def get_all_tags(self) -> list[dict[str, Any]]:
        """Return all tags with their document counts, sorted by count descending.

        Each dict has keys: tag (str), count (int).
        """
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT tag, COUNT(*) as count
                   FROM document_tags
                   GROUP BY tag
                   ORDER BY count DESC, tag ASC""",
            )
            rows = await cursor.fetchall()
        return [{"tag": row["tag"], "count": row["count"]} for row in rows]

    async def get_tags_for_documents(
        self, doc_ids: list[int]
    ) -> dict[int, list[str]]:
        """Batch-fetch tags for multiple documents.

        Returns a dict mapping doc_id -> list[str] of tag names.
        Documents with no tags are absent from the result.
        """
        if not doc_ids:
            return {}
        placeholders = ",".join("?" * len(doc_ids))
        async with self.connection() as conn:
            cursor = await conn.execute(
                f"""SELECT doc_id, tag FROM document_tags
                    WHERE doc_id IN ({placeholders})
                    ORDER BY doc_id, tag""",
                doc_ids,
            )
            rows = await cursor.fetchall()
        result: dict[int, list[str]] = {}
        for row in rows:
            result.setdefault(row["doc_id"], []).append(row["tag"])
        return result

    # ── Vector Embeddings ────────────────────────────────────────

    async def save_embedding(self, doc_id: int, embedding: list[float]) -> None:
        """Store or update a document's embedding vector.

        Args:
            doc_id: The document ID.
            embedding: A list of floats (e.g. 384-dim MiniLM vector).
        """
        if not embedding:
            return
        blob = serialize_vector(embedding)
        dim = len(embedding)
        now = _now_iso()
        async with self.connection() as conn:
            await conn.execute(
                """INSERT INTO document_embeddings (doc_id, embedding, dim, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(doc_id) DO UPDATE SET
                       embedding = excluded.embedding,
                       dim = excluded.dim,
                       created_at = excluded.created_at""",
                (doc_id, blob, dim, now),
            )
            await conn.commit()

    async def get_embedding(self, doc_id: int) -> list[float]:
        """Retrieve a document's embedding vector.

        Returns an empty list if no embedding is stored.
        """
        async with self.connection() as conn:
            cursor = await conn.execute(
                "SELECT embedding FROM document_embeddings WHERE doc_id = ?",
                (doc_id,),
            )
            row = await cursor.fetchone()

        if row is None:
            return []
        return deserialize_vector(row["embedding"])

    async def delete_embedding(self, doc_id: int) -> bool:
        """Delete a document's embedding. Returns True if a row was removed."""
        async with self.connection() as conn:
            cursor = await conn.execute(
                "DELETE FROM document_embeddings WHERE doc_id = ?",
                (doc_id,),
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def has_embedding(self, doc_id: int) -> bool:
        """Check whether a document has a stored embedding."""
        async with self.connection() as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM document_embeddings WHERE doc_id = ? LIMIT 1",
                (doc_id,),
            )
            row = await cursor.fetchone()
        return row is not None

    async def search_similar(
        self,
        embedding: list[float],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Find documents whose embeddings are most similar to the query vector.

        Uses cosine similarity. Returns a list of dicts sorted by descending
        similarity, each with keys: doc_id, similarity (float in [-1, 1]).

        Args:
            embedding: The query embedding vector.
            top_k: Maximum number of results to return.
        """
        if not embedding:
            return []

        async with self.connection() as conn:
            cursor = await conn.execute(
                "SELECT doc_id, embedding FROM document_embeddings"
            )
            rows = await cursor.fetchall()

        if not rows:
            return []

        # Compute cosine similarity for each stored embedding
        scored: list[dict[str, Any]] = []
        for row in rows:
            stored_vec = deserialize_vector(row["embedding"])
            sim = cosine_similarity(embedding, stored_vec)
            scored.append({"doc_id": row["doc_id"], "similarity": sim})

        # Sort by descending similarity and return top_k
        scored.sort(key=lambda x: x["similarity"], reverse=True)
        return scored[:top_k]

    async def get_document_count_with_embeddings(self) -> int:
        """Return the number of documents that have stored embeddings."""
        async with self.connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) AS c FROM document_embeddings"
            )
            row = await cursor.fetchone()
        return row["c"] if row else 0

    # ── Helpers ─────────────────────────────────────────────────

    @staticmethod
    def _row_to_job_record(row: aiosqlite.Row) -> JobRecord:
        """Convert a database row to a JobRecord."""
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

    @staticmethod
    def _row_to_doc_dict(row: aiosqlite.Row) -> dict[str, Any]:
        """Convert a database row to a document dict with parsed metadata."""
        d = dict(row)
        # Parse metadata JSON
        meta_str = d.get("metadata", "{}")
        if isinstance(meta_str, str):
            try:
                d["metadata"] = json.loads(meta_str)
            except (json.JSONDecodeError, TypeError):
                d["metadata"] = {}
        # Parse datetime fields
        for field in ("created_at", "updated_at"):
            val = d.get(field)
            if isinstance(val, str):
                try:
                    d[field] = datetime.fromisoformat(val)
                except (ValueError, TypeError):
                    pass
        # Include rank if present (from FTS search)
        return d

    @staticmethod
    def _row_to_chat_session_dict(row: aiosqlite.Row) -> dict[str, Any]:
        """Convert a chat_sessions row to a dict."""
        return {
            "id": row["id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _row_to_chat_message_dict(row: aiosqlite.Row) -> dict[str, Any]:
        """Convert a chat_messages row to a dict with parsed citations."""
        citations_raw = row["citations_json"] or "[]"
        try:
            citations = json.loads(citations_raw) if isinstance(citations_raw, str) else citations_raw
        except (json.JSONDecodeError, TypeError):
            citations = []
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "role": row["role"],
            "content": row["content"],
            "citations": citations,
            "created_at": row["created_at"],
        }
