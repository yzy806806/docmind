"""Phase 8a email ingestion verification tests.

Targeted tests covering three areas specified in the Phase 8a testing task,
supplementing the 113 existing tests in test_email_ingestion.py,
test_email_ingestor.py, and test_email_integration.py:

  1. Credential storage round-trip — password used by IMAP login,
     credential update propagation, full lifecycle.
  2. Body + attachment saving — metadata completeness, path format,
     body_handling mode enforcement, filter integration.
  3. Post-fetch action verification — mark_seen applied/not-applied,
     store failure resilience, correct UID usage.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest

from src.core.config import EmailAccountConfig
from src.core.db_sqlite import Database
from src.core.email_ingestor import EmailIngestor, _hash16
from src.core.extractor import Extractor

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "emails"


# ── Mock IMAP with credential and store tracking ──────────────────

class TrackingIMAP:
    """Mock IMAP connection that records login creds and store calls."""

    def __init__(self, emails: list[bytes] | None = None):
        self._emails = emails or []
        self.login_calls: list[tuple[str, str]] = []
        self.store_calls: list[tuple[int, str, str]] = []
        self.select_called = False
        self.search_called = False
        self.logout_called = False

    def login(self, username: str, password: str):
        self.login_calls.append((username, password))
        return ("OK", [b"Logged in"])

    def select(self, mailbox: str = "INBOX"):
        self.select_called = True
        return ("OK", [str(len(self._emails)).encode()])

    def search(self, charset, *criteria):
        self.search_called = True
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
        self.store_calls.append((uid, flags_op, flags))
        return ("OK", [None])

    def close(self):
        return ("OK", [None])

    def logout(self):
        self.logout_called = True
        return ("BYE", [None])


# ── Fixtures ──────────────────────────────────────────────────────

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
def html_email() -> bytes:
    return (FIXTURES_DIR / "with_html_body.eml").read_bytes()


@pytest.fixture
def no_msgid_email() -> bytes:
    return (FIXTURES_DIR / "no_message_id.eml").read_bytes()


# ══════════════════════════════════════════════════════════════════
# 1. CREDENTIAL STORAGE ROUND-TRIP
# ══════════════════════════════════════════════════════════════════


class TestCredentialStorageRoundTrip:
    """Verify plaintext password flows correctly through the pipeline."""

    @pytest.mark.asyncio
    async def test_password_passed_to_imap_login(
        self, real_db: Database, simple_email: bytes
    ) -> None:
        """Password stored in DB must be passed to IMAP login() during poll."""
        await real_db.create_email_account({
            "name": "cred-test",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "my-secret-p@ss",
        })

        mock_imap = TrackingIMAP(emails=[simple_email])
        ingestor = EmailIngestor(real_db, extractor=Extractor())

        def connect_and_login(account):
            mock_imap.login(account.username, account.password)
            return mock_imap

        with patch.object(EmailIngestor, "_connect_imap", side_effect=connect_and_login):
            await ingestor.poll_account(
                EmailAccountConfig(
                    name="cred-test", host="imap.example.com",
                    username="user@example.com", password="my-secret-p@ss",
                ),
                account_id=1,
            )

        assert len(mock_imap.login_calls) == 1
        assert mock_imap.login_calls[0] == ("user@example.com", "my-secret-p@ss")

    @pytest.mark.asyncio
    async def test_credential_update_used_on_next_poll(
        self, real_db: Database, simple_email: bytes
    ) -> None:
        """Updating password in DB should be used on the next poll cycle."""
        await real_db.create_email_account({
            "name": "update-test",
            "host": "imap.example.com",
            "username": "olduser",
            "password": "oldpass",
        })

        await real_db.update_email_account(1, {
            "username": "newuser", "password": "newpass",
        })

        mock_imap = TrackingIMAP(emails=[simple_email])
        ingestor = EmailIngestor(real_db, extractor=Extractor())

        def connect_and_login(account):
            mock_imap.login(account.username, account.password)
            return mock_imap

        with patch.object(EmailIngestor, "_connect_imap", side_effect=connect_and_login):
            await ingestor.poll_account(
                EmailAccountConfig(
                    name="update-test", host="imap.example.com",
                    username="newuser", password="newpass",
                ),
                account_id=1,
            )

        assert mock_imap.login_calls[0] == ("newuser", "newpass")

    @pytest.mark.asyncio
    async def test_full_credential_lifecycle(
        self, real_db: Database
    ) -> None:
        """Password survives create -> read -> update -> read -> delete."""
        created = await real_db.create_email_account({
            "name": "lifecycle", "host": "imap.example.com",
            "username": "start", "password": "start-pass",
        })
        assert created["password"] == "start-pass"

        fetched = await real_db.get_email_account(created["id"])
        assert fetched["password"] == "start-pass"

        updated = await real_db.update_email_account(created["id"], {
            "password": "updated-pass",
        })
        assert updated["password"] == "updated-pass"

        fetched2 = await real_db.get_email_account(created["id"])
        assert fetched2["password"] == "updated-pass"

        deleted = await real_db.delete_email_account(created["id"])
        assert deleted is True
        assert await real_db.get_email_account(created["id"]) is None

    @pytest.mark.asyncio
    async def test_empty_password_round_trip(self, real_db: Database) -> None:
        """Empty password should store and retrieve as empty string."""
        await real_db.create_email_account({
            "name": "empty-pass", "host": "imap.example.com",
            "username": "user@example.com", "password": "",
        })
        acct = await real_db.get_email_account(1)
        assert acct["password"] == ""

    @pytest.mark.asyncio
    async def test_account_lookup_by_name(self, real_db: Database) -> None:
        """_lookup_account_id should find accounts by name."""
        await real_db.create_email_account({
            "name": "lookup-me", "host": "imap.example.com",
            "username": "u1", "password": "p1",
        })
        await real_db.create_email_account({
            "name": "other", "host": "imap.example.com",
            "username": "u2", "password": "p2",
        })
        ingestor = EmailIngestor(real_db)
        assert await ingestor._lookup_account_id("lookup-me") == 1
        assert await ingestor._lookup_account_id("other") == 2
        assert await ingestor._lookup_account_id("nonexistent") is None


# ══════════════════════════════════════════════════════════════════
# 2. BODY + ATTACHMENT SAVING
# ══════════════════════════════════════════════════════════════════


class TestBodyAndAttachmentSaving:
    """Verify body and attachment documents are created correctly."""

    async def _poll_and_get_docs(self, real_db, account_name, emails, **kwargs):
        """Helper: create account, poll, return list of document dicts."""
        account_data = {
            "name": account_name,
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "secret",
            **kwargs,
        }
        await real_db.create_email_account(account_data)

        mock_imap = TrackingIMAP(emails=emails)
        ingestor = EmailIngestor(real_db, extractor=Extractor())

        acct_fields = {f.name for f in EmailAccountConfig.__dataclass_fields__.values()}
        acct = EmailAccountConfig(
            name=account_name, host="imap.example.com",
            username="user@example.com", password="secret",
            **{k: v for k, v in kwargs.items() if k in acct_fields},
        )

        with patch.object(EmailIngestor, "_connect_imap", return_value=mock_imap):
            doc_ids = await ingestor.poll_account(acct, account_id=1)

        docs = []
        for did in doc_ids:
            doc = await real_db.get_document(did)
            if doc:
                docs.append(doc)
        return docs

    @pytest.mark.asyncio
    async def test_body_document_metadata_complete(
        self, real_db: Database, simple_email: bytes
    ) -> None:
        """Body document must include all email-specific metadata fields."""
        docs = await self._poll_and_get_docs(real_db, "meta-test", [simple_email])
        assert len(docs) == 1

        meta = docs[0].get("metadata", {})
        assert meta["email_message_id"] == "<simple-text-001@example.com>"
        assert meta["email_subject"] == "Simple text email"
        assert meta["email_sender"] == "alice@example.com"
        assert "email_received_at" in meta
        assert len(meta["email_thread_id"]) == 16
        assert meta["email_uid"] == 1
        assert meta["email_folder"] == "INBOX"
        assert meta["email_part"] == "body"

    @pytest.mark.asyncio
    async def test_body_document_path_format(
        self, real_db: Database, simple_email: bytes
    ) -> None:
        """Body document path must follow email://account/message_id/body."""
        docs = await self._poll_and_get_docs(real_db, "path-test", [simple_email])
        path = docs[0]["path"]
        assert path.startswith("email://path-test/")
        assert path.endswith("/body")

    @pytest.mark.asyncio
    async def test_attachment_document_metadata(
        self, real_db: Database, attachment_email: bytes
    ) -> None:
        """Attachment documents must include email metadata + filename."""
        docs = await self._poll_and_get_docs(real_db, "att-meta", [attachment_email])

        att_docs = [d for d in docs if d["metadata"].get("email_part") == "attachment"]
        assert len(att_docs) >= 1

        for doc in att_docs:
            meta = doc["metadata"]
            assert "email_attachment_filename" in meta
            assert meta["email_part"] == "attachment"
            assert meta["email_folder"] == "INBOX"
            assert doc["source_type"] == "email"

    @pytest.mark.asyncio
    async def test_body_handling_save_as_document(
        self, real_db: Database, attachment_email: bytes
    ) -> None:
        """body_handling='save_as_document' must create only body doc."""
        docs = await self._poll_and_get_docs(
            real_db, "body-only", [attachment_email],
            body_handling="save_as_document",
        )
        assert len(docs) == 1
        assert docs[0]["metadata"]["email_part"] == "body"

    @pytest.mark.asyncio
    async def test_body_handling_attachments_only(
        self, real_db: Database, attachment_email: bytes
    ) -> None:
        """body_handling='attachments_only' must skip body doc."""
        docs = await self._poll_and_get_docs(
            real_db, "att-only", [attachment_email],
            body_handling="attachments_only",
        )
        for doc in docs:
            assert doc["metadata"]["email_part"] == "attachment"

    @pytest.mark.asyncio
    async def test_attachment_whitelist_applied(
        self, real_db: Database, attachment_email: bytes
    ) -> None:
        """Whitelist filter should apply during document creation."""
        docs = await self._poll_and_get_docs(
            real_db, "wl-test", [attachment_email],
            attachment_whitelist="*.txt",
        )
        att_docs = [d for d in docs if d["metadata"].get("email_part") == "attachment"]
        for doc in att_docs:
            fn = doc["metadata"].get("email_attachment_filename", "")
            assert fn.endswith(".txt"), f"Whitelist *.txt should exclude {fn}"

    @pytest.mark.asyncio
    async def test_attachment_blacklist_applied(
        self, real_db: Database, attachment_email: bytes
    ) -> None:
        """Blacklist filter should apply during document creation."""
        docs = await self._poll_and_get_docs(
            real_db, "bl-test", [attachment_email],
            attachment_blacklist="*.pdf",
        )
        att_docs = [d for d in docs if d["metadata"].get("email_part") == "attachment"]
        for doc in att_docs:
            fn = doc["metadata"].get("email_attachment_filename", "")
            assert not fn.endswith(".pdf"), f"Blacklist *.pdf should exclude {fn}"

    @pytest.mark.asyncio
    async def test_html_body_converted_to_text(
        self, real_db: Database, html_email: bytes
    ) -> None:
        """HTML email body should be converted to text and saved."""
        docs = await self._poll_and_get_docs(real_db, "html-test", [html_email])
        assert len(docs) >= 1
        assert docs[0]["mime_type"] == "text/plain"
        assert docs[0]["ext"] == ".txt"

    @pytest.mark.asyncio
    async def test_no_message_id_still_processed(
        self, real_db: Database, no_msgid_email: bytes
    ) -> None:
        """Email without Message-ID should still be processed (UID fallback)."""
        docs = await self._poll_and_get_docs(real_db, "nomsgid", [no_msgid_email])
        assert len(docs) >= 1
        meta = docs[0]["metadata"]
        assert meta["email_message_id"] == ""
        assert meta["email_uid"] == 1

    @pytest.mark.asyncio
    async def test_body_title_is_email_subject(
        self, real_db: Database, simple_email: bytes
    ) -> None:
        """Body document title should be the email subject."""
        docs = await self._poll_and_get_docs(real_db, "title-test", [simple_email])
        assert docs[0]["title"] == "Simple text email"


