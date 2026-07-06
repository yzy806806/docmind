"""Email ingestion service — poll IMAP accounts, parse emails, create documents.

Uses stdlib ``imaplib`` + ``email`` for zero new pip dependencies.  Sync IMAP
calls are wrapped in ``asyncio.to_thread`` to avoid blocking the event loop.

The ingestor reuses:
  - ``Extractor.extract_from_bytes()`` for attachment text extraction
  - ``Database.save_document()`` for persistence
  - ``Database.check_email_duplicate()`` for composite deduplication
  - ``Database.log_email_ingestion()`` for the audit / dedup trail

Phase 8a scope (this module):
  - IMAP connect / search / fetch / disconnect
  - MIME parsing (text/plain, text/html, multipart)
  - Attachment extraction with whitelist/blacklist glob filtering
  - Composite 3-layer deduplication (Message-ID, UID, content hash)
  - Thread ID computation from In-Reply-To / References / Message-ID
  - Post-fetch action: ``mark_seen`` (MVP — move/delete deferred to Phase 8d)

Phase 8b-d (subsequent tasks):
  - REST API endpoints, UI pages, search integration
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import email.utils
import hashlib
import imaplib
import json
import logging
import re
import ssl
import fnmatch
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Optional

from .config import EmailAccountConfig
from .extractor import Extractor

logger = logging.getLogger(__name__)

# Maximum email body size to extract (10 MB).  Larger bodies are truncated.
MAX_BODY_SIZE = 10 * 1024 * 1024


def _hash16(text: str) -> str:
    """Return a 16-character SHA-256 prefix for use as a short identifier."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _normalize_text(text: str) -> str:
    """Normalize text for content-hash deduplication.

    Lowercase, collapse whitespace, strip headers that vary between
    mail servers (Received, DKIM-Signature, etc. are not part of body).
    """
    text = text.lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text


