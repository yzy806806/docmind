"""Tests for HybridSearchEngine integration into the web layer.

Covers:
- /search endpoint uses the hybrid engine from app.state
- /search endpoint falls back to FTS5 when no hybrid engine is configured
- /search results have the 'id' key expected by templates and exports
- Chat WebSocket endpoint passes the hybrid engine to handle_chat
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    """Provide a temporary database file path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "test_hybrid_web.db")


@pytest.fixture
async def db(tmp_db_path: str):
    """Create a connected SQLite Database instance with test docs."""
    from src.core.db_sqlite import Database

    database = Database(db_path=tmp_db_path)
    await database.connect()

    # Insert test documents
    await database.save_document(
        path="/docs/hybrid_test1.txt",
        source_type="api",
        source_name="test-source",
        title="Hybrid Search Test Document",
        ext=".txt",
        mime_type="text/plain",
        body="This document discusses machine learning pipelines and data processing.",
        size=200,
        status="indexed",
    )
    await database.save_document(
        path="/docs/hybrid_test2.txt",
        source_type="api",
        source_name="test-source",
        title="Another ML Document",
        ext=".txt",
        mime_type="text/plain",
        body="Content about neural networks and deep learning models.",
        size=300,
        status="indexed",
    )

    yield database
    await database.disconnect()


@pytest.fixture
async def asgi_client_with_hybrid(tmp_db_path: str, db):
    """Create an httpx ASGI client with hybrid engine on app.state."""
    import httpx
    from src.core.embeddings import EmbeddingClient
    from src.core.search import HybridSearchEngine
    from src.web import server

    # Create hybrid engine with no embedding provider (FTS5-only fallback)
    embed_client = EmbeddingClient()  # no config → not available
    hybrid_engine = HybridSearchEngine(db=db, embed_client=embed_client)

    mock_queue = MagicMock()
    mock_queue.enqueue = AsyncMock(return_value=MagicMock(id="test-job-id"))

    original_db = server._db
    original_queue = server._queue
    server._db = db
    server._queue = mock_queue

    app = server.create_app()
    app.state.hybrid_engine = hybrid_engine

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, app, hybrid_engine

    server._db = original_db
    server._queue = original_queue


@pytest.fixture
async def asgi_client_no_hybrid(tmp_db_path: str, db):
    """Create an httpx ASGI client WITHOUT hybrid engine on app.state."""
    import httpx
    from src.web import server

    mock_queue = MagicMock()
    mock_queue.enqueue = AsyncMock(return_value=MagicMock(id="test-job-id"))

    original_db = server._db
    original_queue = server._queue
    server._db = db
    server._queue = mock_queue

    app = server.create_app()
    # Deliberately do NOT set app.state.hybrid_engine

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, app

    server._db = original_db
    server._queue = original_queue


# ── /search endpoint tests ───────────────────────────────────────


