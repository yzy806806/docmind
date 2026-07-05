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
from .cache import (
    CacheBackend,
    CacheTTLConfig,
    create_cache_backend,
    hash_params,
    make_key,
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
    document_type   TEXT DEFAULT 'other',
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','indexed','summarized','error')),
    metadata        TEXT DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_type, source_name);
CREATE INDEX IF NOT EXISTS idx_documents_type ON documents(document_type);

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

CREATE TABLE IF NOT EXISTS search_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    query           TEXT NOT NULL,
    results_count   INTEGER NOT NULL DEFAULT 0,
    searched_at     TEXT NOT NULL DEFAULT (datetime('now')),
    user_session    TEXT
);

CREATE INDEX IF NOT EXISTS idx_search_log_searched_at ON search_log(searched_at);
CREATE INDEX IF NOT EXISTS idx_search_log_query ON search_log(query);

CREATE TABLE IF NOT EXISTS document_chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id          INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL DEFAULT 0,
    content         TEXT NOT NULL DEFAULT '',
    start_char      INTEGER NOT NULL DEFAULT 0,
    end_char        INTEGER NOT NULL DEFAULT 0,
    token_count     INTEGER NOT NULL DEFAULT 0,
    embedding       BLOB,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (doc_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_document_chunks_doc_id ON document_chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_document_chunks_chunk_index ON document_chunks(chunk_index);

CREATE TABLE IF NOT EXISTS collections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    description     TEXT DEFAULT '',
    parent_id       INTEGER REFERENCES collections(id) ON DELETE CASCADE,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_collections_name_parent ON collections(parent_id, name);

-- Root-level collections (parent_id IS NULL) also need name uniqueness.
-- SQLite treats NULLs as distinct, so the index above doesn't catch
-- duplicate root names. This partial index closes that gap.
CREATE UNIQUE INDEX IF NOT EXISTS uq_collections_root_name
    ON collections(name) WHERE parent_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_collections_parent_id ON collections(parent_id);
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

# FTS5 virtual table for document chunks — enables keyword search at the
# chunk level for more granular retrieval.
CHUNK_FTS_SCHEMA_SQL = r"""
CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts
    USING fts5(
        content,
        content='document_chunks',
        content_rowid='id'
    );
"""

# Triggers to keep chunk FTS5 in sync with the document_chunks table
CHUNK_FTS_TRIGGERS_SQL = r"""
CREATE TRIGGER IF NOT EXISTS document_chunks_ai AFTER INSERT ON document_chunks BEGIN
    INSERT INTO document_chunks_fts(rowid, content)
    VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS document_chunks_ad AFTER DELETE ON document_chunks BEGIN
    INSERT INTO document_chunks_fts(document_chunks_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS document_chunks_au AFTER UPDATE ON document_chunks BEGIN
    INSERT INTO document_chunks_fts(document_chunks_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
    INSERT INTO document_chunks_fts(rowid, content)
    VALUES (new.id, new.content);
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
        cache: Optional[CacheBackend] = None,
    ):
        """Initialize the Database with a path to the SQLite file.

        Args:
            db_path: Path to the SQLite database file. Parent directories
                are created automatically.
            min_size: Ignored (kept for interface compatibility with asyncpg).
            max_size: Maximum number of concurrent connections in the pool.
            cache: Optional CacheBackend for query result caching. If None,
                one is created from environment config via
                ``create_cache_backend()``. Pass a ``NoopCache`` to disable.
        """
        self._db_path = db_path
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()
        self._min_size = min_size
        self._max_size = max_size
        # Cache backend for read-path caching (cache-aside pattern).
        self._cache: CacheBackend = cache or create_cache_backend()
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

    # ── Cache Helpers ───────────────────────────────────────────

    async def _invalidate_document_mutations(self, doc_id: Optional[int] = None) -> None:
        """Invalidate cache keys affected by document mutations.

        Called after document create/update/delete operations.
        If doc_id is provided, also invalidates per-document keys.
        """
        await self._cache.delete_pattern("docmind:docs:list:*")
        await self._cache.delete_pattern("docmind:search:fts:*")
        await self._cache.delete_pattern("docmind:docs:by_tag:*")
        await self._cache.delete("docmind:analytics:stats")
        await self._cache.delete("docmind:analytics:storage")
        await self._cache.delete("docmind:analytics:tag_dist")
        await self._cache.delete_pattern("docmind:analytics:growth:*")
        await self._cache.delete("docmind:collection:counts")
        await self._cache.delete("docmind:tag:all")
        await self._cache.delete_pattern("docmind:analytics:file_type_facets")
        await self._cache.delete_pattern("docmind:analytics:source_facets")
        await self._cache.delete("docmind:doc:type:facet")
        if doc_id is not None:
            await self._cache.delete(make_key("docmind", "doc", "get", doc_id))
            await self._cache.delete(make_key("docmind", "tag", "get", doc_id))
            # Path-based cache uses hash_params(path=path), not doc_id.
            # Use wildcard to invalidate all by_path entries (path may change on save).
            await self._cache.delete_pattern("docmind:doc:by_path:*")

    async def _invalidate_tag_mutations(self, doc_id: Optional[int] = None) -> None:
        """Invalidate cache keys affected by tag add/remove."""
        if doc_id is not None:
            await self._cache.delete(make_key("docmind", "tag", "get", doc_id))
        await self._cache.delete("docmind:tag:all")
        await self._cache.delete_pattern("docmind:docs:by_tag:*")
        await self._cache.delete("docmind:analytics:tag_dist")
        await self._cache.delete("docmind:analytics:stats")

    async def _invalidate_collection_mutations(self, collection_id: Optional[int] = None) -> None:
        """Invalidate cache keys affected by collection mutations."""
        if collection_id is not None:
            await self._cache.delete(make_key("docmind", "collection", "get", collection_id))
        await self._cache.delete("docmind:collection:tree")
        await self._cache.delete("docmind:collection:counts")
        await self._cache.delete_pattern("docmind:docs:list:*")
        await self._cache.delete("docmind:analytics:stats")

    async def _invalidate_job_mutations(self, job_id: Optional[str] = None) -> None:
        """Invalidate cache keys affected by job state changes."""
        await self._cache.delete_pattern("docmind:jobs:list:*")
        await self._cache.delete("docmind:analytics:job_stats")
        await self._cache.delete("docmind:analytics:stats")
        if job_id is not None:
            await self._cache.delete(make_key("docmind", "job", "get", job_id))

    async def _invalidate_chat_mutations(self, session_id: Optional[str] = None) -> None:
        """Invalidate cache keys affected by chat mutations."""
        await self._cache.delete_pattern("docmind:chat:sessions:*")
        await self._cache.delete_pattern("docmind:analytics:chat_activity:*")
        if session_id is not None:
            await self._cache.delete_pattern(
                make_key("docmind", "chat", "messages", session_id, "*")
            )

    async def migrate(self) -> None:
        """Apply schema DDL idempotently."""
        if self._conn is None:
            raise RuntimeError("Database not connected — call db.connect() first")
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.executescript(FTS_SCHEMA_SQL)
        await self._conn.executescript(FTS_TRIGGERS_SQL)
        await self._conn.executescript(CHUNK_FTS_SCHEMA_SQL)
        await self._conn.executescript(CHUNK_FTS_TRIGGERS_SQL)

        # Idempotent ALTER TABLE: add collection_id column to documents
        # if it does not already exist.
        cursor = await self._conn.execute("PRAGMA table_info(documents)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "collection_id" not in columns:
            await self._conn.execute(
                "ALTER TABLE documents ADD COLUMN collection_id INTEGER "
                "REFERENCES collections(id) ON DELETE SET NULL"
            )

        # Idempotent ALTER TABLE: add document_type column for Phase 5b
        # auto-detection if it does not already exist.
        if "document_type" not in columns:
            await self._conn.execute(
                "ALTER TABLE documents ADD COLUMN document_type TEXT DEFAULT 'other'"
            )

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

        job = self._row_to_job_record(row)
        await self._invalidate_job_mutations(job_id)
        return job

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

        if row:
            await self._invalidate_job_mutations(str(row["id"]))
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
        await self._invalidate_job_mutations(job_id)
        await self._invalidate_document_mutations(document_id)

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
        await self._invalidate_job_mutations(job_id)

    async def get_job(self, job_id: str) -> Optional[JobRecord]:
        """Fetch a single job by ID."""
        key = make_key("docmind", "job", "get", job_id)
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT id, state, document_path, document_title, source_name,
                          document_id, error, created_at, updated_at
                   FROM jobs WHERE id = ?""",
                (job_id,),
            )
            row = await cursor.fetchone()

        result = self._row_to_job_record(row) if row else None
        if result is not None:
            await self._cache.set(key, result, ttl=CacheTTLConfig.job_detail)
        return result

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
        cache_key = make_key(
            "docmind", "jobs", "list",
            hash_params(state=state, page=page, per_page=per_page),
        )
        cached = await self._cache.get(cache_key)
        if cached is not None:
            return cached

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
        result = {
            "jobs": [self._row_to_job_record(r) for r in rows],
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
        }
        await self._cache.set(cache_key, result, ttl=CacheTTLConfig.job_list)
        return result

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
        await self._invalidate_job_mutations(job_id)

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
        document_type: str = "other",
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
                       document_type, status, metadata, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                       file_hash      = excluded.file_hash,
                       mtime          = excluded.mtime,
                       size           = excluded.size,
                       title          = excluded.title,
                       ext            = excluded.ext,
                       mime_type      = excluded.mime_type,
                       summary        = excluded.summary,
                       raw_preview    = excluded.raw_preview,
                       body           = excluded.body,
                       document_type  = excluded.document_type,
                       status         = excluded.status,
                       metadata       = excluded.metadata,
                       updated_at     = excluded.updated_at""",
                (
                    path, source_type, source_name, file_hash, mtime, size,
                    title, ext, mime_type, summary, raw_preview, body,
                    document_type, status, meta_json, now, now,
                ),
            )
            await conn.commit()

            cursor = await conn.execute(
                "SELECT id FROM documents WHERE path = ?", (path,)
            )
            row = await cursor.fetchone()

        doc_id = row["id"] if row else 0

        # Invalidate cache for document mutations
        await self._invalidate_document_mutations(doc_id if doc_id else None)

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
        document_type: str = "other",
    ) -> int:
        """Insert or update a document — alias for save_document.

        Provided for backward compatibility with the PostgreSQL Database
        interface.
        """
        return await self.save_document(
            path, source_type, source_name, title, ext, mime_type, body,
            file_hash=file_hash, mtime=mtime, size=size, metadata=metadata,
            document_type=document_type,
        )

    async def update_document_type(self, doc_id: int, doc_type: str) -> bool:
        """Update the document_type column for a document.

        Args:
            doc_id: Internal document ID.
            doc_type: One of the type keys from DOCUMENT_TYPES.

        Returns:
            True if the row was updated, False if the document was not found.
        """
        now = _now_iso()
        async with self.connection() as conn:
            cursor = await conn.execute(
                """UPDATE documents SET document_type = ?, updated_at = ?
                   WHERE id = ?""",
                (doc_type, now, doc_id),
            )
            await conn.commit()
            updated = cursor.rowcount > 0

        if updated:
            await self._invalidate_document_mutations(doc_id)

        return updated

    async def get_documents_by_type(
        self,
        doc_type: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Fetch all documents of a given type.

        Args:
            doc_type: One of the type keys from DOCUMENT_TYPES.
            limit: Maximum number of results.
            offset: Pagination offset.

        Returns:
            List of document dicts.
        """
        key = make_key("docmind", "docs", "by_type", doc_type, offset, limit)
        cached = await self._cache.get(key)
        if cached is not None:
            return cached

        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT * FROM documents
                   WHERE document_type = ?
                   ORDER BY updated_at DESC
                   LIMIT ? OFFSET ?""",
                (doc_type, limit, offset),
            )
            rows = await cursor.fetchall()

        result = [self._row_to_doc_dict(r) for r in rows]
        await self._cache.set(key, result, ttl=CacheTTLConfig.doc_list)
        return result

    async def get_document_type_facet(self) -> list[dict[str, Any]]:
        """Return document_type counts for faceted filtering.

        Returns:
            List of {"value": type_key, "count": N} dicts, sorted by
            count descending.
        """
        key = "docmind:doc:type:facet"
        cached = await self._cache.get(key)
        if cached is not None:
            return cached

        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT document_type AS value, COUNT(*) AS count
                   FROM documents
                   GROUP BY document_type
                   ORDER BY count DESC"""
            )
            rows = await cursor.fetchall()

        result = [{"value": r["value"], "count": r["count"]} for r in rows]
        await self._cache.set(key, result, ttl=CacheTTLConfig.doc_list)
        return result

    async def get_document(self, doc_id: int) -> Optional[dict[str, Any]]:
        """Fetch a document by its internal ID."""
        key = make_key("docmind", "doc", "get", doc_id)
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        async with self.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM documents WHERE id = ?", (doc_id,)
            )
            row = await cursor.fetchone()

        if row is None:
            return None
        result = self._row_to_doc_dict(row)
        await self._cache.set(key, result, ttl=CacheTTLConfig.doc_single)
        return result

    async def get_document_by_path(self, path: str) -> Optional[dict[str, Any]]:
        """Fetch a document by its unique path."""
        path_hash = hash_params(path=path)
        key = make_key("docmind", "doc", "by_path", path_hash)
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        async with self.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM documents WHERE path = ?", (path,)
            )
            row = await cursor.fetchone()

        if row is None:
            return None
        result = self._row_to_doc_dict(row)
        await self._cache.set(key, result, ttl=CacheTTLConfig.doc_by_path)
        return result

    def _build_filter_clause(
        self,
        *,
        source: Optional[str] = None,
        collection_id: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        file_type: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> tuple[str, list[Any]]:
        """Build a WHERE clause + params for document filtering.

        This centralises the optional-parameter branching so that
        ``list_documents``, ``get_document_count``, and future methods
        (e.g. with ``user_id`` scoping) share the same logic.

        Args:
            source: Filter by source_name OR source_type.
            collection_id: Restrict to a collection. 0 = unassigned (IS NULL).
            date_from: ISO date string (inclusive). Compared against created_at.
            date_to: ISO date string (inclusive). Compared against created_at.
            file_type: Filter by file extension (e.g. '.pdf', 'pdf').
            tag: Filter by tag name (requires JOIN on document_tags).

        Returns:
            (where_clause, params) — the clause starts with ``WHERE`` or
            is empty string when no filters are active.  When ``tag`` is
            set, a ``JOIN document_tags`` clause is also included; callers
            that already JOIN should use ``_build_filter_clause`` without
            ``tag`` and add the tag join separately.
        """
        conditions: list[str] = []
        params: list[Any] = []

        if source:
            conditions.append("(source_name = ? OR source_type = ?)")
            params.extend([source, source])

        if collection_id is not None:
            if collection_id == 0:
                conditions.append("collection_id IS NULL")
            else:
                conditions.append("collection_id = ?")
                params.append(collection_id)

        if date_from:
            conditions.append("created_at >= ?")
            params.append(date_from)

        if date_to:
            # Ensure date_to covers the full day by appending time if
            # the user only provided a date (no time component).
            if "T" not in date_to and " " not in date_to and len(date_to) == 10:
                date_to = date_to + " 23:59:59"
            conditions.append("created_at <= ?")
            params.append(date_to)

        if file_type:
            # Normalise: ensure leading dot for ext column match
            ft = file_type.strip()
            if not ft.startswith("."):
                ft = "." + ft
            conditions.append("ext = ?")
            params.append(ft)

        if tag:
            conditions.append("id IN (SELECT doc_id FROM document_tags WHERE tag = ?)")
            params.append(tag)

        if not conditions:
            return ("", [])

        where = "WHERE " + " AND ".join(conditions)
        return (where, params)

    async def list_documents(
        self,
        *,
        source: Optional[str] = None,
        collection_id: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        file_type: Optional[str] = None,
        tag: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List documents with optional multi-filter support.

        All filter parameters are optional and combine with AND logic:
        ``source``, ``collection_id``, ``date_from``, ``date_to``,
        ``file_type``, and ``tag``.

        When ``collection_id`` is ``0``, lists unassigned documents
        (collection_id IS NULL).
        """
        where, params = self._build_filter_clause(
            source=source, collection_id=collection_id,
            date_from=date_from, date_to=date_to,
            file_type=file_type, tag=tag,
        )
        sql = (
            f"SELECT * FROM documents {where} "
            f"ORDER BY created_at DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])
        async with self.connection() as conn:
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
        return [self._row_to_doc_dict(r) for r in rows]

    async def list_documents_paginated(
        self,
        *,
        page: int = 1,
        per_page: int = 20,
        source: Optional[str] = None,
        collection_id: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        file_type: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> dict[str, Any]:
        """List documents with pagination metadata and multi-filter support.

        Args:
            page: Page number (1-indexed).
            per_page: Documents per page.
            source: Optional source name/type filter.
            collection_id: If provided, restrict to documents in that collection.
                Use ``0`` to list unassigned documents (collection_id IS NULL).
            date_from: Optional ISO date string (inclusive) — filter by created_at.
            date_to: Optional ISO date string (inclusive) — filter by created_at.
            file_type: Optional file extension filter (e.g. '.pdf', 'pdf').
            tag: Optional tag name filter.
        """
        # Build cache key from all filter parameters
        key = make_key(
            "docmind", "docs", "list",
            hash_params(
                source=source, collection_id=collection_id,
                date_from=date_from, date_to=date_to,
                file_type=file_type, tag=tag,
                page=page, per_page=per_page,
            ),
        )
        cached = await self._cache.get(key)
        if cached is not None:
            return cached

        offset = (page - 1) * per_page
        docs = await self.list_documents(
            source=source, collection_id=collection_id,
            date_from=date_from, date_to=date_to,
            file_type=file_type, tag=tag,
            limit=per_page, offset=offset,
        )
        total = await self.get_document_count(
            source=source, collection_id=collection_id,
            date_from=date_from, date_to=date_to,
            file_type=file_type, tag=tag,
        )
        result = {
            "documents": docs,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page if per_page > 0 else 0,
        }
        await self._cache.set(key, result, ttl=CacheTTLConfig.doc_list)
        return result

    async def get_document_count(
        self, *, source: Optional[str] = None,
        collection_id: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        file_type: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> int:
        """Return the total number of documents, optionally filtered.

        Args:
            source: Optional source name/type filter.
            collection_id: If provided, count documents in that collection.
                Use ``0`` to count unassigned documents (collection_id IS NULL).
            date_from: Optional ISO date string (inclusive) — filter by created_at.
            date_to: Optional ISO date string (inclusive) — filter by created_at.
            file_type: Optional file extension filter (e.g. '.pdf', 'pdf').
            tag: Optional tag name filter.
        """
        where, params = self._build_filter_clause(
            source=source, collection_id=collection_id,
            date_from=date_from, date_to=date_to,
            file_type=file_type, tag=tag,
        )
        sql = f"SELECT COUNT(*) as c FROM documents {where}"
        async with self.connection() as conn:
            cursor = await conn.execute(sql, params)
            row = await cursor.fetchone()
        return row["c"] if row else 0

    async def delete_document(self, doc_id: int) -> bool:
        """Delete a document by ID. Returns True if deleted, False if not found.

        Chunks and embeddings are cascade-deleted via FK constraints.
        We also explicitly delete chunks to ensure FTS triggers fire
        even if FK cascade hasn't propagated yet.
        """
        async with self.connection() as conn:
            # Explicitly delete chunks first (ensures FTS trigger cleanup)
            await conn.execute(
                "DELETE FROM document_chunks WHERE doc_id = ?", (doc_id,)
            )
            cursor = await conn.execute(
                "DELETE FROM documents WHERE id = ?", (doc_id,)
            )
            await conn.commit()
            deleted = cursor.rowcount > 0

        if deleted:
            await self._invalidate_document_mutations(doc_id)
            await self._invalidate_tag_mutations(doc_id)
            await self._invalidate_collection_mutations()
            await self._invalidate_job_mutations()
        return deleted

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

        await self._invalidate_document_mutations(doc_id)

    async def search_documents(
        self, query: str, limit: int = 30,
        collection_id: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        file_type: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Full-text search using SQLite FTS5 with BM25 ranking.

        Weights: title (A=10.0), summary (B=5.0), raw_preview (C=2.0), body (D=1.0).
        BM25 returns negative scores; we negate to get descending rank order.

        Args:
            query: The search query string.
            limit: Maximum number of results.
            collection_id: If provided, restrict results to documents in the
                given collection. Use ``None`` (default) to search all documents.
            date_from: Optional ISO date string (inclusive). Filter by created_at.
            date_to: Optional ISO date string (inclusive). Filter by created_at.
            file_type: Optional file extension filter (e.g. '.pdf', 'pdf').
            tag: Optional tag name filter.
        """
        safe_query = _sanitize_fts_query(query)
        if not safe_query or safe_query == '""':
            return []

        # Check cache
        search_key = make_key(
            "docmind", "search", "fts",
            hash_params(
                query=query, limit=limit, collection_id=collection_id,
                date_from=date_from, date_to=date_to,
                file_type=file_type, tag=tag,
            ),
        )
        cached = await self._cache.get(search_key)
        if cached is not None:
            return cached

        # Build extra filter conditions for the FTS query
        extra_conditions: list[str] = []
        extra_params: list[Any] = []

        if collection_id is not None:
            extra_conditions.append("d.collection_id = ?")
            extra_params.append(collection_id)

        if date_from:
            extra_conditions.append("d.created_at >= ?")
            extra_params.append(date_from)

        if date_to:
            dt_to = date_to
            if "T" not in dt_to and " " not in dt_to and len(dt_to) == 10:
                dt_to = dt_to + " 23:59:59"
            extra_conditions.append("d.created_at <= ?")
            extra_params.append(dt_to)

        if file_type:
            ft = file_type.strip()
            if not ft.startswith("."):
                ft = "." + ft
            extra_conditions.append("d.ext = ?")
            extra_params.append(ft)

        if tag:
            extra_conditions.append(
                "d.id IN (SELECT doc_id FROM document_tags WHERE tag = ?)"
            )
            extra_params.append(tag)

        extra_clause = ""
        if extra_conditions:
            extra_clause = " AND " + " AND ".join(extra_conditions)

        async with self.connection() as conn:
            cursor = await conn.execute(
                f"""SELECT d.id, d.path, d.source_type, d.source_name,
                           d.file_hash, d.mtime, d.size, d.title, d.ext,
                           d.mime_type, d.summary, d.raw_preview, d.body,
                           d.status, d.metadata, d.created_at, d.updated_at,
                           d.collection_id,
                           -bm25(documents_fts, 10.0, 5.0, 2.0, 1.0) AS rank
                    FROM documents d
                    JOIN documents_fts fts ON d.id = fts.rowid
                    WHERE documents_fts MATCH ?{extra_clause}
                    ORDER BY rank DESC
                    LIMIT ?""",
                [safe_query] + extra_params + [limit],
            )
            rows = await cursor.fetchall()

        result = [self._row_to_doc_dict(r) for r in rows]
        await self._cache.set(search_key, result, ttl=CacheTTLConfig.search)
        return result

    async def fulltext_search(
        self, query: str, limit: int = 30,
        collection_id: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        file_type: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Alias for search_documents -- backward compatibility with db.py."""
        return await self.search_documents(
            query, limit=limit,
            collection_id=collection_id,
            date_from=date_from, date_to=date_to,
            file_type=file_type, tag=tag,
        )

    async def get_stats(self) -> dict[str, Any]:
        """Return knowledge base statistics."""
        key = "docmind:analytics:stats"
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
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

        result = {
            "total": total_row["c"] if total_row else 0,
            "pending": pending_row["c"] if pending_row else 0,
            "indexed": indexed_row["c"] if indexed_row else 0,
            "summarized": summarized_row["c"] if summarized_row else 0,
            "error": error_row["c"] if error_row else 0,
            "active_jobs": job_row["c"] if job_row else 0,
        }
        await self._cache.set(key, result, ttl=CacheTTLConfig.dashboard_stats)
        return result

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
        await self._invalidate_chat_mutations()
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
        key = make_key("docmind", "chat", "sessions", limit)
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
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
        await self._cache.set(key, result, ttl=CacheTTLConfig.chat_sessions)
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
            deleted = cursor.rowcount > 0

        if deleted:
            await self._invalidate_chat_mutations(session_id)
        return deleted

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
            updated = cursor.rowcount > 0

        if updated:
            await self._invalidate_chat_mutations(session_id)
        return updated

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

        await self._invalidate_chat_mutations(session_id)
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
        key = make_key("docmind", "chat", "messages", session_id, limit)
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
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

        result = [self._row_to_chat_message_dict(r) for r in rows]
        await self._cache.set(key, result, ttl=CacheTTLConfig.chat_messages)
        return result

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
        await self._cache.delete("docmind:settings:all")

    async def get_all_settings(self) -> dict[str, str]:
        """Return all settings as a {key: value} dict.

        Values that are SQL NULL are skipped (matching the get_setting
        default semantics — callers that need a value should provide one).
        """
        key = "docmind:settings:all"
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        async with self.connection() as conn:
            cursor = await conn.execute(
                "SELECT key, value FROM settings ORDER BY key"
            )
            rows = await cursor.fetchall()

        result = {
            row["key"]: row["value"]
            for row in rows
            if row["value"] is not None
        }
        await self._cache.set(key, result, ttl=CacheTTLConfig.settings)
        return result

    async def delete_setting(self, key: str) -> bool:
        """Delete a setting by key. Returns True if a row was removed."""
        async with self.connection() as conn:
            cursor = await conn.execute(
                "DELETE FROM settings WHERE key = ?", (key,)
            )
            await conn.commit()
            deleted = cursor.rowcount > 0
        if deleted:
            await self._cache.delete("docmind:settings:all")
        return deleted

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

        await self._invalidate_tag_mutations(doc_id)
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
            removed = cursor.rowcount > 0

        if removed:
            await self._invalidate_tag_mutations(doc_id)
        return removed

    async def get_tags(self, doc_id: int) -> list[str]:
        """Return a list of tag names for the given document, sorted alphabetically."""
        key = make_key("docmind", "tag", "get", doc_id)
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        async with self.connection() as conn:
            cursor = await conn.execute(
                "SELECT tag FROM document_tags WHERE doc_id = ? ORDER BY tag",
                (doc_id,),
            )
            rows = await cursor.fetchall()
        result = [row["tag"] for row in rows]
        await self._cache.set(key, result, ttl=CacheTTLConfig.tag_list)
        return result

    async def get_documents_by_tag(self, tag: str) -> list[dict[str, Any]]:
        """Return all documents that have the given tag, newest first."""
        tag = (tag or "").strip()
        if not tag:
            return []
        key = make_key("docmind", "docs", "by_tag", hash_params(tag=tag))
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT d.* FROM documents d
                   JOIN document_tags dt ON d.id = dt.doc_id
                   WHERE dt.tag = ?
                   ORDER BY d.created_at DESC""",
                (tag,),
            )
            rows = await cursor.fetchall()
        result = [self._row_to_doc_dict(r) for r in rows]
        await self._cache.set(key, result, ttl=CacheTTLConfig.docs_by_tag)
        return result

    async def get_all_tags(self) -> list[dict[str, Any]]:
        """Return all tags with their document counts, sorted by count descending.

        Each dict has keys: tag (str), count (int).
        """
        key = "docmind:tag:all"
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT tag, COUNT(*) as count
                   FROM document_tags
                   GROUP BY tag
                   ORDER BY count DESC, tag ASC""",
            )
            rows = await cursor.fetchall()
        result = [{"tag": row["tag"], "count": row["count"]} for row in rows]
        await self._cache.set(key, result, ttl=CacheTTLConfig.tag_cloud)
        return result

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

    # ── Search Activity Logging ──────────────────────────────────

    async def log_search(
        self, query: str, results_count: int, session: Optional[str] = None
    ) -> int:
        """Log a search query for analytics.

        Args:
            query: The search query string.
            results_count: Number of results returned.
            session: Optional user session identifier.

        Returns:
            The id of the inserted log row.
        """
        now = _now_iso()
        async with self.connection() as conn:
            cursor = await conn.execute(
                """INSERT INTO search_log (query, results_count, searched_at, user_session)
                   VALUES (?, ?, ?, ?)""",
                (query, results_count, now, session),
            )
            await conn.commit()
            row_id = cursor.lastrowid or 0
        # Invalidate search analytics caches since search_log changed.
        await self._cache.delete_pattern("docmind:analytics:search_stats:*")
        await self._cache.delete_pattern("docmind:analytics:popular:*")
        await self._cache.delete_pattern("docmind:analytics:search_trend:*")
        return row_id

    async def get_search_stats(self, days: int = 30) -> dict[str, Any]:
        """Return aggregate search statistics for the last *days* days.

        Returns a dict with keys: total_searches, avg_results, unique_queries.
        """
        key = make_key("docmind", "analytics", "search_stats", days)
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT COUNT(*) as total,
                          AVG(results_count) as avg_results,
                          COUNT(DISTINCT query) as unique_queries
                   FROM search_log
                   WHERE searched_at >= datetime('now', ?)""",
                (f"-{days} days",),
            )
            row = await cursor.fetchone()

        if row is None or row["total"] == 0:
            return {"total_searches": 0, "avg_results": 0.0, "unique_queries": 0}
        result = {
            "total_searches": row["total"],
            "avg_results": round(row["avg_results"] or 0, 2),
            "unique_queries": row["unique_queries"],
        }
        await self._cache.set(key, result, ttl=CacheTTLConfig.search_stats)
        return result

    async def get_popular_queries(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return the most popular search queries by count.

        Each dict has keys: query, count, avg_results.
        """
        key = make_key("docmind", "analytics", "popular", limit)
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT query, COUNT(*) as count, AVG(results_count) as avg_results
                   FROM search_log
                   GROUP BY query
                   ORDER BY count DESC, query ASC
                   LIMIT ?""",
                (limit,),
            )
            rows = await cursor.fetchall()
        result = [
            {
                "query": row["query"],
                "count": row["count"],
                "avg_results": round(row["avg_results"] or 0, 2),
            }
            for row in rows
        ]
        await self._cache.set(key, result, ttl=CacheTTLConfig.popular_queries)
        return result

    async def get_search_trend(self, days: int = 30) -> list[dict[str, Any]]:
        """Return daily search counts for the last *days* days.

        Each dict has keys: date (YYYY-MM-DD), count.
        """
        key = make_key("docmind", "analytics", "search_trend", days)
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT DATE(searched_at) as date, COUNT(*) as count
                   FROM search_log
                   WHERE searched_at >= datetime('now', ?)
                   GROUP BY DATE(searched_at)
                   ORDER BY date ASC""",
                (f"-{days} days",),
            )
            rows = await cursor.fetchall()
        result = [{"date": row["date"], "count": row["count"]} for row in rows]
        await self._cache.set(key, result, ttl=CacheTTLConfig.search_trend)
        return result

    # ── Analytics ────────────────────────────────────────────────

    async def get_document_growth(self, days: int = 30) -> list[dict[str, Any]]:
        """Return daily document creation counts for the last *days* days.

        Each dict has keys: date (YYYY-MM-DD), count.
        """
        key = make_key("docmind", "analytics", "growth", days)
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT DATE(created_at) as date, COUNT(*) as count
                   FROM documents
                   WHERE created_at >= datetime('now', ?)
                   GROUP BY DATE(created_at)
                   ORDER BY date ASC""",
                (f"-{days} days",),
            )
            rows = await cursor.fetchall()
        result = [{"date": row["date"], "count": row["count"]} for row in rows]
        await self._cache.set(key, result, ttl=CacheTTLConfig.doc_growth)
        return result

    async def get_tag_distribution(self) -> list[dict[str, Any]]:
        """Return tag distribution with counts, sorted by count descending.

        Each dict has keys: tag, count.
        """
        key = "docmind:analytics:tag_dist"
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        result = await self.get_all_tags()
        await self._cache.set(key, result, ttl=CacheTTLConfig.tag_dist)
        return result

    async def get_storage_stats(self) -> dict[str, Any]:
        """Return storage statistics.

        Returns a dict with keys: total_size, by_type (dict of ext -> size),
        avg_doc_size, doc_count.
        """
        key = "docmind:analytics:storage"
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT
                       COALESCE(SUM(size), 0) as total_size,
                       COALESCE(AVG(size), 0) as avg_doc_size,
                       COUNT(*) as doc_count
                   FROM documents"""
            )
            row = await cursor.fetchone()

            type_cursor = await conn.execute(
                """SELECT ext, SUM(size) as total_size
                   FROM documents
                   GROUP BY ext
                   ORDER BY total_size DESC"""
            )
            type_rows = await type_cursor.fetchall()

        by_type: dict[str, int] = {}
        for tr in type_rows:
            ext = tr["ext"] or "unknown"
            by_type[ext] = tr["total_size"] or 0

        result = {
            "total_size": row["total_size"] if row else 0,
            "avg_doc_size": round(row["avg_doc_size"], 2) if row else 0,
            "doc_count": row["doc_count"] if row else 0,
            "by_type": by_type,
        }
        await self._cache.set(key, result, ttl=CacheTTLConfig.storage_stats)
        return result

    async def get_file_type_facets(self) -> list[dict[str, Any]]:
        """Return distinct file extensions with document counts.

        Each dict has keys: ext (str), count (int).
        Sorted by count descending then ext ascending.
        Used to populate the file-type facet dropdown on /documents.
        """
        key = "docmind:analytics:file_type_facets"
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT ext, COUNT(*) as count
                   FROM documents
                   GROUP BY ext
                   ORDER BY count DESC, ext ASC"""
            )
            rows = await cursor.fetchall()
        result = [{"ext": row["ext"] or "", "count": row["count"]} for row in rows]
        await self._cache.set(key, result, ttl=CacheTTLConfig.file_type_facets)
        return result

    async def get_source_facets(self) -> list[dict[str, Any]]:
        """Return distinct source types with document counts.

        Each dict has keys: source_type (str), count (int).
        Sorted by count descending then source_type ascending.
        Used to populate the source-type facet dropdown on /documents.
        """
        key = "docmind:analytics:source_facets"
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT source_type, COUNT(*) as count
                   FROM documents
                   GROUP BY source_type
                   ORDER BY count DESC, source_type ASC"""
            )
            rows = await cursor.fetchall()
        result = [{"source_type": row["source_type"] or "", "count": row["count"]} for row in rows]
        await self._cache.set(key, result, ttl=CacheTTLConfig.source_facets)
        return result

    async def get_chat_activity(self, days: int = 30) -> list[dict[str, Any]]:
        """Return daily chat message counts for the last *days* days.

        Each dict has keys: date (YYYY-MM-DD), message_count.
        """
        key = make_key("docmind", "analytics", "chat_activity", days)
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT DATE(created_at) as date, COUNT(*) as message_count
                   FROM chat_messages
                   WHERE created_at >= datetime('now', ?)
                   GROUP BY DATE(created_at)
                   ORDER BY date ASC""",
                (f"-{days} days",),
            )
            rows = await cursor.fetchall()
        result = [{"date": row["date"], "message_count": row["message_count"]} for row in rows]
        await self._cache.set(key, result, ttl=CacheTTLConfig.chat_activity)
        return result

    async def get_job_stats(self) -> dict[str, Any]:
        """Return job queue statistics.

        Returns a dict with keys: by_state (dict of state -> count),
        total, success_rate, avg_processing_time_seconds, recent_failures.
        """
        key = "docmind:analytics:job_stats"
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        async with self.connection() as conn:
            state_cursor = await conn.execute(
                """SELECT state, COUNT(*) as count
                   FROM jobs
                   GROUP BY state"""
            )
            state_rows = await state_cursor.fetchall()

            total_cursor = await conn.execute("SELECT COUNT(*) as c FROM jobs")
            total_row = await total_cursor.fetchone()

            # Average processing time: difference between created_at and updated_at
            # for completed jobs.
            avg_cursor = await conn.execute(
                """SELECT AVG(
                       (julianday(updated_at) - julianday(created_at)) * 86400
                   ) as avg_seconds
                   FROM jobs
                   WHERE state = 'completed'"""
            )
            avg_row = await avg_cursor.fetchone()

            fail_cursor = await conn.execute(
                """SELECT id, document_title, error, created_at
                   FROM jobs
                   WHERE state = 'failed'
                   ORDER BY created_at DESC
                   LIMIT 5"""
            )
            fail_rows = await fail_cursor.fetchall()

        by_state: dict[str, int] = {}
        for sr in state_rows:
            by_state[sr["state"]] = sr["count"]

        total = total_row["c"] if total_row else 0
        completed = by_state.get("completed", 0)
        failed = by_state.get("failed", 0)
        finished = completed + failed
        success_rate = round((completed / finished * 100), 2) if finished > 0 else 0.0

        recent_failures = [
            {
                "id": fr["id"],
                "document_title": fr["document_title"] or "",
                "error": (fr["error"] or "")[:200],
                "created_at": fr["created_at"],
            }
            for fr in fail_rows
        ]

        result = {
            "by_state": by_state,
            "total": total,
            "success_rate": success_rate,
            "avg_processing_time_seconds": round(avg_row["avg_seconds"] or 0, 2) if avg_row else 0.0,
            "recent_failures": recent_failures,
        }
        await self._cache.set(key, result, ttl=CacheTTLConfig.job_stats)
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

    # ── Document Chunks ──────────────────────────────────────────

    async def save_chunks(
        self,
        doc_id: int,
        chunks: list[dict[str, Any]],
    ) -> int:
        """Save document chunks, replacing any existing chunks for the doc.

        Args:
            doc_id: The document ID these chunks belong to.
            chunks: List of chunk dicts from TextChunker.chunk(), each
                with keys: text, start_char, end_char, chunk_index,
                token_count.

        Returns:
            Number of chunks saved.
        """
        if not chunks:
            return 0

        now = _now_iso()
        async with self.connection() as conn:
            # Delete existing chunks (FTS triggers handle cleanup)
            await conn.execute(
                "DELETE FROM document_chunks WHERE doc_id = ?", (doc_id,)
            )
            rows = [
                (doc_id, c["chunk_index"], c["text"],
                 c["start_char"], c["end_char"],
                 c.get("token_count", 0), None, now)
                for c in chunks
            ]
            await conn.executemany(
                """INSERT INTO document_chunks
                       (doc_id, chunk_index, content, start_char, end_char,
                        token_count, embedding, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            await conn.commit()

        return len(chunks)

    async def get_chunks(
        self, doc_id: int
    ) -> list[dict[str, Any]]:
        """Retrieve all chunks for a document, ordered by chunk_index.

        Returns a list of dicts with keys: id, doc_id, chunk_index,
        content, start_char, end_char, token_count, has_embedding,
        created_at.
        """
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT id, doc_id, chunk_index, content,
                          start_char, end_char, token_count,
                          (embedding IS NOT NULL) AS has_embedding,
                          created_at
                   FROM document_chunks
                   WHERE doc_id = ?
                   ORDER BY chunk_index""",
                (doc_id,),
            )
            rows = await cursor.fetchall()

        return [
            {
                "id": r["id"],
                "doc_id": r["doc_id"],
                "chunk_index": r["chunk_index"],
                "content": r["content"],
                "start_char": r["start_char"],
                "end_char": r["end_char"],
                "token_count": r["token_count"],
                "has_embedding": bool(r["has_embedding"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    async def get_chunk_count(self, doc_id: int) -> int:
        """Return the number of chunks stored for a document."""
        async with self.connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) AS c FROM document_chunks WHERE doc_id = ?",
                (doc_id,),
            )
            row = await cursor.fetchone()
        return row["c"] if row else 0

    async def delete_chunks(self, doc_id: int) -> int:
        """Delete all chunks for a document.

        Returns the number of chunks deleted.
        """
        async with self.connection() as conn:
            cursor = await conn.execute(
                "DELETE FROM document_chunks WHERE doc_id = ?",
                (doc_id,),
            )
            await conn.commit()
            return cursor.rowcount

    async def save_chunk_embedding(
        self, chunk_id: int, embedding: list[float]
    ) -> None:
        """Store an embedding vector for a specific chunk."""
        if not embedding:
            return
        blob = serialize_vector(embedding)
        async with self.connection() as conn:
            await conn.execute(
                "UPDATE document_chunks SET embedding = ? WHERE id = ?",
                (blob, chunk_id),
            )
            await conn.commit()

    async def get_chunks_with_embeddings(
        self, doc_id: int
    ) -> list[dict[str, Any]]:
        """Retrieve chunks with their embedding vectors for a document.

        Returns dicts with: id, doc_id, chunk_index, content, start_char,
        end_char, embedding (list[float] or empty).
        """
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT id, doc_id, chunk_index, content,
                          start_char, end_char, embedding
                   FROM document_chunks
                   WHERE doc_id = ? AND embedding IS NOT NULL
                   ORDER BY chunk_index""",
                (doc_id,),
            )
            rows = await cursor.fetchall()

        return [
            {
                "id": r["id"],
                "doc_id": r["doc_id"],
                "chunk_index": r["chunk_index"],
                "content": r["content"],
                "start_char": r["start_char"],
                "end_char": r["end_char"],
                "embedding": deserialize_vector(r["embedding"])
                if r["embedding"] else [],
            }
            for r in rows
        ]

    async def search_chunks_fts(
        self, query: str, top_k: int = 5
    ) -> list[dict[str, Any]]:
        """Full-text search on document chunks using FTS5.

        Returns a list of dicts with: id, doc_id, chunk_index, content,
        start_char, end_char, rank (BM25 score).
        """
        safe_query = _sanitize_fts_query(query)
        if not safe_query or safe_query == '""':
            return []

        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT c.id, c.doc_id, c.chunk_index, c.content,
                          c.start_char, c.end_char,
                          -bm25(document_chunks_fts) AS rank
                   FROM document_chunks c
                   JOIN document_chunks_fts fts ON c.id = fts.rowid
                   WHERE document_chunks_fts MATCH ?
                   ORDER BY rank DESC
                   LIMIT ?""",
                (safe_query, top_k),
            )
            rows = await cursor.fetchall()

        return [
            {
                "id": r["id"],
                "doc_id": r["doc_id"],
                "chunk_index": r["chunk_index"],
                "content": r["content"],
                "start_char": r["start_char"],
                "end_char": r["end_char"],
                "rank": r["rank"],
            }
            for r in rows
        ]

    async def search_chunks_similar(
        self,
        embedding: list[float],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Find chunks whose embeddings are most similar to the query vector.

        Returns dicts with: id, doc_id, chunk_index, content, start_char,
        end_char, similarity (float in [-1, 1]).
        """
        if not embedding:
            return []

        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT id, doc_id, chunk_index, content,
                          start_char, end_char, embedding
                   FROM document_chunks
                   WHERE embedding IS NOT NULL"""
            )
            rows = await cursor.fetchall()

        if not rows:
            return []

        scored: list[dict[str, Any]] = []
        for row in rows:
            stored_vec = deserialize_vector(row["embedding"])
            sim = cosine_similarity(embedding, stored_vec)
            scored.append({
                "id": row["id"],
                "doc_id": row["doc_id"],
                "chunk_index": row["chunk_index"],
                "content": row["content"],
                "start_char": row["start_char"],
                "end_char": row["end_char"],
                "similarity": sim,
            })

        scored.sort(key=lambda x: x["similarity"], reverse=True)
        return scored[:top_k]

    async def get_chunk_count_with_embeddings(self) -> int:
        """Return the total number of chunks with stored embeddings."""
        async with self.connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) AS c FROM document_chunks WHERE embedding IS NOT NULL"
            )
            row = await cursor.fetchone()
        return row["c"] if row else 0

    # ── Collections ──────────────────────────────────────────────

    async def create_collection(
        self,
        name: str,
        description: str = "",
        parent_id: Optional[int] = None,
    ) -> int:
        """Create a new collection and return its id.

        Args:
            name: Collection name (must not be empty or whitespace).
            description: Optional description text.
            parent_id: Optional parent collection id for nesting.

        Returns:
            The new collection's id.

        Raises:
            ValueError: If name is empty or whitespace-only.
        """
        name = (name or "").strip()
        if not name:
            raise ValueError("collection name must not be empty")
        now = _now_iso()
        async with self.connection() as conn:
            cursor = await conn.execute(
                """INSERT INTO collections (name, description, parent_id,
                                            created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (name, description, parent_id, now, now),
            )
            await conn.commit()
            col_id = cursor.lastrowid or 0
        await self._invalidate_collection_mutations(col_id)
        return col_id

    async def get_collection(self, collection_id: int) -> Optional[dict[str, Any]]:
        """Fetch a single collection by id. Returns None if not found."""
        key = make_key("docmind", "collection", "get", collection_id)
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT id, name, description, parent_id,
                          created_at, updated_at
                   FROM collections WHERE id = ?""",
                (collection_id,),
            )
            row = await cursor.fetchone()

        if row is None:
            return None
        result = self._row_to_collection_dict(row)
        await self._cache.set(key, result, ttl=CacheTTLConfig.collection_single)
        return result

    async def list_collections(self) -> list[dict[str, Any]]:
        """Return a flat list of all collections, ordered by name."""
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT id, name, description, parent_id,
                          created_at, updated_at
                   FROM collections ORDER BY name"""
            )
            rows = await cursor.fetchall()
        return [self._row_to_collection_dict(r) for r in rows]

    async def list_collections_tree(self) -> list[dict[str, Any]]:
        """Return a nested tree structure of collections for UI rendering.

        Each node has a ``children`` list. Root collections (parent_id IS NULL)
        are at the top level.
        """
        key = "docmind:collection:tree"
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        all_collections = await self.list_collections()
        by_parent: dict[Optional[int], list[dict[str, Any]]] = {}
        for col in all_collections:
            pid = col["parent_id"]
            by_parent.setdefault(pid, []).append(col)

        def build_tree(parent_id: Optional[int]) -> list[dict[str, Any]]:
            nodes = by_parent.get(parent_id, [])
            for node in nodes:
                node["children"] = build_tree(node["id"])
            return nodes

        result = build_tree(None)
        await self._cache.set(key, result, ttl=CacheTTLConfig.collection_tree)
        return result

    async def update_collection(
        self,
        collection_id: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
        parent_id: Optional[int] = None,
    ) -> bool:
        """Update one or more fields of a collection.

        Returns True if the collection was updated, False if not found.

        Raises:
            ValueError: If name is empty/whitespace, or if setting parent_id
                would create a cycle (collection becomes its own ancestor).
        """
        existing = await self.get_collection(collection_id)
        if existing is None:
            return False

        updates: list[str] = []
        params: list[Any] = []

        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError("collection name must not be empty")
            updates.append("name = ?")
            params.append(name)

        if description is not None:
            updates.append("description = ?")
            params.append(description)

        if parent_id is not None:
            # Cycle detection: parent_id must not be the collection itself
            # or any of its descendants.
            if parent_id == collection_id:
                raise ValueError(
                    "cannot set parent_id to itself — this would create a cycle"
                )
            await self._check_cycle(parent_id, collection_id)
            updates.append("parent_id = ?")
            params.append(parent_id)

        if not updates:
            return True  # nothing to update

        now = _now_iso()
        updates.append("updated_at = ?")
        params.append(now)
        params.append(collection_id)

        async with self.connection() as conn:
            cursor = await conn.execute(
                f"""UPDATE collections SET {', '.join(updates)}
                    WHERE id = ?""",
                params,
            )
            await conn.commit()
            updated = cursor.rowcount > 0
        await self._invalidate_collection_mutations(collection_id)
        return updated

    async def _check_cycle(
        self, new_parent_id: int, collection_id: int
    ) -> None:
        """Raise ValueError if ``collection_id`` is an ancestor of ``new_parent_id``.

        This prevents cycles when moving a collection under a new parent.
        """
        visited: set[int] = set()
        current_id: Optional[int] = new_parent_id
        while current_id is not None:
            if current_id in visited:
                # Safety: should not happen but guard against existing cycles
                break
            visited.add(current_id)
            if current_id == collection_id:
                raise ValueError(
                    "cannot move collection under its own descendant — "
                    "this would create a cycle"
                )
            col = await self.get_collection(current_id)
            if col is None:
                break
            current_id = col["parent_id"]

    async def delete_collection(self, collection_id: int) -> bool:
        """Delete a collection by id.

        Documents in this collection are moved to All Documents
        (collection_id set to NULL). Child collections are cascade-deleted
        via the ON DELETE CASCADE foreign key constraint.

        Returns True if the collection was deleted, False if not found.
        """
        existing = await self.get_collection(collection_id)
        if existing is None:
            return False

        async with self.connection() as conn:
            # Move documents to "All Documents" (NULL collection_id).
            # ON DELETE SET NULL would only apply to the direct collection,
            # but child collections are cascade-deleted, so their documents
            # also need to be unassigned. We handle this explicitly.
            await conn.execute(
                """UPDATE documents SET collection_id = NULL
                   WHERE collection_id IN (
                       WITH RECURSIVE descendant(id) AS (
                           SELECT id FROM collections WHERE id = ?
                           UNION ALL
                           SELECT c.id FROM collections c
                           JOIN descendant d ON c.parent_id = d.id
                       )
                       SELECT id FROM descendant
                   )""",
                (collection_id,),
            )
            cursor = await conn.execute(
                "DELETE FROM collections WHERE id = ?", (collection_id,)
            )
            await conn.commit()
            deleted = cursor.rowcount > 0
        if deleted:
            await self._invalidate_collection_mutations(collection_id)
            await self._invalidate_document_mutations()
        return deleted

    async def get_collection_path(
        self, collection_id: int
    ) -> list[dict[str, Any]]:
        """Return the breadcrumb chain from root to the given collection.

        Returns an empty list if the collection does not exist.
        The list is ordered root-first: [root, ..., parent, self].
        """
        chain: list[dict[str, Any]] = []
        current_id: Optional[int] = collection_id
        visited: set[int] = set()
        while current_id is not None and current_id not in visited:
            visited.add(current_id)
            col = await self.get_collection(current_id)
            if col is None:
                break
            chain.append(col)
            current_id = col["parent_id"]
        chain.reverse()
        return chain

    async def assign_document_to_collection(
        self, doc_id: int, collection_id: int
    ) -> bool:
        """Assign a document to a collection.

        Returns True if the assignment was made, False if the document or
        collection does not exist.
        """
        # Verify the collection exists
        col = await self.get_collection(collection_id)
        if col is None:
            return False
        now = _now_iso()
        async with self.connection() as conn:
            cursor = await conn.execute(
                """UPDATE documents
                   SET collection_id = ?, updated_at = ?
                   WHERE id = ?""",
                (collection_id, now, doc_id),
            )
            await conn.commit()
            assigned = cursor.rowcount > 0
        if assigned:
            await self._invalidate_collection_mutations(collection_id)
            await self._invalidate_document_mutations(doc_id)
        return assigned

    async def remove_document_from_collection(self, doc_id: int) -> bool:
        """Remove a document from its collection (set collection_id to NULL).

        Returns True if the document was updated, False if not found or
        already unassigned.
        """
        now = _now_iso()
        async with self.connection() as conn:
            cursor = await conn.execute(
                """UPDATE documents
                   SET collection_id = NULL, updated_at = ?
                   WHERE id = ? AND collection_id IS NOT NULL""",
                (now, doc_id),
            )
            await conn.commit()
            removed = cursor.rowcount > 0
        if removed:
            await self._invalidate_collection_mutations()
            await self._invalidate_document_mutations(doc_id)
        return removed

    async def list_documents_by_collection(
        self,
        collection_id: int,
        page: int = 1,
        per_page: int = 20,
    ) -> dict[str, Any]:
        """List documents in a collection with pagination metadata."""
        return await self.list_documents_paginated(
            page=page, per_page=per_page, collection_id=collection_id,
        )

    async def get_document_collection(
        self, doc_id: int
    ) -> Optional[dict[str, Any]]:
        """Return the collection a document belongs to, or None if unassigned."""
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT c.id, c.name, c.description, c.parent_id,
                          c.created_at, c.updated_at
                   FROM collections c
                   JOIN documents d ON d.collection_id = c.id
                   WHERE d.id = ?""",
                (doc_id,),
            )
            row = await cursor.fetchone()

        if row is None:
            return None
        return self._row_to_collection_dict(row)

    async def get_collection_counts(self) -> dict[int, int]:
        """Return a mapping of collection_id -> document count.

        Only collections that have at least one document are included.
        """
        key = "docmind:collection:counts"
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        async with self.connection() as conn:
            cursor = await conn.execute(
                """SELECT collection_id, COUNT(*) as cnt
                   FROM documents
                   WHERE collection_id IS NOT NULL
                   GROUP BY collection_id"""
            )
            rows = await cursor.fetchall()
        result = {row["collection_id"]: row["cnt"] for row in rows}
        await self._cache.set(key, result, ttl=CacheTTLConfig.collection_counts)
        return result

    @staticmethod
    def _row_to_collection_dict(row: aiosqlite.Row) -> dict[str, Any]:
        """Convert a collections row to a dict."""
        d = dict(row)
        for field in ("created_at", "updated_at"):
            val = d.get(field)
            if isinstance(val, str):
                try:
                    d[field] = datetime.fromisoformat(val)
                except (ValueError, TypeError):
                    pass
        return d

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
