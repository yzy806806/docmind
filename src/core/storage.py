"""Storage connector — manage WebDAV, local directory, and PostgreSQL sources.

Provides multi-source document ingestion with hash-based change detection,
size-tiered extraction routing, and structured metadata extraction.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from pathlib import Path
from typing import Any, Optional

from .extractor import Extractor


class StorageConnector:
    """Connect to and scan various data sources for document ingestion."""

    def __init__(self, indexer: Any):
        """Initialise the connector with an indexer that provides:
        - needs_update(path, file_hash) -> bool
        - upsert_document(**kwargs) -> int
        - update_summary(doc_id, summary) -> None
        """
        self.indexer = indexer

    # ── WebDAV connector ───────────────────────────────────────

    def scan_webdav(
        self,
        url: str,
        username: str,
        password: str,
        root_path: str = "/",
        source_name: str = "webdav",
    ) -> int:
        """Scan a WebDAV directory recursively and index supported files.

        Returns the number of newly indexed or updated documents.
        """
        from webdav3.client import Client

        options = {
            "webdav_hostname": url,
            "webdav_login": username,
            "webdav_password": password,
        }
        client = Client(options)
        count = 0

        def _scan_dir(remote_path: str) -> None:
            nonlocal count
            try:
                items = client.list(remote_path)
            except Exception as e:
                print(f"[WebDAV] Failed to list {remote_path}: {e}")
                return

            for item in items:
                item_stripped = item.rstrip("/")
                if item.endswith("/"):
                    # Directory — recurse
                    full_path = f"{remote_path.rstrip('/')}/{item_stripped}"
                    _scan_dir(full_path)
                else:
                    full_path = f"{remote_path.rstrip('/')}/{item_stripped}"
                    try:
                        content = client.resource(full_path).read()
                        ext = Path(item).suffix.lower()

                        if ext not in Extractor.SUPPORTED:
                            continue

                        body = Extractor.extract_from_bytes(content, ext)
                        if body is None:
                            continue

                        file_hash = hashlib.sha256(content).hexdigest()

                        if not self.indexer.needs_update(full_path, file_hash):
                            continue

                        mime_type, _ = mimetypes.guess_type(item)
                        self.indexer.upsert_document(
                            path=full_path,
                            source_type="webdav",
                            source_name=source_name,
                            title=item,
                            ext=ext,
                            mime_type=mime_type or "application/octet-stream",
                            body=body,
                            file_hash=file_hash,
                            size=len(content),
                        )
                        count += 1
                    except Exception as e:
                        print(f"[WebDAV] Failed to index {full_path}: {e}")

        _scan_dir(root_path)
        return count

    # ── Local directory scanner ────────────────────────────────

    def scan_directory(
        self, dir_path: str, source_name: str = "local"
    ) -> int:
        """Scan a local directory recursively and index supported files.

        Uses SHA-256 hash for change detection — skips files whose hash
        matches the already-indexed version.
        """
        root = Path(dir_path)
        if not root.exists():
            raise FileNotFoundError(f"Directory not found: {dir_path}")

        count = 0
        for file_path in root.rglob("*"):
            if not file_path.is_file():
                continue

            ext = file_path.suffix.lower()
            if ext not in Extractor.SUPPORTED:
                continue

            try:
                body = Extractor.extract(file_path)
                if body is None:
                    continue

                file_hash = self._hash_file(file_path)
                rel_path = str(file_path.relative_to(root))

                if not self.indexer.needs_update(rel_path, file_hash):
                    continue

                stat = file_path.stat()
                mime_type, _ = mimetypes.guess_type(str(file_path))

                self.indexer.upsert_document(
                    path=rel_path,
                    source_type="local",
                    source_name=source_name,
                    title=file_path.name,
                    ext=ext,
                    mime_type=mime_type or "application/octet-stream",
                    body=body,
                    file_hash=file_hash,
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                )
                count += 1
            except Exception as e:
                print(f"[Local] Failed to index {file_path}: {e}")

        return count

    def _hash_file(self, file_path: Path) -> str:
        """Compute SHA-256 hash of a file on disk."""
        sha = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha.update(chunk)
        return sha.hexdigest()

    # ── PostgreSQL query connector ─────────────────────────────

    async def scan_postgresql(
        self,
        dsn: str,
        query: str,
        *,
        source_name: str = "postgresql",
        id_column: str = "id",
        title_column: str = "title",
        body_column: str = "body",
        metadata_columns: list[str] | None = None,
        batch_size: int = 100,
    ) -> int:
        """Query a PostgreSQL database and index each row as a document.

        Each row becomes a document with path = f\"pg://{source_name}/{id}\".

        Args:
            dsn: PostgreSQL connection string (e.g. postgresql://user:pass@host/db)
            query: SQL query returning rows with at least id, title, body columns
            source_name: Logical name for this data source
            id_column: Column to use as the unique document identifier
            title_column: Column to use as the document title
            body_column: Column to use as the document body
            metadata_columns: Additional columns to store as metadata
            batch_size: Number of rows to fetch per batch

        Returns the number of newly indexed or updated documents.
        """
        import asyncpg

        conn = await asyncpg.connect(dsn)
        count = 0

        try:
            # Fetch all rows (for large datasets, use cursor-based pagination)
            rows = await conn.fetch(query)

            for row in rows:
                row_dict = dict(row)
                doc_id = row_dict.get(id_column)
                title = row_dict.get(title_column, "")
                body = row_dict.get(body_column, "")

                if doc_id is None:
                    continue

                path = f"pg://{source_name}/{doc_id}"
                content = f"{title}\n\n{body}"
                file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

                if not self.indexer.needs_update(path, file_hash):
                    continue

                # Extract metadata from additional columns
                meta: dict[str, Any] = {}
                if metadata_columns:
                    for col in metadata_columns:
                        if col in row_dict and col not in (id_column, title_column, body_column):
                            val = row_dict[col]
                            # Convert non-serialisable types
                            try:
                                import json
                                json.dumps(val)
                                meta[col] = val
                            except (TypeError, ValueError):
                                meta[col] = str(val)

                self.indexer.upsert_document(
                    path=path,
                    source_type="postgresql",
                    source_name=source_name,
                    title=str(title),
                    ext=".txt",
                    mime_type="text/plain",
                    body=str(body),
                    file_hash=file_hash,
                    size=len(content.encode("utf-8")),
                    metadata=meta,
                )
                count += 1
        finally:
            await conn.close()

        return count

    def scan_postgresql_sync(
        self,
        dsn: str,
        query: str,
        **kwargs: Any,
    ) -> int:
        """Synchronous wrapper for scan_postgresql.

        Runs the async version inside a new event loop.
        """
        return asyncio.run(self.scan_postgresql(dsn, query, **kwargs))
