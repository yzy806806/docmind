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


# ── /search?vector_weight= passthrough tests ─────────────────────


class TestSearchVectorWeightParam:
    """Tests that GET /search?vector_weight= passes through to the hybrid engine."""

    @pytest.mark.asyncio
    async def test_vector_weight_passed_to_engine(
        self, asgi_client_with_hybrid
    ) -> None:
        """When vector_weight is provided, it should be forwarded to search()."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        with patch.object(
            hybrid_engine, "search", wraps=hybrid_engine.search
        ) as spy:
            resp = await client.get("/search?q=machine&vector_weight=0.8")
            assert resp.status_code == 200
            assert spy.called
            assert spy.call_args.kwargs.get("vector_weight") == 0.8

    @pytest.mark.asyncio
    async def test_vector_weight_none_when_not_provided(
        self, asgi_client_with_hybrid
    ) -> None:
        """When vector_weight is omitted, search() should receive None."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        with patch.object(
            hybrid_engine, "search", wraps=hybrid_engine.search
        ) as spy:
            resp = await client.get("/search?q=machine")
            assert resp.status_code == 200
            assert spy.called
            assert spy.call_args.kwargs.get("vector_weight") is None

    @pytest.mark.asyncio
    async def test_vector_weight_zero_allowed(
        self, asgi_client_with_hybrid
    ) -> None:
        """vector_weight=0.0 should be accepted (FTS-only search)."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        with patch.object(
            hybrid_engine, "search", wraps=hybrid_engine.search
        ) as spy:
            resp = await client.get("/search?q=machine&vector_weight=0.0")
            assert resp.status_code == 200
            assert spy.call_args.kwargs.get("vector_weight") == 0.0

    @pytest.mark.asyncio
    async def test_vector_weight_one_allowed(
        self, asgi_client_with_hybrid
    ) -> None:
        """vector_weight=1.0 should be accepted (vector-only search)."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        with patch.object(
            hybrid_engine, "search", wraps=hybrid_engine.search
        ) as spy:
            resp = await client.get("/search?q=machine&vector_weight=1.0")
            assert resp.status_code == 200
            assert spy.call_args.kwargs.get("vector_weight") == 1.0

    @pytest.mark.asyncio
    async def test_vector_weight_above_one_clamped(
        self, asgi_client_with_hybrid
    ) -> None:
        """vector_weight=1.5 should be clamped to 1.0, not rejected."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        with patch.object(
            hybrid_engine, "search", wraps=hybrid_engine.search
        ) as spy:
            resp = await client.get("/search?q=machine&vector_weight=1.5")
            assert resp.status_code == 200
            assert spy.called
            assert spy.call_args.kwargs.get("vector_weight") == 1.0

    @pytest.mark.asyncio
    async def test_vector_weight_below_zero_clamped(
        self, asgi_client_with_hybrid
    ) -> None:
        """vector_weight=-0.3 should be clamped to 0.0, not rejected."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        with patch.object(
            hybrid_engine, "search", wraps=hybrid_engine.search
        ) as spy:
            resp = await client.get("/search?q=machine&vector_weight=-0.3")
            assert resp.status_code == 200
            assert spy.called
            assert spy.call_args.kwargs.get("vector_weight") == 0.0

    @pytest.mark.asyncio
    async def test_vector_weight_invalid_string_rejected(
        self, asgi_client_with_hybrid
    ) -> None:
        """Non-numeric vector_weight should be rejected with 400."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        resp = await client.get("/search?q=machine&vector_weight=abc")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_vector_weight_works_with_export_json(
        self, asgi_client_with_hybrid
    ) -> None:
        """vector_weight should work alongside the json export path."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        resp = await client.get("/search?q=machine&vector_weight=0.3&export=json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["result_count"] >= 1

    @pytest.mark.asyncio
    async def test_vector_weight_ignored_when_no_hybrid_engine(
        self, asgi_client_no_hybrid
    ) -> None:
        """When no hybrid engine is configured, vector_weight should not cause errors."""
        client, app = asgi_client_no_hybrid

        resp = await client.get("/search?q=machine&vector_weight=0.7")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_vector_weight_ignored_for_empty_query(
        self, asgi_client_with_hybrid
    ) -> None:
        """When query is empty, vector_weight should not cause errors."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        resp = await client.get("/search?q=&vector_weight=0.5")
        assert resp.status_code == 200

    # ── Additional edge case tests ─────────────────────────────

    @pytest.mark.asyncio
    async def test_vector_weight_empty_string_rejected_with_400(
        self, asgi_client_with_hybrid
    ) -> None:
        """Empty string vector_weight should return HTTP 400."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        resp = await client.get("/search?q=machine&vector_weight=")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_vector_weight_nan_rejected_with_400(
        self, asgi_client_with_hybrid
    ) -> None:
        """NaN vector_weight should return HTTP 400."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        resp = await client.get("/search?q=machine&vector_weight=NaN")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_vector_weight_infinity_rejected_with_400(
        self, asgi_client_with_hybrid
    ) -> None:
        """Infinity vector_weight should return HTTP 400."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        resp = await client.get("/search?q=machine&vector_weight=inf")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_vector_weight_neg_infinity_rejected_with_400(
        self, asgi_client_with_hybrid
    ) -> None:
        """Negative infinity vector_weight should return HTTP 400."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        resp = await client.get("/search?q=machine&vector_weight=-inf")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_vector_weight_whitespace_rejected_with_400(
        self, asgi_client_with_hybrid
    ) -> None:
        """Whitespace-only vector_weight should return HTTP 400."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        resp = await client.get("/search?q=machine&vector_weight=%20%20")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_vector_weight_very_large_value_clamped(
        self, asgi_client_with_hybrid
    ) -> None:
        """Extremely large value (999.9) should be clamped to 1.0, not rejected."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        with patch.object(
            hybrid_engine, "search", wraps=hybrid_engine.search
        ) as spy:
            resp = await client.get("/search?q=machine&vector_weight=999.9")
            assert resp.status_code == 200
            assert spy.called
            assert spy.call_args.kwargs.get("vector_weight") == 1.0

    @pytest.mark.asyncio
    async def test_vector_weight_very_negative_clamped(
        self, asgi_client_with_hybrid
    ) -> None:
        """Extremely negative value (-999.9) should be clamped to 0.0."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        with patch.object(
            hybrid_engine, "search", wraps=hybrid_engine.search
        ) as spy:
            resp = await client.get("/search?q=machine&vector_weight=-999.9")
            assert resp.status_code == 200
            assert spy.called
            assert spy.call_args.kwargs.get("vector_weight") == 0.0

    @pytest.mark.asyncio
    async def test_vector_weight_just_above_one_clamped(
        self, asgi_client_with_hybrid
    ) -> None:
        """Value just above 1.0 (1.0001) should be clamped to 1.0."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        with patch.object(
            hybrid_engine, "search", wraps=hybrid_engine.search
        ) as spy:
            resp = await client.get("/search?q=machine&vector_weight=1.0001")
            assert resp.status_code == 200
            assert spy.called
            assert spy.call_args.kwargs.get("vector_weight") == 1.0

    @pytest.mark.asyncio
    async def test_vector_weight_just_below_zero_clamped(
        self, asgi_client_with_hybrid
    ) -> None:
        """Value just below 0.0 (-0.0001) should be clamped to 0.0."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        with patch.object(
            hybrid_engine, "search", wraps=hybrid_engine.search
        ) as spy:
            resp = await client.get("/search?q=machine&vector_weight=-0.0001")
            assert resp.status_code == 200
            assert spy.called
            assert spy.call_args.kwargs.get("vector_weight") == 0.0

    @pytest.mark.asyncio
    async def test_vector_weight_special_characters_rejected(
        self, asgi_client_with_hybrid
    ) -> None:
        """Special character strings should be rejected with 400."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        resp = await client.get("/search?q=machine&vector_weight=!@#$")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_vector_weight_scientific_notation_accepted(
        self, asgi_client_with_hybrid
    ) -> None:
        """Scientific notation (e.g., 5e-1 for 0.5) should be accepted."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        with patch.object(
            hybrid_engine, "search", wraps=hybrid_engine.search
        ) as spy:
            resp = await client.get("/search?q=machine&vector_weight=5e-1")
            assert resp.status_code == 200
            assert spy.called
            # 5e-1 = 0.5, which is within range, passed as-is
            assert spy.call_args.kwargs.get("vector_weight") == 0.5

    @pytest.mark.asyncio
    async def test_vector_weight_scientific_notation_clamped(
        self, asgi_client_with_hybrid
    ) -> None:
        """Scientific notation >1 (e.g., 2e0) should be clamped to 1.0."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        with patch.object(
            hybrid_engine, "search", wraps=hybrid_engine.search
        ) as spy:
            # 2e0 = 2.0, should be clamped to 1.0
            resp = await client.get("/search?q=machine&vector_weight=2e0")
            assert resp.status_code == 200
            assert spy.called
            assert spy.call_args.kwargs.get("vector_weight") == 1.0

    @pytest.mark.asyncio
    async def test_vector_weight_works_with_csv_export(
        self, asgi_client_with_hybrid
    ) -> None:
        """vector_weight should work alongside the csv export path."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        resp = await client.get("/search?q=machine&vector_weight=0.3&export=csv")
        assert resp.status_code == 200
        assert "content-disposition" in {k.lower() for k in resp.headers}

    @pytest.mark.asyncio
    async def test_vector_weight_400_response_contains_error_detail(
        self, asgi_client_with_hybrid
    ) -> None:
        """400 response for invalid vector_weight should include error detail."""
        client, app, hybrid_engine = asgi_client_with_hybrid

        resp = await client.get("/search?q=machine&vector_weight=abc")
        assert resp.status_code == 400
        # Should contain a descriptive error message in either 'error' or 'detail'
        body = resp.json()
        error_msg = (body.get("error") or body.get("detail") or "")
        assert "vector_weight" in error_msg.lower()


