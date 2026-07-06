"""Edge-case and regression tests for email ingestion pipeline."""
from __future__ import annotations
import asyncio, os, tempfile
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from src.core.config import EmailAccountConfig
from src.core.email_ingestor import EmailIngestor, _hash16, _normalize_text, email_polling_worker
from src.core.extractor import Extractor

@pytest.fixture
def mock_db():
    db = MagicMock()
    db.check_email_duplicate = AsyncMock(return_value=False)
    db.log_email_ingestion = AsyncMock(return_value=1)
    db.update_email_ingestion_log = AsyncMock(return_value=None)
    db.save_document = AsyncMock(return_value=42)
    db.update_email_account_sync = AsyncMock(return_value=None)
    db.update_email_account_error = AsyncMock(return_value=None)
    db.list_email_accounts = AsyncMock(return_value=[])
    db.get_email_account = AsyncMock(return_value={"id": 1, "name": "test"})
    return db

@pytest.fixture
def ingestor(mock_db):
    return EmailIngestor(mock_db, extractor=Extractor())

@pytest.fixture
def tmp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path): os.unlink(path)

@pytest.fixture
async def real_db(tmp_db_path):
    from src.core.db_sqlite import Database
    db = Database(db_path=tmp_db_path)
    await db.connect()
    yield db
    await db.disconnect()

def _make_plain_email(subject="Test", body="Hello world", message_id="<test@example.com>", sender="sender@example.com"):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject; msg["From"] = sender; msg["To"] = "recipient@example.com"
    msg["Message-ID"] = message_id; msg["Date"] = "Mon, 06 Jul 2026 12:00:00 +0000"
    return msg.as_bytes()

def _make_multipart_mixed_email(subject="With Attachments", body="Body text", message_id="<multi@example.com>", attachments=None):
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject; msg["From"] = "sender@example.com"; msg["To"] = "recipient@example.com"
    msg["Message-ID"] = message_id; msg["Date"] = "Mon, 06 Jul 2026 12:00:00 +0000"
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if attachments:
        for filename, content, mime_type, disposition in attachments:
            maintype, subtype = mime_type.split("/", 1)
            att = MIMEText(content.decode("utf-8", errors="replace"), subtype) if maintype == "text" else MIMEApplication(content, subtype)
            att.add_header("Content-Disposition", disposition, filename=filename)
            msg.attach(att)
    return msg.as_bytes()