class EmailIngestor:
    """Poll IMAP accounts, parse emails, extract attachments, create documents.

    Reuses ``Extractor`` for attachment text extraction and ``Database`` for
    persistence.  Designed to be called per-account from the background worker
    or on-demand from the API sync endpoint.
    """

    def __init__(self, db, extractor: Optional[Extractor] = None):
        """Initialize the ingestor.

        Args:
            db: Database instance (must implement the email_* methods).
            extractor: Extractor instance or None (a new one is created).
        """
        self.db = db
        self.extractor = extractor or Extractor()

    # ── IMAP connection ──────────────────────────────────────────

    def _connect_imap(self, account: EmailAccountConfig) -> imaplib.IMAP4 | imaplib.IMAP4_SSL:
        """Open an IMAP connection (sync — call via ``asyncio.to_thread``).

        Raises on connection failure; the caller should catch and log.
        """
        if account.use_ssl:
            ctx = ssl.create_default_context()
            conn = imaplib.IMAP4_SSL(account.host, account.port, ssl_context=ctx)
        else:
            conn = imaplib.IMAP4(account.host, account.port)
        conn.login(account.username, account.password)
        return conn

    @staticmethod
    def _disconnect_imap(conn: imaplib.IMAP4) -> None:
        """Safely close and log out of an IMAP connection."""
        try:
            conn.close()
        except Exception:
            pass  # close() fails if no mailbox selected — ignore
        try:
            conn.logout()
        except Exception:
            pass

    # ── Public entry point ───────────────────────────────────────

    async def poll_account(
        self, account: EmailAccountConfig, account_id: Optional[int] = None
    ) -> list[int]:
        """Poll a single IMAP account. Returns list of created document IDs.

        If ``account_id`` is provided, it is used for dedup/log queries.
        Otherwise the account is looked up by name from the DB.
        """
        # Resolve account_id from DB if not given (env-var-loaded accounts
        # don't carry an ID).
        if account_id is None:
            acct_row = await self._lookup_account_id(account.name)
            if acct_row is None:
                logger.warning("Email account '%s' not found in DB — skipping", account.name)
                return []
            account_id = acct_row

        created_docs: list[int] = []
        folder = account.folder or "INBOX"

        try:
            conn = await asyncio.to_thread(self._connect_imap, account)
        except Exception as e:
            logger.error("IMAP connect failed for '%s': %s", account.name, e)
            await self.db.update_email_account_error(account_id, str(e))
            await self._log_ingestion(
                account_id, None, None, folder, None, None,
                None, "failed", error=f"Connection error: {e}",
            )
            return []

        try:
            status, _ = await asyncio.to_thread(conn.select, folder)
            if status != "OK":
                logger.error("IMAP SELECT '%s' failed for '%s': %s", folder, account.name, status)
                await self.db.update_email_account_error(
                    account_id, f"SELECT {folder} returned {status}"
                )
                return []

            # Search for UNSEEN messages
            status, data = await asyncio.to_thread(conn.search, None, "UNSEEN")
            if status != "OK":
                logger.error("IMAP SEARCH UNSEEN failed for '%s': %s", account.name, status)
                return []

            uid_list = data[0].split() if data and data[0] else []
            logger.info("Account '%s': %d unseen emails in %s", account.name, len(uid_list), folder)

            for uid_bytes in uid_list:
                uid = int(uid_bytes)
                try:
                    doc_ids = await self._process_email(conn, account, account_id, uid, folder)
                    created_docs.extend(doc_ids)
                except Exception as e:
                    logger.exception(
                        "Error processing email UID %d for account '%s': %s",
                        uid, account.name, e,
                    )
                    await self._log_ingestion(
                        account_id, None, uid, folder, None, None,
                        None, "failed", error=str(e),
                    )

        finally:
            await asyncio.to_thread(self._disconnect_imap, conn)

        # Update last_sync_at
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        await self.db.update_email_account_sync(account_id, now_iso)

        logger.info(
            "Account '%s' poll complete: %d documents created", account.name, len(created_docs)
        )
        return created_docs

    # ── Per-email processing ─────────────────────────────────────

    async def _process_email(
        self,
        conn: imaplib.IMAP4,
        account: EmailAccountConfig,
        account_id: int,
        uid: int,
        folder: str,
    ) -> list[int]:
        """Fetch and process a single email by UID."""
        # Fetch the raw email bytes
        status, fetch_data = await asyncio.to_thread(
            conn.fetch, str(uid).encode(), "(RFC822)"
        )
        if status != "OK" or not fetch_data or not fetch_data[0]:
            logger.warning("IMAP FETCH %d returned no data for '%s'", uid, account.name)
            return []

        raw_email = fetch_data[0][1] if isinstance(fetch_data[0], tuple) else b""
        if not raw_email:
            logger.warning("Empty email body for UID %d on '%s'", uid, account.name)
            return []

        # Parse the email
        msg = self.parse_email(raw_email)
        message_id = msg.get("Message-ID", "").strip()
        subject = msg.get("Subject", "")
        sender = msg.get("From", "")
        received_at = msg.get("Date", "")

        # Deduplication check
        dedup_key = self.compute_dedup_key(msg, account_id, uid)
        is_dup = await self._is_duplicate(msg, account_id, folder, uid, dedup_key)
        if is_dup:
            logger.debug("Skipping duplicate email UID %d on '%s'", uid, account.name)
            await self._log_ingestion(
                account_id, message_id or None, uid, folder,
                subject, sender, received_at, "skipped",
                dedup_key=dedup_key,
            )
            return []

        # Log as "processing"
        log_id = await self._log_ingestion(
            account_id, message_id or None, uid, folder,
            subject, sender, received_at, "processing",
            dedup_key=dedup_key,
        )

        try:
            doc_ids = await self._create_documents_from_email(
                msg, account, account_id, uid, folder
            )

            # Update log entry to completed
            await self.db.update_email_ingestion_log(
                log_id, {"status": "completed", "document_ids": doc_ids}
            )

            # Apply post-fetch action (MVP: mark_seen only)
            if account.action_after_fetch == "mark_seen":
                await asyncio.to_thread(
                    conn.store, str(uid).encode(), "+FLAGS", "\\Seen"
                )

            return doc_ids

        except Exception as e:
            logger.exception("Failed to process email UID %d: %s", uid, e)
            await self.db.update_email_ingestion_log(
                log_id, {"status": "failed", "error": str(e)}
            )
            return []

    # ── Email parsing ────────────────────────────────────────────

    def parse_email(self, raw_bytes: bytes) -> EmailMessage:
        """Parse raw IMAP fetch result into an email.message.EmailMessage."""
        return email.message_from_bytes(raw_bytes, policy=email.policy.default)

    def extract_body(self, msg: EmailMessage) -> str:
        """Extract plain text body from an email.

        Preference order:
          1. text/plain part
          2. text/html part (converted to plain text)
          3. Multipart alternative — recurse for text/plain or text/html

        Returns empty string if no text body is found.
        """
        if msg.is_multipart():
            # First pass: look for text/plain
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain":
                    body = part.get_payload(decode=True)
                    if body:
                        charset = part.get_content_charset() or "utf-8"
                        try:
                            text = body.decode(charset, errors="replace")
                        except (LookupError, UnicodeDecodeError):
                            text = body.decode("utf-8", errors="replace")
                        return text[:MAX_BODY_SIZE]

            # Second pass: look for text/html
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/html":
                    body = part.get_payload(decode=True)
                    if body:
                        charset = part.get_content_charset() or "utf-8"
                        try:
                            html = body.decode(charset, errors="replace")
                        except (LookupError, UnicodeDecodeError):
                            html = body.decode("utf-8", errors="replace")
                        return self._html_to_text(html)[:MAX_BODY_SIZE]
        else:
            # Single-part email
            ct = msg.get_content_type()
            body = msg.get_payload(decode=True)
            if body:
                charset = msg.get_content_charset() or "utf-8"
                try:
                    text = body.decode(charset, errors="replace")
                except (LookupError, UnicodeDecodeError):
                    text = body.decode("utf-8", errors="replace")
                if ct == "text/html":
                    return self._html_to_text(text)[:MAX_BODY_SIZE]
                return text[:MAX_BODY_SIZE]

        return ""

    @staticmethod
    def _html_to_text(html: str) -> str:
        """Convert HTML to plain text.

        Uses BeautifulSoup if available; falls back to a regex-based
        stripper that removes tags and decodes common entities.
        """
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            # Remove script and style elements
            for tag in soup(["script", "style"]):
                tag.decompose()
            return soup.get_text(separator="\n", strip=True)
        except ImportError:
            # Fallback: regex-based tag stripping
            text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<[^>]+>", "", text)
            # Decode common HTML entities
            import html as html_module
            text = html_module.unescape(text)
            # Collapse whitespace
            text = re.sub(r"\n\s*\n", "\n\n", text).strip()
            return text

    # ── Attachment extraction ────────────────────────────────────

    def extract_attachments(self, msg: EmailMessage) -> list[dict[str, Any]]:
        """Extract attachment parts from a multipart email.

        Returns a list of dicts with keys: filename, content (bytes),
        mime_type, ext.

        Inline images (Content-Disposition: inline) are skipped — they are
        typically embedded in the HTML body, not standalone documents.
        """
        attachments: list[dict[str, Any]] = []

        if not msg.is_multipart():
            return attachments

        for part in msg.walk():
            # Skip the message root and multipart containers
            if part.is_multipart():
                continue

            cd = part.get("Content-Disposition", "")
            if not cd:
                # No Content-Disposition — could be inline body part, skip
                continue

            # Skip inline images — they belong to the HTML body
            if "inline" in cd.lower():
                continue

            # Only process attachments
            if "attachment" not in cd.lower():
                continue

            filename = part.get_filename()
            if not filename:
                continue

            # Decode RFC 2231 encoded filenames
            filename = str(email.utils.collapse_rfc2231_value(filename) or filename)

            content = part.get_payload(decode=True)
            if not content:
                continue

            mime_type = part.get_content_type()
            ext = Path(filename).suffix.lower()

            attachments.append({
                "filename": filename,
                "content": content,
                "mime_type": mime_type,
                "ext": ext,
            })

        return attachments

    def filter_attachments(
        self,
        attachments: list[dict[str, Any]],
        whitelist: str = "",
        blacklist: str = "",
    ) -> list[dict[str, Any]]:
        """Filter attachments by whitelist/blacklist glob patterns.

        Args:
            attachments: List of attachment dicts from extract_attachments().
            whitelist: Comma-separated glob patterns (e.g. "*.pdf,*.docx").
                       If non-empty, only matching attachments are kept.
            blacklist: Comma-separated glob patterns. Matching attachments
                       are removed.

        Returns: Filtered list of attachment dicts.
        """
        if not attachments:
            return []

        whitelist_patterns = [p.strip() for p in whitelist.split(",") if p.strip()]
        blacklist_patterns = [p.strip() for p in blacklist.split(",") if p.strip()]

        result = []
        for att in attachments:
            filename = att["filename"]

            # Blacklist takes precedence
            if any(fnmatch.fnmatch(filename, pat) for pat in blacklist_patterns):
                logger.debug("Attachment '%s' rejected by blacklist", filename)
                continue

            # If whitelist is specified, only allow matching files
            if whitelist_patterns:
                if not any(fnmatch.fnmatch(filename, pat) for pat in whitelist_patterns):
                    logger.debug("Attachment '%s' not in whitelist", filename)
                    continue

            result.append(att)

        return result

    # ── Thread ID computation ────────────────────────────────────

    def compute_thread_id(self, msg: EmailMessage) -> str:
        """Compute a thread identifier from email headers.

        Priority:
          1. First entry in References header → thread root
          2. In-Reply-To header
          3. Own Message-ID
          4. Fallback: hash of Subject + From

        Returns a 16-character hex string.
        """
        references = msg.get("References", "")
        in_reply_to = msg.get("In-Reply-To", "")
        message_id = msg.get("Message-ID", "")

        # Use first References entry as thread root
        if references:
            first_ref = references.split()[0].strip()
            if first_ref:
                return _hash16(first_ref)

        # Fallback to In-Reply-To
        if in_reply_to:
            return _hash16(in_reply_to.strip())

        # New thread: use own Message-ID
        if message_id:
            return _hash16(message_id.strip())

        # Ultimate fallback: hash of subject + sender
        subject = msg.get("Subject", "")
        sender = msg.get("From", "")
        return _hash16(f"{subject}:{sender}")

    # ── Deduplication ────────────────────────────────────────────

    def compute_dedup_key(
        self, msg: EmailMessage, account_id: int, uid: int
    ) -> str:
        """Compute the composite deduplication key.

        Returns a string in the format ``<type>:<value>``:
          - ``msgid:<hash>`` for Message-ID based dedup
          - ``content:<hash>`` for content-hash based dedup (fallback)

        The UID-based dedup is handled separately via the
        ``check_email_duplicate()`` query (account_id + folder + uid).
        """
        message_id = msg.get("Message-ID", "").strip()
        if message_id:
            return f"msgid:{_hash16(message_id)}"

        # Fallback: content hash of normalized subject + sender + body
        subject = msg.get("Subject", "")
        sender = msg.get("From", "")
        body = self.extract_body(msg)
        content = _normalize_text(f"{subject}:{sender}:{body}")
        return f"content:{_hash16(content)}"

    async def _is_duplicate(
        self,
        msg: EmailMessage,
        account_id: int,
        folder: str,
        uid: int,
        dedup_key: str,
    ) -> bool:
        """Check the ingestion log for an existing entry (duplicate)."""
        message_id = msg.get("Message-ID", "").strip() or None
        return await self.db.check_email_duplicate(
            account_id, message_id, folder, uid, dedup_key
        )

    # ── Document creation ────────────────────────────────────────

    async def _create_documents_from_email(
        self,
        msg: EmailMessage,
        account: EmailAccountConfig,
        account_id: int,
        uid: int,
        folder: str,
    ) -> list[int]:
        """Create Document records for body and/or attachments.

        Behaviour depends on ``account.body_handling``:
          - ``save_with_attachments``: body + each attachment → separate docs
          - ``save_as_document``: body only as one doc (attachments ignored)
          - ``attachments_only``: only attachments, no body doc

        Returns list of created document IDs.
        """
        message_id = msg.get("Message-ID", "").strip()
        subject = msg.get("Subject", "(no subject)")
        sender = msg.get("From", "")
        received_at = msg.get("Date", "")
        thread_id = self.compute_thread_id(msg)

        doc_ids: list[int] = []

        # Create body document (unless attachments_only)
        if account.body_handling in ("save_with_attachments", "save_as_document"):
            body = self.extract_body(msg)
            if body and body.strip():
                body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
                doc_path = f"email://{account.name}/{message_id or uid}/body"
                doc_id = await self.db.save_document(
                    path=doc_path,
                    source_type="email",
                    source_name=account.name,
                    title=subject,
                    ext=".txt",
                    mime_type="text/plain",
                    body=body,
                    file_hash=body_hash,
                    size=len(body.encode("utf-8")),
                    metadata={
                        "email_message_id": message_id,
                        "email_subject": subject,
                        "email_sender": sender,
                        "email_received_at": received_at,
                        "email_thread_id": thread_id,
                        "email_uid": uid,
                        "email_folder": folder,
                        "email_part": "body",
                    },
                )
                if doc_id:
                    doc_ids.append(doc_id)

        # Create attachment documents (unless save_as_document — body only)
        if account.body_handling in ("save_with_attachments", "attachments_only"):
            raw_attachments = self.extract_attachments(msg)
            filtered = self.filter_attachments(
                raw_attachments,
                whitelist=account.attachment_whitelist,
                blacklist=account.attachment_blacklist,
            )

            for att in filtered:
                filename = att["filename"]
                content = att["content"]
                ext = att["ext"]
                mime_type = att["mime_type"]

                # Skip unsupported attachment types
                if ext not in Extractor.SUPPORTED:
                    logger.debug("Skipping unsupported attachment: %s", filename)
                    continue

                # Extract text from attachment
                extracted = self.extractor.extract_from_bytes(content, ext)
                if not extracted:
                    logger.debug("No text extracted from attachment: %s", filename)
                    # Still save the document with empty body for metadata
                    extracted = ""

                file_hash = hashlib.sha256(content).hexdigest()
                doc_path = (
                    f"email://{account.name}/{message_id or uid}/{filename}"
                )
                doc_id = await self.db.save_document(
                    path=doc_path,
                    source_type="email",
                    source_name=account.name,
                    title=filename,
                    ext=ext,
                    mime_type=mime_type,
                    body=extracted,
                    file_hash=file_hash,
                    size=len(content),
                    metadata={
                        "email_message_id": message_id,
                        "email_subject": subject,
                        "email_sender": sender,
                        "email_received_at": received_at,
                        "email_thread_id": thread_id,
                        "email_uid": uid,
                        "email_folder": folder,
                        "email_part": "attachment",
                        "email_attachment_filename": filename,
                    },
                )
                if doc_id:
                    doc_ids.append(doc_id)

        return doc_ids

    # ── Logging helper ───────────────────────────────────────────

    async def _log_ingestion(
        self,
        account_id: int,
        message_id: Optional[str],
        uid: Optional[int],
        folder: str,
        subject: Optional[str],
        sender: Optional[str],
        received_at: Optional[str],
        status: str,
        doc_ids: Optional[list[int]] = None,
        error: Optional[str] = None,
        dedup_key: Optional[str] = None,
    ) -> int:
        """Write an ingestion result to email_ingestion_log. Returns log row ID."""
        return await self.db.log_email_ingestion({
            "account_id": account_id,
            "message_id": message_id,
            "uid": uid,
            "folder": folder,
            "subject": subject,
            "sender": sender,
            "received_at": received_at,
            "status": status,
            "error": error,
            "document_ids": doc_ids or [],
            "dedup_key": dedup_key,
        })

    # ── Account lookup ───────────────────────────────────────────

    async def _lookup_account_id(self, name: str) -> Optional[int]:
        """Look up an email account ID by name from the DB."""
        accounts = await self.db.list_email_accounts(enabled_only=False)
        for acct in accounts:
            if acct.get("name") == name:
                return acct.get("id")
        return None

    # ── Connection test ──────────────────────────────────────────

    async def test_connection(self, account: EmailAccountConfig) -> tuple[bool, str]:
        """Test IMAP connection without polling. Returns (success, message).

        Useful for the API ``/test`` endpoint to verify credentials.
        """
        try:
            conn = await asyncio.to_thread(self._connect_imap, account)
            # Try selecting INBOX to verify access
            status, data = await asyncio.to_thread(conn.select, account.folder or "INBOX")
            await asyncio.to_thread(self._disconnect_imap, conn)
            if status == "OK":
                msg = "Connection successful"
                if data and data[0]:
                    count = data[0].decode() if isinstance(data[0], bytes) else str(data[0])
                    msg = f"Connection successful — {count} messages in {account.folder or 'INBOX'}"
                return True, msg
            return False, f"SELECT returned {status}"
        except Exception as e:
            return False, str(e)


