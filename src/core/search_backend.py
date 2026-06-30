"""Abstract search backend interface with SQLite (FTS5) and PostgreSQL (tsvector) implementations.

Provides a unified API for full-text document indexing and search, with a factory
function to select the backend at runtime.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Result types ────────────────────────────────────────────────────────────


@dataclass
class SearchResult:
    """A single document hit from a full-text search."""

    doc_id: int
    path: str
    title: str
    summary: Optional[str]
    body: str
    rank: float
    snippet: str = ""  # relevant excerpt with highlighting / context


@dataclass
class SearchResults:
    """Container returned by every search operation."""

    query: str
    results: list[SearchResult]
    total_hits: int
    backend: str  # 'sqlite' or 'postgresql'


# ── Abstract backend ────────────────────────────────────────────────────────


class SearchBackend(ABC):
    """Abstract interface for a full-text search backend."""

    @abstractmethod
    def search(self, query: str, limit: int = 30) -> SearchResults:
        """Run a full-text query and return ranked results."""
        ...

    @abstractmethod
    def index_document(
        self,
        doc_id: int,
        path: str,
        title: str,
        summary: Optional[str],
        body: str,
    ) -> None:
        """Insert or update a document in the search index."""
        ...

    @abstractmethod
    def delete_document(self, doc_id: int) -> None:
        """Remove a document from the search index."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Release any resources held by the backend."""
        ...


# ── SQLite (FTS5) backend ───────────────────────────────────────────────────