# ── IMAP polling edge cases ──
class TestIMAPPollingEdgeCases:
    @pytest.mark.asyncio
    async def test_select_failure_returns_empty(self, ingestor, mock_db):
        mock_db.list_email_accounts = AsyncMock(return_value=[{"id":1,"name":"test","host":"h","username":"u","password":"p","port":993,"use_ssl":True,"folder":"INBOX","action_after_fetch":"mark_seen","body_handling":"save_with_attachments","enabled":True}])
        account = EmailAccountConfig(name="test",host="h",username="u",password="p")
        mock_conn = MagicMock()
        mock_conn.select = MagicMock(return_value=("NO",[b"Mailbox does not exist"]))
        mock_conn.close = MagicMock(); mock_conn.logout = MagicMock()
        with patch.object(EmailIngestor,"_connect_imap",return_value=mock_conn):
            doc_ids = await ingestor.poll_account(account, account_id=1)
        assert doc_ids == []
        mock_db.update_email_account_error.assert_called()

    @pytest.mark.asyncio
    async def test_search_failure_returns_empty(self, ingestor, mock_db):
        mock_db.list_email_accounts = AsyncMock(return_value=[{"id":1,"name":"test","host":"h","username":"u","password":"p","port":993,"use_ssl":True,"folder":"INBOX","action_after_fetch":"mark_seen","body_handling":"save_with_attachments","enabled":True}])
        account = EmailAccountConfig(name="test",host="h",username="u",password="p")
        mock_conn = MagicMock()
        mock_conn.select = MagicMock(return_value=("OK",[b"5"]))
        mock_conn.search = MagicMock(return_value=("NO",[b"SEARCH failed"]))
        mock_conn.close = MagicMock(); mock_conn.logout = MagicMock()
        with patch.object(EmailIngestor,"_connect_imap",return_value=mock_conn):
            doc_ids = await ingestor.poll_account(account, account_id=1)
        assert doc_ids == []

    @pytest.mark.asyncio
    async def test_empty_uid_list_no_documents(self, ingestor, mock_db):
        mock_db.list_email_accounts = AsyncMock(return_value=[{"id":1,"name":"test","host":"h","username":"u","password":"p","port":993,"use_ssl":True,"folder":"INBOX","action_after_fetch":"mark_seen","body_handling":"save_with_attachments","enabled":True}])
        account = EmailAccountConfig(name="test",host="h",username="u",password="p")
        mock_conn = MagicMock()
        mock_conn.select = MagicMock(return_value=("OK",[b"0"]))
        mock_conn.search = MagicMock(return_value=("OK",[b""]))
        mock_conn.close = MagicMock(); mock_conn.logout = MagicMock()
        with patch.object(EmailIngestor,"_connect_imap",return_value=mock_conn):
            doc_ids = await ingestor.poll_account(account, account_id=1)
        assert doc_ids == []

    @pytest.mark.asyncio
    async def test_fetch_returns_no_data(self, ingestor, mock_db):
        mock_db.list_email_accounts = AsyncMock(return_value=[{"id":1,"name":"test","host":"h","username":"u","password":"p","port":993,"use_ssl":True,"folder":"INBOX","action_after_fetch":"mark_seen","body_handling":"save_with_attachments","enabled":True}])
        account = EmailAccountConfig(name="test",host="h",username="u",password="p")
        mock_conn = MagicMock()
        mock_conn.select = MagicMock(return_value=("OK",[b"1"]))
        mock_conn.search = MagicMock(return_value=("OK",[b"1"]))
        mock_conn.fetch = MagicMock(return_value=("NO",[b""]))
        mock_conn.close = MagicMock(); mock_conn.logout = MagicMock()
        with patch.object(EmailIngestor,"_connect_imap",return_value=mock_conn):
            doc_ids = await ingestor.poll_account(account, account_id=1)
        assert doc_ids == []

    @pytest.mark.asyncio
    async def test_fetch_returns_empty_data(self, ingestor, mock_db):
        mock_db.list_email_accounts = AsyncMock(return_value=[{"id":1,"name":"test","host":"h","username":"u","password":"p","port":993,"use_ssl":True,"folder":"INBOX","action_after_fetch":"mark_seen","body_handling":"save_with_attachments","enabled":True}])
        account = EmailAccountConfig(name="test",host="h",username="u",password="p")
        mock_conn = MagicMock()
        mock_conn.select = MagicMock(return_value=("OK",[b"1"]))
        mock_conn.search = MagicMock(return_value=("OK",[b"1"]))
        mock_conn.fetch = MagicMock(return_value=("OK",[None]))
        mock_conn.close = MagicMock(); mock_conn.logout = MagicMock()
        with patch.object(EmailIngestor,"_connect_imap",return_value=mock_conn):
            doc_ids = await ingestor.poll_account(account, account_id=1)
        assert doc_ids == []

    @pytest.mark.asyncio
    async def test_process_email_error_logs_failure(self, ingestor, mock_db):
        mock_db.list_email_accounts = AsyncMock(return_value=[{"id":1,"name":"test","host":"h","username":"u","password":"p","port":993,"use_ssl":True,"folder":"INBOX","action_after_fetch":"mark_seen","body_handling":"save_with_attachments","enabled":True}])
        account = EmailAccountConfig(name="test",host="h",username="u",password="p")
        raw_email = _make_plain_email()
        mock_conn = MagicMock()
        mock_conn.select = MagicMock(return_value=("OK",[b"1"]))
        mock_conn.search = MagicMock(return_value=("OK",[b"1"]))
        mock_conn.fetch = MagicMock(return_value=("OK",[(b"1 (RFC822)",raw_email)]))
        mock_conn.close = MagicMock(); mock_conn.logout = MagicMock()
        with patch.object(ingestor,"_create_documents_from_email",side_effect=ValueError("Boom")):
            with patch.object(EmailIngestor,"_connect_imap",return_value=mock_conn):
                doc_ids = await ingestor.poll_account(account, account_id=1)
        assert doc_ids == []
        assert mock_db.log_email_ingestion.call_count >= 1
        assert mock_db.update_email_ingestion_log.call_count >= 1

