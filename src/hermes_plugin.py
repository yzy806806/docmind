"""Hermes Agent plugin — register kb_* tools for DocMind knowledge base.

This module registers synchronous tool functions that Hermes Agent can call
to search, list, read, and ingest documents from the DocMind knowledge base.

Tools registered:
- kb_search(query: str, top_k: int = 5) -> list[dict]
- kb_list(source: str = "") -> list[dict]
- kb_read(doc_id: int, chunk_limit: int = 5000) -> dict
- kb_ingest(path: str) -> dict
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .cli.services import get_service
from .errors import DocMindError
from .logging_config import get_logger, traced
from .validation import (
    validate_doc_id,
    validate_directory_path,
    validate_search_query,
    validate_source_name,
)

logger = get_logger("docmind.hermes")

# ── Tool Handlers ────────────────────────────────────────────────


def kb_search(query: str, top_k: int = 5) -> dict[str, Any]:
    """Search the DocMind knowledge base with dual-hash citations.

    Args:
        query: Natural language search query.
        top_k: Maximum number of results to return (default 5, max 20).

    Returns:
        Dict with ``results`` (list of result dicts) and ``total`` (count).
        Each result includes: doc_id, path, title, summary, snippet, rank,
        and citation (with confidence tier).

    Example:
        >>> kb_search("machine learning pipeline")
        {
            "results": [
                {
                    "doc_id": 42,
                    "title": "ML Pipeline Design",
                    "snippet": "...",
                    "citation": {"confidence": "exact_match", ...}
                }
            ],
            "total": 1
        }
    """
    with traced():
        try:
            validated_query = validate_search_query(query)
            top_k = max(1, min(top_k, 20))
        except DocMindError as e:
            return {"error": e.code, "message": e.message, "results": [], "total": 0}

        logger.info("kb_search query=%r top_k=%d", validated_query, top_k)

        try:
            svc = get_service()
            results = svc.search(validated_query, top_k=top_k, include_citations=True)
            return {
                "results": results,
                "total": len(results),
            }
        except Exception as e:
            logger.exception("kb_search failed")
            return {
                "error": "SEARCH_ERROR",
                "message": str(e),
                "results": [],
                "total": 0,
            }


def kb_list(source: str = "") -> dict[str, Any]:
    """List documents in the knowledge base, optionally filtered by source.

    Args:
        source: Filter by source name (e.g., 'local', 'webdav', 'cli').
                Leave empty to list all documents.

    Returns:
        Dict with ``documents`` (list of document dicts) and ``total``.

    Example:
        >>> kb_list(source="local")
        {
            "documents": [
                {"id": 1, "title": "README.md", "source": "local", ...}
            ],
            "total": 1
        }
    """
    with traced():
        try:
            if source:
                validate_source_name(source)
        except DocMindError as e:
            return {"error": e.code, "message": e.message, "documents": [], "total": 0}

        logger.info("kb_list source=%r", source or "all")

        try:
            svc = get_service()
            documents = svc.list_documents(source=source if source else None, limit=200)
            return {
                "documents": documents,
                "total": len(documents),
            }
        except Exception as e:
            logger.exception("kb_list failed")
            return {
                "error": "LIST_ERROR",
                "message": str(e),
                "documents": [],
                "total": 0,
            }


def kb_read(doc_id: int, chunk_limit: int = 5000) -> dict[str, Any]:
    """Read the full text of a document from the knowledge base.

    Args:
        doc_id: The document's integer ID.
        chunk_limit: Maximum characters to return (default 5000, max 50000).
                     The full body is truncated to this limit.

    Returns:
        Dict with document fields: id, title, path, body (truncated),
        summary, source_type, ext, mime_type, status, metadata.

    Example:
        >>> kb_read(42)
        {
            "id": 42,
            "title": "ML Pipeline Design",
            "body": "Full document text up to 5000 chars...",
            "summary": "A document about ML pipelines.",
            ...
        }
    """
    with traced():
        try:
            validated_id = validate_doc_id(doc_id)
            chunk_limit = max(100, min(chunk_limit, 50000))
        except DocMindError as e:
            return {"error": e.code, "message": e.message}

        logger.info("kb_read doc_id=%d chunk_limit=%d", validated_id, chunk_limit)

        try:
            svc = get_service()
            doc = svc.get_document(validated_id)
            # Truncate body to chunk_limit
            if len(doc.get("body", "")) > chunk_limit:
                doc["body"] = doc["body"][:chunk_limit] + "…"
                doc["truncated"] = True
            else:
                doc["truncated"] = False
            return doc
        except DocMindError as e:
            return {"error": e.code, "message": e.message}
        except Exception as e:
            logger.exception("kb_read failed")
            return {"error": "READ_ERROR", "message": str(e)}


def kb_ingest(path: str) -> dict[str, Any]:
    """Trigger indexing of a file or directory.

    Args:
        path: Absolute or relative path to a file or directory.
              Directories are scanned recursively for supported files.

    Returns:
        Dict with ``count`` (new/updated documents) and ``path``.

    Example:
        >>> kb_ingest("/data/docs/")
        {"count": 15, "path": "/data/docs", "status": "ok"}
    """
    with traced():
        logger.info("kb_ingest path=%r", path)

        try:
            svc = get_service()
            result = svc.ingest_path(path)
            result["status"] = "ok"
            return result
        except DocMindError as e:
            return {"error": e.code, "message": e.message, "count": 0, "path": path}
        except Exception as e:
            logger.exception("kb_ingest failed")
            return {"error": "INGEST_ERROR", "message": str(e), "count": 0, "path": path}


# ── Registry ─────────────────────────────────────────────────────

# Tool definitions for Hermes Agent registration.
# These describe each tool's signature and behavior.
TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "kb_search": {
        "function": kb_search,
        "description": (
            "Search the DocMind knowledge base. Returns ranked results with "
            "dual-hash citations showing source confidence."
        ),
        "parameters": {
            "query": {
                "type": "str",
                "description": "Natural language search query (2-1000 chars).",
                "required": True,
            },
            "top_k": {
                "type": "int",
                "description": "Max results to return (1-20, default 5).",
                "required": False,
                "default": 5,
            },
        },
    },
    "kb_list": {
        "function": kb_list,
        "description": (
            "List documents in the DocMind knowledge base. "
            "Optionally filter by source name."
        ),
        "parameters": {
            "source": {
                "type": "str",
                "description": "Source name filter (e.g., 'local', 'webdav'). Empty for all.",
                "required": False,
                "default": "",
            },
        },
    },
    "kb_read": {
        "function": kb_read,
        "description": (
            "Read the full text of a document from the knowledge base by its ID."
        ),
        "parameters": {
            "doc_id": {
                "type": "int",
                "description": "The document's integer ID.",
                "required": True,
            },
            "chunk_limit": {
                "type": "int",
                "description": "Max characters to return (100-50000, default 5000).",
                "required": False,
                "default": 5000,
            },
        },
    },
    "kb_ingest": {
        "function": kb_ingest,
        "description": (
            "Index a file or directory into the DocMind knowledge base. "
            "Scans directories recursively for supported file types."
        ),
        "parameters": {
            "path": {
                "type": "str",
                "description": "File or directory path to index.",
                "required": True,
            },
        },
    },
}


def get_registered_tools() -> dict[str, dict[str, Any]]:
    """Return the registry of kb_* tools, ready for Hermes Agent.

    This is the integration point — call this from Hermes to register tools.
    """
    return TOOL_REGISTRY


# ── Module-level shortcut ────────────────────────────────────────

# Convenience exports for Hermes Agent to import directly.
# Agent code can do: from docmind.hermes_plugin import kb_search, kb_list, ...
__all__ = [
    "kb_search",
    "kb_list",
    "kb_read",
    "kb_ingest",
    "get_registered_tools",
    "TOOL_REGISTRY",
]
