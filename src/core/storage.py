"""Storage connector — manage WebDAV, local directory, and PostgreSQL sources."""
import hashlib
import mimetypes
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Optional

from .extractor import Extractor
from .indexer import Indexer


class StorageConnector:
    """Connect to and scan various data sources."""

    def __init__(self, indexer: Indexer):
        self.indexer = indexer

    def scan_webdav(self, url: str, username: str, password: str,
                    root_path: str = "/", source_name: str = "webdav") -> int:
        """Scan a WebDAV directory recursively and index files."""
        from webdav3.client import Client

        options = {
            "webdav_hostname": url,
            "webdav_login": username,
            "webdav_password": password,
        }
        client = Client(options)

        count = 0

        def _scan_dir(remote_path: str):
            nonlocal count
            try:
                items = client.list(remote_path)
            except Exception as e:
                print(f"[WebDAV] Failed to list {remote_path}: {e}")
                return

            for item in items:
                full_path = f"{remote_path.rstrip('/')}/{item.strip('/')}"
                if item.endswith("/"):
                    # Directory — recurse
                    _scan_dir(full_path)
                else:
                    # File — download and index
                    try:
                        content = client.resource(full_path).read()
                        ext = Path(item).suffix.lower()

                        # Check supported extension
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

    def scan_directory(self, dir_path: str, source_name: str = "local") -> int:
        """Scan a local directory recursively and index files."""
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

                file_hash = Extractor._file_hash(file_path) if hasattr(Extractor, '_file_hash') else self._hash_file(file_path)
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
        """Compute SHA256 hash of a file."""
        sha = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha.update(chunk)
        return sha.hexdigest()