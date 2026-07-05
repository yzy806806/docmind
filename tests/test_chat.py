"""Tests for chat history persistence: WebSocket handler persistence + REST API.

Covers:
- chat.py handle_chat: session_id resolution, message persistence, history replay
- REST API endpoints: GET /api/v1/chat/sessions, GET .../messages, DELETE session
- GET /chat HTML page: sidebar, session-list, ?session= param support
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
        yield str(Path(tmpdir) / "test_chat.db")


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


# ── handle_chat persistence tests ────────────────────────────────


class TestHandleChatPersistence:
    """Tests that handle_chat persists messages and resolves session_id."""

    @pytest.mark.asyncio
    async def test_connected_message_includes_session_id(self, db) -> None:
        """handle_chat should send session_id in the 'connected' message."""
        from src.web.chat import handle_chat

        ws = AsyncMock()
        ws.query_params = {}
        # Simulate immediate disconnect after the connected message
        ws.receive_text = AsyncMock(side_effect=Exception("disconnect"))

        await handle_chat(ws, db=db, llm_client=MagicMock())

        sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
        connected = [m for m in sent if m["type"] == "connected"][0]
        assert "session_id" in connected
        assert len(connected["session_id"]) > 0
        assert "title" in connected

    @pytest.mark.asyncio
    async def test_existing_session_id_is_reused(self, db) -> None:
        """When session_id is provided and exists, it should be reused."""
        from src.web.chat import handle_chat

        session = await db.create_chat_session(title="My Existing Chat")

        ws = AsyncMock()
        ws.query_params = {"session_id": session["id"]}
        ws.receive_text = AsyncMock(side_effect=Exception("disconnect"))

        await handle_chat(ws, db=db, llm_client=MagicMock())

        sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
        connected = [m for m in sent if m["type"] == "connected"][0]
        assert connected["session_id"] == session["id"]
        assert connected["title"] == "My Existing Chat"

    @pytest.mark.asyncio
    async def test_unknown_session_id_creates_new_session(self, db) -> None:
        """When session_id is provided but unknown, a new session is created."""
        from src.web.chat import handle_chat

        ws = AsyncMock()
        ws.query_params = {"session_id": "unknown-id-xyz"}
        ws.receive_text = AsyncMock(side_effect=Exception("disconnect"))

        await handle_chat(ws, db=db, llm_client=MagicMock())

        sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
        connected = [m for m in sent if m["type"] == "connected"][0]
        assert connected["session_id"] == "unknown-id-xyz"

        # Verify it was persisted
        session = await db.get_chat_session("unknown-id-xyz")
        assert session is not None

    @pytest.mark.asyncio
    async def test_history_replayed_on_connect(self, db) -> None:
        """When connecting to an existing session, history should be replayed."""
        from src.web.chat import handle_chat

        session = await db.create_chat_session()
        await db.save_chat_message(session["id"], "user", "Past question")
        await db.save_chat_message(session["id"], "assistant", "Past answer")

        ws = AsyncMock()
        ws.query_params = {"session_id": session["id"]}
        ws.receive_text = AsyncMock(side_effect=Exception("disconnect"))

        await handle_chat(ws, db=db, llm_client=MagicMock())

        sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
        history_msgs = [m for m in sent if m["type"] == "history"]
        assert len(history_msgs) == 1
        messages = history_msgs[0]["messages"]
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Past question"
        assert messages[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_no_history_message_for_empty_session(self, db) -> None:
        """A session with no messages should not trigger a history message."""
        from src.web.chat import handle_chat

        session = await db.create_chat_session()

        ws = AsyncMock()
        ws.query_params = {"session_id": session["id"]}
        ws.receive_text = AsyncMock(side_effect=Exception("disconnect"))

        await handle_chat(ws, db=db, llm_client=MagicMock())

        sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
        history_msgs = [m for m in sent if m["type"] == "history"]
        assert len(history_msgs) == 0

    @pytest.mark.asyncio
    async def test_user_question_persisted(self, db) -> None:
        """A user question should be saved to the database."""
        from src.web.chat import handle_chat

        ws = AsyncMock()
        ws.query_params = {}
        # First receive: a question, then disconnect
        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"type": "question", "text": "What is RAG?"}),
                Exception("disconnect"),
            ]
        )

        # Mock search + LLM so we get a deterministic answer
        search_engine_mock = MagicMock()
        search_engine_mock.search.return_value = [
            {
                "doc_id": 1,
                "title": "Doc",
                "snippet": "RAG is retrieval-augmented generation.",
                "body": "",
                "citation": {"confidence": "high"},
            }
        ]
        llm_client = MagicMock()
        llm_client.is_configured = True

        async def mock_stream(question, results, **kwargs):
            yield "RAG is a technique."

        llm_client.generate_stream = mock_stream

        with patch(
            "src.web.chat.create_backend"
        ) as mock_create_backend, patch(
            "src.web.chat.SearchEngine"
        ) as mock_search_engine_cls:
            mock_create_backend.return_value = MagicMock()
            mock_search_engine_cls.return_value = search_engine_mock
            await handle_chat(ws, db=db, llm_client=llm_client)

        # Find the session_id from the connected message
        sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
        connected = [m for m in sent if m["type"] == "connected"][0]
        sid = connected["session_id"]

        # Verify the user question was saved
        history = await db.get_chat_history(sid)
        roles = [m["role"] for m in history]
        assert "user" in roles
        user_msg = [m for m in history if m["role"] == "user"][0]
        assert user_msg["content"] == "What is RAG?"

    @pytest.mark.asyncio
    async def test_assistant_answer_persisted_with_citations(self, db) -> None:
        """The assistant answer should be saved with citations."""
        from src.web.chat import handle_chat

        ws = AsyncMock()
        ws.query_params = {}
        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"type": "question", "text": "Tell me about X"}),
                Exception("disconnect"),
            ]
        )

        search_engine_mock = MagicMock()
        search_engine_mock.search.return_value = [
            {
                "doc_id": 7,
                "title": "X Doc",
                "snippet": "X is a thing.",
                "body": "",
                "citation": {"confidence": "medium"},
            }
        ]
        llm_client = MagicMock()
        llm_client.is_configured = True

        async def mock_stream(question, results, **kwargs):
            yield "X is a thing [1]."

        llm_client.generate_stream = mock_stream

        with patch(
            "src.web.chat.create_backend"
        ) as mock_create_backend, patch(
            "src.web.chat.SearchEngine"
        ) as mock_search_engine_cls:
            mock_create_backend.return_value = MagicMock()
            mock_search_engine_cls.return_value = search_engine_mock
            await handle_chat(ws, db=db, llm_client=llm_client)

        sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
        connected = [m for m in sent if m["type"] == "connected"][0]
        sid = connected["session_id"]

        history = await db.get_chat_history(sid)
        assistant_msgs = [m for m in history if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        assert "X is a thing" in assistant_msgs[0]["content"]
        assert len(assistant_msgs[0]["citations"]) == 1
        assert assistant_msgs[0]["citations"][0]["doc_id"] == 7

    @pytest.mark.asyncio
    async def test_session_title_auto_generated_from_first_question(self, db) -> None:
        """The session title should be set from the first user message (50 chars)."""
        from src.web.chat import handle_chat

        long_question = "This is a very long question about document processing pipelines"
        ws = AsyncMock()
        ws.query_params = {}
        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"type": "question", "text": long_question}),
                Exception("disconnect"),
            ]
        )

        search_engine_mock = MagicMock()
        search_engine_mock.search.return_value = []
        llm_client = MagicMock()
        llm_client.is_configured = True

        async def mock_stream(question, results, **kwargs):
            return
            yield  # make it an async generator

        llm_client.generate_stream = mock_stream

        with patch(
            "src.web.chat.create_backend"
        ) as mock_create_backend, patch(
            "src.web.chat.SearchEngine"
        ) as mock_search_engine_cls:
            mock_create_backend.return_value = MagicMock()
            mock_search_engine_cls.return_value = search_engine_mock
            await handle_chat(ws, db=db, llm_client=llm_client)

        sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
        connected = [m for m in sent if m["type"] == "connected"][0]
        sid = connected["session_id"]

        session = await db.get_chat_session(sid)
        assert session["title"] == long_question[:50]

    @pytest.mark.asyncio
    async def test_answer_done_includes_session_id(self, db) -> None:
        """The answer:done message should include session_id."""
        from src.web.chat import handle_chat

        ws = AsyncMock()
        ws.query_params = {}
        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"type": "question", "text": "Hi"}),
                Exception("disconnect"),
            ]
        )

        search_engine_mock = MagicMock()
        search_engine_mock.search.return_value = [
            {
                "doc_id": 1,
                "title": "Doc",
                "snippet": "Hello.",
                "body": "",
                "citation": {"confidence": "low"},
            }
        ]
        llm_client = MagicMock()
        llm_client.is_configured = True

        async def mock_stream(question, results, **kwargs):
            yield "Hello back."

        llm_client.generate_stream = mock_stream

        with patch(
            "src.web.chat.create_backend"
        ) as mock_create_backend, patch(
            "src.web.chat.SearchEngine"
        ) as mock_search_engine_cls:
            mock_create_backend.return_value = MagicMock()
            mock_search_engine_cls.return_value = search_engine_mock
            await handle_chat(ws, db=db, llm_client=llm_client)

        sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
        done = [m for m in sent if m["type"] == "answer:done"][0]
        assert "session_id" in done
        assert len(done["session_id"]) > 0

    @pytest.mark.asyncio
    async def test_handle_chat_without_db_still_works(self) -> None:
        """When db=None, handle_chat should still work (ephemeral session)."""
        from src.web.chat import handle_chat

        ws = AsyncMock()
        ws.query_params = {}
        ws.receive_text = AsyncMock(side_effect=Exception("disconnect"))

        # Should not raise
        await handle_chat(ws, db=None, llm_client=MagicMock())

        sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
        connected = [m for m in sent if m["type"] == "connected"][0]
        assert "session_id" in connected


# ── REST API endpoint tests ──────────────────────────────────────


class TestChatSessionAPI:
    """Tests for the chat session REST API endpoints."""

    @pytest.mark.asyncio
    async def test_list_sessions_empty(self, asgi_client) -> None:
        """GET /api/v1/chat/sessions on empty db returns empty list."""
        resp = await asgi_client.get("/api/v1/chat/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["sessions"] == []

    @pytest.mark.asyncio
    async def test_list_sessions_returns_sessions(self, asgi_client, db) -> None:
        """GET /api/v1/chat/sessions returns created sessions."""
        from src.web import server

        real_db = server._db
        await real_db.create_chat_session(title="Session A")
        await real_db.create_chat_session(title="Session B")

        resp = await asgi_client.get("/api/v1/chat/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        titles = [s["title"] for s in data["sessions"]]
        assert "Session A" in titles
        assert "Session B" in titles

    @pytest.mark.asyncio
    async def test_list_sessions_includes_preview(self, asgi_client) -> None:
        """Session list entries should include a preview field."""
        from src.web import server

        real_db = server._db
        session = await real_db.create_chat_session(title="Preview API")
        await real_db.save_chat_message(session["id"], "user", "Preview content here")

        resp = await asgi_client.get("/api/v1/chat/sessions")
        data = resp.json()
        assert data["count"] == 1
        assert "preview" in data["sessions"][0]
        assert "Preview content here" in data["sessions"][0]["preview"]

    @pytest.mark.asyncio
    async def test_list_sessions_respects_limit(self, asgi_client) -> None:
        """The limit query param should cap the number of sessions returned."""
        from src.web import server

        real_db = server._db
        for i in range(5):
            await real_db.create_chat_session(title=f"S{i}")

        resp = await asgi_client.get("/api/v1/chat/sessions?limit=3")
        data = resp.json()
        assert data["count"] == 3

    @pytest.mark.asyncio
    async def test_get_messages_existing_session(self, asgi_client) -> None:
        """GET .../messages returns the message history for an existing session."""
        from src.web import server

        real_db = server._db
        session = await real_db.create_chat_session(title="Msg Test")
        await real_db.save_chat_message(session["id"], "user", "Q1")
        await real_db.save_chat_message(session["id"], "assistant", "A1")

        resp = await asgi_client.get(
            f"/api/v1/chat/sessions/{session['id']}/messages"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        assert data["messages"][0]["content"] == "Q1"
        assert data["messages"][1]["content"] == "A1"
        assert data["session"]["id"] == session["id"]

    @pytest.mark.asyncio
    async def test_get_messages_missing_session_404(self, asgi_client) -> None:
        """GET .../messages for unknown session returns 404."""
        resp = await asgi_client.get(
            "/api/v1/chat/sessions/nonexistent-id/messages"
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_session_existing(self, asgi_client) -> None:
        """DELETE on an existing session returns 200 and deleted=True."""
        from src.web import server

        real_db = server._db
        session = await real_db.create_chat_session(title="Delete Me")

        resp = await asgi_client.delete(f"/api/v1/chat/sessions/{session['id']}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] is True

        # Verify it's gone
        assert await real_db.get_chat_session(session["id"]) is None

    @pytest.mark.asyncio
    async def test_delete_session_missing_404(self, asgi_client) -> None:
        """DELETE on an unknown session returns 404."""
        resp = await asgi_client.delete("/api/v1/chat/sessions/nonexistent")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_session_cascades_messages(self, asgi_client) -> None:
        """DELETE should also remove all messages in the session."""
        from src.web import server

        real_db = server._db
        session = await real_db.create_chat_session()
        await real_db.save_chat_message(session["id"], "user", "Q")
        await real_db.save_chat_message(session["id"], "assistant", "A")

        await asgi_client.delete(f"/api/v1/chat/sessions/{session['id']}")

        history = await real_db.get_chat_history(session["id"])
        assert history == []


# ── Chat page HTML tests ─────────────────────────────────────────


class TestChatPageSidebar:
    """Tests for the enhanced chat page with session sidebar."""

    def test_chat_page_has_sidebar(self):
        """The chat page should include a sidebar element."""
        from src.web.server import _render_chat_page

        html = _render_chat_page()
        assert "chat-sidebar" in html
        assert "chat-session-list" in html

    def test_chat_page_has_new_chat_button(self):
        """The chat page should have a 'New' chat button."""
        from src.web.server import _render_chat_page

        html = _render_chat_page()
        assert "new-chat-btn" in html
        assert "New" in html

    def test_chat_page_has_session_list_js(self):
        """The chat page should load chat.js which contains loadSessionList."""
        from src.web.server import _render_chat_page
        from pathlib import Path

        html = _render_chat_page()
        assert "/static/js/chat.js" in html
        chat_js = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "js" / "chat.js"
        js_src = chat_js.read_text()
        assert "loadSessionList" in js_src
        assert "/api/v1/chat/sessions" in js_src

    def test_chat_page_has_session_param_support(self):
        """chat.js should support ?session= URL param via getQueryParam."""
        from src.web.server import _render_chat_page
        from pathlib import Path

        html = _render_chat_page()
        assert "/static/js/chat.js" in html
        chat_js = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "js" / "chat.js"
        js_src = chat_js.read_text()
        assert "getQueryParam" in js_src
        assert "session" in js_src

    def test_chat_page_has_delete_session_js(self):
        """chat.js should include deleteSession function."""
        from src.web.server import _render_chat_page
        from pathlib import Path

        html = _render_chat_page()
        assert "/static/js/chat.js" in html
        chat_js = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "js" / "chat.js"
        js_src = chat_js.read_text()
        assert "deleteSession" in js_src

    def test_chat_page_has_history_handler(self):
        """chat.js should handle 'history' message type."""
        from src.web.server import _render_chat_page
        from pathlib import Path

        html = _render_chat_page()
        assert "/static/js/chat.js" in html
        chat_js = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "js" / "chat.js"
        js_src = chat_js.read_text()
        assert "case 'history'" in js_src

    def test_chat_page_connected_includes_session_id(self):
        """The connected handler in chat.js should process session_id."""
        from src.web.server import _render_chat_page
        from pathlib import Path

        html = _render_chat_page()
        assert "/static/js/chat.js" in html
        chat_js = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "js" / "chat.js"
        js_src = chat_js.read_text()
        assert "msg.session_id" in js_src

    def test_chat_page_sidebar_css(self):
        """The external stylesheet should include sidebar styling."""
        from src.web.server import _base_page

        html = _base_page("test", "")
        # Sidebar styles are now in the external stylesheet
        assert "/static/css/styles.css" in html

    def test_chat_page_has_chat_layout(self):
        """The chat page should use a chat-layout container."""
        from src.web.server import _render_chat_page

        html = _render_chat_page()
        assert "chat-layout" in html
