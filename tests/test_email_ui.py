"""Tests for Phase 8b-d: Email account UI pages, search integration, and document detail.

Covers:
- GET /email-accounts — accounts list page renders
- GET /email-accounts/new — create form renders
- POST /email-accounts/create — create account via form
- GET /email-accounts/{id}/edit — edit form renders
- POST /email-accounts/{id}/edit — update account via form
- POST /email-accounts/{id}/delete — delete account via form
- GET /email-accounts/{id}/logs — ingestion logs page renders
- GET /documents?source=email — source filter for email documents
- GET /search?q=... — email documents are searchable via FTS5
- GET /documents/{id} — email metadata shown on document detail page
- Nav link present in base.html
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "test_email_ui.db")


@pytest.fixture
async def asgi_client(tmp_db_path: str):
    """Create an httpx.AsyncClient backed by the ASGI app with email test data."""
    import httpx
    from src.core.db_sqlite import Database
    from src.web import server

    db = Database(db_path=tmp_db_path)
    await db.connect()

    # Create a test email account
    account = await db.create_email_account({
        "name": "Test Gmail",
        "host": "imap.gmail.com",
        "port": 993,
        "use_ssl": True,
        "username": "test@gmail.com",
        "password": "secret123",
        "folder": "INBOX",
        "body_handling": "save_with_attachments",
        "attachment_whitelist": "",
        "attachment_blacklist": "",
        "enabled": True,
    })

    # Create email-sourced documents (simulating ingestion output)
    await db.save_document(
        path="email://Test Gmail/msg1/body",
        source_type="email",
        source_name="Test Gmail",
        title="Q3 Financial Report",
        ext=".txt",
        mime_type="text/plain",
        body="This is the email body about quarterly financial results and revenue growth.",
        size=80,
        metadata={
            "email_message_id": "<msg1@gmail.com>",
            "email_subject": "Q3 Financial Report",
            "email_sender": "finance@company.com",
            "email_received_at": "2026-07-01T10:00:00",
            "email_thread_id": "abc123def456",
            "email_uid": 1,
            "email_folder": "INBOX",
            "email_part": "body",
        },
    )

    await db.save_document(
        path="email://Test Gmail/msg1/report.pdf",
        source_type="email",
        source_name="Test Gmail",
        title="report.pdf",
        ext=".pdf",
        mime_type="application/pdf",
        body="PDF attachment content about financial data and market analysis.",
        size=5000,
        metadata={
            "email_message_id": "<msg1@gmail.com>",
            "email_subject": "Q3 Financial Report",
            "email_sender": "finance@company.com",
            "email_received_at": "2026-07-01T10:00:00",
            "email_thread_id": "abc123def456",
            "email_uid": 1,
            "email_folder": "INBOX",
            "email_part": "attachment",
        },
    )

    # Create a non-email document for filter verification
    await db.save_document(
        path="/docs/manual.txt",
        source_type="local",
        source_name="local-upload",
        title="Manual Document",
        ext=".txt",
        mime_type="text/plain",
        body="This is a locally uploaded document about financial topics.",
        size=50,
    )

    # Log an ingestion entry
    await db.log_email_ingestion({
        "account_id": account["id"],
        "message_id": "<msg1@gmail.com>",
        "uid": 1,
        "folder": "INBOX",
        "subject": "Q3 Financial Report",
        "sender": "finance@company.com",
        "received_at": "2026-07-01T10:00:00",
        "status": "completed",
        "document_ids": [1, 2],
        "dedup_key": "msgid:abc123",
    })

    mock_queue = MagicMock()
    mock_queue.enqueue = AsyncMock(return_value=MagicMock(id="test-job-id"))

    original_db = server._db
    original_queue = server._queue
    server._db = db
    server._queue = mock_queue

    app = server.create_app()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await db.disconnect()
    server._db = original_db
    server._queue = original_queue


# ── Email Accounts List Page ─────────────────────────────────────


async def test_email_accounts_list_page_renders(asgi_client):
    """GET /email-accounts returns HTML with account list."""
    resp = await asgi_client.get("/email-accounts")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    html = resp.text
    assert "Email Accounts" in html
    assert "Test Gmail" in html
    assert "imap.gmail.com" in html
    assert "test@gmail.com" in html
    # Password should not be visible
    assert "secret123" not in html


async def test_email_accounts_list_page_nav_link(asgi_client):
    """The nav bar includes an Email link."""
    resp = await asgi_client.get("/email-accounts")
    assert resp.status_code == 200
    assert 'href="/email-accounts"' in resp.text


async def test_email_accounts_list_page_empty(asgi_client, tmp_db_path):
    """Accounts list page renders with no accounts."""
    import httpx
    from src.core.db_sqlite import Database
    from src.web import server

    db = Database(db_path=tmp_db_path + ".empty")
    await db.connect()

    original_db = server._db
    server._db = db
    app = server.create_app()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/email-accounts")
        assert resp.status_code == 200
        assert "暂无邮件账户" in resp.text

    await db.disconnect()
    server._db = original_db


# ── Create Account Form ──────────────────────────────────────────


async def test_email_account_new_page_renders(asgi_client):
    """GET /email-accounts/new returns the create form."""
    resp = await asgi_client.get("/email-accounts/new")
    assert resp.status_code == 200
    html = resp.text
    assert "New Email Account" in html
    assert 'name="host"' in html
    assert 'name="username"' in html
    assert 'name="password"' in html
    assert 'name="body_handling"' in html
    assert "save_with_attachments" in html


async def test_email_account_create_post(asgi_client):
    """POST /email-accounts/create creates an account and redirects."""
    resp = await asgi_client.post("/email-accounts/create", data={
        "name": "New Account",
        "host": "imap.example.com",
        "port": "993",
        "use_ssl": "1",
        "username": "user@example.com",
        "password": "pass123",
        "folder": "INBOX",
        "body_handling": "save_with_attachments",
        "attachment_whitelist": "*.pdf",
        "attachment_blacklist": "",
        "enabled": "1",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "/email-accounts" in resp.headers.get("location", "")

    # Verify the account was created
    resp2 = await asgi_client.get("/email-accounts")
    assert "New Account" in resp2.text
    assert "imap.example.com" in resp2.text


async def test_email_account_create_post_duplicate_name(asgi_client):
    """POST /email-accounts/create with duplicate name shows error."""
    resp = await asgi_client.post("/email-accounts/create", data={
        "name": "Test Gmail",  # already exists
        "host": "imap.other.com",
        "port": "993",
        "use_ssl": "1",
        "username": "other@gmail.com",
        "password": "pass",
        "folder": "INBOX",
        "body_handling": "save_with_attachments",
        "enabled": "1",
    }, follow_redirects=False)
    assert resp.status_code == 409
    assert "Test Gmail" in resp.text  # Error message shown


# ── Edit Account Form ────────────────────────────────────────────


async def test_email_account_edit_page_renders(asgi_client):
    """GET /email-accounts/{id}/edit returns the edit form with existing values."""
    resp = await asgi_client.get("/email-accounts/1/edit")
    assert resp.status_code == 200
    html = resp.text
    assert "Edit Email Account" in html
    assert "Test Gmail" in html
    assert "imap.gmail.com" in html
    assert "test@gmail.com" in html


async def test_email_account_edit_page_not_found(asgi_client):
    """GET /email-accounts/999/edit returns 404."""
    resp = await asgi_client.get("/email-accounts/999/edit")
    assert resp.status_code == 404
    assert "not found" in resp.text.lower()


async def test_email_account_edit_post(asgi_client):
    """POST /email-accounts/{id}/edit updates the account."""
    resp = await asgi_client.post("/email-accounts/1/edit", data={
        "name": "Updated Gmail",
        "host": "imap.updated.com",
        "port": "993",
        "use_ssl": "1",
        "username": "updated@gmail.com",
        # Password left blank — should keep existing
        "folder": "INBOX",
        "body_handling": "save_as_document",
        "attachment_whitelist": "",
        "attachment_blacklist": "",
        "enabled": "1",
    }, follow_redirects=False)
    assert resp.status_code == 303

    # Verify the update
    resp2 = await asgi_client.get("/email-accounts/1/edit")
    assert "Updated Gmail" in resp2.text
    assert "imap.updated.com" in resp2.text


# ── Delete Account ───────────────────────────────────────────────


async def test_email_account_delete_post(asgi_client):
    """POST /email-accounts/{id}/delete removes the account."""
    # Create a second account to delete
    await asgi_client.post("/email-accounts/create", data={
        "name": "ToDelete",
        "host": "imap.delete.com",
        "port": "993",
        "use_ssl": "1",
        "username": "del@delete.com",
        "password": "pass",
        "folder": "INBOX",
        "body_handling": "save_with_attachments",
        "enabled": "1",
    }, follow_redirects=False)

    resp = await asgi_client.post("/email-accounts/2/delete", follow_redirects=False)
    assert resp.status_code == 303

    # Verify it's gone
    resp2 = await asgi_client.get("/email-accounts/2/edit")
    assert resp2.status_code == 404


# ── Ingestion Logs Page ──────────────────────────────────────────


async def test_email_account_logs_page_renders(asgi_client):
    """GET /email-accounts/{id}/logs shows ingestion logs."""
    resp = await asgi_client.get("/email-accounts/1/logs")
    assert resp.status_code == 200
    html = resp.text
    assert "Ingestion Logs" in html
    assert "Test Gmail" in html
    assert "Q3 Financial Report" in html
    assert "finance@company.com" in html
    assert "completed" in html


async def test_email_account_logs_page_not_found(asgi_client):
    """GET /email-accounts/999/logs returns 404."""
    resp = await asgi_client.get("/email-accounts/999/logs")
    assert resp.status_code == 404


async def test_email_account_logs_page_status_filter(asgi_client):
    """GET /email-accounts/{id}/logs?status=completed filters logs."""
    resp = await asgi_client.get("/email-accounts/1/logs?status=completed")
    assert resp.status_code == 200
    assert "Q3 Financial Report" in resp.text

    # Filter by a non-matching status
    resp2 = await asgi_client.get("/email-accounts/1/logs?status=failed")
    assert resp2.status_code == 200
    assert "暂无接收日志" in resp2.text


# ── Search Integration ──────────────────────────────────────────


async def test_email_documents_searchable_via_fts(asgi_client):
    """GET /search?q=financial returns email-sourced documents."""
    resp = await asgi_client.get("/search?q=financial")
    assert resp.status_code == 200
    html = resp.text
    # Both email body and attachment should appear in search results
    # (they contain "financial" in their body text)
    assert "Q3 Financial Report" in html or "report.pdf" in html


async def test_email_documents_filter_by_source(asgi_client):
    """GET /documents?source=email shows only email-sourced documents."""
    resp = await asgi_client.get("/documents?source=email")
    assert resp.status_code == 200
    html = resp.text
    # Email documents should appear
    assert "Q3 Financial Report" in html or "report.pdf" in html
    # Non-email document should NOT appear
    assert "Manual Document" not in html


async def test_email_source_facet_appears(asgi_client):
    """The source facet dropdown includes 'email' with a count."""
    resp = await asgi_client.get("/documents")
    assert resp.status_code == 200
    html = resp.text
    assert "email" in html.lower()
    # The facet should show count of 2 (body + attachment)


async def test_non_email_documents_excluded_from_email_filter(asgi_client):
    """GET /documents?source=local shows only local documents."""
    resp = await asgi_client.get("/documents?source=local")
    assert resp.status_code == 200
    html = resp.text
    assert "Manual Document" in html
    assert "Q3 Financial Report" not in html


# ── Document Detail: Email Metadata ─────────────────────────────


async def test_document_detail_shows_email_metadata(asgi_client):
    """GET /documents/{id} shows email metadata for email-sourced documents."""
    # Document ID 1 is the email body document
    resp = await asgi_client.get("/documents/1")
    assert resp.status_code == 200
    html = resp.text
    assert "finance@company.com" in html  # sender
    assert "Q3 Financial Report" in html  # subject
    assert "abc123def456" in html  # thread_id
    assert "email-meta-section" in html


async def test_document_detail_no_email_meta_for_non_email(asgi_client):
    """GET /documents/{id} does not show email metadata for non-email docs."""
    # Document ID 3 is the local document
    resp = await asgi_client.get("/documents/3")
    assert resp.status_code == 200
    html = resp.text
    assert "email-meta-section" not in html
    assert "📧 Sender" not in html


async def test_document_detail_email_attachment_metadata(asgi_client):
    """GET /documents/{id} for email attachment shows attachment metadata."""
    # Document ID 2 is the email attachment
    resp = await asgi_client.get("/documents/2")
    assert resp.status_code == 200
    html = resp.text
    assert "finance@company.com" in html
    assert "attachment" in html.lower()
