"""Tests for src.core.email_ingestor — email parsing, extraction, dedup, threading.

Covers:
- Email parsing from raw bytes (.eml fixtures)
- Body extraction: text/plain, text/html, multipart/alternative
- Attachment extraction: filenames, content, mime types
- Attachment filtering: whitelist/blacklist glob patterns
- Thread ID computation: References, In-Reply-To, Message-ID, fallback
- Deduplication key computation: msgid, content-hash fallback
- HTML to text conversion: script/style stripping
- EmailAccountConfig dataclass defaults
- EmailConfig: env-var loading, enabled flag, poll interval
- SourceType.EMAIL enum value
- Database email_* methods: CRUD, logging, dedup check (in-memory SQLite)
"""

from __future__ import annotations

import asyncio
import email
import json
import os
import tempfile
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.config import EmailAccountConfig, EmailConfig, _load_email_accounts_from_env
from src.core.email_ingestor import (
    EmailIngestor,
    _hash16,
    _normalize_text,
    email_polling_worker,
)
from src.core.extractor import Extractor
from src.core.models import SourceType

# ── Fixtures ─────────────────────────────────────────────────────

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "emails"


@pytest.fixture
def simple_email_bytes() -> bytes:
    return (FIXTURES_DIR / "simple_text.eml").read_bytes()


@pytest.fixture
def attachment_email_bytes() -> bytes:
    return (FIXTURES_DIR / "with_attachments.eml").read_bytes()


@pytest.fixture
def html_email_bytes() -> bytes:
    return (FIXTURES_DIR / "with_html_body.eml").read_bytes()


@pytest.fixture
def threaded_email_bytes() -> bytes:
    return (FIXTURES_DIR / "threaded.eml").read_bytes()


@pytest.fixture
def no_msgid_email_bytes() -> bytes:
    return (FIXTURES_DIR / "no_message_id.eml").read_bytes()


@pytest.fixture
def mock_db() -> MagicMock:
    """Mock Database with async email_* methods."""
    db = MagicMock()
    db.check_email_duplicate = AsyncMock(return_value=False)
    db.log_email_ingestion = AsyncMock(return_value=1)
    db.update_email_ingestion_log = AsyncMock(return_value=None)
    db.save_document = AsyncMock(return_value=42)
    db.update_email_account_sync = AsyncMock(return_value=None)
    db.update_email_account_error = AsyncMock(return_value=None)
    db.list_email_accounts = AsyncMock(return_value=[])
    return db


@pytest.fixture
def ingestor(mock_db: MagicMock) -> EmailIngestor:
    return EmailIngestor(mock_db, extractor=Extractor())


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.unlink(path)


# ── Import smoke test ────────────────────────────────────────────


def test_import_email_ingestor() -> None:
    assert EmailIngestor is not None
    assert email_polling_worker is not None


# ── SourceType.EMAIL ─────────────────────────────────────────────


def test_source_type_email_exists() -> None:
    """SourceType.EMAIL must be present with value 'email'."""
    assert hasattr(SourceType, "EMAIL")
    assert SourceType.EMAIL.value == "email"
    assert SourceType.EMAIL == "email"


# ── EmailAccountConfig defaults ──────────────────────────────────


def test_email_account_config_defaults() -> None:
    acct = EmailAccountConfig()
    assert acct.id == 0
    assert acct.name == ""
    assert acct.host == ""
    assert acct.port == 993
    assert acct.use_ssl is True
    assert acct.folder == "INBOX"
    assert acct.action_after_fetch == "mark_seen"
    assert acct.body_handling == "save_with_attachments"
    assert acct.enabled is True
    assert acct.deduplication_strategy == "message_id"


def test_email_account_config_custom() -> None:
    acct = EmailAccountConfig(
        name="test-account",
        host="imap.example.com",
        username="user@example.com",
        password="secret",
        port=143,
        use_ssl=False,
        body_handling="attachments_only",
    )
    assert acct.name == "test-account"
    assert acct.host == "imap.example.com"
    assert acct.port == 143
    assert acct.use_ssl is False
    assert acct.body_handling == "attachments_only"