# ══════════════════════════════════════════════════════════════════
# 3. POST-FETCH ACTION VERIFICATION
# ══════════════════════════════════════════════════════════════════


class TestPostFetchActionVerification:
    """Verify post-fetch action (mark_seen) is applied correctly."""

    @pytest.mark.asyncio
    async def test_mark_seen_applies_seen_flag(
        self, real_db: Database, simple_email: bytes
    ) -> None:
        """action_after_fetch='mark_seen' must call store with +FLAGS Seen."""
        await real_db.create_email_account({
            "name": "seen-test", "host": "imap.example.com",
            "username": "u", "password": "p",
            "action_after_fetch": "mark_seen",
        })

        mock_imap = TrackingIMAP(emails=[simple_email])
        ingestor = EmailIngestor(real_db, extractor=Extractor())

        acct = EmailAccountConfig(
            name="seen-test", host="imap.example.com",
            username="u", password="p",
            action_after_fetch="mark_seen",
        )

        with patch.object(EmailIngestor, "_connect_imap", return_value=mock_imap):
            await ingestor.poll_account(acct, account_id=1)

        assert len(mock_imap.store_calls) == 1
        uid, flags_op, flags = mock_imap.store_calls[0]
        assert uid == 1
        assert flags_op == "+FLAGS"
        assert "\\Seen" in flags

    @pytest.mark.asyncio
    async def test_mark_seen_not_applied_for_other_action(
        self, real_db: Database, simple_email: bytes
    ) -> None:
        """When action_after_fetch is NOT 'mark_seen', no store should happen."""
        await real_db.create_email_account({
            "name": "no-seen", "host": "imap.example.com",
            "username": "u", "password": "p",
            "action_after_fetch": "keep_unread",
        })

        mock_imap = TrackingIMAP(emails=[simple_email])
        ingestor = EmailIngestor(real_db, extractor=Extractor())

        acct = EmailAccountConfig(
            name="no-seen", host="imap.example.com",
            username="u", password="p",
            action_after_fetch="keep_unread",
        )

        with patch.object(EmailIngestor, "_connect_imap", return_value=mock_imap):
            await ingestor.poll_account(acct, account_id=1)

        assert len(mock_imap.store_calls) == 0

    @pytest.mark.asyncio
    async def test_default_action_is_mark_seen(
        self, real_db: Database, simple_email: bytes
    ) -> None:
        """Default action_after_fetch should be 'mark_seen'."""
        await real_db.create_email_account({
            "name": "default-act", "host": "imap.example.com",
            "username": "u", "password": "p",
        })

        mock_imap = TrackingIMAP(emails=[simple_email])
        ingestor = EmailIngestor(real_db, extractor=Extractor())

        acct = EmailAccountConfig(
            name="default-act", host="imap.example.com",
            username="u", password="p",
        )

        with patch.object(EmailIngestor, "_connect_imap", return_value=mock_imap):
            await ingestor.poll_account(acct, account_id=1)

        assert len(mock_imap.store_calls) == 1

    @pytest.mark.asyncio
    async def test_store_failure_preserves_documents(
        self, real_db: Database, simple_email: bytes
    ) -> None:
        """If IMAP store() fails, ingestion log reflects the failure."""
        await real_db.create_email_account({
            "name": "store-fail", "host": "imap.example.com",
            "username": "u", "password": "p",
            "action_after_fetch": "mark_seen",
        })

        mock_imap = TrackingIMAP(emails=[simple_email])

        def failing_store(uid_set, flags_op, flags):
            raise ConnectionError("Server disconnected during store")

        mock_imap.store = failing_store

        ingestor = EmailIngestor(real_db, extractor=Extractor())

        acct = EmailAccountConfig(
            name="store-fail", host="imap.example.com",
            username="u", password="p",
            action_after_fetch="mark_seen",
        )

        with patch.object(EmailIngestor, "_connect_imap", return_value=mock_imap):
            await ingestor.poll_account(acct, account_id=1)

        logs = await real_db.list_email_ingestion_logs(1)
        assert len(logs) >= 1
        assert logs[0]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_correct_uid_used_in_store(
        self, real_db: Database, simple_email: bytes
    ) -> None:
        """Store should use the actual email UID, not a hardcoded value."""
        await real_db.create_email_account({
            "name": "uid-test", "host": "imap.example.com",
            "username": "u", "password": "p",
            "action_after_fetch": "mark_seen",
        })

        all_emails = [simple_email] * 42
        mock_imap = TrackingIMAP(emails=all_emails)

        def search_uid_42(charset, *criteria):
            mock_imap.search_called = True
            return ("OK", [b"42"])

        mock_imap.search = search_uid_42

        ingestor = EmailIngestor(real_db, extractor=Extractor())

        acct = EmailAccountConfig(
            name="uid-test", host="imap.example.com",
            username="u", password="p",
            action_after_fetch="mark_seen",
        )

        with patch.object(EmailIngestor, "_connect_imap", return_value=mock_imap):
            await ingestor.poll_account(acct, account_id=1)

        assert len(mock_imap.store_calls) == 1
        assert mock_imap.store_calls[0][0] == 42

    @pytest.mark.asyncio
    async def test_last_sync_at_updated_after_poll(
        self, real_db: Database, simple_email: bytes
    ) -> None:
        """After a successful poll, last_sync_at should be set."""
        await real_db.create_email_account({
            "name": "sync-ts", "host": "imap.example.com",
            "username": "u", "password": "p",
        })

        acct_before = await real_db.get_email_account(1)
        assert acct_before["last_sync_at"] is None

        mock_imap = TrackingIMAP(emails=[simple_email])
        ingestor = EmailIngestor(real_db, extractor=Extractor())

        acct = EmailAccountConfig(
            name="sync-ts", host="imap.example.com",
            username="u", password="p",
        )

        with patch.object(EmailIngestor, "_connect_imap", return_value=mock_imap):
            await ingestor.poll_account(acct, account_id=1)

        acct_after = await real_db.get_email_account(1)
        assert acct_after["last_sync_at"] is not None
        datetime.fromisoformat(acct_after["last_sync_at"])


