"""Service facade encapsulating DocMind business operations.

Provides a synchronous API for ingest, search, list, show, and summarize
that the CLI, Hermes plugin, and REST endpoints can share.

All methods accept validated inputs and return structured results.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..core.config import config
from ..core.extractor import Extractor
from ..core.models import DocumentStatusResponse
from ..core.search import SearchEngine
from ..core.search_backend import (
    SearchBackend,
    SQLiteSearchBackend,
    create_backend,
)
from ..core.storage import StorageConnector
from ..core.summarizer import Summarizer
from ..errors import (
    DocMindError,
    DocumentNotFoundError,
    IngestError,
    ValidationError,
)
from ..logging_config import get_logger, log_duration
from ..validation import (
    validate_directory_path,
    validate_doc_id,
    validate_document_path,
    validate_search_query,
    validate_source_name,
)

logger = get_logger(__name__)


class DocMindService:
    """Main service facade for DocMind operations.

    Wires together the extractor, indexer (via StorageConnector),
    search backend, search engine, and summarizer into a cohesive API.
    """

    def __init__(
        self,
        *,
        index_db_path: str = "data/docmind.db",
        search_db_path: str = "data/docmind_fts.db",
        llm_client: Any = None,
    ):
        """Initialize the service with database paths and optional LLM client.

        Args:
            index_db_path: Path to the SQLite index database (Indexer).
            search_db_path: Path to the SQLite FTS5 search database.
            llm_client: Optional LLM client for summarization and ranking.
        """
        # Import Indexer here to avoid circular imports
        from ..core.indexer import Indexer

        self._indexer = Indexer(index_db_path)
        self._search_backend: SearchBackend = create_backend(
            "sqlite", db_path=search_db_path
        )
        self._search_engine = SearchEngine(
            backend=self._search_backend, llm_client=llm_client
        )
        self._storage = StorageConnector(self._indexer)
        self._summarizer = Summarizer(llm_client=llm_client)
        self._llm_client = llm_client

    # ── Ingest ─────────────────────────────────────────────────

    def ingest_path(self, path: str, *, source_name: str = "cli") -> dict[str, Any]:
        """Ingest a file or directory into the knowledge base.

        Args:
            path: File or directory path to ingest.
            source_name: Logical source name for tracking.

        Returns:
            Dict with ``count`` (new/updated documents) and ``path``.
        """
        abs_path = Path(path).resolve()

        if abs_path.is_dir():
            validated = validate_directory_path(str(abs_path))
            with log_duration(logger, "ingest_directory", path=path):
                count = self._storage.scan_directory(
                    str(validated), source_name=source_name
                )
                # Sync search backend with indexer
                self._sync_search_backend()
        elif abs_path.is_file():
            validated = validate_document_path(
                str(abs_path), base_dir=abs_path.parent
            )
            ext = validated.suffix.lower()
            if ext not in Extractor.SUPPORTED:
                raise ValidationError(
                    f"Unsupported file type: {ext}. "
                    f"Supported: {sorted(Extractor.SUPPORTED)}"
                )
            with log_duration(logger, "ingest_file", path=path):
                count = self._storage.scan_directory(
                    str(validated.parent), source_name=source_name
                )
                self._sync_search_backend()
        else:
            raise ValidationError(f"Path does not exist: {path}")

        logger.info("Ingested %d documents from %s", count, path)
        return {"count": count, "path": str(abs_path)}

    def _sync_search_backend(self) -> None:
        """Sync all indexed documents from Indexer into SearchBackend.

        Reads all documents from the Indexer and indexes them in the
        SearchBackend for full-text search.
        """
        docs = self._indexer.list_by_path("", limit=10000)
        for doc in docs:
            doc_id = doc["id"]
            path = doc.get("path", "")
            title = doc.get("title", "")
            summary = doc.get("summary")
            body = doc.get("body", "")
            self._search_backend.index_document(
                doc_id=doc_id,
                path=path,
                title=title,
                summary=summary,
                body=body,
            )

    # ── Search ─────────────────────────────────────────────────

    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        include_citations: bool = True,
    ) -> list[dict[str, Any]]:
        """Search the knowledge base.

        Args:
            query: Search query string.
            top_k: Maximum number of results to return.
            include_citations: Whether to include dual-hash citations.

        Returns:
            List of result dicts with keys: doc_id, path, title, summary,
            snippet, rank, and optionally citation.
        """
        validated_query = validate_search_query(query)
        with log_duration(logger, "search", query=validated_query):
            results = self._search_engine.search(
                validated_query,
                top_k=top_k,
                include_citations=include_citations,
            )
        return results

    # ── List ───────────────────────────────────────────────────

    def list_documents(
        self,
        *,
        source: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List indexed documents, optionally filtered by source.

        Args:
            source: Filter by source name (e.g., 'local', 'webdav').
            limit: Maximum number of documents to return.

        Returns:
            List of document dicts.
        """
        if source is not None:
            validate_source_name(source)

        # Use indexer's list capability
        # list_by_path with '' as prefix lists all
        docs = self._indexer.list_by_path("", limit=limit)

        if source:
            docs = [
                d for d in docs
                if d.get("source_name", d.get("source_type", "")) == source
            ]

        return docs

    # ── Show ───────────────────────────────────────────────────

    def get_document(self, doc_id: int | str) -> dict[str, Any]:
        """Get full details of a single document.

        Args:
            doc_id: Document ID as int or string.

        Returns:
            Document dict with all fields.

        Raises:
            DocumentNotFoundError: If no document with that ID exists.
        """
        validated_id = validate_doc_id(doc_id)
        doc = self._indexer.get_document_by_id(validated_id)

        if doc is None:
            raise DocumentNotFoundError(validated_id)

        return doc

    # ── Summarize ──────────────────────────────────────────────

    def summarize_document(
        self,
        doc_id: int | str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Generate or retrieve a summary for a document.

        Args:
            doc_id: Document ID.
            force: If True, regenerate even if a summary exists.

        Returns:
            Dict with ``doc_id``, ``title``, ``summary``.
        """
        validated_id = validate_doc_id(doc_id)
        doc = self._indexer.get_document_by_id(validated_id)

        if doc is None:
            raise DocumentNotFoundError(validated_id)

        # Return existing summary if available and not forcing
        existing_summary = doc.get("summary")
        if existing_summary and not force:
            return {
                "doc_id": validated_id,
                "title": doc.get("title", ""),
                "summary": existing_summary,
                "cached": True,
            }

        title = doc.get("title", "")
        body = doc.get("body", "")

        with log_duration(logger, "summarize", doc_id=validated_id):
            summary = self._summarizer.summarize(title, body)

        if summary:
            self._indexer.update_summary(validated_id, summary)

        return {
            "doc_id": validated_id,
            "title": title,
            "summary": summary or "",
            "cached": False,
        }

    # ── Stats ──────────────────────────────────────────────────

    def get_stats(self) -> dict[str, Any]:
        """Return knowledge base statistics."""
        stats = self._indexer.stats()
        return stats

    # ── Cleanup ───────────────────────────────────────────────

    def close(self) -> None:
        """Release resources."""
        self._search_backend.close()
        self._indexer.close()


# ── Module-level singleton ────────────────────────────────────

_service: Optional[DocMindService] = None


def get_service(
    *,
    index_db_path: str = "data/docmind.db",
    search_db_path: str = "data/docmind_fts.db",
    llm_client: Any = None,
) -> DocMindService:
    """Get or create the module-level service singleton."""
    global _service
    if _service is None:
        _service = DocMindService(
            index_db_path=index_db_path,
            search_db_path=search_db_path,
            llm_client=llm_client,
        )
    return _service