# ── EmailConfig ──────────────────────────────────────────────────


def test_email_config_defaults() -> None:
    cfg = EmailConfig()
    assert cfg.enabled is False  # Disabled by default
    assert cfg.poll_interval_seconds == 600.0
    assert cfg.accounts == []


def test_email_config_env_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test loading accounts from indexed env vars."""
    monkeypatch.setenv("DOCMIND_EMAIL_ACCOUNT_0_HOST", "imap0.example.com")
    monkeypatch.setenv("DOCMIND_EMAIL_ACCOUNT_0_USERNAME", "user0")
    monkeypatch.setenv("DOCMIND_EMAIL_ACCOUNT_0_PASSWORD", "pass0")
    monkeypatch.setenv("DOCMIND_EMAIL_ACCOUNT_0_NAME", "account-0")
    monkeypatch.setenv("DOCMIND_EMAIL_ACCOUNT_1_HOST", "imap1.example.com")
    monkeypatch.setenv("DOCMIND_EMAIL_ACCOUNT_1_USERNAME", "user1")
    monkeypatch.setenv("DOCMIND_EMAIL_ACCOUNT_1_PASSWORD", "pass1")

    accounts = _load_email_accounts_from_env()
    assert len(accounts) == 2
    assert accounts[0].host == "imap0.example.com"
    assert accounts[0].username == "user0"
    assert accounts[0].password == "pass0"
    assert accounts[0].name == "account-0"
    assert accounts[1].host == "imap1.example.com"


def test_email_config_env_no_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env vars set → empty accounts list."""
    # Clear any existing env vars
    for key in list(os.environ.keys()):
        if key.startswith("DOCMIND_EMAIL_ACCOUNT_"):
            monkeypatch.delenv(key, raising=False)

    accounts = _load_email_accounts_from_env()
    assert accounts == []


# ── Email parsing ────────────────────────────────────────────────


class TestEmailParsing:
    """Test parse_email() with .eml fixtures."""

    def test_parse_simple_text(self, ingestor: EmailIngestor, simple_email_bytes: bytes) -> None:
        msg = ingestor.parse_email(simple_email_bytes)
        assert msg.get("Subject") == "Simple text email"
        assert msg.get("From") == "alice@example.com"
        assert msg.get("Message-ID") == "<simple-text-001@example.com>"

    def test_parse_attachment_email(self, ingestor: EmailIngestor, attachment_email_bytes: bytes) -> None:
        msg = ingestor.parse_email(attachment_email_bytes)
        assert msg.get("Subject") == "Email with PDF and TXT attachments"
        assert msg.is_multipart()

    def test_parse_html_email(self, ingestor: EmailIngestor, html_email_bytes: bytes) -> None:
        msg = ingestor.parse_email(html_email_bytes)
        assert msg.get("Subject") == "HTML email with inline image"
        assert msg.is_multipart()

    def test_parse_threaded_email(self, ingestor: EmailIngestor, threaded_email_bytes: bytes) -> None:
        msg = ingestor.parse_email(threaded_email_bytes)
        assert msg.get("In-Reply-To") == "<original-003@example.com>"
        assert "<original-001@example.com>" in msg.get("References", "")

    def test_parse_no_msgid_email(self, ingestor: EmailIngestor, no_msgid_email_bytes: bytes) -> None:
        msg = ingestor.parse_email(no_msgid_email_bytes)
        assert msg.get("Message-ID") is None or msg.get("Message-ID") == ""


# ── Body extraction ──────────────────────────────────────────────