class SQLiteSearchBackend(SearchBackend):
    """Full-text search powered by SQLite's FTS5 extension.

    Maintains a ``documents`` table and a content-synchronised FTS5 virtual table
    via triggers, so inserts/updates/deletes are automatically reflected in the
    full-text index.
    """

    def __init__(self, db_path: str = "data/docmind_fts.db"):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # ── Schema ──────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        """Create tables and triggers idempotently."""
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id      INTEGER PRIMARY KEY,
                path    TEXT NOT NULL,
                title   TEXT NOT NULL,
                summary TEXT,
                body    TEXT NOT NULL DEFAULT ''
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts
                USING fts5(title, summary, body, content='documents', content_rowid='id');

            -- Keep FTS in sync with the content table
            CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
                INSERT INTO documents_fts(rowid, title, summary, body)
                VALUES (new.id, new.title, new.summary, new.body);
            END;

            CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
                INSERT INTO documents_fts(documents_fts, rowid, title, summary, body)
                VALUES ('delete', old.id, old.title, old.summary, old.body);
            END;

            CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
                INSERT INTO documents_fts(documents_fts, rowid, title, summary, body)
                VALUES ('delete', old.id, old.title, old.summary, old.body);
                INSERT INTO documents_fts(rowid, title, summary, body)
                VALUES (new.id, new.title, new.summary, new.body);
            END;
        """
        )
        self._conn.commit()

    # ── SearchBackend interface ─────────────────────────────────────────

    def search(self, query: str, limit: int = 30) -> SearchResults:
        """FTS5 MATCH query with BM25 ranking."""
        rows = self._conn.execute(
            """
            SELECT
                d.id,
                d.path,
                d.title,
                d.summary,
                d.body,
                bm25(documents_fts, 0.0, 10.0, 5.0) AS rank
            FROM documents d
            JOIN documents_fts fts ON d.id = fts.rowid
            WHERE documents_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()

        results: list[SearchResult] = []
        for r in rows:
            snippet = self._extract_snippet(r["body"], query)
            results.append(
                SearchResult(
                    doc_id=r["id"],
                    path=r["path"],
                    title=r["title"],
                    summary=r["summary"],
                    body=r["body"],
                    rank=float(r["rank"]),
                    snippet=snippet,
                )
            )

        # Total hits: count without limit (still filtered by MATCH)
        total_row = self._conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM documents d
            JOIN documents_fts fts ON d.id = fts.rowid
            WHERE documents_fts MATCH ?
            """,
            (query,),
        ).fetchone()
        total_hits = total_row["cnt"] if total_row else 0

        return SearchResults(
            query=query,
            results=results,
            total_hits=total_hits,
            backend="sqlite",
        )

    def index_document(
        self,
        doc_id: int,
        path: str,
        title: str,
        summary: Optional[str],
        body: str,
    ) -> None:
        """DELETE then INSERT — ensures FTS5 content-sync triggers fire correctly.

        ``INSERT OR REPLACE`` on content-sync FTS5 tables does not reliably
        remove old entries, so we use explicit DELETE + INSERT.
        """
        self._conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        self._conn.execute(
            """
            INSERT INTO documents (id, path, title, summary, body)
            VALUES (?, ?, ?, ?, ?)
            """,
            (doc_id, path, title, summary, body),
        )
        self._conn.commit()

    def delete_document(self, doc_id: int) -> None:
        """DELETE — FTS triggers keep the index in sync."""
        self._conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _extract_snippet(body: str, query: str, context_chars: int = 300) -> str:
        """Return a small relevant excerpt from *body*.

        Tries to find a region containing one of the query tokens; falls back to
        the first *context_chars* characters.
        """
        if not body:
            return ""

        # Tokenise the query into words (at least 3 chars to avoid noise)
        tokens: list[str] = [
            t for t in re.split(r"\W+", query.lower()) if len(t) >= 3
        ]
        body_lower = body.lower()

        best_pos = -1
        for token in tokens:
            pos = body_lower.find(token)
            if pos != -1:
                best_pos = pos
                break

        if best_pos == -1:
            # No token matched — return the beginning of the body
            return body[:context_chars]

        # Centre the window around the match
        half = context_chars // 2
        start = max(0, best_pos - half)
        end = min(len(body), start + context_chars)
        snippet = body[start:end]

        # Add ellipsis hints
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(body) else ""
        return f"{prefix}{snippet}{suffix}"


# ── PostgreSQL (tsvector) backend ───────────────────────────────────────────


class PostgresSearchBackend(SearchBackend):
    """Full-text search powered by PostgreSQL's ``tsvector`` / ``tsquery``.

    All methods are **async** — the caller is expected to manage the event loop.
    A connection pool is created lazily on first use.
    """

    # DDL for the self-contained search table
    _TABLE_DDL = """
        CREATE TABLE IF NOT EXISTS search_index (
            id          INTEGER PRIMARY KEY,
            path        TEXT NOT NULL,
            title       TEXT NOT NULL,
            summary     TEXT,
            body        TEXT NOT NULL DEFAULT '',
            search_vec  tsvector GENERATED ALWAYS AS (
                            setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                            setweight(to_tsvector('english', coalesce(summary, '')), 'B') ||
                            setweight(to_tsvector('english', coalesce(body, '')), 'C')
                        ) STORED
        );

        CREATE INDEX IF NOT EXISTS idx_search_vec
            ON search_index USING GIN(search_vec);
    """

    def __init__(self, dsn: str):
        import asyncpg  # type: ignore

        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None
        self._asyncpg = asyncpg

    # ── Lazy pool ───────────────────────────────────────────────────────

    async def _get_pool(self) -> asyncpg.Pool:
        """Return (or create) the connection pool and ensure the table exists."""
        if self._pool is None:
            self._pool = await self._asyncpg.create_pool(
                dsn=self._dsn, min_size=2, max_size=10
            )
            async with self._pool.acquire() as conn:
                await conn.execute(self._TABLE_DDL)
            logger.info("PostgresSearchBackend pool created (dsn=%s)", self._dsn)
        return self._pool

    # ── SearchBackend interface (async) ─────────────────────────────────

    async def search(self, query: str, limit: int = 30) -> SearchResults:
        """Full-text search with ts_rank and ts_headline snippets."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    id,
                    path,
                    title,
                    summary,
                    body,
                    ts_rank(search_vec, plainto_tsquery('english', $1)) AS rank,
                    ts_headline('english', body, plainto_tsquery('english', $1),
                                'StartSel=<mark>, StopSel=</mark>, MaxWords=50, MinWords=20') AS snippet
                FROM search_index
                WHERE search_vec @@ plainto_tsquery('english', $1)
                ORDER BY rank DESC
                LIMIT $2
                """,
                query,
                limit,
            )

            total_row = await conn.fetchrow(
                """
                SELECT COUNT(*) AS cnt
                FROM search_index
                WHERE search_vec @@ plainto_tsquery('english', $1)
                """,
                query,
            )
            total_hits = total_row["cnt"] if total_row else 0

        results: list[SearchResult] = []
        for r in rows:
            results.append(
                SearchResult(
                    doc_id=r["id"],
                    path=r["path"],
                    title=r["title"],
                    summary=r["summary"],
                    body=r["body"],
                    rank=float(r["rank"]),
                    snippet=r["snippet"] or "",
                )
            )

        return SearchResults(
            query=query,
            results=results,
            total_hits=total_hits,
            backend="postgresql",
        )

    async def index_document(
        self,
        doc_id: int,
        path: str,
        title: str,
        summary: Optional[str],
        body: str,
    ) -> None:
        """INSERT … ON CONFLICT DO UPDATE — tsvector regenerates automatically."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO search_index (id, path, title, summary, body)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (id) DO UPDATE SET
                    path    = excluded.path,
                    title   = excluded.title,
                    summary = excluded.summary,
                    body    = excluded.body
                """,
                doc_id,
                path,
                title,
                summary,
                body,
            )

    async def delete_document(self, doc_id: int) -> None:
        """Remove a document by id."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM search_index WHERE id = $1", doc_id)

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


# ── Factory ─────────────────────────────────────────────────────────────────


def create_backend(backend_type: str, **kwargs) -> SearchBackend:
    """Create a search backend instance by name.

    Args:
        backend_type: ``'sqlite'`` or ``'postgresql'``.
        **kwargs: Passed directly to the backend constructor
            (e.g. ``db_path`` for SQLite, ``dsn`` for PostgreSQL).

    Raises:
        ValueError: If *backend_type* is unknown.
    """
    if backend_type == "sqlite":
        return SQLiteSearchBackend(**kwargs)
    elif backend_type == "postgresql":
        return PostgresSearchBackend(**kwargs)
    else:
        raise ValueError(f"Unknown backend type: {backend_type}")
