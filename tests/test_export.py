"""Tests for export and summarization features.

Covers:
- Chat session export: GET /api/v1/chat/sessions/{id}/export?format=markdown|json|txt
- Search results export: GET /search?q=xxx&export=csv|json
- Document summary export: GET /documents/{id}/summary/export?format=md|txt
- Document summary regeneration: POST /documents/{id}/regenerate-summary
- Batch summary generation: POST /api/v1/documents/summarize-all
- Content-Disposition headers for downloads
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "test_export.db")


@pytest.fixture
async def db(tmp_db_path: str):
    from src.core.db_sqlite import Database

    database = Database(db_path=tmp_db_path)
    await database.connect()
    yield database
    await database.disconnect()


@pytest.fixture
async def asgi_client(tmp_db_path: str):
    """httpx.AsyncClient backed by the ASGI app with a real Database."""
    import httpx
    from src.core.db_sqlite import Database
    from src.web import server

    db = Database(db_path=tmp_db_path)
    await db.connect()

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


# ── Chat session export tests ────────────────────────────────────


class TestChatExport:
    """Tests for GET /api/v1/chat/sessions/{id}/export."""

    @pytest.mark.asyncio
    async def test_export_markdown(self, asgi_client) -> None:
        """Markdown export should contain conversation as MD with headers."""
        from src.web import server

        real_db = server._db
        session = await real_db.create_chat_session(title="Test Chat")
        await real_db.save_chat_message(session["id"], "user", "What is AI?")
        await real_db.save_chat_message(
            session["id"], "assistant", "AI is artificial intelligence."
        )

        resp = await asgi_client.get(
            f"/api/v1/chat/sessions/{session['id']}/export?format=markdown"
        )
        assert resp.status_code == 200
        body = resp.text
        assert "# Test Chat" in body
        assert "What is AI?" in body
        assert "AI is artificial intelligence." in body
        assert "👤 You" in body or "You" in body
        assert "🤖 Assistant" in body or "Assistant" in body
        # Content-Disposition header for download
        assert "content-disposition" in {k.lower() for k in resp.headers}
        assert "attachment" in resp.headers.get("content-disposition", "").lower()
        assert ".md" in resp.headers.get("content-disposition", "")

    @pytest.mark.asyncio
    async def test_export_json(self, asgi_client) -> None:
        """JSON export should return valid JSON with messages array."""
        from src.web import server

        real_db = server._db
        session = await real_db.create_chat_session(title="JSON Chat")
        await real_db.save_chat_message(session["id"], "user", "Hello")
        await real_db.save_chat_message(
            session["id"],
            "assistant",
            "Hi there",
            citations=[{"ref": 1, "doc_id": 5, "title": "Doc"}],
        )

        resp = await asgi_client.get(
            f"/api/v1/chat/sessions/{session['id']}/export?format=json"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == session["id"]
        assert data["title"] == "JSON Chat"
        assert len(data["messages"]) == 2
        assert data["messages"][0]["role"] == "user"
        assert data["messages"][0]["content"] == "Hello"
        assert data["messages"][1]["role"] == "assistant"
        assert len(data["messages"][1]["citations"]) == 1
        assert "content-disposition" in {k.lower() for k in resp.headers}
        assert ".json" in resp.headers.get("content-disposition", "")

    @pytest.mark.asyncio
    async def test_export_txt(self, asgi_client) -> None:
        """Plain text export should contain conversation text."""
        from src.web import server

        real_db = server._db
        session = await real_db.create_chat_session(title="TXT Chat")
        await real_db.save_chat_message(session["id"], "user", "Question 1")
        await real_db.save_chat_message(session["id"], "assistant", "Answer 1")

        resp = await asgi_client.get(
            f"/api/v1/chat/sessions/{session['id']}/export?format=txt"
        )
        assert resp.status_code == 200
        body = resp.text
        assert "Question 1" in body
        assert "Answer 1" in body
        assert "[You]" in body or "You" in body
        assert "[Assistant]" in body or "Assistant" in body
        assert "content-disposition" in {k.lower() for k in resp.headers}
        assert ".txt" in resp.headers.get("content-disposition", "")

    @pytest.mark.asyncio
    async def test_export_default_is_markdown(self, asgi_client) -> None:
        """When no format is given, markdown should be returned."""
        from src.web import server

        real_db = server._db
        session = await real_db.create_chat_session(title="Default")
        await real_db.save_chat_message(session["id"], "user", "test")

        resp = await asgi_client.get(
            f"/api/v1/chat/sessions/{session['id']}/export"
        )
        assert resp.status_code == 200
        assert "# Default" in resp.text

    @pytest.mark.asyncio
    async def test_export_404_for_missing_session(self, asgi_client) -> None:
        """Exporting a non-existent session should return 404."""
        resp = await asgi_client.get(
            "/api/v1/chat/sessions/nonexistent-id/export?format=markdown"
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_export_empty_session(self, asgi_client) -> None:
        """Exporting a session with no messages should still work."""
        from src.web import server

        real_db = server._db
        session = await real_db.create_chat_session(title="Empty")

        resp = await asgi_client.get(
            f"/api/v1/chat/sessions/{session['id']}/export?format=json"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["messages"] == []

    @pytest.mark.asyncio
    async def test_export_markdown_includes_citations(self, asgi_client) -> None:
        """Markdown export should include citation sources."""
        from src.web import server

        real_db = server._db
        session = await real_db.create_chat_session(title="Cite Chat")
        await real_db.save_chat_message(
            session["id"],
            "assistant",
            "See document [1].",
            citations=[
                {
                    "ref": 1,
                    "doc_id": 42,
                    "title": "Important Doc",
                    "confidence": "high",
                }
            ],
        )

        resp = await asgi_client.get(
            f"/api/v1/chat/sessions/{session['id']}/export?format=markdown"
        )
        assert resp.status_code == 200
        assert "Important Doc" in resp.text
        assert "Sources" in resp.text or "sources" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_export_filename_sanitized(self, asgi_client) -> None:
        """The download filename should be sanitized (no special chars)."""
        from src.web import server

        real_db = server._db
        session = await real_db.create_chat_session(title="Test/File<>?*")

        resp = await asgi_client.get(
            f"/api/v1/chat/sessions/{session['id']}/export?format=markdown"
        )
        assert resp.status_code == 200
        cd = resp.headers.get("content-disposition", "")
        # Should not contain raw special chars in filename
        assert "/" not in cd.split("filename=")[-1].strip('"')


# ── Search results export tests ──────────────────────────────────


class TestSearchExport:
    """Tests for GET /search?q=xxx&export=csv|json."""

    @pytest.mark.asyncio
    async def test_export_csv(self, asgi_client) -> None:
        """CSV export should return CSV with header row and data."""
        from src.web import server

        real_db = server._db
        await real_db.save_document(
            path="/docs/test1.txt",
            source_type="api",
            source_name="test",
            title="CSV Test Document",
            ext=".txt",
            mime_type="text/plain",
            body="This document contains searchable text about revenue.",
            size=100,
            status="indexed",
        )

        resp = await asgi_client.get("/search?q=revenue&export=csv")
        assert resp.status_code == 200
        body = resp.text
        assert "id" in body.split("\n")[0]  # CSV header
        assert "title" in body.split("\n")[0]
        assert "CSV Test Document" in body
        assert "content-disposition" in {k.lower() for k in resp.headers}
        assert ".csv" in resp.headers.get("content-disposition", "")

    @pytest.mark.asyncio
    async def test_export_json(self, asgi_client) -> None:
        """JSON export should return structured JSON with results array."""
        from src.web import server

        real_db = server._db
        await real_db.save_document(
            path="/docs/test2.txt",
            source_type="api",
            source_name="test",
            title="JSON Export Doc",
            ext=".txt",
            mime_type="text/plain",
            body="Content about machine learning models.",
            size=100,
            status="indexed",
        )

        resp = await asgi_client.get("/search?q=machine&export=json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["query"] == "machine"
        assert data["result_count"] >= 1
        assert any("JSON Export Doc" in r["title"] for r in data["results"])
        assert "content-disposition" in {k.lower() for k in resp.headers}
        assert ".json" in resp.headers.get("content-disposition", "")

    @pytest.mark.asyncio
    async def test_export_csv_empty_results(self, asgi_client) -> None:
        """CSV export with no results should still return a valid CSV header."""
        resp = await asgi_client.get("/search?q=nonexistentxyz&export=csv")
        assert resp.status_code == 200
        body = resp.text
        # Should have header row
        assert "id" in body.split("\n")[0]

    @pytest.mark.asyncio
    async def test_no_export_returns_html(self, asgi_client) -> None:
        """Without export param, should return HTML page."""
        from src.web import server

        real_db = server._db
        await real_db.save_document(
            path="/docs/html_test.txt",
            source_type="api",
            source_name="test",
            title="HTML Result",
            ext=".txt",
            mime_type="text/plain",
            body="Searchable content here.",
            size=50,
            status="indexed",
        )

        resp = await asgi_client.get("/search?q=Searchable")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_search_page_has_export_buttons(self, asgi_client) -> None:
        """The HTML search results page should contain export buttons."""
        from src.web import server

        real_db = server._db
        await real_db.save_document(
            path="/docs/btn_test.txt",
            source_type="api",
            source_name="test",
            title="Button Test",
            ext=".txt",
            mime_type="text/plain",
            body="Button test content.",
            size=50,
            status="indexed",
        )

        resp = await asgi_client.get("/search?q=Button")
        assert resp.status_code == 200
        body = resp.text
        assert "export=csv" in body or "Export CSV" in body
        assert "export=json" in body or "Export JSON" in body


# ── Document summary export tests ────────────────────────────────


class TestDocumentSummaryExport:
    """Tests for GET /documents/{id}/summary/export."""

    @pytest.mark.asyncio
    async def test_export_summary_md(self, asgi_client) -> None:
        """Markdown summary export should contain title and summary."""
        from src.web import server

        real_db = server._db
        doc_id = await real_db.save_document(
            path="/docs/sum_md.txt",
            source_type="api",
            source_name="test",
            title="Summary MD Test",
            ext=".txt",
            mime_type="text/plain",
            body="Body content here.",
            size=50,
            status="summarized",
        )
        await real_db.update_summary(doc_id, "This is a test summary.")

        resp = await asgi_client.get(
            f"/documents/{doc_id}/summary/export?format=md"
        )
        assert resp.status_code == 200
        body = resp.text
        assert "# Summary MD Test" in body
        assert "This is a test summary." in body
        assert "content-disposition" in {k.lower() for k in resp.headers}
        assert "_summary.md" in resp.headers.get("content-disposition", "")

    @pytest.mark.asyncio
    async def test_export_summary_txt(self, asgi_client) -> None:
        """Plain text summary export should contain summary text."""
        from src.web import server

        real_db = server._db
        doc_id = await real_db.save_document(
            path="/docs/sum_txt.txt",
            source_type="api",
            source_name="test",
            title="Summary TXT Test",
            ext=".txt",
            mime_type="text/plain",
            body="Body text.",
            size=50,
            status="summarized",
        )
        await real_db.update_summary(doc_id, "TXT format summary.")

        resp = await asgi_client.get(
            f"/documents/{doc_id}/summary/export?format=txt"
        )
        assert resp.status_code == 200
        body = resp.text
        assert "Summary TXT Test" in body
        assert "TXT format summary." in body
        assert "SUMMARY" in body
        assert "content-disposition" in {k.lower() for k in resp.headers}
        assert "_summary.txt" in resp.headers.get("content-disposition", "")

    @pytest.mark.asyncio
    async def test_export_summary_no_summary(self, asgi_client) -> None:
        """Export when no summary exists should show placeholder."""
        from src.web import server

        real_db = server._db
        doc_id = await real_db.save_document(
            path="/docs/no_sum.txt",
            source_type="api",
            source_name="test",
            title="No Summary Doc",
            ext=".txt",
            mime_type="text/plain",
            body="Content without summary.",
            size=50,
            status="indexed",
        )

        resp = await asgi_client.get(
            f"/documents/{doc_id}/summary/export?format=md"
        )
        assert resp.status_code == 200
        assert "No summary available" in resp.text

    @pytest.mark.asyncio
    async def test_export_summary_404(self, asgi_client) -> None:
        """Exporting summary for non-existent doc should return 404."""
        resp = await asgi_client.get("/documents/99999/summary/export?format=md")
        assert resp.status_code == 404


# ── Regenerate summary tests ─────────────────────────────────────


class TestRegenerateSummary:
    """Tests for POST /documents/{id}/regenerate-summary."""

    @pytest.mark.asyncio
    async def test_regenerate_summary_success(self, asgi_client) -> None:
        """Regenerate summary should update the doc and re-render detail."""
        from src.web import server

        real_db = server._db
        doc_id = await real_db.save_document(
            path="/docs/regen.txt",
            source_type="api",
            source_name="test",
            title="Regen Test Document",
            ext=".txt",
            mime_type="text/plain",
            body=(
                "This is a long enough body for extractive summarization. "
                "It has multiple sentences. The summarizer should pick key ones. "
                "Revenue is important. Another sentence here for length."
            ),
            size=200,
            status="indexed",
        )

        resp = await asgi_client.post(f"/documents/{doc_id}/regenerate-summary")
        assert resp.status_code == 200
        # Should re-render the document detail page (HTML)
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Regen Test Document" in resp.text

        # Verify summary was updated in DB
        doc = await real_db.get_document(doc_id)
        assert doc.get("summary") is not None
        assert len(doc["summary"]) > 0

    @pytest.mark.asyncio
    async def test_regenerate_summary_404(self, asgi_client) -> None:
        """Regenerating summary for non-existent doc returns 404."""
        resp = await asgi_client.post("/documents/99999/regenerate-summary")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_regenerate_summary_detail_has_button(self, asgi_client) -> None:
        """The document detail page should have a Regenerate Summary button."""
        from src.web import server

        real_db = server._db
        doc_id = await real_db.save_document(
            path="/docs/btn.txt",
            source_type="api",
            source_name="test",
            title="Button Check",
            ext=".txt",
            mime_type="text/plain",
            body="Content.",
            size=50,
            status="indexed",
        )

        resp = await asgi_client.get(f"/documents/{doc_id}")
        assert resp.status_code == 200
        assert "regenerate-summary" in resp.text
        assert "重新生成摘要" in resp.text


# ── Batch summarize-all tests ────────────────────────────────────


class TestSummarizeAll:
    """Tests for POST /api/v1/documents/summarize-all."""

    @pytest.mark.asyncio
    async def test_summarize_all_creates_jobs(self, asgi_client) -> None:
        """summarize-all should create jobs for indexed docs."""
        from src.web import server

        real_db = server._db
        # Create docs that need summaries (status = 'indexed')
        for i in range(3):
            await real_db.save_document(
                path=f"/docs/batch_{i}.txt",
                source_type="api",
                source_name="test",
                title=f"Batch Doc {i}",
                ext=".txt",
                mime_type="text/plain",
                body=f"Content of batch doc {i}.",
                size=50,
                status="indexed",
            )

        resp = await asgi_client.post("/api/v1/documents/summarize-all")
        assert resp.status_code == 200
        data = resp.json()
        assert data["jobs_created"] == 3
        assert len(data["job_ids"]) == 3

    @pytest.mark.asyncio
    async def test_summarize_all_no_pending(self, asgi_client) -> None:
        """When no docs need summarization, jobs_created should be 0."""
        from src.web import server

        real_db = server._db
        # Create a summarized doc
        doc_id = await real_db.save_document(
            path="/docs/done.txt",
            source_type="api",
            source_name="test",
            title="Done Doc",
            ext=".txt",
            mime_type="text/plain",
            body="Content.",
            size=50,
            status="indexed",
        )
        await real_db.update_summary(doc_id, "Already summarized.")

        resp = await asgi_client.post("/api/v1/documents/summarize-all")
        assert resp.status_code == 200
        data = resp.json()
        assert data["jobs_created"] == 0

    @pytest.mark.asyncio
    async def test_summarize_all_response_has_message(self, asgi_client) -> None:
        """The response should include a human-readable message."""
        resp = await asgi_client.post("/api/v1/documents/summarize-all")
        assert resp.status_code == 200
        data = resp.json()
        assert "message" in data


# ── Document detail page UI tests ────────────────────────────────


class TestDocumentDetailUI:
    """Tests for UI elements on the document detail page."""

    @pytest.mark.asyncio
    async def test_detail_has_summary_export_links(self, asgi_client) -> None:
        """Document detail should have summary export links."""
        from src.web import server

        real_db = server._db
        doc_id = await real_db.save_document(
            path="/docs/ui_test.txt",
            source_type="api",
            source_name="test",
            title="UI Test",
            ext=".txt",
            mime_type="text/plain",
            body="Content.",
            size=50,
            status="indexed",
        )

        resp = await asgi_client.get(f"/documents/{doc_id}")
        assert resp.status_code == 200
        body = resp.text
        assert "summary/export?format=md" in body
        assert "summary/export?format=txt" in body


# ── Chat page UI tests ───────────────────────────────────────────


class TestChatPageUI:
    """Tests for UI elements on the chat page."""

    @pytest.mark.asyncio
    async def test_chat_page_has_export_button(self, asgi_client) -> None:
        """Chat page should contain an Export button."""
        resp = await asgi_client.get("/chat")
        assert resp.status_code == 200
        body = resp.text
        assert "Export" in body
        assert "exportChat" in body
        assert "toggleExportMenu" in body


# ── Summarizer integration tests ─────────────────────────────────


class TestSummarizerIntegration:
    """Tests for the summarizer pipeline integration."""

    @pytest.mark.asyncio
    async def test_generate_summary_extractive_fallback(self) -> None:
        """_generate_summary_for_doc should work with no LLM (extractive)."""
        from src.web.server import _generate_summary_for_doc

        doc = {
            "title": "Extractive Test",
            "body": (
                "This is the first sentence about revenue. "
                "This is the second sentence about growth. "
                "Third sentence covers market analysis. "
                "Fourth sentence discusses quarterly earnings. "
                "Fifth sentence mentions strategic planning. "
                "Sixth sentence is about operational efficiency."
            ),
        }
        result = await _generate_summary_for_doc(doc)
        assert result is not None
        assert len(result) > 10

    @pytest.mark.asyncio
    async def test_generate_summary_empty_body(self) -> None:
        """Empty body should return None."""
        from src.web.server import _generate_summary_for_doc

        doc = {"title": "Empty", "body": ""}
        result = await _generate_summary_for_doc(doc)
        assert result is None

    @pytest.mark.asyncio
    async def test_generate_summary_with_llm_adapter(self) -> None:
        """_SyncLLMAdapter should bridge async LLM to sync chat()."""
        from src.web.server import _SyncLLMAdapter

        mock_async_client = MagicMock()

        async def mock_generate(question, context_chunks, max_tokens=None):
            return f"LLM answer to: {question[:30]}"

        mock_async_client.generate = mock_generate

        adapter = _SyncLLMAdapter(mock_async_client)
        result = adapter.chat("What is the summary?", max_tokens=100)
        assert "LLM answer" in result

    @pytest.mark.asyncio
    async def test_generate_summary_llm_adapter_handles_error(self) -> None:
        """_SyncLLMAdapter should return empty string on LLM failure."""
        from src.web.server import _SyncLLMAdapter

        mock_async_client = MagicMock()

        async def mock_generate(question, context_chunks, max_tokens=None):
            raise RuntimeError("LLM unavailable")

        mock_async_client.generate = mock_generate

        adapter = _SyncLLMAdapter(mock_async_client)
        result = adapter.chat("test prompt", max_tokens=100)
        assert result == ""