class TestBodyExtraction:
    """Test extract_body() with various email types."""

    def test_simple_text_body(self, ingestor: EmailIngestor, simple_email_bytes: bytes) -> None:
        msg = ingestor.parse_email(simple_email_bytes)
        body = ingestor.extract_body(msg)
        assert "Hello Bob" in body
        assert "simple plain-text email" in body
        assert "regards" in body.lower()

    def test_html_body_plain_text_part(self, ingestor: EmailIngestor, html_email_bytes: bytes) -> None:
        msg = ingestor.parse_email(html_email_bytes)
        body = ingestor.extract_body(msg)
        # Should find the text/plain part first
        assert "plain text" in body.lower()

    def test_html_fallback(self, ingestor: EmailIngestor) -> None:
        """When only text/html is available, it should be converted to text."""
        msg = email.message_from_string(
            "From: test@example.com\r\n"
            "To: user@example.com\r\n"
            "Subject: HTML Only\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "\r\n"
            "<html><body><p>Hello <b>World</b></p></body></html>"
        )
        body = ingestor.extract_body(msg)
        assert "Hello" in body
        assert "World" in body
        # Script/style should be stripped
        assert "<script>" not in body
        assert "<style>" not in body

    def test_empty_body(self, ingestor: EmailIngestor) -> None:
        """Empty email should return empty string."""
        msg = email.message_from_string(
            "From: test@example.com\r\n"
            "To: user@example.com\r\n"
            "Subject: Empty\r\n"
            "Content-Type: text/plain\r\n"
            "\r\n"
        )
        body = ingestor.extract_body(msg)
        assert body == ""


# ── Attachment extraction ────────────────────────────────────────


class TestAttachmentExtraction:
    """Test extract_attachments() and filter_attachments()."""

    def test_extract_attachments(self, ingestor: EmailIngestor, attachment_email_bytes: bytes) -> None:
        msg = ingestor.parse_email(attachment_email_bytes)
        attachments = ingestor.extract_attachments(msg)
        assert len(attachments) == 2

        filenames = [a["filename"] for a in attachments]
        assert "notes.txt" in filenames
        assert "report.pdf" in filenames

    def test_no_attachments_simple(self, ingestor: EmailIngestor, simple_email_bytes: bytes) -> None:
        msg = ingestor.parse_email(simple_email_bytes)
        attachments = ingestor.extract_attachments(msg)
        assert attachments == []

    def test_attachment_content(self, ingestor: EmailIngestor, attachment_email_bytes: bytes) -> None:
        msg = ingestor.parse_email(attachment_email_bytes)
        attachments = ingestor.extract_attachments(msg)

        txt_att = [a for a in attachments if a["filename"] == "notes.txt"][0]
        # Base64 decoded content: "This is a test notes file.\n"
        assert txt_att["content"] == b"This is a test notes file.\n"
        assert txt_att["ext"] == ".txt"

    def test_filter_whitelist(self, ingestor: EmailIngestor, attachment_email_bytes: bytes) -> None:
        msg = ingestor.parse_email(attachment_email_bytes)
        attachments = ingestor.extract_attachments(msg)
        filtered = ingestor.filter_attachments(attachments, whitelist="*.pdf")
        assert len(filtered) == 1
        assert filtered[0]["filename"] == "report.pdf"

    def test_filter_blacklist(self, ingestor: EmailIngestor, attachment_email_bytes: bytes) -> None:
        msg = ingestor.parse_email(attachment_email_bytes)
        attachments = ingestor.extract_attachments(msg)
        filtered = ingestor.filter_attachments(attachments, blacklist="*.pdf")
        assert len(filtered) == 1
        assert filtered[0]["filename"] == "notes.txt"

    def test_filter_no_patterns(self, ingestor: EmailIngestor, attachment_email_bytes: bytes) -> None:
        msg = ingestor.parse_email(attachment_email_bytes)
        attachments = ingestor.extract_attachments(msg)
        filtered = ingestor.filter_attachments(attachments)
        assert len(filtered) == 2  # All pass when no filter


# ── Thread ID computation ────────────────────────────────────────


