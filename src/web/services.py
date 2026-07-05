"""Export and summarization service helpers.

Contains search result export (CSV/JSON), document summary generation,
document type auto-detection, and the synchronous LLM adapter used by
the summarizer.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from typing import Optional

from fastapi.responses import PlainTextResponse, Response

from ..core.config import config

logger = logging.getLogger(__name__)






def _export_search_results(
    query: str, results: list[dict], fmt: str
) -> Response:
    """Build a CSV or JSON download response for search results.

    Args:
        query: The original search query.
        results: List of document dicts from fulltext_search.
        fmt: "csv" or "json".

    Returns:
        A ``Response`` with Content-Disposition for file download.
    """
    safe_q = "".join(
        c if c.isalnum() or c in "-_" else "_" for c in query
    )[:40] or "search"

    if fmt == "json":
        payload = {
            "query": query,
            "result_count": len(results),
            "results": [
                {
                    "id": r.get("id"),
                    "title": r.get("title", ""),
                    "path": r.get("path", ""),
                    "source": r.get("source_name", r.get("source_type", "")),
                    "ext": r.get("ext", ""),
                    "mime_type": r.get("mime_type", ""),
                    "status": r.get("status", ""),
                    "summary": r.get("summary", ""),
                    "snippet": (r.get("raw_preview", "") or "")[:300],
                    "rank": r.get("rank"),
                    "created_at": str(r.get("created_at", "")),
                }
                for r in results
            ],
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        return Response(
            content=body,
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{safe_q}_results.json"'
                )
            },
        )

    # CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "id",
            "title",
            "path",
            "source",
            "ext",
            "mime_type",
            "status",
            "rank",
            "summary",
            "snippet",
            "created_at",
        ]
    )
    for r in results:
        snippet = (r.get("raw_preview", "") or "")[:300]
        writer.writerow(
            [
                r.get("id", ""),
                r.get("title", ""),
                r.get("path", ""),
                r.get("source_name", r.get("source_type", "")),
                r.get("ext", ""),
                r.get("mime_type", ""),
                r.get("status", ""),
                f"{r.get('rank', 0):.4f}" if r.get("rank") is not None else "",
                r.get("summary", "") or "",
                snippet,
                str(r.get("created_at", "")),
            ]
        )
    body = output.getvalue()
    return PlainTextResponse(
        content=body,
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{safe_q}_results.csv"'
            )
        },
    )


def _export_documents_bulk(
    documents: list[dict],
    fmt: str,
    not_found_ids: list[int] | None = None,
    invalid_ids: list[str] | None = None,
) -> Response:
    """Build a CSV or JSON download response for bulk document export.

    Unlike ``_export_search_results`` which includes search rank and
    snippet, this exports full document metadata (no rank, full body
    preview up to 500 chars).

    Args:
        documents: List of document dicts from get_document().
        fmt: "csv" or "json".
        not_found_ids: IDs that were requested but not found in the DB.
        invalid_ids: IDs that failed validation (non-integer, etc.).

    Returns:
        A ``Response`` with Content-Disposition for file download.
    """
    not_found_ids = not_found_ids or []
    invalid_ids = invalid_ids or []

    if fmt == "json":
        payload = {
            "exported_count": len(documents),
            "not_found": not_found_ids,
            "not_found_count": len(not_found_ids),
            "invalid_ids": invalid_ids,
            "documents": [
                {
                    "id": d.get("id"),
                    "title": d.get("title", ""),
                    "path": d.get("path", ""),
                    "source": d.get("source_name", d.get("source_type", "")),
                    "ext": d.get("ext", ""),
                    "mime_type": d.get("mime_type", ""),
                    "status": d.get("status", ""),
                    "summary": d.get("summary", ""),
                    "body_preview": (d.get("body", "") or "")[:500],
                    "size": d.get("size", 0),
                    "created_at": str(d.get("created_at", "")),
                    "updated_at": str(d.get("updated_at", "")),
                }
                for d in documents
            ],
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        return Response(
            content=body,
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    'attachment; filename="documents_export.json"'
                )
            },
        )

    # CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "id",
            "title",
            "path",
            "source",
            "ext",
            "mime_type",
            "status",
            "summary",
            "body_preview",
            "size",
            "created_at",
            "updated_at",
        ]
    )
    for d in documents:
        writer.writerow(
            [
                d.get("id", ""),
                d.get("title", ""),
                d.get("path", ""),
                d.get("source_name", d.get("source_type", "")),
                d.get("ext", ""),
                d.get("mime_type", ""),
                d.get("status", ""),
                d.get("summary", "") or "",
                (d.get("body", "") or "")[:500],
                d.get("size", 0),
                str(d.get("created_at", "")),
                str(d.get("updated_at", "")),
            ]
        )
    body = output.getvalue()
    return PlainTextResponse(
        content=body,
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                'attachment; filename="documents_export.csv"'
            )
        },
    )


async def _generate_summary_for_doc(doc: dict) -> Optional[str]:
    """Generate a summary for a document using the Summarizer.

    Uses the configured LLMClient when available (wrapped in a sync
    adapter), falling back to extractive summarization otherwise.
    The sync Summarizer is run in a thread to avoid blocking the
    async event loop.

    Args:
        doc: Document dict with at least 'title' and 'body' keys.

    Returns:
        The summary string, or None if summarization failed.
    """
    import asyncio

    from ..core.summarizer import Summarizer
    from ..core.llm_client import LLMClient

    # Build a sync LLM adapter if an LLM is configured
    llm_client = None
    try:
        llm_config = config.llm
        client = LLMClient(llm_config)
        if client.is_configured:
            llm_client = _SyncLLMAdapter(client)
    except Exception:
        pass

    summarizer = Summarizer(llm_client=llm_client)
    title = doc.get("title", "Untitled")
    body = doc.get("body", "") or ""

    # Run the sync summarizer in a thread to avoid blocking
    result = await asyncio.to_thread(summarizer.summarize, title, body)
    return result


async def _detect_document_type(
    title: str, body: str, ext: str = ""
) -> tuple[str, str]:
    """Detect document type using LLM or keyword fallback.

    Uses the configured LLMClient when available, falling back to
    keyword-based heuristic.

    Args:
        title: Document title.
        body: Document body text.
        ext: File extension (e.g. '.pdf').

    Returns:
        Tuple of (type_key, detection_method) where detection_method
        is 'llm' or 'keyword'.
    """
    from ..core.detector import DocumentDetector
    from ..core.llm_client import LLMClient

    # Build LLM client if configured
    llm_client = None
    try:
        llm_config = config.llm
        client = LLMClient(llm_config)
        if client.is_configured:
            llm_client = client
    except Exception:
        pass

    detector = DocumentDetector(
        llm_client=llm_client,
        max_body_chars=config.auto_detection.max_body_chars,
    )

    method = detector.detection_method
    type_key = await detector.detect(title, body or "", ext=ext)

    return type_key, method


class _SyncLLMAdapter:
    """Synchronous wrapper around the async LLMClient.

    The Summarizer expects a client with a sync ``chat(prompt, max_tokens)``
    method. This adapter runs the async LLM call in a separate thread with
    its own event loop, bridging the sync/async boundary safely.

    This adapter is always called from within ``asyncio.to_thread()`` (in
    ``_generate_summary_for_doc``), so it runs in a worker thread where no
    event loop is active. It also handles the edge case of being called
    directly from within a running event loop by spawning a thread.
    """

    def __init__(self, async_client) -> None:
        self._async_client = async_client

    def chat(self, prompt: str, max_tokens: int = 150) -> str:
        """Call the LLM synchronously by running the async call in a thread.

        Uses a dedicated thread with a fresh event loop to avoid conflicts
        with any running event loop in the caller's context.
        """
        import asyncio
        import threading

        try:
            result_box: list = [None]
            error_box: list = [None]

            def _run() -> None:
                loop = asyncio.new_event_loop()
                try:
                    result_box[0] = loop.run_until_complete(
                        self._async_generate(prompt, max_tokens)
                    )
                except Exception as e:
                    error_box[0] = e
                finally:
                    loop.close()

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            t.join()

            if error_box[0] is not None:
                raise error_box[0]
            return result_box[0] or ""
        except Exception as e:
            print(f"[_SyncLLMAdapter] LLM call failed: {e}")
            return ""

    async def _async_generate(self, prompt: str, max_tokens: int) -> str:
        """Generate text using the async LLMClient's generate method."""
        # LLMClient.generate expects (question, context_chunks, max_tokens)
        # We pass the prompt as the question with an empty context list.
        return await self._async_client.generate(
            prompt, context_chunks=[], max_tokens=max_tokens
        )



