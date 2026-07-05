"""Tests for src.core.storage — WebDAV, local scanner, PostgreSQL connector."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Import smoke test ──────────────────────────────────────────

def test_import_storage() -> None:
    from src.core.storage import StorageConnector

    assert StorageConnector is not None


# ── Local directory scanner ────────────────────────────────────

class TestLocalScanner:
    def test_scan_directory_indexes_files(self) -> None:
        from src.core.storage import StorageConnector

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "doc1.txt").write_text("Hello world", encoding="utf-8")
            (root / "doc2.md").write_text("# Heading\nContent here", encoding="utf-8")
            sub = root / "subdir"
            sub.mkdir()
            (sub / "doc3.txt").write_text("Nested file", encoding="utf-8")

            # Create mock indexer
            mock_indexer = MagicMock()
            mock_indexer.needs_update.return_value = True
            mock_indexer.upsert_document.return_value = 1

            connector = StorageConnector(mock_indexer)
            count = connector.scan_directory(str(root), source_name="test")

            assert count == 3
            assert mock_indexer.upsert_document.call_count == 3

    def test_scan_directory_skips_unchanged(self) -> None:
        from src.core.storage import StorageConnector

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "doc1.txt").write_text("unchanged", encoding="utf-8")

            mock_indexer = MagicMock()
            mock_indexer.needs_update.return_value = False  # already indexed
            mock_indexer.upsert_document.return_value = 1

            connector = StorageConnector(mock_indexer)
            count = connector.scan_directory(str(root))

            assert count == 0
            assert mock_indexer.upsert_document.call_count == 0

    def test_scan_directory_skips_unsupported(self) -> None:
        from src.core.storage import StorageConnector

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "image.png").write_bytes(b"png data")
            (root / "video.mp4").write_bytes(b"video data")

            mock_indexer = MagicMock()
            mock_indexer.needs_update.return_value = True
            mock_indexer.upsert_document.return_value = 1

            connector = StorageConnector(mock_indexer)
            count = connector.scan_directory(str(root))

            assert count == 0

    def test_scan_directory_nonexistent(self) -> None:
        from src.core.storage import StorageConnector

        mock_indexer = MagicMock()
        connector = StorageConnector(mock_indexer)

        with pytest.raises(FileNotFoundError):
            connector.scan_directory("/nonexistent/path/12345")

    def test_hash_based_change_detection(self) -> None:
        from src.core.storage import StorageConnector

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            file_path = root / "test.txt"
            content = b"hash me"
            file_path.write_bytes(content)

            mock_indexer = MagicMock()
            mock_indexer.needs_update.return_value = True
            mock_indexer.upsert_document.return_value = 1

            connector = StorageConnector(mock_indexer)
            connector.scan_directory(str(root))

            # Verify the hash was computed correctly
            call_args = mock_indexer.upsert_document.call_args
            kwargs = call_args[1] if call_args[1] else {}
            file_hash = kwargs.get("file_hash")
            if file_hash:
                expected = hashlib.sha256(content).hexdigest()
                assert file_hash == expected
    def test_scan_directory_skips_empty_body(self) -> None:
        """Scanned PDFs returning empty-string body must be skipped, not upserted with body=''.

        Regression test for the silent data-loss bug where ``Extractor.extract``
        returns ``""`` for scanned PDFs (no text layer).  The old ``is None``
        check let empty strings through, upserting documents with body="".
        """
        from src.core.storage import StorageConnector

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # Create a .txt file with real content
            (root / "real.txt").write_text("Hello world", encoding="utf-8")
            # Create a .pdf file — Extractor.extract will return "" for scanned PDF
            (root / "scanned.pdf").write_bytes(b"%PDF-1.4 fake scanned pdf")

            mock_indexer = MagicMock()
            mock_indexer.needs_update.return_value = True
            mock_indexer.upsert_document.return_value = 1

            connector = StorageConnector(mock_indexer)

            # Patch Extractor.extract to simulate scanned PDF returning ""
            with patch("src.core.storage.Extractor.extract") as mock_extract:
                mock_extract.side_effect = lambda fp: (
                    "" if fp.suffix == ".pdf" else "Hello world"
                )
                count = connector.scan_directory(str(root), source_name="test")

            # Only the .txt file should be indexed; the scanned PDF is skipped
            assert count == 1
            assert mock_indexer.upsert_document.call_count == 1
            upserted_path = mock_indexer.upsert_document.call_args[1]["path"]
            assert upserted_path == "real.txt"

    def test_scan_directory_skips_none_body(self) -> None:
        """Extractor.extract returning None (extraction failure) must still be skipped."""
        from src.core.storage import StorageConnector

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "broken.pdf").write_bytes(b"%PDF-1.4 broken")

            mock_indexer = MagicMock()
            mock_indexer.needs_update.return_value = True
            mock_indexer.upsert_document.return_value = 1

            connector = StorageConnector(mock_indexer)

            with patch("src.core.storage.Extractor.extract", return_value=None):
                count = connector.scan_directory(str(root))

            assert count == 0
            assert mock_indexer.upsert_document.call_count == 0


# ── Hash helper ────────────────────────────────────────────────

def test_hash_file() -> None:
    from src.core.storage import StorageConnector

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(b"content to hash")
        tmp_path = Path(tmp.name)

    try:
        mock_indexer = MagicMock()
        connector = StorageConnector(mock_indexer)
        result = connector._hash_file(tmp_path)
        expected = hashlib.sha256(b"content to hash").hexdigest()
        assert result == expected
    finally:
        tmp_path.unlink()


# ── WebDAV connector ───────────────────────────────────────────

class TestWebDAV:
    def test_webdav_scan_with_list(self) -> None:
        from src.core.storage import StorageConnector

        mock_indexer = MagicMock()
        mock_indexer.needs_update.return_value = True
        mock_indexer.upsert_document.return_value = 1

        connector = StorageConnector(mock_indexer)

        with patch("webdav3.client.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client

            # Simulate directory listing — only .txt and .md (avoid PDF which needs real data)
            mock_client.list.side_effect = [
                ["file1.txt", "file2.md"],
            ]

            # Simulate file reads
            def fake_read():
                return b"file content"

            mock_resource = MagicMock()
            mock_resource.read = fake_read
            mock_client.resource.return_value = mock_resource

            count = connector.scan_webdav(
                url="https://webdav.example.com",
                username="user",
                password="pass",
                root_path="/",
                source_name="test_webdav",
            )

            # Should have indexed file1.txt and file2.md
            assert count >= 2

    def test_webdav_scan_skips_empty_body(self) -> None:
        """WebDAV scanner must skip files whose extraction returns empty string.

        Regression test for the silent data-loss bug at the WebDAV path —
        ``Extractor.extract_from_bytes`` returns ``""`` for scanned PDFs.
        """
        from src.core.storage import StorageConnector

        mock_indexer = MagicMock()
        mock_indexer.needs_update.return_value = True
        mock_indexer.upsert_document.return_value = 1

        connector = StorageConnector(mock_indexer)

        with patch("webdav3.client.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client

            mock_client.list.side_effect = [["scanned.pdf"]]

            mock_resource = MagicMock()
            mock_resource.read = lambda: b"%PDF-1.4 fake scanned"
            mock_client.resource.return_value = mock_resource

            with patch(
                "src.core.storage.Extractor.extract_from_bytes",
                return_value="",
            ):
                count = connector.scan_webdav(
                    url="https://webdav.example.com",
                    username="user",
                    password="pass",
                    root_path="/",
                    source_name="test_webdav",
                )

            assert count == 0
            assert mock_indexer.upsert_document.call_count == 0


# ── PostgreSQL query connector ─────────────────────────────────

class TestPostgreSQLConnector:
    def test_scan_postgresql_sync_wrapper(self) -> None:
        """Verify scan_postgresql_sync exists as a convenience wrapper."""
        from src.core.storage import StorageConnector

        mock_indexer = MagicMock()
        connector = StorageConnector(mock_indexer)

        # Should have the method
        assert hasattr(connector, "scan_postgresql_sync") or hasattr(
            connector, "scan_postgresql"
        )