class TestThreadID:
    """Test compute_thread_id() with various header combinations."""

    def test_thread_from_references(self, ingestor: EmailIngestor, threaded_email_bytes: bytes) -> None:
        msg = ingestor.parse_email(threaded_email_bytes)
        tid = ingestor.compute_thread_id(msg)
        # Should hash the first Reference entry
        expected = _hash16("<original-001@example.com>")
        assert tid == expected

    def test_thread_from_in_reply_to(self, ingestor: EmailIngestor) -> None:
        msg = email.message_from_string(
            "From: test@example.com\r\n"
            "To: user@example.com\r\n"
            "Subject: Re: Test\r\n"
            "Message-ID: <reply-002@example.com>\r\n"
            "In-Reply-To: <original-001@example.com>\r\n"
            "\r\n"
            "Reply body"
        )
        tid = ingestor.compute_thread_id(msg)
        assert tid == _hash16("<original-001@example.com>")

    def test_thread_new_message_id(self, ingestor: EmailIngestor, simple_email_bytes: bytes) -> None:
        msg = ingestor.parse_email(simple_email_bytes)
        tid = ingestor.compute_thread_id(msg)
        expected = _hash16("<simple-text-001@example.com>")
        assert tid == expected

    def test_thread_fallback_subject_sender(self, ingestor: EmailIngestor, no_msgid_email_bytes: bytes) -> None:
        msg = ingestor.parse_email(no_msgid_email_bytes)
        tid = ingestor.compute_thread_id(msg)
        expected = _hash16("Email without Message-ID:irene@example.com")
        assert tid == expected

    def test_thread_id_is_16_chars(self, ingestor: EmailIngestor, simple_email_bytes: bytes) -> None:
        msg = ingestor.parse_email(simple_email_bytes)
        tid = ingestor.compute_thread_id(msg)
        assert len(tid) == 16


# ── Deduplication ────────────────────────────────────────────────


class TestDeduplication:
    """Test compute_dedup_key() and _is_duplicate()."""

    def test_dedup_key_with_message_id(self, ingestor: EmailIngestor, simple_email_bytes: bytes) -> None:
        msg = ingestor.parse_email(simple_email_bytes)
        key = ingestor.compute_dedup_key(msg, account_id=1, uid=100)
        assert key.startswith("msgid:")
        expected = f"msgid:{_hash16('<simple-text-001@example.com>')}"
        assert key == expected

    def test_dedup_key_no_message_id(self, ingestor: EmailIngestor, no_msgid_email_bytes: bytes) -> None:
        msg = ingestor.parse_email(no_msgid_email_bytes)
        key = ingestor.compute_dedup_key(msg, account_id=1, uid=100)
        assert key.startswith("content:")

    @pytest.mark.asyncio
    async def test_is_duplicate_false(self, ingestor: EmailIngestor, simple_email_bytes: bytes) -> None:
        msg = ingestor.parse_email(simple_email_bytes)
        result = await ingestor._is_duplicate(msg, account_id=1, folder="INBOX", uid=1, dedup_key="msgid:abc")
        assert result is False

    @pytest.mark.asyncio
    async def test_is_duplicate_true(self, ingestor: EmailIngestor, mock_db: MagicMock, simple_email_bytes: bytes) -> None:
        mock_db.check_email_duplicate = AsyncMock(return_value=True)
        msg = ingestor.parse_email(simple_email_bytes)
        result = await ingestor._is_duplicate(msg, account_id=1, folder="INBOX", uid=1, dedup_key="msgid:abc")
        assert result is True


# ── Helper functions ─────────────────────────────────────────────


class TestHelpers:
    """Test module-level helper functions."""

    def test_hash16_length(self) -> None:
        result = _hash16("test string")
        assert len(result) == 16

    def test_hash16_deterministic(self) -> None:
        assert _hash16("hello") == _hash16("hello")

    def test_hash16_different_inputs(self) -> None:
        assert _hash16("hello") != _hash16("world")

    def test_normalize_text(self) -> None:
        result = _normalize_text("  Hello   World  ")
        assert result == "hello world"

    def test_normalize_text_empty(self) -> None:
        assert _normalize_text("") == ""