# ── Multipart handling edge cases ──
class TestMultipartEdgeCases:
    def test_nested_multipart_alternative_inside_mixed(self, ingestor):
        msg = MIMEMultipart("mixed")
        msg["Subject"] = "Nested"; msg["From"] = "sender@example.com"; msg["To"] = "recipient@example.com"; msg["Message-ID"] = "<nested@example.com>"
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText("Plain text body","plain","utf-8")); alt.attach(MIMEText("<p>HTML body</p>","html","utf-8"))
        msg.attach(alt)
        att = MIMEApplication(b"file content"); att.add_header("Content-Disposition","attachment",filename="doc.txt"); msg.attach(att)
        parsed = ingestor.parse_email(msg.as_bytes())
        assert "Plain text body" in ingestor.extract_body(parsed)
        attachments = ingestor.extract_attachments(parsed)
        assert len(attachments) == 1 and attachments[0]["filename"] == "doc.txt"

    def test_inline_disposition_skipped(self, ingestor):
        raw = _make_multipart_mixed_email(attachments=[("inline.png",b"\x89PNG","image/png","inline"),("real.pdf",b"%PDF-1.4","application/pdf","attachment")])
        msg = ingestor.parse_email(raw)
        filenames = [a["filename"] for a in ingestor.extract_attachments(msg)]
        assert "inline.png" not in filenames and "real.pdf" in filenames

    def test_rfc2231_encoded_filename(self, ingestor):
        msg = MIMEMultipart("mixed")
        msg["Subject"]="Encoded";msg["From"]="sender@example.com";msg["To"]="recipient@example.com";msg["Message-ID"]="<rfc2231@example.com>"
        msg.attach(MIMEText("body","plain","utf-8"))
        att = MIMEApplication(b"content"); att["Content-Disposition"] = "attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf"; msg.attach(att)
        attachments = ingestor.extract_attachments(ingestor.parse_email(msg.as_bytes()))
        assert len(attachments)==1 and "r\u00e9sum\u00e9" in attachments[0]["filename"]

    def test_no_content_disposition_skipped(self, ingestor):
        msg = MIMEMultipart("mixed")
        msg["Subject"]="No CD";msg["From"]="sender@example.com";msg["To"]="recipient@example.com";msg["Message-ID"]="<nocd@example.com>"
        msg.attach(MIMEText("body","plain","utf-8")); msg.attach(MIMEApplication(b"content"))
        assert len(ingestor.extract_attachments(ingestor.parse_email(msg.as_bytes()))) == 0

    def test_attachment_with_empty_content(self, ingestor):
        raw = _make_multipart_mixed_email(attachments=[("empty.txt",b"","text/plain","attachment"),("real.txt",b"real content","text/plain","attachment")])
        filenames = [a["filename"] for a in ingestor.extract_attachments(ingestor.parse_email(raw))]
        assert "empty.txt" not in filenames and "real.txt" in filenames

    def test_no_filename_attachment_skipped(self, ingestor):
        msg = MIMEMultipart("mixed")
        msg["Subject"]="No Filename";msg["From"]="sender@example.com";msg["To"]="recipient@example.com";msg["Message-ID"]="<nofilename@example.com>"
        msg.attach(MIMEText("body","plain","utf-8"))
        att = MIMEApplication(b"content"); att.add_header("Content-Disposition","attachment"); msg.attach(att)
        assert len(ingestor.extract_attachments(ingestor.parse_email(msg.as_bytes()))) == 0

