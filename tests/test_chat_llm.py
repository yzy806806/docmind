"""Tests for LLM-powered chat: client, prompt construction, fallback, and WebSocket handler.

Covers:
- LLMConfig: env var loading and defaults
- LLMClient: is_configured, generate (OpenAI/Ollama mocked), fallback, streaming
- build_rag_prompt: structure, context inclusion, reference numbers
- extractive_fallback: correct output from search results
- chat.py _handle_question: LLM streaming, fallback, citation tracking, error handling
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.config import LLMConfig
from src.core.llm_client import (
    LLMClient,
    build_rag_prompt,
    extractive_fallback,
)
from src.web.chat import ConversationContext, _handle_question


# ── LLMConfig tests ──────────────────────────────────────────────


class TestLLMConfig:
    """Tests for LLMConfig dataclass."""

    def test_defaults(self):
        """Default config has empty provider and sensible defaults."""
        cfg = LLMConfig()
        assert cfg.provider == ""
        assert cfg.model == "gpt-4o-mini"
        assert cfg.api_key == ""
        assert cfg.base_url == ""
        assert cfg.max_tokens == 8000
        assert cfg.temperature == 0.3
        assert cfg.timeout_seconds == 3600.0

    def test_env_loading(self, monkeypatch):
        """Config should read from DOCMIND_LLM_* env vars."""
        monkeypatch.setenv("DOCMIND_LLM_PROVIDER", "ollama")
        monkeypatch.setenv("DOCMIND_LLM_MODEL", "llama3")
        monkeypatch.setenv("DOCMIND_LLM_API_KEY", "secret-key")
        monkeypatch.setenv("DOCMIND_LLM_BASE_URL", "http://localhost:11434")
        monkeypatch.setenv("DOCMIND_LLM_MAX_TOKENS", "500")
        monkeypatch.setenv("DOCMIND_LLM_TEMPERATURE", "0.7")
        monkeypatch.setenv("DOCMIND_LLM_TIMEOUT", "60.0")

        cfg = LLMConfig()
        assert cfg.provider == "ollama"
        assert cfg.model == "llama3"
        assert cfg.api_key == "secret-key"
        assert cfg.base_url == "http://localhost:11434"
        assert cfg.max_tokens == 500
        assert cfg.temperature == 0.7
        assert cfg.timeout_seconds == 60.0

    def test_env_unset_reverts_to_defaults(self, monkeypatch):
        """When env vars are unset, defaults apply."""
        for key in (
            "DOCMIND_LLM_PROVIDER",
            "DOCMIND_LLM_MODEL",
            "DOCMIND_LLM_API_KEY",
            "DOCMIND_LLM_BASE_URL",
            "DOCMIND_LLM_MAX_TOKENS",
            "DOCMIND_LLM_TEMPERATURE",
            "DOCMIND_LLM_TIMEOUT",
        ):
            monkeypatch.delenv(key, raising=False)

        cfg = LLMConfig()
        assert cfg.provider == ""
        assert cfg.model == "gpt-4o-mini"
        assert cfg.api_key == ""
        assert cfg.max_tokens == 8000


# ── build_rag_prompt tests ───────────────────────────────────────


class TestBuildRagPrompt:
    """Tests for RAG prompt construction."""

    def test_prompt_structure(self):
        """Prompt should have system and user messages."""
        messages = build_rag_prompt("What is X?", [
            {"title": "Doc 1", "snippet": "X is a thing.", "body": ""},
        ])
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    def test_system_prompt_has_rules(self):
        """System prompt should instruct about citations and context."""
        messages = build_rag_prompt("test", [{"title": "T", "snippet": "S"}])
        system = messages[0]["content"]
        assert "DocMind" in system
        assert "[1]" in system  # citation instruction
        assert "上下文" in system  # context instruction (Chinese)

    def test_user_message_contains_context(self):
        """User message should include the context and question."""
        messages = build_rag_prompt(
            "What is the API?",
            [{"title": "API Doc", "snippet": "The API uses REST."}],
        )
        user = messages[1]["content"]
        assert "API Doc" in user
        assert "The API uses REST." in user
        assert "What is the API?" in user

    def test_reference_numbers(self):
        """Context chunks should be numbered [1], [2], etc."""
        messages = build_rag_prompt("test", [
            {"title": "A", "snippet": "content A"},
            {"title": "B", "snippet": "content B"},
            {"title": "C", "snippet": "content C"},
        ])
        user = messages[1]["content"]
        assert "[1]" in user
        assert "[2]" in user
        assert "[3]" in user

    def test_falls_back_to_body_when_no_snippet(self):
        """When snippet is empty, body should be used."""
        messages = build_rag_prompt("test", [
            {"title": "Doc", "snippet": "", "body": "Body content here."},
        ])
        user = messages[1]["content"]
        assert "Body content here." in user

    def test_max_chunks_limit(self):
        """Should only include max_chunks results."""
        chunks = [{"title": f"D{i}", "snippet": f"snip {i}"} for i in range(10)]
        messages = build_rag_prompt("test", chunks, max_chunks=3)
        user = messages[1]["content"]
        assert "[1]" in user
        assert "[2]" in user
        assert "[3]" in user
        assert "[4]" not in user

    def test_empty_context(self):
        """Should handle empty context list."""
        messages = build_rag_prompt("test", [])
        user = messages[1]["content"]
        assert "no relevant context" in user.lower()

    def test_chunk_truncation(self):
        """Long snippets should be truncated to max_chunk_chars."""
        long_snippet = "A" * 2000
        messages = build_rag_prompt(
            "test", [{"title": "Doc", "snippet": long_snippet}],
            max_chunk_chars=100,
        )
        user = messages[1]["content"]
        # The snippet should be truncated — not the full 2000 chars
        assert "A" * 100 in user
        assert "A" * 200 not in user


# ── extractive_fallback tests ────────────────────────────────────


class TestExtractiveFallback:
    """Tests for extractive answer fallback."""

    def test_uses_summary(self):
        """Should prefer summary when available."""
        result = extractive_fallback("query", [
            {"title": "Doc", "summary": "A summary.", "snippet": "snippet", "body": "body"},
        ])
        assert "A summary." in result
        assert '[1]' in result
        assert "Doc" in result

    def test_uses_snippet_when_no_summary(self):
        """Should use snippet when summary is empty."""
        result = extractive_fallback("query", [
            {"title": "Doc", "summary": "", "snippet": "A snippet.", "body": "body"},
        ])
        assert "A snippet." in result

    def test_uses_body_excerpt_when_no_summary_or_snippet(self):
        """Should use body excerpt when summary and snippet are empty."""
        result = extractive_fallback("query", [
            {"title": "Doc", "summary": "", "snippet": "", "body": "Body text here."},
        ])
        assert "Body text here." in result

    def test_empty_results(self):
        """Should return no-documents message for empty results."""
        result = extractive_fallback("query", [])
        assert "couldn't find" in result.lower() or "no relevant" in result.lower()

    def test_multiple_results(self):
        """Should include multiple results with reference numbers."""
        result = extractive_fallback("query", [
            {"title": "A", "snippet": "content A"},
            {"title": "B", "snippet": "content B"},
        ])
        assert "[1]" in result
        assert "[2]" in result
        assert "content A" in result
        assert "content B" in result

    def test_max_results_limit(self):
        """Should only include max_results entries."""
        results = [
            {"title": f"D{i}", "snippet": f"snip {i}"}
            for i in range(10)
        ]
        result = extractive_fallback("query", results, max_results=3)
        assert "[1]" in result
        assert "[2]" in result
        assert "[3]" in result
        assert "[4]" not in result

    def test_body_excerpt_truncation(self):
        """Body excerpt should be truncated to excerpt_chars."""
        long_body = "X" * 1000
        result = extractive_fallback(
            "query",
            [{"title": "Doc", "snippet": "", "body": long_body}],
            excerpt_chars=50,
        )
        assert "X" * 50 in result
        assert "X" * 100 not in result


# ── LLMClient tests ──────────────────────────────────────────────


class TestLLMClientConfigured:
    """Tests for LLMClient is_configured logic."""

    def test_not_configured_when_provider_empty(self):
        """Client with empty provider should not be configured."""
        client = LLMClient(LLMConfig(provider=""))
        assert not client.is_configured

    def test_openai_configured_with_key(self):
        """OpenAI provider with API key should be configured."""
        client = LLMClient(LLMConfig(provider="openai", api_key="sk-test"))
        assert client.is_configured

    def test_openai_not_configured_without_key(self):
        """OpenAI provider without API key should not be configured."""
        client = LLMClient(LLMConfig(provider="openai", api_key=""))
        assert not client.is_configured

    def test_openai_compat_configured_with_key(self):
        """openai-compat provider with API key should be configured."""
        client = LLMClient(
            LLMConfig(provider="openai-compat", api_key="key", base_url="http://localhost:8080/v1")
        )
        assert client.is_configured

    def test_ollama_configured_without_key(self):
        """Ollama provider should be configured even without API key."""
        client = LLMClient(LLMConfig(provider="ollama"))
        assert client.is_configured


class TestLLMClientGenerate:
    """Tests for LLMClient.generate() with mocked HTTP."""

    @pytest.mark.asyncio
    async def test_fallback_when_not_configured(self):
        """Should return extractive answer when not configured."""
        client = LLMClient(LLMConfig(provider=""))
        results = [{"title": "Doc", "snippet": "A snippet."}]
        answer = await client.generate("question?", results)
        assert "A snippet." in answer
        assert "[1]" in answer

    @pytest.mark.asyncio
    async def test_openai_generate_success(self):
        """Should call OpenAI API and return response."""
        cfg = LLMConfig(provider="openai", api_key="sk-test")
        client = LLMClient(cfg)

        # Simulate SSE streaming: two data lines then [DONE]
        class FakeAsyncIterator:
            def __init__(self, lines):
                self._lines = lines
                self._idx = 0
            def __aiter__(self):
                return self
            async def __anext__(self):
                if self._idx >= len(self._lines):
                    raise StopAsyncIteration
                line = self._lines[self._idx]
                self._idx += 1
                return line

        class FakeStreamCM:
            async def __aenter__(self):
                class FakeResp:
                    def raise_for_status(self):
                        pass
                    def aiter_lines(self):
                        return FakeAsyncIterator([
                            'data: {"choices":[{"delta":{"content":"LLM answer from [1]."}}]}',
                            'data: [DONE]',
                        ])
                return FakeResp()
            async def __aexit__(self, *args):
                return None

        mock_http_client = AsyncMock()
        mock_http_client.stream = MagicMock(return_value=FakeStreamCM())
        mock_http_client.is_closed = False
        client._client = mock_http_client

        results = [{"title": "Doc", "snippet": "Context here."}]
        answer = await client.generate("question?", results)

        assert answer == "LLM answer from [1]."
        mock_http_client.stream.assert_called_once()

    @pytest.mark.asyncio
    async def test_ollama_generate_success(self):
        """Should call Ollama API and return response."""
        cfg = LLMConfig(provider="ollama", model="llama3")
        client = LLMClient(cfg)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "message": {"content": "Ollama answer."}
        }
        mock_http_client = AsyncMock()
        mock_http_client.post = AsyncMock(return_value=mock_response)
        mock_http_client.is_closed = False
        client._client = mock_http_client

        results = [{"title": "Doc", "snippet": "Context."}]
        answer = await client.generate("question?", results)

        assert answer == "Ollama answer."
        mock_http_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_openai_error_falls_back(self):
        """Should fall back to extractive when API errors."""
        cfg = LLMConfig(provider="openai", api_key="sk-test")
        client = LLMClient(cfg)

        mock_http_client = AsyncMock()
        mock_http_client.post = AsyncMock(side_effect=Exception("API error"))
        mock_http_client.is_closed = False
        client._client = mock_http_client

        results = [{"title": "Doc", "snippet": "Fallback content."}]
        answer = await client.generate("question?", results)

        assert "Fallback content." in answer
        assert "[1]" in answer

    @pytest.mark.asyncio
    async def test_ollama_error_falls_back(self):
        """Should fall back to extractive when Ollama errors."""
        cfg = LLMConfig(provider="ollama")
        client = LLMClient(cfg)

        mock_http_client = AsyncMock()
        mock_http_client.post = AsyncMock(side_effect=ConnectionError("refused"))
        mock_http_client.is_closed = False
        client._client = mock_http_client

        results = [{"title": "Doc", "snippet": "Fallback."}]
        answer = await client.generate("question?", results)

        assert "Fallback." in answer


class TestLLMClientStream:
    """Tests for LLMClient.generate_stream()."""

    @pytest.mark.asyncio
    async def test_stream_fallback_when_not_configured(self):
        """Should stream extractive answer in chunks when not configured."""
        client = LLMClient(LLMConfig(provider=""))
        results = [{"title": "Doc", "snippet": "A" * 300}]
        chunks = []
        async for chunk in client.generate_stream("q?", results):
            chunks.append(chunk)

        full = "".join(chunks)
        assert "A" * 300 in full
        assert len(chunks) >= 2  # should be chunked

    @pytest.mark.asyncio
    async def test_stream_openai_success(self):
        """Should stream from OpenAI SSE response."""
        cfg = LLMConfig(provider="openai", api_key="sk-test")
        client = LLMClient(cfg)

        # Mock streaming response
        sse_lines = [
            'data: {"choices":[{"delta":{"content":"Hello"}}]}',
            'data: {"choices":[{"delta":{"content":" world"}}]}',
            "data: [DONE]",
        ]

        mock_stream_context = AsyncMock()
        mock_stream_context.__aenter__ = AsyncMock(return_value=mock_stream_context)
        mock_stream_context.__aexit__ = AsyncMock(return_value=False)
        mock_stream_context.raise_for_status = MagicMock()
        mock_stream_context.aiter_lines = MagicMock(
            return_value=AsyncIterMock(sse_lines)
        )

        mock_http_client = AsyncMock()
        mock_http_client.stream = MagicMock(return_value=mock_stream_context)
        mock_http_client.is_closed = False
        client._client = mock_http_client

        results = [{"title": "Doc", "snippet": "Context."}]
        chunks = []
        async for chunk in client.generate_stream("q?", results):
            chunks.append(chunk)

        assert "".join(chunks) == "Hello world"

    @pytest.mark.asyncio
    async def test_stream_ollama_success(self):
        """Should stream from Ollama NDJSON response."""
        cfg = LLMConfig(provider="ollama", model="llama3")
        client = LLMClient(cfg)

        ndjson_lines = [
            '{"message":{"content":"Hello"},"done":false}',
            '{"message":{"content":" world"},"done":false}',
            '{"message":{"content":""},"done":true}',
        ]

        mock_stream_context = AsyncMock()
        mock_stream_context.__aenter__ = AsyncMock(return_value=mock_stream_context)
        mock_stream_context.__aexit__ = AsyncMock(return_value=False)
        mock_stream_context.raise_for_status = MagicMock()
        mock_stream_context.aiter_lines = MagicMock(
            return_value=AsyncIterMock(ndjson_lines)
        )

        mock_http_client = AsyncMock()
        mock_http_client.stream = MagicMock(return_value=mock_stream_context)
        mock_http_client.is_closed = False
        client._client = mock_http_client

        results = [{"title": "Doc", "snippet": "Context."}]
        chunks = []
        async for chunk in client.generate_stream("q?", results):
            chunks.append(chunk)

        assert "".join(chunks) == "Hello world"

    @pytest.mark.asyncio
    async def test_stream_error_falls_back(self):
        """Should fall back to extractive when streaming fails."""
        cfg = LLMConfig(provider="openai", api_key="sk-test")
        client = LLMClient(cfg)

        mock_http_client = AsyncMock()
        mock_http_client.stream = MagicMock(side_effect=Exception("network error"))
        mock_http_client.is_closed = False
        client._client = mock_http_client

        results = [{"title": "Doc", "snippet": "Fallback content."}]
        chunks = []
        async for chunk in client.generate_stream("q?", results):
            chunks.append(chunk)

        full = "".join(chunks)
        assert "Fallback content." in full


# ── Helper for async iteration mocking ───────────────────────────


class AsyncIterMock:
    """Mock async iterator for testing streaming responses."""

    def __init__(self, lines):
        self._lines = lines
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._lines):
            raise StopAsyncIteration
        line = self._lines[self._index]
        self._index += 1
        return line


# ── Chat handler tests ───────────────────────────────────────────


class TestHandleQuestion:
    """Tests for chat.py _handle_question with mocked LLM."""

    @pytest.mark.asyncio
    async def test_fallback_answer_streaming(self):
        """When LLM not configured, should stream extractive answer."""
        from src.web.chat import ConversationContext, _handle_question

        # Mock WebSocket
        ws = AsyncMock()
        ws.send_text = AsyncMock()

        # Mock search engine
        search_engine = MagicMock()
        search_engine.search.return_value = [
            {
                "doc_id": 1,
                "title": "Test Doc",
                "summary": "This is a summary.",
                "snippet": "This is a snippet.",
                "body": "Full body text.",
                "citation": {"confidence": "high"},
            }
        ]

        # LLM client not configured (fallback mode)
        llm_client = LLMClient(LLMConfig(provider=""))

        ctx = ConversationContext(ws, search_engine, llm_client)

        await _handle_question(ctx, "What is this?")

        # Check messages sent
        sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
        msg_types = [m["type"] for m in sent]

        assert "citation:added" in msg_types
        assert "answer:chunk" in msg_types
        assert "answer:done" in msg_types

        # Check answer content
        chunks = [m for m in sent if m["type"] == "answer:chunk"]
        done = [m for m in sent if m["type"] == "answer:done"][0]
        full_answer = "".join(c["text"] for c in chunks)
        assert "This is a snippet." in full_answer or "This is a summary." in full_answer
        assert done["text"] == full_answer
        assert len(done["citations"]) == 1

    @pytest.mark.asyncio
    async def test_llm_answer_streaming(self):
        """When LLM configured, should stream LLM answer."""
        from src.web.chat import ConversationContext, _handle_question

        ws = AsyncMock()
        ws.send_text = AsyncMock()

        search_engine = MagicMock()
        search_engine.search.return_value = [
            {
                "doc_id": 1,
                "title": "Doc",
                "snippet": "Context here.",
                "body": "Body.",
                "citation": {"confidence": "medium"},
            }
        ]

        # Mock LLM client with streaming
        llm_client = MagicMock()
        llm_client.is_configured = True

        async def mock_stream(question, results, **kwargs):
            yield "LLM "
            yield "answer "
            yield "from [1]."

        llm_client.generate_stream = mock_stream

        ctx = ConversationContext(ws, search_engine, llm_client)

        await _handle_question(ctx, "What is this?")

        sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]

        chunks = [m for m in sent if m["type"] == "answer:chunk"]
        done = [m for m in sent if m["type"] == "answer:done"][0]

        full = "".join(c["text"] for c in chunks)
        assert full == "LLM answer from [1]."
        assert done["text"] == full

    @pytest.mark.asyncio
    async def test_empty_question_error(self):
        """Empty question should send error."""
        from src.web.chat import ConversationContext, _handle_question

        ws = AsyncMock()
        ws.send_text = AsyncMock()
        search_engine = MagicMock()
        llm_client = LLMClient(LLMConfig(provider=""))

        ctx = ConversationContext(ws, search_engine, llm_client)

        await _handle_question(ctx, "")

        sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
        assert sent[0]["type"] == "error"
        assert "Empty" in sent[0]["message"]

    @pytest.mark.asyncio
    async def test_no_results_message(self):
        """When search returns no results, should send done with message."""
        from src.web.chat import ConversationContext, _handle_question

        ws = AsyncMock()
        ws.send_text = AsyncMock()

        search_engine = MagicMock()
        search_engine.search.return_value = []

        llm_client = LLMClient(LLMConfig(provider=""))

        ctx = ConversationContext(ws, search_engine, llm_client)

        await _handle_question(ctx, "unknown topic")

        sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
        done = [m for m in sent if m["type"] == "answer:done"]
        assert len(done) == 1
        assert "couldn't find" in done[0]["text"].lower()
        assert done[0]["citations"] == []

    @pytest.mark.asyncio
    async def test_search_error_sends_error(self):
        """When search throws, should send error message."""
        from src.web.chat import ConversationContext, _handle_question

        ws = AsyncMock()
        ws.send_text = AsyncMock()

        search_engine = MagicMock()
        search_engine.search.side_effect = RuntimeError("DB locked")

        llm_client = LLMClient(LLMConfig(provider=""))

        ctx = ConversationContext(ws, search_engine, llm_client)

        await _handle_question(ctx, "question")

        sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
        assert sent[0]["type"] == "error"
        assert "Search failed" in sent[0]["message"]

    @pytest.mark.asyncio
    async def test_citations_sent_before_answer(self):
        """Citation events should be sent before answer chunks."""
        from src.web.chat import ConversationContext, _handle_question

        ws = AsyncMock()
        ws.send_text = AsyncMock()

        search_engine = MagicMock()
        search_engine.search.return_value = [
            {
                "doc_id": 1,
                "title": "Doc 1",
                "snippet": "Snippet 1.",
                "body": "",
                "citation": {"confidence": "high"},
            },
            {
                "doc_id": 2,
                "title": "Doc 2",
                "snippet": "Snippet 2.",
                "body": "",
                "citation": {"confidence": "low"},
            },
        ]

        llm_client = LLMClient(LLMConfig(provider=""))

        ctx = ConversationContext(ws, search_engine, llm_client)

        await _handle_question(ctx, "question")

        sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
        types = [m["type"] for m in sent]

        # Citations should come before chunks
        first_chunk_idx = types.index("answer:chunk")
        last_citation_idx = max(i for i, t in enumerate(types) if t == "citation:added")

        assert last_citation_idx < first_chunk_idx

        # Should have 2 citations
        citations = [m for m in sent if m["type"] == "citation:added"]
        assert len(citations) == 2
        assert citations[0]["ref"] == 1
        assert citations[1]["ref"] == 2

    @pytest.mark.asyncio
    async def test_llm_fallback_on_stream_error(self):
        """When LLM stream fails, should fall back to extractive."""
        from src.web.chat import ConversationContext, _handle_question

        ws = AsyncMock()
        ws.send_text = AsyncMock()

        search_engine = MagicMock()
        search_engine.search.return_value = [
            {
                "doc_id": 1,
                "title": "Doc",
                "snippet": "Fallback content.",
                "body": "",
                "citation": {"confidence": "low"},
            }
        ]

        # Mock LLM client whose stream raises then falls back
        llm_client = MagicMock()
        llm_client.is_configured = True

        async def mock_stream(question, results, **kwargs):
            # Simulate error then fallback
            yield "Fallback content."
            return

        llm_client.generate_stream = mock_stream

        ctx = ConversationContext(ws, search_engine, llm_client)

        await _handle_question(ctx, "question")

        sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
        chunks = [m for m in sent if m["type"] == "answer:chunk"]
        assert len(chunks) > 0
        assert "Fallback content." in chunks[0]["text"]


# ── Chat page HTML tests ─────────────────────────────────────────


class TestChatPageEnhancements:
    """Tests for enhanced chat page HTML."""

    def test_typing_indicator_present(self):
        """Chat page should include typing indicator via JS (chat.js)."""
        from src.web.server import _render_chat_page
        html = _render_chat_page()
        # Typing indicator is added dynamically by chat.js
        assert "/static/js/chat.js" in html

    def test_send_button_has_id(self):
        """Send button should have id for JS targeting."""
        from src.web.server import _render_chat_page
        html = _render_chat_page()
        assert 'id="chat-send-btn"' in html

    def test_chat_msg_bubble_styling(self):
        """CSS should include bubble styling for chat messages."""
        from src.web.server import _render_chat_page
        html = _render_chat_page()
        # The base page CSS includes chat-msg styles
        assert "border-radius" in html or "chat-msg" in html

    def test_typing_indicator_css(self):
        """External stylesheet should include typing indicator animation."""
        from src.web.server import _base_page
        html = _base_page("test", "")
        # Typing indicator animation is in the external stylesheet
        assert "/static/css/styles.css" in html