# ── Document creation (mocked DB) ────────────────────────────────


class TestDocumentCreation:
    """Test _create_documents_from_email() with mocked Database."""

    @pytest.mark.asyncio
    async def test_create_body_document(
        self, ingestor: EmailIngestor, mock_db: MagicMock, simple_email_bytes: bytes
    ) -> None:
        msg = ingestor.parse_email(simple_email_bytes)
        account = EmailAccountConfig(name="test", body_handling="save_with_attachments")

        doc_ids = await ingestor._create_documents_from_email(
            msg, account, account_id=1, uid=100, folder="INBOX"
        )

        # Body is saved as one document
        assert len(doc_ids) == 1
        assert doc_ids[0] == 42
        mock_db.save_document.assert_called_once()

        # Check the save_document call had email source_type
        call_args = mock_db.save_document.call_args
        assert call_args.kwargs["source_type"] == "email"
        assert call_args.kwargs["source_name"] == "test"

    @pytest.mark.asyncio
    async def test_create_body_and_attachments(
        self, ingestor: EmailIngestor, mock_db: MagicMock, attachment_email_bytes: bytes
    ) -> None:
        msg = ingestor.parse_email(attachment_email_bytes)
        account = EmailAccountConfig(name="test", body_handling="save_with_attachments")

        doc_ids = await ingestor._create_documents_from_email(
            msg, account, account_id=1, uid=100, folder="INBOX"
        )

        # Body + 2 attachments = 3 documents (but .pdf may not extract text — still saved)
        # The .txt attachment will be extracted, the .pdf "report" may not extract
        # but should still be saved with empty body
        assert len(doc_ids) >= 2  # At least body + txt attachment
        assert mock_db.save_document.call_count >= 2

    @pytest.mark.asyncio
    async def test_attachments_only(
        self, ingestor: EmailIngestor, mock_db: MagicMock, attachment_email_bytes: bytes
    ) -> None:
        msg = ingestor.parse_email(attachment_email_bytes)
        account = EmailAccountConfig(name="test", body_handling="attachments_only")

        doc_ids = await ingestor._create_documents_from_email(
            msg, account, account_id=1, uid=100, folder="INBOX"
        )

        # Only attachments, no body doc
        # .txt should extract, .pdf may or may not
        assert len(doc_ids) >= 1

    @pytest.mark.asyncio
    async def test_body_only(
        self, ingestor: EmailIngestor, mock_db: MagicMock, attachment_email_bytes: bytes
    ) -> None:
        msg = ingestor.parse_email(attachment_email_bytes)
        account = EmailAccountConfig(name="test", body_handling="save_as_document")

        doc_ids = await ingestor._create_documents_from_email(
            msg, account, account_id=1, uid=100, folder="INBOX"
        )

        # Only body, no attachments
        assert len(doc_ids) == 1
        assert mock_db.save_document.call_count == 1


# ── Database integration (in-memory SQLite) ─────────────────────