# ── Deduplication edge cases ──
class TestDedupEdgeCases:
    @pytest.mark.asyncio
    async def test_uid_based_dedup_without_message_id(self, real_db):
        acct = await real_db.create_email_account({"name":"dedup-test","host":"imap.example.com","username":"user","password":"pass"})
        await real_db.log_email_ingestion({"account_id":acct["id"],"message_id":None,"uid":100,"folder":"INBOX","status":"completed","dedup_key":"content:abc123"})
        assert await real_db.check_email_duplicate(account_id=acct["id"],message_id=None,folder="INBOX",uid=100,dedup_key="content:abc123") is True

    @pytest.mark.asyncio
    async def test_same_message_id_different_folder_is_duplicate(self, real_db):
        acct = await real_db.create_email_account({"name":"folder-test","host":"imap.example.com","username":"user","password":"pass"})
        await real_db.log_email_ingestion({"account_id":acct["id"],"message_id":"<msg@example.com>","uid":100,"folder":"INBOX","status":"completed","dedup_key":"msgid:abc"})
        assert await real_db.check_email_duplicate(account_id=acct["id"],message_id="<msg@example.com>",folder="Archive",uid=100,dedup_key="msgid:abc") is True

    @pytest.mark.asyncio
    async def test_uid_only_dedup_respects_folder(self, real_db):
        acct = await real_db.create_email_account({"name":"folder-test2","host":"imap.example.com","username":"user","password":"pass"})
        await real_db.log_email_ingestion({"account_id":acct["id"],"message_id":None,"uid":100,"folder":"INBOX","status":"completed","dedup_key":"content:hash123"})
        assert await real_db.check_email_duplicate(account_id=acct["id"],message_id=None,folder="Archive",uid=100,dedup_key="content:different") is False

    @pytest.mark.asyncio
    async def test_dedup_key_different_accounts(self, real_db):
        a1 = await real_db.create_email_account({"name":"acct1","host":"imap1.com","username":"u1","password":"p1"})
        a2 = await real_db.create_email_account({"name":"acct2","host":"imap2.com","username":"u2","password":"p2"})
        await real_db.log_email_ingestion({"account_id":a1["id"],"message_id":"<shared@example.com>","uid":1,"folder":"INBOX","status":"completed","dedup_key":"msgid:shared_hash"})
        assert await real_db.check_email_duplicate(account_id=a2["id"],message_id="<shared@example.com>",folder="INBOX",uid=1,dedup_key="msgid:shared_hash") is False

    def test_content_hash_stable_for_identical_body(self, ingestor):
        m1=ingestor.parse_email(_make_plain_email(body="Identical body content",message_id="")); m2=ingestor.parse_email(_make_plain_email(body="Identical body content",message_id=""))
        assert ingestor.compute_dedup_key(m1,1,1) == ingestor.compute_dedup_key(m2,1,2)

    def test_content_hash_different_for_different_body(self, ingestor):
        m1=ingestor.parse_email(_make_plain_email(body="Body A",message_id="")); m2=ingestor.parse_email(_make_plain_email(body="Body B",message_id=""))
        assert ingestor.compute_dedup_key(m1,1,1) != ingestor.compute_dedup_key(m2,1,2)

# ── Body extraction edge cases ──
class TestBodyExtractionEdgeCases:
    def test_body_truncated_at_max_size(self, ingestor):
        from src.core.email_ingestor import MAX_BODY_SIZE
        body = ingestor.extract_body(ingestor.parse_email(_make_plain_email(body="x"*(MAX_BODY_SIZE+5000))))
        assert len(body) <= MAX_BODY_SIZE

    def test_unknown_charset_fallback_to_utf8(self, ingestor):
        msg = MIMEText("Hello","plain","utf-8")
        msg.replace_header("Content-Type",'text/plain; charset="x-unknown-charset-999"')
        msg["Subject"]="Unknown Charset";msg["From"]="sender@example.com";msg["To"]="recipient@example.com";msg["Message-ID"]="<charset@example.com>"
        assert "Hello" in ingestor.extract_body(ingestor.parse_email(msg.as_bytes()))

    def test_html_body_fallback_when_no_plain_text(self, ingestor):
        msg = MIMEMultipart("alternative")
        msg["Subject"]="HTML Only";msg["From"]="sender@example.com";msg["To"]="recipient@example.com";msg["Message-ID"]="<htmlonly@example.com>"
        msg.attach(MIMEText("<p>HTML <b>only</b> content</p>","html","utf-8"))
        body = ingestor.extract_body(ingestor.parse_email(msg.as_bytes()))
        assert "HTML" in body and "only" in body and "content" in body

    def test_body_with_only_whitespace(self, ingestor):
        body = ingestor.extract_body(ingestor.parse_email(_make_plain_email(body="   \n  \t  \n   ")))
        assert body.strip() == "" or body == "   \n  \t  \n   "

    def test_base64_encoded_body_decoded(self, ingestor):
        msg = MIMEText("Decoded base64 content","plain","utf-8")
        msg["Subject"]="Base64";msg["From"]="sender@example.com";msg["To"]="recipient@example.com";msg["Message-ID"]="<base64@example.com>"
        msg["Content-Transfer-Encoding"]="base64"
        assert "Decoded base64 content" in ingestor.extract_body(ingestor.parse_email(msg.as_bytes()))

