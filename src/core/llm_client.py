"""LLM client for RAG-based answer generation.

Supports OpenAI-compatible APIs (including self-hosted vLLM, LM Studio, etc.)
and Ollama as providers. When no provider is configured, falls back to an
extractive answer built from search result snippets — the same behaviour the
chat handler had before LLM integration.

Key design decisions:
  * Uses httpx.AsyncClient for non-blocking API calls (httpx is already a
    project dependency).
  * generate() is async and returns a single string — the WebSocket handler
    is responsible for chunking and streaming it to the client.
  * All API errors are caught; on failure the extractive fallback is used so
    the chat never breaks from the user's perspective.
  * The RAG prompt is assembled from a system instruction, the retrieved
    context chunks (with reference numbers), and the user question.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Optional

import httpx

from .config import LLMConfig

logger = logging.getLogger(__name__)

# ── Prompt construction ──────────────────────────────────────────


SYSTEM_PROMPT = (
    "你是 DocMind 文档知识库助手，根据提供的文档内容回答问题。请遵守：\n"
    "1. 仅根据上下文中的信息回答，不要编造。\n"
    "2. 如果上下文中没有答案，请说\"我不知道\"。\n"
    "3. 使用 [1]、[2] 等引用来源编号。\n"
    "4. 用中文简洁回答。\n"
)


def build_rag_prompt(
    question: str,
    context_chunks: list[dict[str, Any]],
    *,
    max_chunks: int = 5,
    max_chunk_chars: int = 1200,
) -> list[dict[str, str]]:
    """Build chat-style messages for the LLM from retrieved context.

    Args:
        question: The user's question.
        context_chunks: Search results with at least 'snippet' or 'body'
            and 'title' keys.
        max_chunks: Maximum number of chunks to include.
        max_chunk_chars: Truncate each chunk to this many characters.

    Returns:
        List of {"role": ..., "content": ...} message dicts.
    """
    parts: list[str] = []
    for i, chunk in enumerate(context_chunks[:max_chunks], 1):
        title = chunk.get("title", "Untitled")
        snippet = chunk.get("snippet", "") or ""
        body = chunk.get("body", "") or ""
        # Prefer snippet (already excerpted by search); fall back to body
        text = snippet if snippet else body[:max_chunk_chars]
        if len(text) > max_chunk_chars:
            text = text[:max_chunk_chars]
        parts.append(f"[{i}] (Source: {title})\n{text}")

    context_text = "\n\n".join(parts) if parts else "(no relevant context found)"

    user_msg = (
        f"Answer the following question using only the document context below.\n\n"
        f"Document Context:\n{context_text}\n\n"
        f"Question: {question}\n\n"
        f"Answer (cite sources as [1], [2], etc.):"
    )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]


def extractive_fallback(
    question: str,
    results: list[dict[str, Any]],
    *,
    max_results: int = 3,
    excerpt_chars: int = 300,
) -> str:
    """Build a simple extractive answer from search results.

    This is the pre-LLM behaviour: concatenate snippets/summaries from the
    top results with reference markers. Used when no LLM is configured or
    when the LLM call fails.
    """
    if not results:
        return "I couldn't find any relevant documents for your question."

    parts: list[str] = []
    for i, result in enumerate(results[:max_results]):
        ref = i + 1
        title = result.get("title", "Untitled")
        summary = result.get("summary", "") or ""
        snippet = result.get("snippet", "") or ""
        body = result.get("body", "") or ""

        if summary:
            parts.append(f'From [{ref}] "{title}": {summary}')
        elif snippet:
            parts.append(f'From [{ref}] "{title}": {snippet}')
        elif body:
            parts.append(f'From [{ref}] "{title}": {body[:excerpt_chars]}')

    return "\n\n".join(parts) if parts else "No relevant content found."


# ── LLM Client ───────────────────────────────────────────────────


class LLMClient:
    """Async LLM client supporting OpenAI-compatible APIs and Ollama.

    Args:
        config: An LLMConfig instance. If provider is empty, the client
            operates in fallback mode (no API calls).
    """

    def __init__(self, config: Optional[LLMConfig] = None) -> None:
        self.config = config or LLMConfig()
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def is_configured(self) -> bool:
        """True when a provider and (for non-ollama) API key are set."""
        p = self.config.provider
        if p == "ollama":
            return True  # Ollama doesn't require an API key
        if p in ("openai", "openai-compat"):
            return bool(self.config.api_key)
        return False

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.timeout_seconds),
            )
        return self._client

    async def generate(
        self,
        question: str,
        context_chunks: list[dict[str, Any]],
        *,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate an answer from the question and retrieved context.

        Falls back to extractive answer if LLM is not configured or fails.

        Args:
            question: The user's question.
            context_chunks: Search results to use as RAG context.
            max_tokens: Override config.max_tokens if provided.

        Returns:
            Generated answer string.
        """
        if not self.is_configured:
            return extractive_fallback(question, context_chunks)

        messages = build_rag_prompt(question, context_chunks)
        tokens = max_tokens or self.config.max_tokens

        try:
            if self.config.provider == "ollama":
                return await self._call_ollama(messages, tokens)
            else:
                return await self._call_openai(messages, tokens)
        except Exception:
            logger.exception(
                "LLM generate failed (provider=%s), using extractive fallback",
                self.config.provider,
            )
            return extractive_fallback(question, context_chunks)

    async def generate_stream(
        self,
        question: str,
        context_chunks: list[dict[str, Any]],
        *,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """Stream the LLM answer token-by-token.

        Falls back to yielding the extractive answer in chunks if LLM is
        not configured or streaming fails.

        Yields:
            String chunks of the answer.
        """
        if not self.is_configured:
            # Stream the extractive answer in chunks
            answer = extractive_fallback(question, context_chunks)
            chunk_size = 200
            for i in range(0, len(answer), chunk_size):
                yield answer[i : i + chunk_size]
            return

        messages = build_rag_prompt(question, context_chunks)
        tokens = max_tokens or self.config.max_tokens

        try:
            if self.config.provider == "ollama":
                async for chunk in self._stream_ollama(messages, tokens):
                    yield chunk
            else:
                async for chunk in self._stream_openai(messages, tokens):
                    yield chunk
        except Exception:
            logger.exception(
                "LLM stream failed (provider=%s), using extractive fallback",
                self.config.provider,
            )
            answer = extractive_fallback(question, context_chunks)
            chunk_size = 200
            for i in range(0, len(answer), chunk_size):
                yield answer[i : i + chunk_size]

    # ── OpenAI-compatible ────────────────────────────────────────

    def _openai_url(self) -> str:
        base = self.config.base_url or "https://api.openai.com/v1"
        base = base.rstrip("/")
        return f"{base}/chat/completions"

    def _openai_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    async def _call_openai(
        self, messages: list[dict[str, str]], max_tokens: int
    ) -> str:
        """OpenAI-compatible call using streaming to avoid gateway timeouts."""
        client = await self._get_client()
        payload = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": self.config.temperature,
            "stream": True,
        }
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        async with client.stream(
            "POST",
            self._openai_url(),
            json=payload,
            headers=self._openai_headers(),
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    c = delta.get("content", "")
                    r = delta.get("reasoning_content", "")
                    if c:
                        content_parts.append(c)
                    if r:
                        reasoning_parts.append(r)
                except (json.JSONDecodeError, IndexError, KeyError):
                    continue
        content = "".join(content_parts)
        if not content:
            content = "".join(reasoning_parts)
        return content

    async def _stream_openai(
        self, messages: list[dict[str, str]], max_tokens: int
    ) -> AsyncIterator[str]:
        """Streaming OpenAI-compatible call using SSE."""
        client = await self._get_client()
        payload = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": self.config.temperature,
            "stream": True,
        }
        async with client.stream(
            "POST",
            self._openai_url(),
            json=payload,
            headers=self._openai_headers(),
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]  # strip "data: " prefix
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if not content:
                        # Reasoning model fallback
                        content = delta.get("reasoning_content", "")
                    if content:
                        yield content
                except (json.JSONDecodeError, IndexError, KeyError):
                    continue

    # ── Ollama ───────────────────────────────────────────────────

    def _ollama_url(self) -> str:
        base = self.config.base_url or "http://localhost:11434"
        base = base.rstrip("/")
        return f"{base}/api/chat"

    async def _call_ollama(
        self, messages: list[dict[str, str]], max_tokens: int
    ) -> str:
        """Non-streaming Ollama call (Ollama uses num_predict, not max_tokens)."""
        client = await self._get_client()
        payload = {
            "model": self.config.model,
            "messages": messages,
            "stream": False,
            "options": {
                "num_predict": max_tokens,
                "temperature": self.config.temperature,
            },
        }
        resp = await client.post(
            self._ollama_url(),
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("message", {}).get("content", "")

    async def _stream_ollama(
        self, messages: list[dict[str, str]], max_tokens: int
    ) -> AsyncIterator[str]:
        """Streaming Ollama call (NDJSON, one JSON object per line)."""
        client = await self._get_client()
        payload = {
            "model": self.config.model,
            "messages": messages,
            "stream": True,
            "options": {
                "num_predict": max_tokens,
                "temperature": self.config.temperature,
            },
        }
        async with client.stream(
            "POST",
            self._ollama_url(),
            json=payload,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                    content = chunk.get("message", {}).get("content", "")
                    if content:
                        yield content
                    if chunk.get("done"):
                        break
                except json.JSONDecodeError:
                    continue

    async def close(self) -> None:
        """Close the underlying httpx client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