class TestDatabaseEmailMethods:
    """Test Database email_* methods with a real SQLite database."""

    @pytest.fixture
    async def real_db(self, tmp_db_path: str):
        from src.core.db_sqlite import Database

        db = Database(db_path=tmp_db_path)
        await db.connect()
        yield db
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_create_and_get_email_account(self, real_db) -> None:
        acct = await real_db.create_email_account({
            "name": "test-imap",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "secret",
        })
        assert acct["name"] == "test-imap"
        assert acct["host"] == "imap.example.com"
        assert acct["id"] is not None

        fetched = await real_db.get_email_account(acct["id"])
        assert fetched["name"] == "test-imap"
        assert fetched["username"] == "user@example.com"

    @pytest.mark.asyncio
    async def test_list_email_accounts(self, real_db) -> None:
        await real_db.create_email_account({
            "name": "acct1", "host": "imap1.com", "username": "u1", "password": "p1"
        })
        await real_db.create_email_account({
            "name": "acct2", "host": "imap2.com", "username": "u2", "password": "p2",
            "enabled": False,
        })

        all_accts = await real_db.list_email_accounts()
        assert len(all_accts) == 2

        enabled = await real_db.list_email_accounts(enabled_only=True)
        assert len(enabled) == 1
        assert enabled[0]["name"] == "acct1"

    @pytest.mark.asyncio
    async def test_update_email_account(self, real_db) -> None:
        acct = await real_db.create_email_account({
            "name": "test", "host": "imap.example.com",
            "username": "user", "password": "pass"
        })
        updated = await real_db.update_email_account(acct["id"], {"port": 143, "use_ssl": False})
        assert updated["port"] == 143
        assert updated["use_ssl"] is False

    @pytest.mark.asyncio
    async def test_delete_email_account(self, real_db) -> None:
        acct = await real_db.create_email_account({
            "name": "todelete", "host": "imap.example.com",
            "username": "user", "password": "pass"
        })
        deleted = await real_db.delete_email_account(acct["id"])
        assert deleted is True
        fetched = await real_db.get_email_account(acct["id"])
        assert fetched is None

    @pytest.mark.asyncio
    async def test_log_and_check_duplicate(self, real_db) -> None:
        acct = await real_db.create_email_account({
            "name": "test", "host": "imap.example.com",
            "username": "user", "password": "pass"
        })

        # Log an ingestion
        log_id = await real_db.log_email_ingestion({
            "account_id": acct["id"],
            "message_id": "<test-001@example.com>",
            "uid": 100,
            "folder": "INBOX",
            "subject": "Test",
            "sender": "test@example.com",
            "status": "completed",
            "document_ids": [1, 2],
            "dedup_key": "msgid:abc123",
        })
        assert log_id > 0

        # Check duplicate by message_id
        is_dup = await real_db.check_email_duplicate(
            account_id=acct["id"],
            message_id="<test-001@example.com>",
            folder="INBOX",
            uid=100,
            dedup_key="msgid:abc123",
        )
        assert is_dup is True

        # Check non-duplicate
        is_dup_new = await real_db.check_email_duplicate(
            account_id=acct["id"],
            message_id="<new@example.com>",
            folder="INBOX",
            uid=200,
            dedup_key="msgid:new",
        )
        assert is_dup_new is False

    @pytest.mark.asyncio
    async def test_list_ingestion_logs(self, real_db) -> None:
        acct = await real_db.create_email_account({
            "name": "test", "host": "imap.example.com",
            "username": "user", "password": "pass"
        })

        for i in range(5):
            await real_db.log_email_ingestion({
                "account_id": acct["id"],
                "message_id": f"<msg-{i}@example.com>",
                "uid": 100 + i,
                "folder": "INBOX",
                "subject": f"Subject {i}",
                "sender": "sender@example.com",
                "status": "completed" if i % 2 == 0 else "failed",
                "document_ids": [i],
            })

        all_logs = await real_db.list_email_ingestion_logs(acct["id"])
        assert len(all_logs) == 5

        completed = await real_db.list_email_ingestion_logs(acct["id"], status="completed")
        assert len(completed) == 3

        failed = await real_db.list_email_ingestion_logs(acct["id"], status="failed")
        assert len(failed) == 2

        count = await real_db.count_email_ingestion_logs(acct["id"])
        assert count == 5

    @pytest.mark.asyncio
    async def test_update_email_account_sync(self, real_db) -> None:
        acct = await real_db.create_email_account({
            "name": "test", "host": "imap.example.com",
            "username": "user", "password": "pass"
        })
        now = datetime.now(timezone.utc).isoformat()
        await real_db.update_email_account_sync(acct["id"], now, last_error=None)
        fetched = await real_db.get_email_account(acct["id"])
        assert fetched["last_sync_at"] == now
