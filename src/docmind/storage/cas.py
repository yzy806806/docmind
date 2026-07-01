"""Content-addressable storage (CAS) layer.

Uses SHA-256 content hashing to deduplicate documents at rest.
Each document version is stored once by content_hash; multiple pointers
can reference the same blob.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from io import BufferedIOBase
from pathlib import Path
from typing import AsyncIterator, Protocol
from uuid import UUID, uuid4

from docmind.errors import ErrorCode, StorageError


class StorageBackend(Protocol):
    """Abstract storage backend interface."""

    async def put(self, content: bytes, content_hash: str) -> None:
        ...

    async def get(self, content_hash: str) -> bytes | None:
        ...

    async def delete(self, content_hash: str) -> None:
        ...

    async def exists(self, content_hash: str) -> bool:
        ...


class LocalStorageBackend:
    """Local filesystem storage backend."""

    def __init__(self, base_path: Path) -> None:
        self.base_path = base_path
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _blob_path(self, content_hash: str) -> Path:
        # Shard by first 2 hex chars to avoid too many files in one directory
        return self.base_path / content_hash[:2] / content_hash[2:]

    async def put(self, content: bytes, content_hash: str) -> None:
        path = self._blob_path(content_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    async def get(self, content_hash: str) -> bytes | None:
        path = self._blob_path(content_hash)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    async def delete(self, content_hash: str) -> None:
        path = self._blob_path(content_hash)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    async def exists(self, content_hash: str) -> bool:
        return self._blob_path(content_hash).exists()


class ContentAddressableStorage:
    """High-level CAS with integrity verification."""

    def __init__(self, backend: StorageBackend) -> None:
        self.backend = backend

    @staticmethod
    def compute_hash(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def verify_integrity(content: bytes, expected_hash: str) -> bool:
        computed = hashlib.sha256(content).hexdigest()
        return computed == expected_hash

    async def store(self, content: bytes, expected_hash: str | None = None) -> str:
        """Store content and return its hash. Optionally verify against expected_hash."""
        content_hash = self.compute_hash(content)
        if expected_hash and content_hash != expected_hash:
            raise StorageError(
                f"Content hash mismatch: expected {expected_hash}, got {content_hash}",
                ErrorCode.STORAGE_INTEGRITY_ERROR,
                {"expected": expected_hash, "computed": content_hash},
            )
        await self.backend.put(content, content_hash)
        return content_hash

    async def retrieve(self, content_hash: str) -> bytes:
        """Retrieve content and verify integrity."""
        content = await self.backend.get(content_hash)
        if content is None:
            raise StorageError(
                f"Content not found: {content_hash}",
                ErrorCode.NOT_FOUND,
                {"content_hash": content_hash},
            )
        if not self.verify_integrity(content, content_hash):
            raise StorageError(
                f"Content integrity check failed for hash {content_hash}",
                ErrorCode.STORAGE_INTEGRITY_ERROR,
                {"content_hash": content_hash},
            )
        return content

    async def delete(self, content_hash: str) -> None:
        await self.backend.delete(content_hash)

    async def exists(self, content_hash: str) -> bool:
        return await self.backend.exists(content_hash)
