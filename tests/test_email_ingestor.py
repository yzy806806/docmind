"""Unit tests for the EmailIngestor service.

Tests cover:
  - Email parsing (MIME structure, multipart, single-part)
  - Body extraction (text/plain, text/html, multipart/alternative)
  - Attachment extraction and filtering (whitelist/blacklist glob patterns)
  - Thread ID computation (References, In-Reply-To, Message-ID, fallback)
  - Deduplication key computation (Message-ID, content hash fallback)
  - DB schema: email_accounts and email_ingestion_log tables round-trip
  - SourceType.EMAIL registration
  - Background worker integration (smoke test with mock DB)

Tests use real .eml bytes constructed in-memory (no .eml fixture files needed)
and a real SQLite Database for persistence round-trip verification.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from datetime import datetime, timezone
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from src.core.config import EmailAccountConfig, EmailConfig
from src.core.email_ingestor import EmailIngestor, email_polling_worker, _hash16
from src.core.models import SourceType


# ── Helpers ──────────────────────────────────────────────────────


def make_simple_text_email(
    subject: str = "Test Subject",
    sender: str = "sender@example.com",
    to: str = "recipient@example.com",
    body: str = "Hello, this is a test email.",
    message_id: str = "<test123@example.com>",
) -> bytes:
    """Build a simple text/plain email and return raw bytes."""
    msg = MIMEText(body, _subtype="plain", _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg["Message-ID"] = message_id
    msg["Date"] = "Mon, 06 Jul 2026 12:00:00 +0000"
    return msg.as_bytes()


def make_html_email(
    subject: str = "HTML Email",
    body_html: str = "<html><body><h1>Hello</h1><p>HTML content</p></body></html>",
    message_id: str = "<html123@example.com>",
) -> bytes:
    """Build a text/html email and return raw bytes."""
    msg = MIMEText(body_html, _subtype="html", _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = "sender@example.com"
    msg["To"] = "recipient@example.com"
    msg["Message-ID"] = message_id
    msg["Date"] = "Mon, 06 Jul 2026 12:00:00 +0000"
    return msg.as_bytes()


def make_multipart_email(
    subject: str = "Multipart Email",
    plain_body: str = "This is the plain text part.",
    html_body: str = "<html><body><p>HTML part</p></body></html>",
    message_id: str = "<multi123@example.com>",
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> bytes:
    """Build a multipart/alternative email with optional attachments.

    attachments: list of (filename, content_bytes, mime_type)
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = "sender@example.com"
    msg["To"] = "recipient@example.com"
    msg["Message-ID"] = message_id
    msg["Date"] = "Mon, 06 Jul 2026 12:00:00 +0000"

    part1 = MIMEText(plain_body, "plain", "utf-8")
    part2 = MIMEText(html_body, "html", "utf-8")
    msg.attach(part1)
    msg.attach(part2)

    # If attachments, convert to multipart/mixed
    if attachments:
        mixed = MIMEMultipart("mixed")
        # Copy headers
        for k, v in msg.items():
            mixed[k] = v
        mixed.attach(msg)
        for filename, content, mime_type in attachments:
            att = MIMEApplication(content)
            att.add_header("Content-Disposition", "attachment", filename=filename)
            att.set_type(mime_type)
            mixed.attach(att)
        return mixed.as_bytes()

    return msg.as_bytes()