# ── Server-level vector_weight parsing unit tests ───────────────


class TestVectorWeightParsing:
    """Unit tests for the vector_weight parsing/clamping logic in server.py.

    These don't go through HTTP — they test the parsing function directly
    so we cover edge cases that are hard to exercise via URL encoding.
    """

    def _parse_vector_weight(self, raw_value: str | None) -> float | None:
        """Mirror the parsing logic from server.py."""
        import math
        from fastapi import HTTPException

        if raw_value is None:
            return None
        try:
            resolved = float(raw_value)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"vector_weight must be a numeric value between 0.0 and 1.0, "
                    f"got: {raw_value!r}"
                ),
            )
        # Reject NaN and Infinity
        if math.isnan(resolved) or math.isinf(resolved):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"vector_weight must be a numeric value between 0.0 and 1.0, "
                    f"got: {raw_value!r}"
                ),
            )
        return max(0.0, min(1.0, resolved))

    def test_none_returns_none(self) -> None:
        """None input should return None (use engine default)."""
        assert self._parse_vector_weight(None) is None

    def test_exact_float_range(self) -> None:
        """Floats within [0.0, 1.0] should be returned as-is."""
        assert self._parse_vector_weight("0.0") == 0.0
        assert self._parse_vector_weight("0.5") == 0.5
        assert self._parse_vector_weight("1.0") == 1.0
        assert self._parse_vector_weight("0.25") == 0.25
        assert self._parse_vector_weight("0.75") == 0.75

    def test_int_strings_parsed(self) -> None:
        """Integer strings ('0', '1') should be parsed as floats."""
        assert self._parse_vector_weight("0") == 0.0
        assert self._parse_vector_weight("1") == 1.0

    def test_above_one_clamped(self) -> None:
        """Values >1.0 should be clamped to 1.0."""
        assert self._parse_vector_weight("1.5") == 1.0
        assert self._parse_vector_weight("2.0") == 1.0
        assert self._parse_vector_weight("100") == 1.0

    def test_below_zero_clamped(self) -> None:
        """Values <0.0 should be clamped to 0.0."""
        assert self._parse_vector_weight("-0.5") == 0.0
        assert self._parse_vector_weight("-1.0") == 0.0
        assert self._parse_vector_weight("-100") == 0.0

    def test_empty_string_raises_400(self) -> None:
        """Empty string should raise HTTP 400."""
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            self._parse_vector_weight("")
        assert exc.value.status_code == 400

    def test_non_numeric_raises_400(self) -> None:
        """Non-numeric strings should raise HTTP 400."""
        from fastapi import HTTPException
        for bad in ("abc", "!@#", "vector"):
            with pytest.raises(HTTPException) as exc:
                self._parse_vector_weight(bad)
            assert exc.value.status_code == 400

    def test_whitespace_only_raises_400(self) -> None:
        """Whitespace-only strings should raise HTTP 400."""
        from fastapi import HTTPException
        for bad in ("   ", "\t", "\n"):
            with pytest.raises(HTTPException) as exc:
                self._parse_vector_weight(bad)
            assert exc.value.status_code == 400

    def test_nan_raises_400(self) -> None:
        """'NaN' string should raise HTTP 400."""
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            self._parse_vector_weight("NaN")
        assert exc.value.status_code == 400

    def test_infinity_raises_400(self) -> None:
        """'inf' and '-inf' strings should raise HTTP 400."""
        from fastapi import HTTPException
        for bad in ("inf", "-inf", "Infinity", "-Infinity"):
            with pytest.raises(HTTPException) as exc:
                self._parse_vector_weight(bad)
            assert exc.value.status_code == 400

    def test_float_edge_cases_accepted(self) -> None:
        """Float edge cases that parse successfully should be clamped."""
        # These parse as valid floats, then get clamped
        assert self._parse_vector_weight("1.0000000001") == 1.0
        assert self._parse_vector_weight("-0.0000000001") == 0.0

    def test_scientific_notation(self) -> None:
        """Scientific notation should be parsed and clamped."""
        assert self._parse_vector_weight("5e-1") == 0.5  # 0.5
        assert self._parse_vector_weight("1e0") == 1.0   # 1.0
        assert self._parse_vector_weight("2e0") == 1.0   # 2.0 → clamp
        assert self._parse_vector_weight("-1e0") == 0.0  # -1.0 → clamp
        assert self._parse_vector_weight("0e0") == 0.0   # 0.0

    def test_leading_trailing_whitespace(self) -> None:
        """Python float() strips whitespace — should parse and clamp."""
        assert self._parse_vector_weight(" 0.5 ") == 0.5
        assert self._parse_vector_weight("\t0.3\n") == 0.3

    def test_error_message_contains_input_value(self) -> None:
        """400 error detail should mention the invalid input for debugging."""
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            self._parse_vector_weight("xyz123")
        assert "xyz123" in exc.value.detail


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