class TestSearchEndpointHybridIntegration:
    """Tests that GET /search uses the HybridSearchEngine."""

    @pytest.mark.asyncio
    async def test_search_with_hybrid_engine_returns_results(
        self, asgi_client_with_hybrid
    ) -> None:
        """GET /search?q= should return results when hybrid engine is active."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        resp = await client.get("/search?q=machine")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        # The search results should contain the document title
        assert "Hybrid Search Test Document" in resp.text or "Another ML Document" in resp.text

    @pytest.mark.asyncio
    async def test_search_with_hybrid_engine_calls_search_method(
        self, asgi_client_with_hybrid
    ) -> None:
        """The hybrid engine's search() method should be called."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        # Spy on the hybrid engine's search method
        with patch.object(
            hybrid_engine, "search", wraps=hybrid_engine.search
        ) as spy:
            resp = await client.get("/search?q=machine")
            assert resp.status_code == 200
            assert spy.called
            # Verify it was called with the right query and top_k
            call_args = spy.call_args
            assert call_args[0][0] == "machine" or call_args.kwargs.get("query") == "machine"
            assert call_args.kwargs.get("top_k") == 20 or (
                len(call_args[0]) > 1 and call_args[0][1] == 20
            )

    @pytest.mark.asyncio
    async def test_search_hybrid_results_have_id_key(
        self, asgi_client_with_hybrid
    ) -> None:
        """Hybrid search results should have 'id' key for template compatibility."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        # Verify by checking the JSON export — it uses r.get("id")
        resp = await client.get("/search?q=machine&export=json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["result_count"] >= 1
        for r in data["results"]:
            # The 'id' field must be present (not None) for template/export compat
            assert "id" in r
            assert r["id"] is not None

    @pytest.mark.asyncio
    async def test_search_fallback_when_no_hybrid_engine(
        self, asgi_client_no_hybrid
    ) -> None:
        """GET /search should still work when no hybrid engine is on app.state."""
        client, app = asgi_client_no_hybrid

        resp = await client.get("/search?q=machine")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_search_fallback_returns_results(
        self, asgi_client_no_hybrid
    ) -> None:
        """FTS5 fallback should return the same search results."""
        client, app = asgi_client_no_hybrid

        resp = await client.get("/search?q=machine&export=json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["result_count"] >= 1
        assert any("machine" in r.get("title", "").lower() or "machine" in r.get("snippet", "").lower() for r in data["results"])

    @pytest.mark.asyncio
    async def test_search_empty_query_returns_form(
        self, asgi_client_with_hybrid
    ) -> None:
        """GET /search with empty query should return the search form."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        resp = await client.get("/search?q=")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_search_export_csv_with_hybrid(
        self, asgi_client_with_hybrid
    ) -> None:
        """CSV export should work with hybrid search results."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        resp = await client.get("/search?q=machine&export=csv")
        assert resp.status_code == 200
        body = resp.text
        # CSV header should contain 'id'
        assert "id" in body.split("\n")[0]
        assert "content-disposition" in {k.lower() for k in resp.headers}
        assert ".csv" in resp.headers.get("content-disposition", "")


# ── Chat WebSocket hybrid engine tests ──────────────────────────


class TestChatHybridEngineIntegration:
    """Tests that the chat WebSocket passes the hybrid engine to handle_chat."""

    @pytest.mark.asyncio
    async def test_handle_chat_accepts_search_engine_param(self) -> None:
        """handle_chat should accept a search_engine parameter without error."""
        from src.web.chat import handle_chat

        # Create a mock WebSocket
        ws = MagicMock()
        ws.accept = AsyncMock()
        ws.query_params = {}
        ws.receive_text = AsyncMock(side_effect=Exception("test done"))
        ws.send_text = AsyncMock()

        # Create a mock search engine (HybridSearchEngine)
        mock_engine = MagicMock()
        mock_engine.search = AsyncMock(return_value=[])
        mock_engine.search_chunks = AsyncMock(return_value=[])

        mock_llm = MagicMock()
        mock_llm.generate_stream = AsyncMock(return_value=iter([]))
        mock_llm.close = AsyncMock()

        # Should not raise — search_engine is accepted
        try:
            await handle_chat(ws, search_engine=mock_engine, llm_client=mock_llm)
        except Exception:
            pass  # The receive_text will raise; that's expected

        # Verify the WebSocket was accepted
        ws.accept.assert_awaited()

    @pytest.mark.asyncio
    async def test_handle_chat_without_search_engine_falls_back(
        self, tmp_db_path: str
    ) -> None:
        """handle_chat without search_engine should create a plain SearchEngine."""
        from src.web.chat import handle_chat

        ws = MagicMock()
        ws.accept = AsyncMock()
        ws.query_params = {}
        ws.receive_text = AsyncMock(side_effect=Exception("test done"))
        ws.send_text = AsyncMock()

        mock_llm = MagicMock()
        mock_llm.generate_stream = AsyncMock(return_value=iter([]))
        mock_llm.close = AsyncMock()

        # No search_engine provided — should create one internally
        try:
            await handle_chat(
                ws,
                search_db_path=tmp_db_path,
                llm_client=mock_llm,
            )
        except Exception:
            pass

        ws.accept.assert_awaited()