# ── Thread ID edge cases ──
class TestThreadIDEdgeCases:
    def test_references_with_extra_whitespace(self, ingestor):
        msg = MIMEText("Body","plain","utf-8")
        msg["Subject"]="Whitespace";msg["From"]="sender@example.com";msg["To"]="recipient@example.com";msg["Message-ID"]="<refs@example.com>"
        msg["References"]="  <root@example.com>   <mid@example.com>  ";msg["In-Reply-To"]="<mid@example.com>"
        assert ingestor.compute_thread_id(ingestor.parse_email(msg.as_bytes())) == _hash16("<root@example.com>")

    def test_single_reference_entry(self, ingestor):
        msg = MIMEText("Body","plain","utf-8")
        msg["Subject"]="Single Ref";msg["From"]="sender@example.com";msg["To"]="recipient@example.com";msg["Message-ID"]="<single@example.com>"
        msg["References"]="<only-ref@example.com>";msg["In-Reply-To"]="<only-ref@example.com>"
        assert ingestor.compute_thread_id(ingestor.parse_email(msg.as_bytes())) == _hash16("<only-ref@example.com>")

    def test_empty_references_uses_in_reply_to(self, ingestor):
        msg = MIMEText("Body","plain","utf-8")
        msg["Subject"]="Re: Test";msg["From"]="sender@example.com";msg["To"]="recipient@example.com";msg["Message-ID"]="<reply@example.com>"
        msg["References"]="";msg["In-Reply-To"]="<parent@example.com>"
        assert ingestor.compute_thread_id(ingestor.parse_email(msg.as_bytes())) == _hash16("<parent@example.com>")

    def test_fallback_thread_id_is_deterministic(self, ingestor):
        m1=MIMEText("Body","plain","utf-8");m1["Subject"]="Same Subject";m1["From"]="same@example.com"
        m2=MIMEText("Different Body","plain","utf-8");m2["Subject"]="Same Subject";m2["From"]="same@example.com"
        t1=ingestor.compute_thread_id(ingestor.parse_email(m1.as_bytes()));t2=ingestor.compute_thread_id(ingestor.parse_email(m2.as_bytes()))
        assert t1==t2 and len(t1)==16

# ── Body handling modes ──
class TestBodyHandlingModes:
    @pytest.mark.asyncio
    async def test_attachments_only_no_attachments(self, ingestor, mock_db):
        msg = ingestor.parse_email(_make_plain_email(body="Body with no attachments"))
        account = EmailAccountConfig(name="test",body_handling="attachments_only")
        assert len(await ingestor._create_documents_from_email(msg,account,1,100,"INBOX")) == 0

    @pytest.mark.asyncio
    async def test_save_as_document_ignores_attachments(self, ingestor, mock_db):
        raw = _make_multipart_mixed_email(body="Body text",attachments=[("file.txt",b"attachment content","text/plain","attachment")])
        msg = ingestor.parse_email(raw); account = EmailAccountConfig(name="test",body_handling="save_as_document")
        mock_db.save_document.reset_mock()
        doc_ids = await ingestor._create_documents_from_email(msg,account,1,100,"INBOX")
        assert len(doc_ids)==1 and mock_db.save_document.call_count==1

    @pytest.mark.asyncio
    async def test_empty_body_save_with_attachments(self, ingestor, mock_db):
        msg = MIMEMultipart("mixed")
        msg["Subject"]="Empty Body";msg["From"]="sender@example.com";msg["To"]="recipient@example.com";msg["Message-ID"]="<empty@example.com>"
        msg.attach(MIMEText("","plain","utf-8"))
        att = MIMEApplication(b"content"); att.add_header("Content-Disposition","attachment",filename="file.txt"); msg.attach(att)
        account = EmailAccountConfig(name="test",body_handling="save_with_attachments")
        mock_db.save_document.reset_mock()
        assert len(await ingestor._create_documents_from_email(ingestor.parse_email(msg.as_bytes()),account,1,100,"INBOX")) == 1

    @pytest.mark.asyncio
    async def test_save_with_attachments_body_and_attachment(self, ingestor, mock_db):
        raw = _make_multipart_mixed_email(body="Hello body",attachments=[("notes.txt",b"Note content","text/plain","attachment")])
        msg = ingestor.parse_email(raw); account = EmailAccountConfig(name="test",body_handling="save_with_attachments")
        mock_db.save_document.reset_mock()
        doc_ids = await ingestor._create_documents_from_email(msg,account,1,100,"INBOX")
        assert len(doc_ids)==2 and mock_db.save_document.call_count==2

