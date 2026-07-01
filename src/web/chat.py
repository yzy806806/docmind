"""DocMind WebSocket chat handler with citation tracking and streaming.

Provides a real-time Q&A interface where users can ask questions about their
documents and receive answers with inline citation references.

Protocol:
    Client → Server: {"type": "question", "text": "What is the pipeline design?"}
    Server → Client: {"type": "citation:added", "doc_id": 42, "title": "...", "snippet": "..."}
    Server → Client: {"type": "answer:chunk", "text": "The pipeline..."}
    Server → Client: {"type": "answer:done", "citations": [...]}
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect

from ..core.search import SearchEngine
from ..core.search_backend import SearchBackend, create_backend

logger = logging.getLogger(__name__)

# ── Conversation context ─────────────────────────────────────────


class ConversationContext:
    """Holds per-connection state for a chat session."""

    def __init__(self, websocket: WebSocket, search_engine: SearchEngine):
        self.websocket = websocket
        self.search_engine = search_engine
        self.citation_count: int = 0
        self.collected_citations: list[dict[str, Any]] = []

    def add_citation(self, doc_id: int, title: str, snippet: str, confidence: str) -> int:
        """Record a citation and return its reference number."""
        self.citation_count += 1
        ref = self.citation_count
        citation = {
            "ref": ref,
            "doc_id": doc_id,
            "title": title,
            "snippet": snippet,
            "confidence": confidence,
        }
        self.collected_citations.append(citation)
        return ref


# ── WebSocket handler ────────────────────────────────────────────


async def handle_chat(
    websocket: WebSocket,
    *,
    search_db_path: str = "data/docmind_fts.db",
) -> None:
    """Handle a WebSocket chat connection for real-time document Q&A.

    Lifecycle:
    1. Accept connection, send welcome
    2. Loop: receive question → search → stream answer with citations
    3. On disconnect: clean up
    """
    await websocket.accept()

    # Initialize search backend
    backend = create_backend("sqlite", db_path=search_db_path)
    search_engine = SearchEngine(backend=backend)

    ctx = ConversationContext(websocket, search_engine)

    await _send(ctx, {
        "type": "connected",
        "message": "DocMind chat ready. Ask questions about your documents.",
    })

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await _send(ctx, {
                    "type": "error",
                    "message": "Invalid JSON",
                })
                continue

            msg_type = message.get("type", "")

            if msg_type == "question":
                await _handle_question(ctx, message.get("text", ""))
            elif msg_type == "ping":
                await _send(ctx, {"type": "pong"})
            else:
                await _send(ctx, {
                    "type": "error",
                    "message": f"Unknown message type: {msg_type}",
                })

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception:
        logger.exception("WebSocket handler error")
    finally:
        backend.close()


async def _handle_question(ctx: ConversationContext, text: str) -> None:
    """Process a question: search, cite, and answer."""
    if not text or not text.strip():
        await _send(ctx, {"type": "error", "message": "Empty question"})
        return

    logger.info("Chat question: %s", text[:100])

    # Search for relevant documents
    try:
        results = ctx.search_engine.search(
            text,
            top_k=5,
            include_citations=True,
        )
    except Exception as e:
        logger.exception("Search failed during chat")
        await _send(ctx, {"type": "error", "message": f"Search failed: {e}"})
        return

    if not results:
        await _send(ctx, {
            "type": "answer:done",
            "text": "I couldn't find any relevant documents for your question.",
            "citations": [],
        })
        return

    # Reset citations for this question
    ctx.citation_count = 0
    ctx.collected_citations = []

    # Send citation events for each source
    for result in results:
        citation = result.get("citation", {})
        ref = ctx.add_citation(
            doc_id=result["doc_id"],
            title=result.get("title", "Untitled"),
            snippet=result.get("snippet", ""),
            confidence=citation.get("confidence", "low"),
        )

        await _send(ctx, {
            "type": "citation:added",
            "ref": ref,
            "doc_id": result["doc_id"],
            "title": result.get("title", ""),
            "snippet": result.get("snippet", ""),
            "confidence": citation.get("confidence", "low"),
        })

    # Build an answer from the top results
    answer_parts: list[str] = []
    for i, result in enumerate(results[:3]):
        ref = i + 1
        title = result.get("title", "Untitled")
        summary = result.get("summary", "")
        snippet = result.get("snippet", "")

        if summary:
            answer_parts.append(f"From [{ref}] \"{title}\": {summary}")
        elif snippet:
            answer_parts.append(f"From [{ref}] \"{title}\": {snippet}")
        else:
            body = result.get("body", "")
            excerpt = body[:300]
            answer_parts.append(f"From [{ref}] \"{title}\": {excerpt}")

    answer_text = "\n\n".join(answer_parts) if answer_parts else "No relevant content found."

    # Stream answer in chunks (simulated — real streaming would use LLM)
    chunk_size = 200
    for i in range(0, len(answer_text), chunk_size):
        chunk = answer_text[i:i + chunk_size]
        await _send(ctx, {"type": "answer:chunk", "text": chunk})
        await asyncio.sleep(0.01)  # small delay for streaming effect

    # Send final answer with full citation list
    await _send(ctx, {
        "type": "answer:done",
        "text": answer_text,
        "citations": ctx.collected_citations,
    })


async def _send(ctx: ConversationContext, message: dict[str, Any]) -> None:
    """Send a JSON message over the WebSocket."""
    try:
        await ctx.websocket.send_text(json.dumps(message, default=str))
    except Exception:
        pass  # Connection may be closed
