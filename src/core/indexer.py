"""SQLite + FTS5 indexer — the core search engine."""
import sqlite3
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional


class Indexer:
    """Manages the SQLite database with FTS5 full-text search."""

    def __init__(self, db_path: str = "data/docmind.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                path        TEXT UNIQUE NOT NULL,
                source_type TEXT NOT NULL,           -- webdav, local, postgresql
                source_name TEXT NOT NULL,           -- connection name
                file_hash   TEXT,                    -- SHA256 for change detection
                mtime       REAL,                    -- file modification timestamp
                size        INTEGER,                 -- bytes
                title       TEXT NOT NULL,            -- display name
                ext         TEXT,                     -- file extension
                mime_type   TEXT,                     -- MIME type
                summary     TEXT,                     -- LLM-generated summary
                raw_preview TEXT,                     -- first N chars auto-preview
                body        TEXT,                     -- full text content
                document_type TEXT DEFAULT 'other',    -- auto-detected type
                status      TEXT DEFAULT 'pending',   -- pending, indexed, summarized, error
                metadata    TEXT DEFAULT '{}',        -- JSON: extra metadata
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts
                USING fts5(path, title, summary, raw_preview, body, content='documents', content_rowid='id');

            -- Triggers to keep FTS in sync
            CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
                INSERT INTO documents_fts(rowid, path, title, summary, raw_preview, body)
                VALUES (new.id, new.path, new.title, new.summary, new.raw_preview, new.body);
            END;

            CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
                INSERT INTO documents_fts(documents_fts, rowid, path, title, summary, raw_preview, body)
                VALUES ('delete', old.id, old.path, old.title, old.summary, old.raw_preview, old.body);
            END;

            CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
                INSERT INTO documents_fts(documents_fts, rowid, path, title, summary, raw_preview, body)
                VALUES ('delete', old.id, old.path, old.title, old.summary, old.raw_preview, old.body);
                INSERT INTO documents_fts(rowid, path, title, summary, raw_preview, body)
                VALUES (new.id, new.path, new.title, new.summary, new.raw_preview, new.body);
            END;

            CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
            CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_type, source_name);
        """)
        self.conn.commit()

    def file_hash(self, path: Path) -> str:
        """Compute SHA256 of a file."""
        sha = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha.update(chunk)
        return sha.hexdigest()

    def needs_update(self, path: str, current_hash: str) -> bool:
        """Check if file needs re-indexing."""
        row = self.conn.execute(
            "SELECT file_hash FROM documents WHERE path = ?", (path,)
        ).fetchone()
        if row is None:
            return True  # New file
        return row["file_hash"] != current_hash

    def upsert_document(
        self,
        path: str,
        source_type: str,
        source_name: str,
        title: str,
        ext: str,
        mime_type: str,
        body: str,
        file_hash: Optional[str] = None,
        mtime: float = 0,
        size: int = 0,
        metadata: dict | None = None,
    ) -> int:
        """Insert or update a document record."""
        now = datetime.now(timezone.utc).isoformat()
        raw_preview = body[:500] if body else ""
        meta_json = str(metadata or {})

        self.conn.execute("""
            INSERT INTO documents (path, source_type, source_name, file_hash, mtime, size,
                                   title, ext, mime_type, body, raw_preview, document_type, status, metadata, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'other', 'indexed', ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                file_hash = excluded.file_hash,
                mtime = excluded.mtime,
                size = excluded.size,
                title = excluded.title,
                ext = excluded.ext,
                mime_type = excluded.mime_type,
                body = excluded.body,
                raw_preview = excluded.raw_preview,
                status = 'indexed',
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
        """, (path, source_type, source_name, file_hash, mtime, size,
              title, ext, mime_type, body, raw_preview, meta_json, now))
        self.conn.commit()
        return self.conn.execute("SELECT id FROM documents WHERE path = ?", (path,)).fetchone()["id"]

    def search_fts(self, query: str, limit: int = 30) -> list[dict]:
        """FTS5 keyword search."""
        rows = self.conn.execute(
            """SELECT d.* FROM documents d
               JOIN documents_fts fts ON d.id = fts.rowid
               WHERE documents_fts MATCH ?
               ORDER BY rank LIMIT ?""",
            (query, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def list_by_path(self, path_prefix: str, limit: int = 100) -> list[dict]:
        """List documents under a given path prefix."""
        rows = self.conn.execute(
            "SELECT * FROM documents WHERE path LIKE ? ORDER BY path LIMIT ?",
            (f"{path_prefix}%", limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def list_directory(self, dir_path: str) -> list[dict]:
        """List documents in a directory (non-recursive)."""
        # dir_path should end with / to match exact directory
        if not dir_path.endswith("/"):
            dir_path += "/"

        rows = self.conn.execute(
            """SELECT * FROM documents
               WHERE path LIKE ? AND path NOT LIKE ?
               ORDER BY path""",
            (f"{dir_path}%", f"{dir_path}%/%")
        ).fetchall()
        return [dict(r) for r in rows]

    def get_document(self, path: str) -> Optional[dict]:
        """Get a single document by path."""
        row = self.conn.execute(
            "SELECT * FROM documents WHERE path = ?", (path,)
        ).fetchone()
        return dict(row) if row else None

    def get_document_by_id(self, doc_id: int) -> Optional[dict]:
        """Get a single document by its integer ID."""
        row = self.conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_pending_summaries(self, limit: int = 10) -> list[dict]:
        """Get documents that need LLM summarization."""
        rows = self.conn.execute(
            "SELECT * FROM documents WHERE status = 'indexed' LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def update_summary(self, doc_id: int, summary: str):
        """Update the LLM-generated summary for a document."""
        self.conn.execute(
            "UPDATE documents SET summary = ?, status = 'summarized', updated_at = ? WHERE id = ?",
            (summary, datetime.now(timezone.utc).isoformat(), doc_id)
        )
        self.conn.commit()

    def stats(self) -> dict:
        """Return database statistics."""
        total = self.conn.execute("SELECT COUNT(*) as c FROM documents").fetchone()["c"]
        pending = self.conn.execute("SELECT COUNT(*) as c FROM documents WHERE status = 'pending'").fetchone()["c"]
        indexed = self.conn.execute("SELECT COUNT(*) as c FROM documents WHERE status = 'indexed'").fetchone()["c"]
        summarized = self.conn.execute("SELECT COUNT(*) as c FROM documents WHERE status = 'summarized'").fetchone()["c"]
        return {
            "total": total,
            "pending": pending,
            "indexed": indexed,
            "summarized": summarized,
        }

    def close(self):
        self.conn.close()