# ── Background worker edge cases ──
class TestWorkerEdgeCases:
    @pytest.mark.asyncio
    async def test_worker_per_account_error_non_fatal(self):
        mock_db = AsyncMock()
        mock_db.list_email_accounts = AsyncMock(return_value=[{"id":1,"name":"bad","host":"h","username":"u","password":"p","port":993,"use_ssl":True,"folder":"INBOX","action_after_fetch":"mark_seen","body_handling":"save_with_attachments","enabled":True},{"id":2,"name":"good","host":"h","username":"u","password":"p","port":993,"use_ssl":True,"folder":"INBOX","action_after_fetch":"mark_seen","body_handling":"save_with_attachments","enabled":True}])
        mock_db.update_email_account_error = AsyncMock(); mock_db.update_email_account_sync = AsyncMock()
        call_count=0; names_seen=[]
        async def selective_poll(self,account,account_id=None):
            nonlocal call_count; call_count+=1; names_seen.append(account.name)
            if account.name=="bad": raise Exception("Account 1 failed")
            return [42]
        task = asyncio.create_task(email_polling_worker(mock_db,poll_interval=0.01))
        try:
            with patch.object(EmailIngestor,"poll_account",selective_poll):
                for _ in range(50):
                    if call_count>=2: break
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass
        assert "bad" in names_seen and "good" in names_seen
        assert mock_db.update_email_account_error.call_count >= 1

    @pytest.mark.asyncio
    async def test_worker_skips_disabled_accounts(self):
        mock_db = AsyncMock()
        mock_db.list_email_accounts = AsyncMock(return_value=[{"id":1,"name":"enabled","host":"h","username":"u","password":"p","port":993,"use_ssl":True,"folder":"INBOX","action_after_fetch":"mark_seen","body_handling":"save_with_attachments","enabled":True}])
        mock_db.update_email_account_error = AsyncMock(); mock_db.update_email_account_sync = AsyncMock()
        polled=[]
        async def track_poll(self,account,account_id=None): polled.append(account.name); return []
        task = asyncio.create_task(email_polling_worker(mock_db,poll_interval=0.01))
        try:
            with patch.object(EmailIngestor,"poll_account",track_poll):
                for _ in range(50):
                    if polled and "enabled" in polled: break
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass
        assert "enabled" in polled
        mock_db.list_email_accounts.assert_called_with(enabled_only=True)

# ── Connection test edge cases ──
class TestConnectionEdgeCases:
    @pytest.mark.asyncio
    async def test_connection_select_failure(self, ingestor):
        account = EmailAccountConfig(name="test",host="h",username="u",password="p")
        mock_conn = MagicMock(); mock_conn.select = MagicMock(return_value=("NO",[b"Permission denied"]))
        with patch.object(ingestor,"_connect_imap",return_value=mock_conn):
            with patch.object(ingestor,"_disconnect_imap"):
                success,msg = await ingestor.test_connection(account)
        assert success is False
        assert "NO" in msg or "SELECT" in msg

    @pytest.mark.asyncio
    async def test_connection_empty_mailbox(self, ingestor):
        account = EmailAccountConfig(name="test",host="h",username="u",password="p")
        mock_conn = MagicMock(); mock_conn.select = MagicMock(return_value=("OK",[b"0"]))
        with patch.object(ingestor,"_connect_imap",return_value=mock_conn):
            with patch.object(ingestor,"_disconnect_imap"):
                success,msg = await ingestor.test_connection(account)
        assert success is True
        assert "0 messages" in msg or "successful" in msg.lower()

# ── Helper function edge cases ──
class TestHelperEdgeCases:
    def test_hash16_unicode_input(self): assert len(_hash16("caf\u00e9 r\u00e9sum\u00e9"))==16
    def test_hash16_empty_string(self): assert len(_hash16(""))==16
    def test_normalize_text_with_newlines(self): assert _normalize_text("Line1\n\nLine2\nLine3")=="line1 line2 line3"
    def test_normalize_text_with_tabs(self): assert _normalize_text("col1\tcol2\tcol3")=="col1 col2 col3"
    def test_normalize_text_mixed_case(self): assert _normalize_text("Hello WORLD")=="hello world"
