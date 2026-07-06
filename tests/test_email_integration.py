"""Integration tests for email ingestion — mocked IMAP server, full pipeline.

Tests the complete email ingestion pipeline with:
- Mocked imaplib.IMAP4_SSL (no real network calls)
- Real Database (in-memory SQLite)
- Real Extractor (for attachment text extraction)
- Real .eml fixtures as email content

Covers:
- Full poll_account() cycle: connect → search → fetch → process → disconnect
- Deduplication: re-processing same email → skipped
- Error handling: connection failure, fetch failure
- Post-fetch action: mark_seen
- Background worker: email_polling_worker() with mocked DB
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.config import EmailAccountConfig
from src.core.db_sqlite import Database
from src.core.email_ingestor import EmailIngestor, email_polling_worker
from src.core.extractor import Extractor

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "emails"


# ── Helpers ──────────────────────────────────────────────────────


class MockIMAPConnection:
    """Minimal mock of imaplib.IMAP4_SSL for testing.

    Returns canned responses for select, search, fetch, store, logout.
    """

    def __init__(self, emails: list[bytes] | None = None):
        self._emails = emails or []
        self._selected = False
        self._stored_flags: dict[int, str] = {}
        self.select_called = False
        self.search_called = False
        self.logout_called = False

    def select(self, mailbox: str = "INBOX"):
        self.select_called = True
        self._selected = True
        return ("OK", [str(len(self._emails)).encode()])

    def search(self, charset, *criteria):
        self.search_called = True
        # Return UIDs 1..N for N emails
        uids = b" ".join(str(i + 1).encode() for i in range(len(self._emails)))
        return ("OK", [uids])

    def fetch(self, uid_set, message_parts):
        uid = int(uid_set)
        if uid < 1 or uid > len(self._emails):
            return ("NO", [b""])
        raw = self._emails[uid - 1]
        return ("OK", [(b"1 (RFC822)", raw)])

    def store(self, uid_set, flags_op, flags):
        uid = int(uid_set)
        self._stored_flags[uid] = flags
        return ("OK", [None])

    def close(self):
        self._selected = False
        return ("OK", [None])

    def logout(self):
        self.logout_called = True
        self._selected = False
        return ("BYE", [None])


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
async def real_db(tmp_db_path: str):
    db = Database(db_path=tmp_db_path)
    await db.connect()
    yield db
    await db.disconnect()


@pytest.fixture
def simple_email() -> bytes:
    return (FIXTURES_DIR / "simple_text.eml").read_bytes()


@pytest.fixture
def attachment_email() -> bytes:
    return (FIXTURES_DIR / "with_attachments.eml").read_bytes()


@pytest.fixture
def test_account() -> EmailAccountConfig:
    return EmailAccountConfig(
        id=1,
        name="test-account",
        host="imap.example.com",
        port=993,
        use_ssl=True,
        username="user@example.com",
        password="secret",
        folder="INBOX",
        action_after_fetch="mark_seen",
        body_handling="save_with_attachments",
    )


# ── Integration tests ────────────────────────────────────────────


class TestPollAccountIntegration:
    """Full poll_account() cycle with mocked IMAP and real Database."""

    @pytest.mark.asyncio
    async def test_poll_account_success(
        self, real_db: Database, test_account: EmailAccountConfig, simple_email: bytes
    ) -> None:
        """Test a successful poll cycle: connect, search, fetch, process."""
        # Create the account in the DB
        await real_db.create_email_account({
            "name": "test-account",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "secret",
        })

        mock_imap = MockIMAPConnection(emails=[simple_email])
        ingestor = EmailIngestor(real_db, extractor=Extractor())

        # Patch _connect_imap to return our mock
        with patch.object(EmailIngestor, "_connect_imap", return_value=mock_imap):
            doc_ids = await ingestor.poll_account(test_account, account_id=1)

        # Should have created 1 document (email body)
        assert len(doc_ids) == 1
        assert mock_imap.select_called
        assert mock_imap.search_called
        assert mock_imap.logout_called

        # Verify the document was saved
        doc = await real_db.get_document(doc_ids[0])
        assert doc is not None
        assert doc["source_type"] == "email"
        assert doc["source_name"] == "test-account"

    @pytest.mark.asyncio
    async def test_poll_account_with_attachments(
        self, real_db: Database, test_account: EmailAccountConfig, attachment_email: bytes
    ) -> None:
        """Test polling an email with attachments — body + attachments saved."""
        await real_db.create_email_account({
            "name": "test-account",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "secret",
        })

        mock_imap = MockIMAPConnection(emails=[attachment_email])
        ingestor = EmailIngestor(real_db, extractor=Extractor())

        with patch.object(EmailIngestor, "_connect_imap", return_value=mock_imap):
            doc_ids = await ingestor.poll_account(test_account, account_id=1)

        # Body + at least 1 attachment (txt) should be saved
        # PDF may or may not extract text, but should still be saved
        assert len(doc_ids) >= 2

    @pytest.mark.asyncio
    async def test_poll_account_dedup(
        self, real_db: Database, test_account: EmailAccountConfig, simple_email: bytes
    ) -> None:
        """Re-processing the same email should be skipped (deduplication)."""
        await real_db.create_email_account({
            "name": "test-account",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "secret",
        })

        mock_imap = MockIMAPConnection(emails=[simple_email])
        ingestor = EmailIngestor(real_db, extractor=Extractor())

        # First poll — creates documents
        with patch.object(EmailIngestor, "_connect_imap", return_value=mock_imap):
            doc_ids_1 = await ingestor.poll_account(test_account, account_id=1)
        assert len(doc_ids_1) == 1

        # Second poll — should skip (dedup)
        mock_imap_2 = MockIMAPConnection(emails=[simple_email])
        with patch.object(EmailIngestor, "_connect_imap", return_value=mock_imap_2):
            doc_ids_2 = await ingestor.poll_account(test_account, account_id=1)
        assert len(doc_ids_2) == 0  # Skipped

        # Verify ingestion log has one "completed" and one "skipped"
        logs = await real_db.list_email_ingestion_logs(1)
        assert len(logs) == 2
        statuses = [log["status"] for log in logs]
        assert "completed" in statuses
        assert "skipped" in statuses

    @pytest.mark.asyncio
    async def test_poll_account_connection_failure(
        self, real_db: Database, test_account: EmailAccountConfig
    ) -> None:
        """Connection failure should update account error, not crash."""
        await real_db.create_email_account({
            "name": "test-account",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "secret",
        })

        ingestor = EmailIngestor(real_db, extractor=Extractor())

        with patch.object(
            EmailIngestor,
            "_connect_imap",
            side_effect=ConnectionRefusedError("Connection refused"),
        ):
            doc_ids = await ingestor.poll_account(test_account, account_id=1)

        # No documents created
        assert doc_ids == []

        # Account error should be updated
        acct = await real_db.get_email_account(1)
        assert acct is not None
        assert acct["last_error"] is not None
        assert "Connection refused" in acct["last_error"]

    @pytest.mark.asyncio
    async def test_poll_account_mark_seen(
        self, real_db: Database, test_account: EmailAccountConfig, simple_email: bytes
    ) -> None:
        """Post-fetch action 'mark_seen' should store \\Seen flag."""
        await real_db.create_email_account({
            "name": "test-account",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "secret",
        })

        mock_imap = MockIMAPConnection(emails=[simple_email])
        ingestor = EmailIngestor(real_db, extractor=Extractor())

        with patch.object(EmailIngestor, "_connect_imap", return_value=mock_imap):
            await ingestor.poll_account(test_account, account_id=1)

        # Verify \\Seen flag was stored
        assert 1 in mock_imap._stored_flags
        assert "\\Seen" in mock_imap._stored_flags[1]

    @pytest.mark.asyncio
    async def test_poll_account_no_new_messages(
        self, real_db: Database, test_account: EmailAccountConfig
    ) -> None:
        """No new messages → no documents, no errors."""
        await real_db.create_email_account({
            "name": "test-account",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "secret",
        })

        mock_imap = MockIMAPConnection(emails=[])  # No emails
        ingestor = EmailIngestor(real_db, extractor=Extractor())

        with patch.object(EmailIngestor, "_connect_imap", return_value=mock_imap):
            doc_ids = await ingestor.poll_account(test_account, account_id=1)

        assert doc_ids == []
        assert mock_imap.search_called


class TestEmailPollingWorker:
    """Tests for the background email_polling_worker function."""

    @pytest.mark.asyncio
    async def test_worker_no_accounts(self, real_db: Database) -> None:
        """Worker with no enabled accounts should not crash."""
        # Run one cycle with a very short timeout
        task = asyncio.create_task(
            email_polling_worker(real_db, extractor=Extractor(), poll_interval=0.1)
        )
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_worker_with_account(
        self, real_db: Database, test_account: EmailAccountConfig, simple_email: bytes
    ) -> None:
        """Worker should poll enabled accounts and create documents."""
        await real_db.create_email_account({
            "name": "test-account",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "secret",
        })

        mock_imap = MockIMAPConnection(emails=[simple_email])

        task = asyncio.create_task(
            email_polling_worker(real_db, extractor=Extractor(), poll_interval=10.0)
        )

        # Wait for one poll cycle
        with patch.object(EmailIngestor, "_connect_imap", return_value=mock_imap):
            await asyncio.sleep(0.5)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Check that documents were created
        docs = await real_db.list_documents()
        assert len(docs) >= 1
        assert docs[0]["source_type"] == "email"

    @pytest.mark.asyncio
    async def test_worker_handles_errors(
        self, real_db: Database, test_account: EmailAccountConfig
    ) -> None:
        """Worker should handle errors gracefully and continue."""
        await real_db.create_email_account({
            "name": "test-account",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "secret",
        })

        task = asyncio.create_task(
            email_polling_worker(real_db, extractor=Extractor(), poll_interval=0.1)
        )

        # Patch to raise on first call
        call_count = 0
        original_connect = EmailIngestor._connect_imap

        def failing_connect(self, account):
            nonlocal call_count
            call_count += 1
            raise ConnectionRefusedError("Mock failure")

        with patch.object(EmailIngestor, "_connect_imap", failing_connect):
            await asyncio.sleep(0.3)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Worker should not have crashed — error should be on account
        acct = await real_db.get_email_account(1)
        assert acct is not None
        assert acct["last_error"] is not None


class TestSchemaMigration:
    """Verify that the email tables are created on database initialization."""

    @pytest.mark.asyncio
    async def test_email_tables_exist(self, real_db: Database) -> None:
        """email_accounts and email_ingestion_log tables must exist after connect()."""
        async with real_db.connection() as conn:
            # Check email_accounts
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='email_accounts'"
            )
            assert await cursor.fetchone() is not None

            # Check email_ingestion_log
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='email_ingestion_log'"
            )
            assert await cursor.fetchone() is not None

    @pytest.mark.asyncio
    async def test_email_indexes_exist(self, real_db: Database) -> None:
        """Dedup indexes must exist for efficient lookup."""
        async with real_db.connection() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_email_%'"
            )
            indexes = await cursor.fetchall()
            index_names = [r["name"] for r in indexes]
            assert "idx_email_log_account" in index_names
            assert "idx_email_log_message_id" in index_names
            assert "idx_email_log_uid" in index_names
            assert "idx_email_log_status" in index_names
            assert "idx_email_log_dedup" in index_names

    @pytest.mark.asyncio
    async def test_cascade_delete(self, real_db: Database) -> None:
        """Deleting an email account should cascade-delete its log entries."""
        acct = await real_db.create_email_account({
            "name": "test", "host": "imap.example.com",
            "username": "u", "password": "p"
        })

        await real_db.log_email_ingestion({
            "account_id": acct["id"],
            "message_id": "<test@example.com>",
            "uid": 1,
            "folder": "INBOX",
            "status": "completed",
        })

        # Verify log exists
        logs_before = await real_db.list_email_ingestion_logs(acct["id"])
        assert len(logs_before) == 1

        # Delete account
        await real_db.delete_email_account(acct["id"])

        # Log should be cascade-deleted
        logs_after = await real_db.list_email_ingestion_logs(acct["id"])
        assert len(logs_after) == 0