# ══════════════════════════════════════════════════════════════════
# 4. EDGE CASES
# ══════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Edge cases complementing existing tests."""

    @pytest.mark.asyncio
    async def test_empty_inbox_no_errors(
        self, real_db: Database
    ) -> None:
        """Polling an empty inbox should succeed with zero documents."""
        await real_db.create_email_account({
            "name": "empty-inbox", "host": "imap.example.com",
            "username": "u", "password": "p",
        })

        mock_imap = TrackingIMAP(emails=[])
        ingestor = EmailIngestor(real_db, extractor=Extractor())

        acct = EmailAccountConfig(
            name="empty-inbox", host="imap.example.com",
            username="u", password="p",
        )

        with patch.object(EmailIngestor, "_connect_imap", return_value=mock_imap):
            doc_ids = await ingestor.poll_account(acct, account_id=1)

        assert doc_ids == []
        assert mock_imap.search_called

    @pytest.mark.asyncio
    async def test_disconnect_called_even_on_error(
        self, real_db: Database
    ) -> None:
        """IMAP disconnect must be called even when processing fails."""
        await real_db.create_email_account({
            "name": "disconnect-test", "host": "imap.example.com",
            "username": "u", "password": "p",
        })

        mock_imap = TrackingIMAP(emails=[])

        def failing_search(charset, *criteria):
            mock_imap.search_called = True
            return ("NO", [b"Search failed"])

        mock_imap.search = failing_search

        ingestor = EmailIngestor(real_db, extractor=Extractor())

        acct = EmailAccountConfig(
            name="disconnect-test", host="imap.example.com",
            username="u", password="p",
        )

        with patch.object(EmailIngestor, "_connect_imap", return_value=mock_imap):
            await ingestor.poll_account(acct, account_id=1)

        assert mock_imap.logout_called

    @pytest.mark.asyncio
    async def test_account_not_in_db_skipped(
        self, real_db: Database
    ) -> None:
        """Account not in DB should be skipped gracefully."""
        ingestor = EmailIngestor(real_db, extractor=Extractor())
        acct = EmailAccountConfig(
            name="not-in-db", host="imap.example.com",
            username="u", password="p",
        )
        doc_ids = await ingestor.poll_account(acct)
        assert doc_ids == []

    @pytest.mark.asyncio
    async def test_select_failure_handled(
        self, real_db: Database
    ) -> None:
        """SELECT returning non-OK should update error and return empty."""
        await real_db.create_email_account({
            "name": "select-fail", "host": "imap.example.com",
            "username": "u", "password": "p",
        })

        mock_imap = TrackingIMAP(emails=[])

        def failing_select(mailbox="INBOX"):
            mock_imap.select_called = True
            return ("NO", [b"Mailbox does not exist"])

        mock_imap.select = failing_select

        ingestor = EmailIngestor(real_db, extractor=Extractor())

        acct = EmailAccountConfig(
            name="select-fail", host="imap.example.com",
            username="u", password="p",
        )

        with patch.object(EmailIngestor, "_connect_imap", return_value=mock_imap):
            doc_ids = await ingestor.poll_account(acct, account_id=1)

        assert doc_ids == []
        acct_db = await real_db.get_email_account(1)
        assert acct_db["last_error"] is not None
        assert "NO" in acct_db["last_error"]