def make_threaded_email(
    subject: str = "Re: Thread Test",
    in_reply_to: str = "<original@example.com>",
    references: str = "<original@example.com> <reply1@example.com>",
    message_id: str = "<reply2@example.com>",
) -> bytes:
    """Build a threaded email with In-Reply-To and References headers."""
    msg = MIMEText("Reply body", "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = "replier@example.com"
    msg["To"] = "recipient@example.com"
    msg["Message-ID"] = message_id
    msg["In-Reply-To"] = in_reply_to
    msg["References"] = references
    msg["Date"] = "Mon, 06 Jul 2026 12:00:00 +0000"
    return msg.as_bytes()


@pytest.fixture
def tmp_db_path():
    """Provide a temporary database file path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "test_email.db")


@pytest.fixture
async def db(tmp_db_path):
    """Create a connected SQLite Database instance."""
    from src.core.db_sqlite import Database

    database = Database(db_path=tmp_db_path)
    await database.connect()
    yield database
    await database.disconnect()


@pytest.fixture
def ingestor(db):
    """Create an EmailIngestor with the test DB."""
    return EmailIngestor(db=db)


# ── SourceType.EMAIL ─────────────────────────────────────────────


class TestSourceTypeEmail:
    """Verify SourceType.EMAIL is registered."""

    def test_email_source_type_exists(self):
        assert hasattr(SourceType, "EMAIL")

    def test_email_source_type_value(self):
        assert SourceType.EMAIL == "email"
        assert SourceType.EMAIL.value == "email"

    def test_email_source_type_is_str_enum(self):
        assert isinstance(SourceType.EMAIL, str)
        assert SourceType("email") is SourceType.EMAIL


# ── Email parsing ────────────────────────────────────────────────


class TestParseEmail:
    """Test parse_email() with various email formats."""

    def test_parse_simple_text_email(self, ingestor):
        raw = make_simple_text_email()
        msg = ingestor.parse_email(raw)
        assert msg["Subject"] == "Test Subject"
        assert msg["From"] == "sender@example.com"
        assert msg["Message-ID"] == "<test123@example.com>"

    def test_parse_html_email(self, ingestor):
        raw = make_html_email()
        msg = ingestor.parse_email(raw)
        assert msg["Subject"] == "HTML Email"
        assert msg["Message-ID"] == "<html123@example.com>"

    def test_parse_multipart_email(self, ingestor):
        raw = make_multipart_email()
        msg = ingestor.parse_email(raw)
        assert msg.is_multipart()
        assert msg["Subject"] == "Multipart Email"

    def test_parse_email_with_attachments(self, ingestor):
        raw = make_multipart_email(
            attachments=[("doc.txt", b"Text content", "text/plain")]
        )
        msg = ingestor.parse_email(raw)
        assert msg.is_multipart()

    def test_parse_email_preserves_headers(self, ingestor):
        raw = make_threaded_email()
        msg = ingestor.parse_email(raw)
        assert msg["In-Reply-To"] == "<original@example.com>"
        assert "References" in msg


# ── Body extraction ──────────────────────────────────────────────


class TestExtractBody:
    """Test extract_body() with various email formats."""

    def test_extract_body_plain_text(self, ingestor):
        raw = make_simple_text_email(body="Hello, world!")
        msg = ingestor.parse_email(raw)
        body = ingestor.extract_body(msg)
        assert "Hello, world!" in body

    def test_extract_body_html_only(self, ingestor):
        raw = make_html_email(body_html="<html><body><p>HTML content</p></body></html>")
        msg = ingestor.parse_email(raw)
        body = ingestor.extract_body(msg)
        assert "HTML content" in body

    def test_extract_body_multipart_prefers_plain(self, ingestor):
        """In multipart/alternative, text/plain should be preferred over HTML."""
        raw = make_multipart_email(
            plain_body="PLAIN TEXT HERE",
            html_body="<p>HTML VERSION</p>",
        )
        msg = ingestor.parse_email(raw)
        body = ingestor.extract_body(msg)
        assert "PLAIN TEXT HERE" in body

    def test_extract_body_empty_when_no_text(self, ingestor):
        """Email with no text/plain or text/html returns empty string."""
        msg = EmailMessage()
        msg["Subject"] = "Empty"
        body = ingestor.extract_body(msg)
        assert body == ""

    def test_extract_body_strips_html_tags(self, ingestor):
        raw = make_html_email(
            body_html="<html><body><script>alert(1)</script><p>Visible text</p></body></html>"
        )
        msg = ingestor.parse_email(raw)
        body = ingestor.extract_body(msg)
        assert "Visible text" in body
        assert "<script>" not in body
        assert "alert" not in body


# ── Attachment extraction ────────────────────────────────────────


class TestExtractAttachments:
    """Test extract_attachments() and filter_attachments()."""

    def test_no_attachments_in_plain_email(self, ingestor):
        raw = make_simple_text_email()
        msg = ingestor.parse_email(raw)
        atts = ingestor.extract_attachments(msg)
        assert atts == []

    def test_extract_single_attachment(self, ingestor):
        raw = make_multipart_email(
            attachments=[("report.txt", b"Report content", "text/plain")]
        )
        msg = ingestor.parse_email(raw)
        atts = ingestor.extract_attachments(msg)
        assert len(atts) == 1
        assert atts[0]["filename"] == "report.txt"
        assert atts[0]["content"] == b"Report content"
        assert atts[0]["ext"] == ".txt"

    def test_extract_multiple_attachments(self, ingestor):
        raw = make_multipart_email(
            attachments=[
                ("file1.txt", b"Content 1", "text/plain"),
                ("file2.txt", b"Content 2", "text/plain"),
            ]
        )
        msg = ingestor.parse_email(raw)
        atts = ingestor.extract_attachments(msg)
        assert len(atts) == 2
        filenames = [a["filename"] for a in atts]
        assert "file1.txt" in filenames
        assert "file2.txt" in filenames

    def test_extract_attachment_ext_lowercase(self, ingestor):
        raw = make_multipart_email(
            attachments=[("DOC.PDF", b"%PDF-1.4", "application/pdf")]
        )
        msg = ingestor.parse_email(raw)
        atts = ingestor.extract_attachments(msg)
        assert len(atts) == 1
        assert atts[0]["ext"] == ".pdf"


class TestFilterAttachments:
    """Test filter_attachments() whitelist/blacklist glob patterns."""

    def test_empty_filters_return_all(self, ingestor):
        atts = [
            {"filename": "a.txt", "content": b"", "mime_type": "", "ext": ".txt"},
            {"filename": "b.pdf", "content": b"", "mime_type": "", "ext": ".pdf"},
        ]
        result = ingestor.filter_attachments(atts, whitelist="", blacklist="")
        assert len(result) == 2

    def test_whitelist_filters_by_pattern(self, ingestor):
        atts = [
            {"filename": "a.txt", "content": b"", "mime_type": "", "ext": ".txt"},
            {"filename": "b.pdf", "content": b"", "mime_type": "", "ext": ".pdf"},
            {"filename": "c.docx", "content": b"", "mime_type": "", "ext": ".docx"},
        ]
        result = ingestor.filter_attachments(atts, whitelist="*.pdf,*.docx")
        assert len(result) == 2
        filenames = [a["filename"] for a in result]
        assert "b.pdf" in filenames
        assert "c.docx" in filenames
        assert "a.txt" not in filenames

    def test_blacklist_removes_matching(self, ingestor):
        atts = [
            {"filename": "a.txt", "content": b"", "mime_type": "", "ext": ".txt"},
            {"filename": "b.pdf", "content": b"", "mime_type": "", "ext": ".pdf"},
        ]
        result = ingestor.filter_attachments(atts, blacklist="*.pdf")
        assert len(result) == 1
        assert result[0]["filename"] == "a.txt"

    def test_blacklist_takes_precedence_over_whitelist(self, ingestor):
        atts = [
            {"filename": "a.pdf", "content": b"", "mime_type": "", "ext": ".pdf"},
            {"filename": "b.pdf", "content": b"", "mime_type": "", "ext": ".pdf"},
        ]
        # Whitelist allows PDFs, but blacklist removes a.pdf
        result = ingestor.filter_attachments(atts, whitelist="*.pdf", blacklist="a.*")
        assert len(result) == 1
        assert result[0]["filename"] == "b.pdf"

    def test_empty_attachments_list(self, ingestor):
        result = ingestor.filter_attachments([], whitelist="*.pdf", blacklist="")
        assert result == []


# ── Thread ID computation ────────────────────────────────────────


class TestComputeThreadId:
    """Test compute_thread_id() with various header combinations."""

    def test_uses_references_first(self, ingestor):
        raw = make_threaded_email(
            references="<root@example.com> <reply1@example.com>",
            in_reply_to="<reply1@example.com>",
            message_id="<reply2@example.com>",
        )
        msg = ingestor.parse_email(raw)
        thread_id = ingestor.compute_thread_id(msg)
        # Should hash the first References entry
        expected = _hash16("<root@example.com>")
        assert thread_id == expected

    def test_uses_in_reply_to_when_no_references(self, ingestor):
        raw = make_threaded_email(
            references="",
            in_reply_to="<original@example.com>",
            message_id="<reply2@example.com>",
        )
        msg = ingestor.parse_email(raw)
        # Need to clear References header
        del msg["References"]
        thread_id = ingestor.compute_thread_id(msg)
        expected = _hash16("<original@example.com>")
        assert thread_id == expected

    def test_uses_message_id_for_new_thread(self, ingestor):
        raw = make_simple_text_email(message_id="<unique123@example.com>")
        msg = ingestor.parse_email(raw)
        # Remove References and In-Reply-To if present
        del msg["References"]
        del msg["In-Reply-To"]
        thread_id = ingestor.compute_thread_id(msg)
        expected = _hash16("<unique123@example.com>")
        assert thread_id == expected

    def test_fallback_to_subject_sender_when_no_headers(self, ingestor):
        msg = EmailMessage()
        msg["Subject"] = "Test Subject"
        msg["From"] = "sender@example.com"
        thread_id = ingestor.compute_thread_id(msg)
        expected = _hash16("Test Subject:sender@example.com")
        assert thread_id == expected

    def test_thread_id_is_16_chars(self, ingestor):
        raw = make_simple_text_email()
        msg = ingestor.parse_email(raw)
        thread_id = ingestor.compute_thread_id(msg)
        assert len(thread_id) == 16

    def test_same_email_same_thread_id(self, ingestor):
        """The same email should always produce the same thread ID."""
        raw = make_simple_text_email(message_id="<consistent@example.com>")
        msg1 = ingestor.parse_email(raw)
        msg2 = ingestor.parse_email(raw)
        del msg1["References"]
        del msg1["In-Reply-To"]
        del msg2["References"]
        del msg2["In-Reply-To"]
        assert ingestor.compute_thread_id(msg1) == ingestor.compute_thread_id(msg2)


# ── Deduplication key computation ────────────────────────────────


class TestComputeDedupKey:
    """Test compute_dedup_key() for composite deduplication."""

    def test_uses_message_id_when_present(self, ingestor):
        raw = make_simple_text_email(message_id="<dedup123@example.com>")
        msg = ingestor.parse_email(raw)
        key = ingestor.compute_dedup_key(msg, account_id=1, uid=100)
        assert key.startswith("msgid:")
        assert key == f"msgid:{_hash16('<dedup123@example.com>')}"

    def test_uses_content_hash_when_no_message_id(self, ingestor):
        msg = EmailMessage()
        msg["Subject"] = "Test"
        msg["From"] = "sender@example.com"
        msg.set_content("Body content")
        key = ingestor.compute_dedup_key(msg, account_id=1, uid=100)
        assert key.startswith("content:")

    def test_dedup_key_is_deterministic(self, ingestor):
        raw = make_simple_text_email(message_id="<det@example.com>")
        msg = ingestor.parse_email(raw)
        key1 = ingestor.compute_dedup_key(msg, account_id=1, uid=1)
        key2 = ingestor.compute_dedup_key(msg, account_id=1, uid=1)
        assert key1 == key2

    def test_different_message_ids_different_keys(self, ingestor):
        raw1 = make_simple_text_email(message_id="<one@example.com>")
        raw2 = make_simple_text_email(message_id="<two@example.com>")
        msg1 = ingestor.parse_email(raw1)
        msg2 = ingestor.parse_email(raw2)
        key1 = ingestor.compute_dedup_key(msg1, 1, 1)
        key2 = ingestor.compute_dedup_key(msg2, 1, 1)
        assert key1 != key2


# ── DB schema round-trip ─────────────────────────────────────────


class TestEmailAccountDBRoundTrip:
    """Verify email_accounts table CRUD with plaintext password column."""

    async def test_create_and_get_email_account(self, db):
        data = {
            "name": "test-account",
            "host": "imap.example.com",
            "port": 993,
            "use_ssl": True,
            "username": "user@example.com",
            "password": "supersecret123",
            "folder": "INBOX",
        }
        created = await db.create_email_account(data)
        assert created["name"] == "test-account"
        assert created["password"] == "supersecret123"
        assert created["id"] is not None

        fetched = await db.get_email_account(created["id"])
        assert fetched["password"] == "supersecret123"
        assert fetched["host"] == "imap.example.com"

    async def test_plaintext_password_round_trips_correctly(self, db):
        """The password column must store and return plaintext exactly."""
        test_passwords = [
            "simple",
            "p@ssw0rd!#$%",
            "unicode: üñîçödé",
            "very-long-password-" + "x" * 200,
        ]
        for i, pwd in enumerate(test_passwords):
            data = {
                "name": f"acct-{i}",
                "host": "imap.example.com",
                "username": f"user{i}@example.com",
                "password": pwd,
            }
            created = await db.create_email_account(data)
            fetched = await db.get_email_account(created["id"])
            assert fetched["password"] == pwd, f"Password mismatch for index {i}"

    async def test_list_email_accounts(self, db):
        for i in range(3):
            await db.create_email_account({
                "name": f"account-{i}",
                "host": "imap.example.com",
                "username": f"user{i}@example.com",
                "password": f"pass{i}",
                "enabled": i < 2,  # Third is disabled
            })
        all_accounts = await db.list_email_accounts()
        assert len(all_accounts) == 3

        enabled = await db.list_email_accounts(enabled_only=True)
        assert len(enabled) == 2

    async def test_update_email_account(self, db):
        created = await db.create_email_account({
            "name": "test",
            "host": "old.example.com",
            "username": "user@example.com",
            "password": "oldpass",
        })
        updated = await db.update_email_account(created["id"], {
            "host": "new.example.com",
            "password": "newpass",
        })
        assert updated["host"] == "new.example.com"
        assert updated["password"] == "newpass"

    async def test_delete_email_account(self, db):
        created = await db.create_email_account({
            "name": "to-delete",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "pass",
        })
        deleted = await db.delete_email_account(created["id"])
        assert deleted is True
        assert await db.get_email_account(created["id"]) is None

    async def test_update_email_account_sync(self, db):
        created = await db.create_email_account({
            "name": "sync-test",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "pass",
        })
        now = datetime.now(timezone.utc).isoformat()
        await db.update_email_account_sync(created["id"], now, "Connection timeout")
        fetched = await db.get_email_account(created["id"])
        assert fetched["last_sync_at"] == now
        assert fetched["last_error"] == "Connection timeout"

    async def test_update_email_account_error(self, db):
        created = await db.create_email_account({
            "name": "err-test",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "pass",
        })
        await db.update_email_account_error(created["id"], "Auth failed")
        fetched = await db.get_email_account(created["id"])
        assert fetched["last_error"] == "Auth failed"


class TestEmailIngestionLogDBRoundTrip:
    """Verify email_ingestion_log table operations."""

    async def test_log_and_list_ingestion(self, db):
        acct = await db.create_email_account({
            "name": "log-test",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "pass",
        })
        log_id = await db.log_email_ingestion({
            "account_id": acct["id"],
            "message_id": "<msg1@example.com>",
            "uid": 100,
            "folder": "INBOX",
            "subject": "Test",
            "sender": "sender@example.com",
            "received_at": "Mon, 06 Jul 2026 12:00:00 +0000",
            "status": "completed",
            "document_ids": [1, 2, 3],
            "dedup_key": "msgid:abc123",
        })
        assert log_id > 0

        logs = await db.list_email_ingestion_logs(acct["id"])
        assert len(logs) == 1
        assert logs[0]["subject"] == "Test"
        assert logs[0]["document_ids"] == [1, 2, 3]

    async def test_update_ingestion_log_status(self, db):
        acct = await db.create_email_account({
            "name": "upd-test",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "pass",
        })
        log_id = await db.log_email_ingestion({
            "account_id": acct["id"],
            "message_id": "<msg2@example.com>",
            "uid": 200,
            "folder": "INBOX",
            "subject": "Test",
            "sender": "sender@example.com",
            "received_at": "",
            "status": "processing",
        })
        await db.update_email_ingestion_log(log_id, {
            "status": "completed",
            "document_ids": [42],
        })
        logs = await db.list_email_ingestion_logs(acct["id"])
        assert logs[0]["status"] == "completed"
        assert logs[0]["document_ids"] == [42]

    async def test_check_email_duplicate(self, db):
        acct = await db.create_email_account({
            "name": "dup-test",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "pass",
        })
        # No entries yet — not a duplicate
        is_dup = await db.check_email_duplicate(
            acct["id"], "<msg@example.com>", "INBOX", 1, "msgid:hash"
        )
        assert is_dup is False

        # Insert a log entry
        await db.log_email_ingestion({
            "account_id": acct["id"],
            "message_id": "<msg@example.com>",
            "uid": 1,
            "folder": "INBOX",
            "subject": "Test",
            "sender": "sender@example.com",
            "received_at": "",
            "status": "completed",
            "dedup_key": "msgid:hash",
        })

        # Same message_id — now it's a duplicate
        is_dup = await db.check_email_duplicate(
            acct["id"], "<msg@example.com>", "INBOX", 1, "msgid:hash"
        )
        assert is_dup is True

    async def test_count_email_ingestion_logs(self, db):
        acct = await db.create_email_account({
            "name": "count-test",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "pass",
        })
        for i in range(5):
            await db.log_email_ingestion({
                "account_id": acct["id"],
                "message_id": f"<msg{i}@example.com>",
                "uid": i,
                "folder": "INBOX",
                "subject": f"Test {i}",
                "sender": "sender@example.com",
                "received_at": "",
                "status": "completed" if i < 3 else "failed",
            })
        total = await db.count_email_ingestion_logs(acct["id"])
        assert total == 5

        completed = await db.count_email_ingestion_logs(acct["id"], status="completed")
        assert completed == 3

        failed = await db.count_email_ingestion_logs(acct["id"], status="failed")
        assert failed == 2

    async def test_list_logs_with_status_filter(self, db):
        acct = await db.create_email_account({
            "name": "filter-test",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "pass",
        })
        for i in range(3):
            await db.log_email_ingestion({
                "account_id": acct["id"],
                "message_id": f"<m{i}@example.com>",
                "uid": i,
                "folder": "INBOX",
                "subject": f"Subject {i}",
                "sender": "s@example.com",
                "received_at": "",
                "status": "skipped" if i == 1 else "completed",
            })
        completed_logs = await db.list_email_ingestion_logs(
            acct["id"], status="completed"
        )
        assert len(completed_logs) == 2
        for log in completed_logs:
            assert log["status"] == "completed"


# ── Config ───────────────────────────────────────────────────────


class TestEmailConfig:
    """Verify EmailConfig and EmailAccountConfig dataclasses."""

    def test_email_account_config_defaults(self):
        acct = EmailAccountConfig()
        assert acct.port == 993
        assert acct.use_ssl is True
        assert acct.folder == "INBOX"
        assert acct.action_after_fetch == "mark_seen"
        assert acct.body_handling == "save_with_attachments"
        assert acct.deduplication_strategy == "message_id"
        assert acct.enabled is True
        assert acct.password == ""  # Plaintext, empty by default

    def test_email_config_defaults(self):
        cfg = EmailConfig()
        assert cfg.enabled is False  # Disabled by default
        assert cfg.poll_interval_seconds == 600.0
        assert cfg.accounts == []

    def test_email_account_config_custom_values(self):
        acct = EmailAccountConfig(
            name="work",
            host="imap.work.com",
            username="me@work.com",
            password="secret",
            port=143,
            use_ssl=False,
        )
        assert acct.name == "work"
        assert acct.host == "imap.work.com"
        assert acct.password == "secret"
        assert acct.port == 143
        assert acct.use_ssl is False


# ── HTML to text ─────────────────────────────────────────────────


class TestHtmlToText:
    """Test the _html_to_text static method."""

    def test_strips_script_tags(self, ingestor):
        html = "<p>Text</p><script>alert(1)</script><p>More</p>"
        result = EmailIngestor._html_to_text(html)
        assert "alert" not in result
        assert "Text" in result
        assert "More" in result

    def test_strips_style_tags(self, ingestor):
        html = "<style>body { color: red; }</style><p>Content</p>"
        result = EmailIngestor._html_to_text(html)
        assert "color" not in result
        assert "Content" in result

    def test_decodes_html_entities(self, ingestor):
        html = "<p>Price: &pound;100 &amp; tax</p>"
        result = EmailIngestor._html_to_text(html)
        assert "£" in result or "pound" in result.lower()
        assert "&" in result


# ── Connection test ──────────────────────────────────────────────


class TestConnectionTest:
    """Test test_connection() with mocked IMAP."""

    async def test_connection_success(self, ingestor):
        account = EmailAccountConfig(
            name="test",
            host="imap.example.com",
            username="user@example.com",
            password="pass",
        )
        mock_conn = MagicMock()
        mock_conn.select = MagicMock(return_value=("OK", [b"5"]))
        with patch.object(ingestor, "_connect_imap", return_value=mock_conn):
            with patch.object(ingestor, "_disconnect_imap"):
                success, msg = await ingestor.test_connection(account)
        assert success is True
        assert "successful" in msg.lower()

    async def test_connection_failure(self, ingestor):
        account = EmailAccountConfig(
            name="test",
            host="bad.example.com",
            username="user@example.com",
            password="wrong",
        )
        with patch.object(
            ingestor, "_connect_imap", side_effect=Exception("Auth failed")
        ):
            success, msg = await ingestor.test_connection(account)
        assert success is False
        assert "Auth failed" in msg


# ── Worker smoke test ────────────────────────────────────────────


class TestEmailPollingWorker:
    """Smoke test for the email_polling_worker background function."""

    async def test_worker_handles_no_accounts(self):
        """Worker should handle gracefully when no enabled accounts exist."""
        mock_db = AsyncMock()
        mock_db.list_email_accounts = AsyncMock(return_value=[])

        # Run one iteration then cancel
        task = asyncio.create_task(
            email_polling_worker(mock_db, poll_interval=0.01)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Verify it queried for accounts
        mock_db.list_email_accounts.assert_called_with(enabled_only=True)

    async def test_worker_handles_db_error(self):
        """Worker should log and continue on DB errors."""
        mock_db = AsyncMock()
        mock_db.list_email_accounts = AsyncMock(
            side_effect=Exception("DB connection lost")
        )

        task = asyncio.create_task(
            email_polling_worker(mock_db, poll_interval=0.01)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Should have been called (and failed gracefully)
        assert mock_db.list_email_accounts.called