# ── Background worker ────────────────────────────────────────────


async def email_polling_worker(
    db,
    extractor: Optional[Extractor] = None,
    poll_interval: float = 600.0,
) -> None:
    """Long-lived background worker that polls all enabled email accounts.

    Intended to be started as an ``asyncio.create_task`` in the ``lifespan``
    context manager.  Runs forever until cancelled.

    Args:
        db: Database instance.
        extractor: Extractor instance (or None to create one).
        poll_interval: Seconds between poll cycles (default 600 = 10 min).
    """
    ingestor = EmailIngestor(db, extractor)
    logger.info("Email polling worker started (interval=%ss)", poll_interval)

    while True:
        try:
            accounts = await db.list_email_accounts(enabled_only=True)
            if not accounts:
                logger.debug("No enabled email accounts — skipping poll cycle")
            else:
                for acct_dict in accounts:
                    # Convert DB dict to EmailAccountConfig
                    acct = EmailAccountConfig(
                        name=acct_dict.get("name", ""),
                        host=acct_dict.get("host", ""),
                        port=acct_dict.get("port", 993),
                        use_ssl=acct_dict.get("use_ssl", True),
                        username=acct_dict.get("username", ""),
                        password=acct_dict.get("password", ""),
                        folder=acct_dict.get("folder", "INBOX"),
                        action_after_fetch=acct_dict.get("action_after_fetch", "mark_seen"),
                        move_to_folder=acct_dict.get("move_to_folder"),
                        body_handling=acct_dict.get("body_handling", "save_with_attachments"),
                        attachment_whitelist=acct_dict.get("attachment_whitelist", ""),
                        attachment_blacklist=acct_dict.get("attachment_blacklist", ""),
                        deduplication_strategy=acct_dict.get("deduplication_strategy", "message_id"),
                        enabled=acct_dict.get("enabled", True),
                    )
                    account_id = acct_dict.get("id")
                    try:
                        await ingestor.poll_account(acct, account_id=account_id)
                    except Exception as e:
                        logger.error(
                            "Email poll failed for '%s': %s", acct.name, e
                        )
                        if account_id is not None:
                            await db.update_email_account_error(account_id, str(e))
        except Exception as e:
            logger.exception("Email polling worker cycle failed: %s", e)

        await asyncio.sleep(poll_interval)
