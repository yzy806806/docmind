"""DocMind web server — FastAPI application.

Exposes the full document processing and knowledge base API:

REST:
- GET  /                         Dashboard with stats
- GET  /search?q=                Search page with results + citations
- GET  /documents                List all documents (tag cloud, tag filter)
- GET  /documents?tag=xxx        Filter documents by tag
- GET  /documents/<id>           Document detail with summary and tags
- POST /documents/<id>/tags      Add a tag to a document
- POST /documents/<id>/tags/<tag>/delete  Remove a tag
- GET  /jobs                     Job processing status page (filterable, auto-refresh)
- GET  /jobs/<job_id>            Job detail page with error and document link
- GET  /analytics                Full analytics page with charts and date range
- GET  /api/v1/analytics         Analytics data as JSON
- GET  /upload                   Upload form (drag-and-drop multi-file)
- POST /upload                   File upload form (single or batch via files[])
- POST /api/v1/documents/submit  Programmatic document submission
- POST /api/v1/documents/batch   Batch document submission
- GET  /api/v1/documents         List documents (JSON, pagination, collection_id filter)
- GET  /api/v1/documents/{id}/status  Document processing status
- GET  /api/v1/jobs/{id}         Background job status
- POST /api/v1/collections       Create a collection
- GET  /api/v1/collections       List all collections (flat)
- GET  /api/v1/collections/tree  List collections as nested tree
- GET  /api/v1/collections/{id}  Get a single collection
- PUT  /api/v1/collections/{id}  Update a collection
- DELETE /api/v1/collections/{id}  Delete a collection
- POST /api/v1/documents/{doc_id}/collection  Assign document to collection
- DELETE /api/v1/documents/{doc_id}/collection  Remove document from collection
- GET  /api/v1/collections/{id}/documents  List documents in a collection

WebSocket:
- WS   /chat                     Real-time Q&A with citation tracking
"""

from __future__ import annotations

import json
import logging
import math
import mimetypes
import uuid
import csv
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Security,
    UploadFile,
    WebSocket,
    status,
)
from fastapi.openapi.utils import get_openapi
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles

from ..core.config import config
from ..core.db_sqlite import Database
from ..core.job_queue import JobQueue
from ..core.models import (
    BatchDocumentItem,
    BatchSubmissionRequest,
    DocumentCreate,
    DocumentStatus,
    DocumentStatusResponse,
    ErrorResponse,
    JobRecord,
    JobState,
    JobStatusResponse,
    SubmissionAccepted,
)
from ..errors import (
    DocMindError,
    DocumentNotFoundError,
    IngestError,
    ValidationError,
)
from ..validation import validate_doc_id, validate_search_query
from .services import _export_search_results, _generate_summary_for_doc, _SyncLLMAdapter
from .chat import handle_chat

logger = logging.getLogger(__name__)

from .rendering import (
    _base_page,
    _render_template,
    _render_dashboard,
    _render_analytics_page,
    _render_search_form,
    _render_search_results,
    _render_documents_list,
    _render_documents_table_partial,
    _render_document_detail,
    _render_upload_form,
    _render_upload_success,
    _render_upload_batch,
    _render_pagination,
    _render_delete_success,
    _render_chat_page,
    _mask_api_key,
    _render_settings_page,
    _render_settings_redirect,
    _render_login_page,
    _reload_llm_config_from_db,
    _render_jobs_page,
    _render_job_detail,
    _render_error,
    _svg_line_chart,
    _svg_bar_chart,
    _svg_pie_chart,
    _escape,
    _fmt_date,
    _fmt_size,
)
from .auth import (
    auth_middleware,
    apply_auth_settings_from_db,
    ensure_session_secret,
    generate_api_key,
    check_password,
    login_response,
    logout_response,
    unauthorized_response,
    auth_enabled,
)


_db: Optional[Database] = None
_queue: Optional[JobQueue] = None

# ── OpenAPI security scheme ────────────────────────────────────
# The X-API-Key header used by auth.py for programmatic API access.
# Applying this as a Security() dependency on API v1 routes makes the
# "Authorize" button appear in Swagger UI and documents the security
# requirement in the generated OpenAPI schema.
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, scheme_name="ApiKeyAuth")


def get_db() -> Database:
    if _db is None:
        raise RuntimeError("Database not initialized")
    return _db


def get_queue() -> JobQueue:
    if _queue is None:
        raise RuntimeError("JobQueue not initialized")
    return _queue


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application lifecycle — startup and shutdown."""
    global _db, _queue
    _db = Database(
        db_path=config.database.path,
        min_size=config.database.pool_min_size,
        max_size=config.database.pool_max_size,
    )
    await _db.connect()

    # ── Hydrate auth config from DB settings ───────────────────────
    # Operators can enable auth via env vars (DOCMIND_AUTH_*) or via the
    # settings page. DB-stored values take precedence so that toggling
    # auth from the UI persists across restarts.
    try:
        stored = await _db.get_all_settings()
        apply_auth_settings_from_db(stored)
    except Exception:
        logger.exception("Failed to hydrate auth config from DB — auth may be misconfigured")
    # Ensure env-var-supplied api_key still wins if no DB value is set.
    if not config.auth.api_key and config.auth.enabled:
        logger.warning("Auth is enabled but no API key is configured — generating one")
        config.auth.api_key = generate_api_key()

    _queue = JobQueue(_db)
    logger.info(
        "DocMind server started on %s:%d (auth %s)",
        config.server.host,
        config.server.port,
        "enabled" if config.auth.enabled else "disabled",
    )
    yield
    if _db:
        await _db.disconnect()
        _db = None
    logger.info("DocMind server shut down")


# ── App factory ────────────────────────────────────────────────


def create_app() -> FastAPI:
    app = FastAPI(
        title="DocMind Document Knowledge Base",
        version="0.1.0",
        description=(
            "AI-powered enterprise document knowledge base.\n\n"
            "## Authentication\n\n"
            "When authentication is enabled, all API endpoints require either:\n"
            "- A valid `X-API-Key` header (for programmatic/API access), or\n"
            "- A valid session cookie (set by `POST /login`)\n\n"
            "Use the **Authorize** button below to set your API key for testing.\n\n"
            "## Interactive Docs\n\n"
            "- **Swagger UI**: `GET /docs` \n"
            "- **ReDoc**: `GET /redoc`\n"
            "- **OpenAPI JSON**: `GET /openapi.json`"
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # ── Custom OpenAPI schema with security scheme ────────────
    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema

        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            servers=[
                {"url": "/api/v1", "description": "API v1 base path"},
                {"url": "http://localhost:8080", "description": "Local development"},
            ],
        )

        # The ApiKeyAuth security scheme is auto-generated by the
        # APIKeyHeader(scheme_name="ApiKeyAuth") dependency on API routes.
        # Apply it globally so every endpoint shows the lock icon in
        # Swagger UI, even routes that don't explicitly use Security().
        schema["security"] = [{"ApiKeyAuth": []}]

        # Add tag descriptions for better docs organization
        schema["tags"] = [
            {
                "name": "documents",
                "description": "Document submission, status, and management operations.",
            },
            {
                "name": "collections",
                "description": "Collection (folder) CRUD, tree structure, and document assignment.",
            },
            {
                "name": "jobs",
                "description": "Background processing job status and tracking.",
            },
            {
                "name": "chat",
                "description": "Chat session management, history, and export.",
            },
        ]

        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi

    # ── Auth middleware ─────────────────────────────────────
    # Registered before any route so it runs on every request. When
    # config.auth.enabled is False it is a no-op pass-through.
    @app.middleware("http")
    async def _auth_mw(request: Request, call_next):
        return await auth_middleware(request, call_next)

    # ── Error handlers ──────────────────────────────────────

    @app.exception_handler(DocMindError)
    async def docmind_error_handler(
        request: Request, exc: DocMindError
    ) -> JSONResponse:
        status_code = exc.status_code if hasattr(exc, "status_code") else 500
        return JSONResponse(
            status_code=status_code,
            content={
                "error": exc.code if hasattr(exc, "code") else "ERROR",
                "message": exc.message if hasattr(exc, "message") else str(exc),
                "detail": exc.detail if hasattr(exc, "detail") else None,
                "trace_id": str(uuid.uuid4()),
            },
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(
        request: Request, exc: HTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                error=exc.detail or "HTTP error",
                trace_id=str(uuid.uuid4()),
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        trace_id = str(uuid.uuid4())
        logger.exception("Unhandled error [trace_id=%s]", trace_id)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=ErrorResponse(
                error="Internal server error",
                detail=str(exc) if config.debug else None,
                trace_id=trace_id,
            ).model_dump(),
        )

    # ── Static files (JS, CSS, images) ──────────────────────────
    # Serves shared vanilla-JS modules from src/web/static/ under the
    # /static URL prefix. Templates reference them as <script src="/static/js/...">
    # This is the "islands" convention: small self-contained JS files
    # that pages pull in via <script src> rather than inline <script> blocks.
    _static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

    # ── Web UI Routes ───────────────────────────────────────

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard():
        """Dashboard page with knowledge base statistics and analytics."""
        db = get_db()
        try:
            stats = await db.get_stats()
            recent = await db.list_documents(limit=10)
            # Analytics data for the enhanced dashboard
            doc_growth = await db.get_document_growth(days=30)
            tag_dist = await db.get_tag_distribution()
            storage = await db.get_storage_stats()
            search_stats = await db.get_search_stats(days=30)
            popular_queries = await db.get_popular_queries(limit=5)
            search_trend = await db.get_search_trend(days=30)
            chat_activity = await db.get_chat_activity(days=30)
            job_stats = await db.get_job_stats()
        except Exception:
            stats = {
                "total": 0, "pending": 0, "indexed": 0,
                "summarized": 0, "active_jobs": 0,
            }
            recent = []
            doc_growth = []
            tag_dist = []
            storage = {"total_size": 0, "by_type": {}, "avg_doc_size": 0, "doc_count": 0}
            search_stats = {"total_searches": 0, "avg_results": 0.0, "unique_queries": 0}
            popular_queries = []
            search_trend = []
            chat_activity = []
            job_stats = {"by_state": {}, "total": 0, "success_rate": 0.0,
                         "avg_processing_time_seconds": 0.0, "recent_failures": []}

        html = _render_dashboard(stats, recent, doc_growth, tag_dist, storage,
                                 search_stats, popular_queries, search_trend,
                                 chat_activity, job_stats)
        return HTMLResponse(content=html)

    @app.get("/analytics", response_class=HTMLResponse, include_in_schema=False)
    async def analytics_page(days: int = Query(default=30, ge=1, le=365)):
        """Full analytics page with charts and date range selector."""
        db = get_db()
        try:
            stats = await db.get_stats()
            doc_growth = await db.get_document_growth(days=days)
            tag_dist = await db.get_tag_distribution()
            storage = await db.get_storage_stats()
            search_stats = await db.get_search_stats(days=days)
            popular_queries = await db.get_popular_queries(limit=20)
            search_trend = await db.get_search_trend(days=days)
            chat_activity = await db.get_chat_activity(days=days)
            job_stats = await db.get_job_stats()
        except Exception:
            stats = {"total": 0, "pending": 0, "indexed": 0,
                     "summarized": 0, "error": 0, "active_jobs": 0}
            doc_growth = []
            tag_dist = []
            storage = {"total_size": 0, "by_type": {}, "avg_doc_size": 0, "doc_count": 0}
            search_stats = {"total_searches": 0, "avg_results": 0.0, "unique_queries": 0}
            popular_queries = []
            search_trend = []
            chat_activity = []
            job_stats = {"by_state": {}, "total": 0, "success_rate": 0.0,
                         "avg_processing_time_seconds": 0.0, "recent_failures": []}

        html = _render_analytics_page(
            stats, doc_growth, tag_dist, storage, search_stats,
            popular_queries, search_trend, chat_activity, job_stats, days,
        )
        return HTMLResponse(content=html)

    @app.get("/api/v1/analytics", include_in_schema=False)
    async def analytics_api(days: int = Query(default=30, ge=1, le=365)):
        """Return analytics data as JSON."""
        db = get_db()
        try:
            stats = await db.get_stats()
            doc_growth = await db.get_document_growth(days=days)
            tag_dist = await db.get_tag_distribution()
            storage = await db.get_storage_stats()
            search_stats = await db.get_search_stats(days=days)
            popular_queries = await db.get_popular_queries(limit=20)
            search_trend = await db.get_search_trend(days=days)
            chat_activity = await db.get_chat_activity(days=days)
            job_stats = await db.get_job_stats()
        except Exception as exc:
            return {"error": str(exc)}

        return {
            "days": days,
            "stats": stats,
            "document_growth": doc_growth,
            "tag_distribution": tag_dist,
            "storage": storage,
            "search_stats": search_stats,
            "popular_queries": popular_queries,
            "search_trend": search_trend,
            "chat_activity": chat_activity,
            "job_stats": job_stats,
        }

    @app.get("/search", response_class=HTMLResponse, include_in_schema=False)
    async def search_page(
        q: str = Query(default="", description="Search query"),
        export: str = Query(
            default="", description="Export format: csv or json (empty = HTML page)"
        ),
    ):
        """Search page with results and citations.

        When ``export`` is ``csv`` or ``json``, returns a downloadable
        file instead of the HTML results page.
        """
        if not q.strip():
            return HTMLResponse(content=_render_search_form())

        try:
            validated_q = validate_search_query(q)
        except ValidationError as e:
            return HTMLResponse(content=_render_search_form(error=e.message))

        db = get_db()
        results: list[dict] = []
        try:
            results = await db.fulltext_search(validated_q, limit=20)
        except Exception:
            pass

        # Log the search for analytics (best-effort, don't fail on logging errors)
        try:
            await db.log_search(validated_q, len(results))
        except Exception:
            pass

        # ── Export path ───────────────────────────────────────
        fmt = export.lower().strip()
        if fmt in ("csv", "json"):
            return _export_search_results(validated_q, results, fmt)

        html = _render_search_results(validated_q, results)
        return HTMLResponse(content=html)

    @app.get("/documents", response_class=HTMLResponse, include_in_schema=False)
    async def list_documents_page(
        source: str = Query(default=""),
        tag: str = Query(default=""),
        collection_id: Optional[int] = Query(default=None),
        date_from: str = Query(default="", description="ISO date (inclusive)"),
        date_to: str = Query(default="", description="ISO date (inclusive)"),
        file_type: str = Query(default="", description="File extension filter"),
        page: int = Query(default=1, ge=1),
        per_page: int = Query(default=20, ge=1, le=100),
    ):
        """List documents with pagination, optional multi-filter support.

        Filters combine with AND logic: source, tag, collection_id,
        date_from, date_to, and file_type.
        """
        db = get_db()
        try:
            result = await db.list_documents_paginated(
                page=page, per_page=per_page,
                source=source if source else None,
                collection_id=collection_id,
                date_from=date_from if date_from else None,
                date_to=date_to if date_to else None,
                file_type=file_type if file_type else None,
                tag=tag if tag else None,
            )
            documents = result["documents"]
            total = result["total"]
            total_pages = result["total_pages"]
        except Exception:
            documents = []
            total = 0
            total_pages = 0

        # Fetch tags for the displayed documents (batch)
        doc_ids = [d["id"] for d in documents]
        tags_map = await db.get_tags_for_documents(doc_ids) if doc_ids else {}

        # Fetch all tags for the tag cloud sidebar
        all_tags = await db.get_all_tags()

        # Fetch collection tree + counts for the collection sidebar
        collection_tree = await db.list_collections_tree()
        collection_counts = await db.get_collection_counts()

        # Fetch collection path for breadcrumb navigation
        # (only when a real collection is selected, not unassigned/id=0)
        collection_path: list[dict] = []
        if collection_id is not None and collection_id > 0:
            collection_path = await db.get_collection_path(collection_id)

        html = _render_documents_list(
            documents, source, page, per_page, total, total_pages,
            tags_map=tags_map, all_tags=all_tags, active_tag=tag,
            collection_tree=collection_tree,
            collection_counts=collection_counts,
            active_collection_id=collection_id,
            collection_path=collection_path,
            date_from=date_from, date_to=date_to, file_type=file_type,
        )
        return HTMLResponse(content=html)

    # ── HTMX Partial-Swap Endpoint (ADR-003) ──────────────────────
    # Returns an HTML fragment (not a full page) for HTMX hx-get swaps.
    # The client-side hx-target="#doc-table-region" replaces the document
    # table + pagination without a full page reload. Progressive enhancement:
    # if HTMX is absent, the filter form's normal GET /documents still works.
    @app.get(
        "/documents/partials/table",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def documents_table_partial(
        source: str = Query(default=""),
        tag: str = Query(default=""),
        collection_id: Optional[int] = Query(default=None),
        date_from: str = Query(default=""),
        date_to: str = Query(default=""),
        file_type: str = Query(default=""),
        page: int = Query(default=1, ge=1),
        per_page: int = Query(default=20, ge=1, le=100),
    ):
        """Return the document table region as an HTML fragment for HTMX.

        Accepts the same filter/pagination params as GET /documents.
        Returns only the #doc-table-region content (table + pagination),
        without page chrome. See ADR-003 HTMX usage guidelines.
        """
        db = get_db()
        try:
            result = await db.list_documents_paginated(
                page=page, per_page=per_page,
                source=source if source else None,
                collection_id=collection_id,
                date_from=date_from if date_from else None,
                date_to=date_to if date_to else None,
                file_type=file_type if file_type else None,
                tag=tag if tag else None,
            )
            documents = result["documents"]
            total = result["total"]
            total_pages = result["total_pages"]
        except Exception:
            documents = []
            total = 0
            total_pages = 0

        doc_ids = [d["id"] for d in documents]
        tags_map = await db.get_tags_for_documents(doc_ids) if doc_ids else {}

        html = _render_documents_table_partial(
            documents, page, per_page, total, total_pages,
            tags_map=tags_map, active_tag=tag, source=source,
            active_collection_id=collection_id,
            date_from=date_from, date_to=date_to, file_type=file_type,
        )
        return HTMLResponse(content=html)

    @app.get("/documents/{doc_id}", response_class=HTMLResponse, include_in_schema=False)
    async def document_detail(doc_id: int):
        """Document detail page with summary and tags."""
        try:
            validate_doc_id(doc_id)
        except ValidationError as e:
            return HTMLResponse(
                content=_render_error("Invalid document ID", e.message),
                status_code=400,
            )

        db = get_db()
        doc = await db.get_document(doc_id)
        if doc is None:
            return HTMLResponse(
                content=_render_error("Not Found", f"Document {doc_id} not found"),
                status_code=404,
            )

        tags = await db.get_tags(doc_id)
        current_collection = await db.get_document_collection(doc_id)
        all_collections = await db.list_collections()
        html = _render_document_detail(
            doc, tags,
            current_collection=current_collection,
            all_collections=all_collections,
        )
        return HTMLResponse(content=html)

    @app.get(
        "/documents/{doc_id}/view",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def document_viewer_page(
        doc_id: int,
        page: int = Query(default=1, ge=1),
        per_page: int = Query(default=5000, ge=500, le=50000),
    ):
        """Document viewer: full content, formatted by file type, paginated."""
        try:
            validate_doc_id(doc_id)
        except ValidationError as e:
            return HTMLResponse(
                content=_render_error("Invalid document ID", e.message),
                status_code=400,
            )

        db = get_db()
        doc = await db.get_document(doc_id)
        if doc is None:
            return HTMLResponse(
                content=_render_error("Not Found", f"Document {doc_id} not found"),
                status_code=404,
            )

        from .document_viewer import render_document_viewer

        html = render_document_viewer(doc, page=page, per_page=per_page)
        return HTMLResponse(content=html)

    @app.post(
        "/documents/{doc_id}/tags",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def add_tag_form(doc_id: int, tag: str = Form(default="")):
        """Add a tag to a document via form POST, then redirect back to detail."""
        try:
            validate_doc_id(doc_id)
        except ValidationError as e:
            return HTMLResponse(
                content=_render_error("Invalid document ID", e.message),
                status_code=400,
            )

        db = get_db()
        # Verify document exists
        doc = await db.get_document(doc_id)
        if doc is None:
            return HTMLResponse(
                content=_render_error("Not Found", f"Document {doc_id} not found"),
                status_code=404,
            )

        tag = (tag or "").strip()
        if tag:
            try:
                await db.add_tag(doc_id, tag)
            except ValueError:
                pass  # empty tag, silently ignore

        # Re-render detail page with updated tags
        tags = await db.get_tags(doc_id)
        html = _render_document_detail(doc, tags)
        return HTMLResponse(content=html)

    @app.post(
        "/documents/{doc_id}/tags/{tag}/delete",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def remove_tag_form(doc_id: int, tag: str):
        """Remove a tag from a document via form POST, then re-render detail."""
        try:
            validate_doc_id(doc_id)
        except ValidationError as e:
            return HTMLResponse(
                content=_render_error("Invalid document ID", e.message),
                status_code=400,
            )

        db = get_db()
        doc = await db.get_document(doc_id)
        if doc is None:
            return HTMLResponse(
                content=_render_error("Not Found", f"Document {doc_id} not found"),
                status_code=404,
            )

        await db.remove_tag(doc_id, tag)

        # Re-render detail page with updated tags
        tags = await db.get_tags(doc_id)
        html = _render_document_detail(doc, tags)
        return HTMLResponse(content=html)

    @app.get("/upload", response_class=HTMLResponse, include_in_schema=False)
    async def upload_form_page(request: Request):
        """Render the upload form page (GET).

        Supports ``?done=1`` to show a small success banner after the JS
        batch uploader finishes and redirects here.
        """
        done = request.query_params.get("done") == "1"
        html = _render_upload_form(error="")
        if done:
            # Lightweight: inject a success banner at the top of the form.
            banner = (
                '<div class="success" style="margin-bottom:12px;">'
                "✅ Batch upload complete. See "
                '<a href="/documents">documents</a> and '
                '<a href="/jobs">jobs</a>.'
                "</div>"
            )
            html = html.replace(
                '<div class="upload-form">',
                '<div class="upload-form">' + banner,
                1,
            )
        return HTMLResponse(content=html)

    @app.post("/upload", response_class=HTMLResponse, include_in_schema=False)
    async def upload_page(
        file: UploadFile = File(None),
        files: List[UploadFile] = File(default_factory=list),
    ):
        """File upload form handler (single or batch).

        Backward-compat: the original single-file field ``file`` is still
        honoured. The new drag-and-drop UI sends ``files[]`` (multiple).
        Both are merged into one batch list so a plain curl with
        ``-F file=...`` keeps working unchanged.
        """
        # Merge legacy single-file field and new multi-file field
        all_files: List[UploadFile] = list(files)
        if file is not None and file.filename:
            # Avoid double-counting if the client sent the same file under
            # both field names (some browsers do this on legacy fallback).
            names = {f.filename for f in all_files if f.filename}
            if file.filename not in names:
                all_files.insert(0, file)

        if not all_files:
            return HTMLResponse(content=_render_upload_form())

        db = get_db()
        queue = get_queue()

        results: list[dict] = []
        errors: list[dict] = []

        for f in all_files:
            ext = Path(f.filename or "").suffix.lower()
            if ext and ext not in config.document_limits.supported_extensions:
                errors.append({
                    "filename": f.filename or "unknown",
                    "error": f"Unsupported file type: {ext}",
                })
                continue

            try:
                raw = await f.read()
            except Exception as e:
                errors.append({
                    "filename": f.filename or "unknown",
                    "error": f"Could not read file: {e}",
                })
                continue

            if len(raw) > config.document_limits.max_file_size_bytes:
                errors.append({
                    "filename": f.filename or "unknown",
                    "error": (
                        f"File too large (max "
                        f"{config.document_limits.max_file_size_bytes:,} bytes)"
                    ),
                })
                continue

            display_title = f.filename or "untitled"
            mime_type = (
                f.content_type
                or mimetypes.guess_type(f.filename or "")[0]
                or "application/octet-stream"
            )

            try:
                body = _extract_body(raw, ext or "", f.filename or "")
            except Exception as e:
                errors.append({
                    "filename": f.filename or "unknown",
                    "error": f"Extraction failed: {e}",
                })
                continue

            try:
                doc_id = await db.upsert_document(
                    path=f"/uploads/{f.filename or 'unknown'}",
                    source_type="api",
                    source_name="web-upload",
                    title=display_title,
                    ext=ext or "",
                    mime_type=mime_type,
                    body=body,
                    size=len(raw),
                )

                # Auto-generate summary on upload (best-effort)
                try:
                    summary = await _generate_summary_for_doc(
                        {"title": display_title, "body": body}
                    )
                    if summary:
                        await db.update_summary(doc_id, summary)
                except Exception:
                    logger.warning(
                        "Summary generation failed for doc %s, continuing",
                        doc_id,
                    )

                job = await queue.enqueue(
                    document_path=f"/uploads/{f.filename or 'unknown'}",
                    document_title=display_title,
                    source_name="web-upload",
                )

                results.append({
                    "title": display_title,
                    "doc_id": doc_id,
                    "job_id": job.id,
                })
            except Exception as e:
                logger.exception("Upload failed for %s", f.filename)
                errors.append({
                    "filename": f.filename or "unknown",
                    "error": str(e),
                })

        # Single-file success → keep the original simple success page so
        # existing scrapers / curl users see the same UX.
        if len(results) == 1 and not errors:
            r = results[0]
            return HTMLResponse(
                content=_render_upload_success(
                    r["title"], r["doc_id"], r["job_id"]
                )
            )

        # Batch (or partial-failure) → batch results page
        return HTMLResponse(
            content=_render_upload_batch(results, errors)
        )

    @app.post(
        "/documents/{doc_id}/delete",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def delete_document_form(doc_id: int):
        """Delete a document via form POST, then redirect to documents list."""
        try:
            validate_doc_id(doc_id)
        except ValidationError as e:
            return HTMLResponse(
                content=_render_error("Invalid document ID", e.message),
                status_code=400,
            )

        db = get_db()
        deleted = await db.delete_document(doc_id)
        if not deleted:
            return HTMLResponse(
                content=_render_error("Not Found", f"Document {doc_id} not found"),
                status_code=404,
            )

        html = _render_delete_success(doc_id)
        return HTMLResponse(content=html)

    @app.delete(
        "/api/v1/documents/bulk",
        tags=["documents"],
        summary="Delete multiple documents in bulk",
    )
    async def bulk_delete_documents_api(request: Request, api_key: str = Security(api_key_header)):
        """Delete multiple documents by ID.

        Request body (JSON):
            {"doc_ids": [1, 2, 3]}

        Returns a summary of deleted / not-found / invalid IDs.
        Non-existent IDs are reported in ``not_found`` but do not
        cause a 4xx — the request still succeeds for IDs that exist.
        Invalid IDs (non-positive-integers) cause a 400.
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid JSON body",
            )

        if not isinstance(body, dict) or "doc_ids" not in body:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Body must be a JSON object with a "doc_ids" array',
            )

        raw_ids = body["doc_ids"]
        if not isinstance(raw_ids, list):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='"doc_ids" must be an array of positive integers',
            )

        if len(raw_ids) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='"doc_ids" must contain at least one ID',
            )

        parsed_ids: list[int] = []
        for raw in raw_ids:
            try:
                parsed_ids.append(validate_doc_id(raw))
            except ValidationError as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid document ID {raw!r}: {e.message}",
                )

        db = get_db()
        deleted_ids: list[int] = []
        not_found_ids: list[int] = []

        for doc_id in parsed_ids:
            ok = await db.delete_document(doc_id)
            if ok:
                deleted_ids.append(doc_id)
            else:
                not_found_ids.append(doc_id)

        return {
            "deleted": deleted_ids,
            "deleted_count": len(deleted_ids),
            "not_found": not_found_ids,
            "not_found_count": len(not_found_ids),
            "requested_count": len(parsed_ids),
        }

    @app.delete(
        "/api/v1/documents/{doc_id}",
        tags=["documents"],
        summary="Delete a document and its FTS index entry",
    )
    async def delete_document_api(doc_id: int, api_key: str = Security(api_key_header)):
        """Delete a document by ID. Returns 404 if not found."""
        try:
            validate_doc_id(doc_id)
        except ValidationError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=e.message,
            )

        db = get_db()
        deleted = await db.delete_document(doc_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No document with id {doc_id}",
            )

        return {"id": doc_id, "deleted": True}

    # ── Bulk document delete ──────────────────────────────────────

    @app.post(
        "/documents/bulk-delete",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def bulk_delete_documents_form(request: Request):
        """Delete multiple documents via form POST, then show success page.

        Accepts form-encoded ``doc_ids`` fields (one per checkbox).
        Redirects back to the documents list on success.
        """
        from urllib.parse import urlencode
        from fastapi import Form as _Form  # noqa: F811 – local alias for clarity

        form = await request.form()
        raw_ids = form.getlist("doc_ids")

        if not raw_ids:
            return HTMLResponse(
                content=_render_error(
                    "No documents selected",
                    "Please select at least one document to delete.",
                ),
                status_code=400,
            )

        # Parse & validate IDs; collect failures
        parsed_ids: list[int] = []
        invalid: list[str] = []
        for raw in raw_ids:
            try:
                parsed_ids.append(validate_doc_id(raw))
            except ValidationError:
                invalid.append(str(raw))

        db = get_db()
        deleted_ids: list[int] = []
        not_found_ids: list[int] = []

        for doc_id in parsed_ids:
            ok = await db.delete_document(doc_id)
            if ok:
                deleted_ids.append(doc_id)
            else:
                not_found_ids.append(doc_id)

        html = _render_template(
            "bulk_delete_success.html",
            deleted_ids=deleted_ids,
            not_found_ids=not_found_ids,
            invalid_ids=invalid,
            total_requested=len(parsed_ids),
        )
        return HTMLResponse(content=html)

    @app.get("/chat", response_class=HTMLResponse, include_in_schema=False)
    async def chat_page():
        """Chat page with WebSocket client for real-time Q&A."""
        html = _render_chat_page()
        return HTMLResponse(content=html)

    # ── Auth: login / logout routes ────────────────────────────

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_page(error: str = Query(default="")):
        """Render the login page.

        If auth is disabled, redirect to the dashboard instead.
        """
        if not auth_enabled():
            return RedirectResponse(url="/", status_code=303)
        html = _render_login_page(error=error)
        return HTMLResponse(content=html)

    @app.post("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_submit(password: str = Form(default="")):
        """Validate the submitted password and set a session cookie.

        On success: redirect to / with a signed session cookie.
        On failure: re-render the login page with an error message.
        """
        if not auth_enabled():
            # Auth was disabled after the form was rendered — just go home.
            return RedirectResponse(url="/", status_code=303)

        if check_password(password):
            return login_response()
        html = _render_login_page(error="Invalid password. Please try again.")
        return HTMLResponse(content=html, status_code=401)

    @app.get("/logout", include_in_schema=False)
    async def logout_get():
        """GET /logout — clear session and redirect to /login."""
        return logout_response()

    @app.post("/logout", include_in_schema=False)
    async def logout_post():
        """POST /logout — clear session and redirect to /login."""
        return logout_response()

    @app.get("/health", include_in_schema=False)
    async def health():
        """Health-check endpoint — always public (no auth required)."""
        return {"status": "ok"}

    # ── Settings page (LLM configuration) ─────────────────────

    @app.get("/settings", response_class=HTMLResponse, include_in_schema=False)
    async def settings_page(
        saved: str = Query(default="", description="Show success banner when '1'")
    ):
        """Settings page for LLM provider/model/key configuration."""
        db = get_db()
        try:
            settings = await db.get_all_settings()
        except Exception:
            settings = {}

        success = saved == "1"
        html = _render_settings_page(settings, success=success)
        return HTMLResponse(content=html)

    @app.post("/settings", response_class=HTMLResponse, include_in_schema=False)
    async def settings_save(
        provider: str = Form(default=""),
        model: str = Form(default=""),
        api_key: str = Form(default=""),
        base_url: str = Form(default=""),
        max_tokens: str = Form(default="1000"),
        temperature: str = Form(default="0.3"),
        chat_fallback: str = Form(default=""),
        auth_enabled_field: str = Form(default="", alias="auth_enabled"),
        auth_api_key: str = Form(default=""),
    ):
        """Save LLM settings to the DB and reload the in-memory config.

        Also persists auth settings (enable/disable, password) to the
        DB settings table and rehydrates the in-memory AuthConfig.

        Security note: if the submitted api_key field is the masked
        placeholder (``****`` prefix), the existing stored key is kept
        unchanged — the user only sees the masked value in the form.
        The same masking logic applies to the auth_api_key field.
        """
        db = get_db()

        # ── Persist each LLM field ─────────────────────────────
        await db.set_setting("llm_provider", provider.strip())
        await db.set_setting("llm_model", model.strip())

        # API key masking: never overwrite the stored key with the
        # masked placeholder. Only update when the user typed a new key.
        masked_placeholder_prefix = "****"
        submitted_key = api_key.strip()
        if submitted_key and not submitted_key.startswith(masked_placeholder_prefix):
            await db.set_setting("llm_api_key", submitted_key)
        # If empty or masked, leave the existing stored value alone.

        await db.set_setting("llm_base_url", base_url.strip())

        # Numeric fields with bounds validation
        try:
            mt = int(max_tokens)
            mt = max(100, min(4000, mt))
        except (ValueError, TypeError):
            mt = 1000
        await db.set_setting("llm_max_tokens", str(mt))

        try:
            temp = float(temperature)
            temp = max(0.0, min(1.0, temp))
        except (ValueError, TypeError):
            temp = 0.3
        await db.set_setting("llm_temperature", f"{temp:.2f}")

        # Chat fallback toggle: HTML checkboxes only submit when checked
        fallback_val = "1" if chat_fallback else "0"
        await db.set_setting("llm_chat_fallback", fallback_val)

        # ── Persist auth settings ─────────────────────────────
        # auth_enabled checkbox: only present in form data when checked.
        new_auth_enabled = bool(auth_enabled_field)
        await db.set_setting("auth_enabled", "1" if new_auth_enabled else "0")

        # Auth API key masking — same rule as the LLM key.
        submitted_auth_key = auth_api_key.strip()
        if submitted_auth_key and not submitted_auth_key.startswith(masked_placeholder_prefix):
            await db.set_setting("auth_api_key", submitted_auth_key)

        # Ensure a stable session secret exists (generate+persist once).
        if new_auth_enabled:
            existing_secret = await db.get_setting("auth_session_secret")
            if not existing_secret:
                secret = ensure_session_secret()
                await db.set_setting("auth_session_secret", secret)
            # If enabling with no api_key set yet, generate one.
            existing_key = await db.get_setting("auth_api_key")
            if not existing_key and not submitted_auth_key:
                generated = generate_api_key()
                await db.set_setting("auth_api_key", generated)

        # ── Reload the in-memory LLMConfig from DB ──────────────
        # Re-read all settings (including the just-saved ones) and apply
        # them to the global config singleton. The LLMClient in chat.py
        # constructs from config.llm on each WebSocket connection, so
        # the new values take effect on the next chat request.
        saved_settings = await db.get_all_settings()
        _reload_llm_config_from_db(saved_settings)
        # Rehydrate auth config too so the middleware sees the new state.
        apply_auth_settings_from_db(saved_settings)

        # Redirect back to /settings?saved=1 to show success banner
        html = _render_settings_redirect()
        return HTMLResponse(
            content=html,
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/settings?saved=1"},
        )

    # ── Jobs page ─────────────────────────────────────────────

    @app.get("/jobs", response_class=HTMLResponse, include_in_schema=False)
    async def jobs_page(
        state: str = Query(default="", description="Filter by job state"),
        page: int = Query(default=1, ge=1),
        per_page: int = Query(default=20, ge=1, le=100),
    ):
        """Job processing status page with table, filter, and auto-refresh."""
        db = get_db()
        # Validate the state filter — empty means "all"
        valid_states = {"", "pending", "processing", "completed", "failed"}
        if state not in valid_states:
            state = ""

        try:
            result = await db.list_jobs_paginated(
                state=state or None, page=page, per_page=per_page
            )
            jobs = result["jobs"]
            total = result["total"]
            total_pages = result["total_pages"]
        except Exception:
            jobs = []
            total = 0
            total_pages = 0

        # Check if there are any active (pending/processing) jobs for auto-refresh
        try:
            pending_count = await db.count_jobs(state="pending")
            processing_count = await db.count_jobs(state="processing")
        except Exception:
            pending_count = 0
            processing_count = 0
        has_active = pending_count + processing_count > 0

        html = _render_jobs_page(
            jobs, state, page, per_page, total, total_pages, has_active
        )
        return HTMLResponse(content=html)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse, include_in_schema=False)
    async def job_detail_page(job_id: str):
        """Job detail page showing all fields, error, and linked document."""
        db = get_db()
        job = await db.get_job(job_id)
        if job is None:
            return HTMLResponse(
                content=_render_error("Not Found", f"Job {job_id} not found"),
                status_code=404,
            )

        # Fetch associated document if document_id is set
        document = None
        if job.document_id is not None:
            document = await db.get_document(job.document_id)

        html = _render_job_detail(job, document)
        return HTMLResponse(content=html)

    # ── WebSocket ───────────────────────────────────────────

    @app.websocket("/chat")
    async def chat_endpoint(websocket: WebSocket):
        """Real-time Q&A with citation tracking and persisted history."""
        db = get_db()
        await handle_chat(websocket, db=db)

    # ── Chat session REST API ───────────────────────────────

    @app.get(
        "/api/v1/chat/sessions",
        tags=["chat"],
        summary="List recent chat sessions",
    )
    async def list_chat_sessions(
        limit: int = Query(default=50, ge=1, le=200),
        api_key: str = Security(api_key_header),
    ):
        """Return recent chat sessions (newest first) with preview + timestamps."""
        db = get_db()
        sessions = await db.list_chat_sessions(limit=limit)
        return {"sessions": sessions, "count": len(sessions)}

    @app.get(
        "/api/v1/chat/sessions/{session_id}/messages",
        tags=["chat"],
        summary="Get full message history for a chat session",
    )
    async def get_chat_messages(session_id: str, api_key: str = Security(api_key_header)):
        """Return all messages for a session, oldest first. 404 if session missing."""
        db = get_db()
        session = await db.get_chat_session(session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No chat session with id {session_id}",
            )
        messages = await db.get_chat_history(session_id, limit=200)
        return {
            "session": session,
            "messages": messages,
            "count": len(messages),
        }

    @app.delete(
        "/api/v1/chat/sessions/{session_id}",
        tags=["chat"],
        summary="Delete a chat session and all its messages",
    )
    async def delete_chat_session_api(session_id: str, api_key: str = Security(api_key_header)):
        """Delete a chat session. Returns 404 if not found."""
        db = get_db()
        deleted = await db.delete_chat_session(session_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No chat session with id {session_id}",
            )
        return {"id": session_id, "deleted": True}

    # ── Chat session export ─────────────────────────────────────

    @app.get(
        "/api/v1/chat/sessions/{session_id}/export",
        tags=["chat"],
        summary="Export a chat session conversation",
    )
    async def export_chat_session(
        session_id: str,
        format: str = Query(
            default="markdown",
            description="Export format: markdown, json, or txt",
        ),
        api_key: str = Security(api_key_header),
    ):
        """Export a full chat conversation as Markdown, JSON, or plain text.

        Sets Content-Disposition so the browser downloads the file.
        Returns 404 if the session does not exist.
        """
        db = get_db()
        session = await db.get_chat_session(session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No chat session with id {session_id}",
            )

        messages = await db.get_chat_history(session_id, limit=10000)
        safe_title = session.get("title", "chat") or "chat"
        # Sanitize filename: keep alphanumerics, dashes, underscores
        safe_filename = "".join(
            c if c.isalnum() or c in "-_" else "_" for c in safe_title
        )[:60] or "chat"

        fmt = format.lower().strip()
        if fmt == "json":
            payload = {
                "session_id": session_id,
                "title": session.get("title", ""),
                "created_at": str(session.get("created_at", "")),
                "updated_at": str(session.get("updated_at", "")),
                "messages": [
                    {
                        "role": m["role"],
                        "content": m["content"],
                        "citations": m.get("citations", []),
                        "created_at": str(m.get("created_at", "")),
                    }
                    for m in messages
                ],
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2)
            return Response(
                content=body,
                media_type="application/json",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="{safe_filename}.json"'
                    )
                },
            )

        if fmt == "txt":
            lines: list[str] = []
            lines.append(f"Conversation: {session.get('title', '')}")
            lines.append(f"Session ID: {session_id}")
            lines.append(
                f"Created: {session.get('created_at', '')}"
            )
            lines.append("=" * 60)
            for m in messages:
                role = "You" if m["role"] == "user" else "Assistant"
                lines.append(f"\n[{role}]")
                lines.append(m["content"])
                citations = m.get("citations", [])
                if citations:
                    lines.append("\nSources:")
                    for c in citations:
                        lines.append(
                            f"  [{c.get('ref', '?')}] "
                            f"{c.get('title', 'Untitled')} "
                            f"(doc_id: {c.get('doc_id', '?')})"
                        )
            body = "\n".join(lines)
            return PlainTextResponse(
                content=body,
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="{safe_filename}.txt"'
                    )
                },
            )

        # Default: markdown
        md_lines: list[str] = []
        md_lines.append(f"# {session.get('title', 'Chat Export')}")
        md_lines.append("")
        md_lines.append(f"- **Session ID:** `{session_id}`")
        md_lines.append(
            f"- **Created:** {session.get('created_at', '')}"
        )
        md_lines.append(
            f"- **Updated:** {session.get('updated_at', '')}"
        )
        md_lines.append(f"- **Messages:** {len(messages)}")
        md_lines.append("")
        md_lines.append("---")
        md_lines.append("")
        for m in messages:
            if m["role"] == "user":
                md_lines.append(f"## 👤 You")
            else:
                md_lines.append(f"## 🤖 Assistant")
            md_lines.append("")
            md_lines.append(m["content"])
            md_lines.append("")
            citations = m.get("citations", [])
            if citations:
                md_lines.append("**Sources:**")
                md_lines.append("")
                for c in citations:
                    md_lines.append(
                        f"- [{c.get('ref', '?')}] "
                        f"{c.get('title', 'Untitled')} "
                        f"(doc_id: {c.get('doc_id', '?')}, "
                        f"confidence: {c.get('confidence', 'low')})"
                    )
                md_lines.append("")
            md_lines.append("---")
            md_lines.append("")
        body = "\n".join(md_lines)
        return PlainTextResponse(
            content=body,
            media_type="text/markdown",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{safe_filename}.md"'
                )
            },
        )

    # ── Document summary export ─────────────────────────────────

    @app.get(
        "/documents/{doc_id}/summary/export",
        response_class=Response,
        include_in_schema=False,
    )
    async def export_document_summary(
        doc_id: int,
        format: str = Query(
            default="md",
            description="Export format: md or txt",
        ),
    ):
        """Export a document's title, summary, and metadata as Markdown/txt."""
        try:
            validate_doc_id(doc_id)
        except ValidationError as e:
            return HTMLResponse(
                content=_render_error("Invalid document ID", e.message),
                status_code=400,
            )

        db = get_db()
        doc = await db.get_document(doc_id)
        if doc is None:
            return HTMLResponse(
                content=_render_error("Not Found", f"Document {doc_id} not found"),
                status_code=404,
            )

        title = doc.get("title", "Untitled")
        safe_filename = "".join(
            c if c.isalnum() or c in "-_" else "_" for c in title
        )[:60] or "document"
        summary = doc.get("summary") or "(No summary available)"
        fmt = format.lower().strip()

        if fmt == "txt":
            lines = [
                f"Document: {title}",
                f"ID: {doc_id}",
                f"Path: {doc.get('path', '')}",
                f"Source: {doc.get('source_name', doc.get('source_type', ''))}",
                f"Type: {doc.get('ext', '')} ({doc.get('mime_type', '')})",
                f"Status: {doc.get('status', '')}",
                f"Created: {doc.get('created_at', '')}",
                f"Updated: {doc.get('updated_at', '')}",
                "",
                "=" * 60,
                "SUMMARY",
                "=" * 60,
                summary,
            ]
            body = "\n".join(lines)
            return PlainTextResponse(
                content=body,
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="{safe_filename}_summary.txt"'
                    )
                },
            )

        # Default: markdown
        md = f"""# {title}

| Field | Value |
|-------|-------|
| **ID** | {doc_id} |
| **Path** | `{doc.get('path', '')}` |
| **Source** | {doc.get('source_name', doc.get('source_type', ''))} |
| **Type** | {doc.get('ext', '')} ({doc.get('mime_type', '')}) |
| **Status** | {doc.get('status', '')} |
| **Created** | {doc.get('created_at', '')} |
| **Updated** | {doc.get('updated_at', '')} |

---

## Summary

{summary}
"""
        return PlainTextResponse(
            content=md,
            media_type="text/markdown",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{safe_filename}_summary.md"'
                )
            },
        )

    # ── Document summary regeneration ───────────────────────────

    @app.post(
        "/documents/{doc_id}/regenerate-summary",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def regenerate_summary(doc_id: int):
        """Regenerate the LLM/extractive summary for a document.

        Re-runs the Summarizer on the document body and updates the
        stored summary. Re-renders the document detail page.
        """
        try:
            validate_doc_id(doc_id)
        except ValidationError as e:
            return HTMLResponse(
                content=_render_error("Invalid document ID", e.message),
                status_code=400,
            )

        db = get_db()
        doc = await db.get_document(doc_id)
        if doc is None:
            return HTMLResponse(
                content=_render_error("Not Found", f"Document {doc_id} not found"),
                status_code=404,
            )

        new_summary = await _generate_summary_for_doc(doc)
        if new_summary:
            await db.update_summary(doc_id, new_summary)

        # Re-fetch and re-render
        doc = await db.get_document(doc_id)
        tags = await db.get_tags(doc_id)
        html = _render_document_detail(doc, tags)
        return HTMLResponse(content=html)

    @app.post(
        "/api/v1/documents/summarize-all",
        tags=["documents"],
        summary="Create summary-generation jobs for all documents missing summaries",
    )
    async def summarize_all_documents(api_key: str = Security(api_key_header)):
        """Enqueue background jobs to summarize all documents that lack a summary.

        For each document with status 'indexed' (no summary), a job is
        enqueued. The job worker calls the Summarizer and stores the result.

        Returns the count of jobs created and their IDs.
        """
        db = get_db()
        queue = get_queue()

        pending = await db.get_pending_summaries(limit=10000)
        job_ids: list[str] = []
        for doc in pending:
            job = await queue.enqueue(
                document_path=doc.get("path", f"/docs/{doc['id']}"),
                document_title=doc.get("title", f"Document {doc['id']}"),
                source_name="summarize-all",
            )
            job_ids.append(job.id)

        logger.info(
            "summarize-all: enqueued %d summary jobs", len(job_ids)
        )
        return {
            "jobs_created": len(job_ids),
            "job_ids": job_ids,
            "message": (
                f"Created {len(job_ids)} summary job(s). "
                "View progress at /jobs."
            ) if job_ids else "No documents need summarization.",
        }

    # ── API v1 Routes ───────────────────────────────────────

    @app.get("/health", include_in_schema=False)
    async def health_check():
        return {"status": "ok"}

    @app.post(
        "/api/v1/documents/submit",
        response_model=SubmissionAccepted,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["documents"],
        summary="Submit a single document for processing",
    )
    async def submit_document(
        file: UploadFile = File(...),
        title: Optional[str] = Form(None),
        source_name: str = Form("api"),
        api_key: str = Security(api_key_header),
    ):
        ext = _get_ext(file.filename)
        if ext and ext not in config.document_limits.supported_extensions:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"Unsupported file type: {ext}",
            )

        raw = await file.read()
        if len(raw) > config.document_limits.max_file_size_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File exceeds maximum size",
            )

        display_title = title or file.filename or "untitled"
        mime_type = (
            file.content_type
            or mimetypes.guess_type(file.filename or "")[0]
            or "application/octet-stream"
        )

        body = _extract_body(raw, ext or "", file.filename or "")

        doc = DocumentCreate(
            path=f"/submissions/{file.filename or 'unknown'}",
            source_name=source_name,
            title=display_title,
            ext=ext or "",
            mime_type=mime_type,
            body=body,
            size=len(raw),
        )

        db = get_db()
        queue = get_queue()

        doc_id = await db.upsert_document(
            path=doc.path,
            source_type="api",
            source_name=doc.source_name,
            title=doc.title,
            ext=doc.ext,
            mime_type=doc.mime_type,
            body=doc.body,
            size=doc.size,
            metadata=doc.metadata,
        )

        # Auto-generate summary on submit (best-effort)
        try:
            summary = await _generate_summary_for_doc(
                {"title": doc.title, "body": doc.body}
            )
            if summary:
                await db.update_summary(doc_id, summary)
        except Exception:
            logger.warning(
                "Summary generation failed for doc %s, continuing", doc_id
            )

        job = await queue.enqueue(
            document_path=doc.path,
            document_title=doc.title,
            source_name=doc.source_name,
        )

        logger.info("Document %s submitted → job %s", doc.path, job.id)

        return SubmissionAccepted(
            job_id=job.id,
            status="pending",
            document_path=doc.path,
        )

    @app.post(
        "/api/v1/documents/batch",
        response_model=dict,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["documents"],
        summary="Submit multiple documents in one request",
    )
    async def batch_submit_documents(body: BatchSubmissionRequest, api_key: str = Security(api_key_header)):
        if len(body.documents) > config.document_limits.max_batch_size:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Batch exceeds maximum size of {config.document_limits.max_batch_size}",
            )

        queue = get_queue()
        jobs: list[SubmissionAccepted] = []

        for item in body.documents:
            job = await queue.enqueue(
                document_path=item.path,
                document_title=item.title,
                source_name=item.source_name,
            )
            jobs.append(
                SubmissionAccepted(
                    job_id=job.id,
                    status="pending",
                    document_path=item.path,
                )
            )

        logger.info("Batch submitted: %d documents", len(jobs))
        return {"jobs": [j.model_dump() for j in jobs]}

    @app.get(
        "/api/v1/documents/{doc_id}/status",
        response_model=DocumentStatusResponse,
        tags=["documents"],
        summary="Get document processing status",
    )
    async def get_document_status(doc_id: int, api_key: str = Security(api_key_header)):
        db = get_db()
        doc = await db.get_document(doc_id)
        if doc is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No document with id {doc_id}",
            )

        return DocumentStatusResponse(
            id=doc["id"],
            status=DocumentStatus(doc["status"]),
            path=doc["path"],
            title=doc["title"],
            summary=doc.get("summary"),
            ext=doc.get("ext", ""),
            mime_type=doc.get("mime_type", ""),
            size=doc.get("size", 0),
            created_at=doc["created_at"],
            updated_at=doc["updated_at"],
        )

    @app.get(
        "/api/v1/jobs/{job_id}",
        response_model=JobStatusResponse,
        tags=["jobs"],
        summary="Get background job status",
    )
    async def get_job_status(job_id: str, api_key: str = Security(api_key_header)):
        queue = get_queue()
        job = await queue.get_status(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No job with id {job_id}",
            )

        return JobStatusResponse(
            job_id=job.id,
            status=job.state,
            document_id=job.document_id,
            error=job.error,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )

    # ── Collections REST API ─────────────────────────────────────

    @app.get(
        "/api/v1/documents",
        tags=["documents"],
        summary="List documents with optional collection_id filter",
    )
    async def list_documents_api(
        page: int = Query(default=1, ge=1),
        per_page: int = Query(default=20, ge=1, le=100),
        source: Optional[str] = Query(default=None),
        collection_id: Optional[int] = Query(
            default=None,
            description="Filter by collection. Use 0 for unassigned documents.",
        ),
        date_from: Optional[str] = Query(
            default=None,
            description="ISO date string (inclusive). Filter by created_at.",
        ),
        date_to: Optional[str] = Query(
            default=None,
            description="ISO date string (inclusive). Filter by created_at.",
        ),
        file_type: Optional[str] = Query(
            default=None,
            description="File extension filter (e.g. '.pdf', 'pdf').",
        ),
        tag: Optional[str] = Query(
            default=None,
            description="Tag name filter.",
        ),
        api_key: str = Security(api_key_header),
    ):
        """List documents with pagination and optional multi-filter support.

        All filters combine with AND logic: source, collection_id,
        date_from, date_to, file_type, and tag.

        When ``collection_id`` is provided, only documents in that collection
        are returned. Use ``collection_id=0`` to list documents that are not
        assigned to any collection.
        """
        db = get_db()
        result = await db.list_documents_paginated(
            page=page,
            per_page=per_page,
            source=source,
            collection_id=collection_id,
            date_from=date_from,
            date_to=date_to,
            file_type=file_type,
            tag=tag,
        )
        return result

    @app.post(
        "/api/v1/collections",
        status_code=status.HTTP_201_CREATED,
        tags=["collections"],
        summary="Create a new collection",
    )
    async def create_collection_api(
        request: Request, api_key: str = Security(api_key_header)
    ):
        """Create a new collection.

        Request body (JSON):
            {"name": "...", "description": "...", "parent_id": <int|null>}

        - ``name`` is required (non-empty).
        - ``parent_id`` if provided must reference an existing collection.
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid JSON body",
            )

        if not isinstance(body, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Body must be a JSON object",
            )

        name = (body.get("name") or "").strip() if isinstance(body.get("name"), str) else ""
        if not name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Field 'name' is required and must not be empty",
            )

        description = body.get("description") or ""
        if not isinstance(description, str):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Field 'description' must be a string",
            )

        parent_id = body.get("parent_id")
        db = get_db()

        if parent_id is not None:
            if not isinstance(parent_id, int) or parent_id <= 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Field 'parent_id' must be a positive integer or null",
                )
            parent = await db.get_collection(parent_id)
            if parent is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Parent collection {parent_id} not found",
                )

        try:
            col_id = await db.create_collection(
                name=name, description=description, parent_id=parent_id,
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )

        col = await db.get_collection(col_id)
        return col

    @app.get(
        "/api/v1/collections",
        tags=["collections"],
        summary="List all collections (flat)",
    )
    async def list_collections_api(api_key: str = Security(api_key_header)):
        """Return a flat list of all collections ordered by name."""
        db = get_db()
        collections = await db.list_collections()
        return {"collections": collections, "count": len(collections)}

    @app.get(
        "/api/v1/collections/tree",
        tags=["collections"],
        summary="List collections as a nested tree",
    )
    async def list_collections_tree_api(api_key: str = Security(api_key_header)):
        """Return collections as a nested tree structure for UI rendering."""
        db = get_db()
        tree = await db.list_collections_tree()
        return {"tree": tree}

    @app.get(
        "/api/v1/collections/{collection_id}",
        tags=["collections"],
        summary="Get a single collection by id",
    )
    async def get_collection_api(
        collection_id: int, api_key: str = Security(api_key_header)
    ):
        """Return a single collection. 404 if not found."""
        db = get_db()
        col = await db.get_collection(collection_id)
        if col is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No collection with id {collection_id}",
            )
        return col

    @app.put(
        "/api/v1/collections/{collection_id}",
        tags=["collections"],
        summary="Update a collection",
    )
    async def update_collection_api(
        collection_id: int,
        request: Request,
        api_key: str = Security(api_key_header),
    ):
        """Update one or more fields of a collection.

        Request body (JSON, all fields optional):
            {"name": "...", "description": "...", "parent_id": <int|null>}

        - 404 if the collection does not exist.
        - 400 if name is empty/whitespace or parent_id is invalid.
        - 409 if setting parent_id would create a cycle.
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid JSON body",
            )

        if not isinstance(body, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Body must be a JSON object",
            )

        db = get_db()
        existing = await db.get_collection(collection_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No collection with id {collection_id}",
            )

        name = body.get("name")
        description = body.get("description")
        parent_id = body.get("parent_id")

        # Validate name if provided
        if name is not None:
            if not isinstance(name, str) or not name.strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Field 'name' must not be empty",
                )

        # Validate description if provided
        if description is not None and not isinstance(description, str):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Field 'description' must be a string",
            )

        # Validate parent_id if provided
        if parent_id is not None:
            if not isinstance(parent_id, int) or parent_id <= 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Field 'parent_id' must be a positive integer or null",
                )
            if parent_id == collection_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot set parent_id to itself — this would create a cycle",
                )
            parent = await db.get_collection(parent_id)
            if parent is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Parent collection {parent_id} not found",
                )

        try:
            updated = await db.update_collection(
                collection_id,
                name=name,
                description=description,
                parent_id=parent_id,
            )
        except ValueError as e:
            # Cycle detection raises ValueError
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(e),
            )

        if not updated:
            # Should not happen since we checked existence above, but guard
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No collection with id {collection_id}",
            )

        col = await db.get_collection(collection_id)
        return col

    @app.delete(
        "/api/v1/collections/{collection_id}",
        tags=["collections"],
        summary="Delete a collection",
    )
    async def delete_collection_api(
        collection_id: int, api_key: str = Security(api_key_header)
    ):
        """Delete a collection.

        Documents in this collection (and its descendants) are moved to
        "All Documents" (collection_id set to NULL). Child collections are
        cascade-deleted. Returns 404 if the collection does not exist.
        """
        db = get_db()
        deleted = await db.delete_collection(collection_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No collection with id {collection_id}",
            )
        return {"id": collection_id, "deleted": True}

    @app.post(
        "/api/v1/documents/{doc_id}/collection",
        tags=["collections"],
        summary="Assign a document to a collection",
    )
    async def assign_document_to_collection_api(
        doc_id: int,
        request: Request,
        api_key: str = Security(api_key_header),
    ):
        """Assign a document to a collection.

        Request body (JSON): ``{"collection_id": <int>}``

        - 404 if the document or collection does not exist.
        """
        try:
            validate_doc_id(doc_id)
        except ValidationError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=e.message,
            )

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid JSON body",
            )

        if not isinstance(body, dict) or "collection_id" not in body:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Body must be a JSON object with a "collection_id" field',
            )

        collection_id = body["collection_id"]
        if not isinstance(collection_id, int) or collection_id <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='"collection_id" must be a positive integer',
            )

        db = get_db()

        # Verify document exists
        doc = await db.get_document(doc_id)
        if doc is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No document with id {doc_id}",
            )

        # Verify collection exists
        col = await db.get_collection(collection_id)
        if col is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No collection with id {collection_id}",
            )

        ok = await db.assign_document_to_collection(doc_id, collection_id)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Document or collection not found",
            )

        return {"doc_id": doc_id, "collection_id": collection_id, "assigned": True}

    @app.delete(
        "/api/v1/documents/{doc_id}/collection",
        tags=["collections"],
        summary="Remove a document from its collection",
    )
    async def remove_document_from_collection_api(
        doc_id: int, api_key: str = Security(api_key_header)
    ):
        """Remove a document from its collection (set collection_id to NULL).

        Returns 404 if the document does not exist. If the document was
        already unassigned, returns 200 with ``removed: False``.
        """
        try:
            validate_doc_id(doc_id)
        except ValidationError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=e.message,
            )

        db = get_db()
        doc = await db.get_document(doc_id)
        if doc is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No document with id {doc_id}",
            )

        removed = await db.remove_document_from_collection(doc_id)
        return {"doc_id": doc_id, "removed": removed}

    @app.get(
        "/api/v1/collections/{collection_id}/documents",
        tags=["collections"],
        summary="List documents in a collection",
    )
    async def list_documents_by_collection_api(
        collection_id: int,
        page: int = Query(default=1, ge=1),
        per_page: int = Query(default=20, ge=1, le=100),
        api_key: str = Security(api_key_header),
    ):
        """List documents in a collection with pagination metadata.

        Returns 404 if the collection does not exist.
        """
        db = get_db()
        col = await db.get_collection(collection_id)
        if col is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No collection with id {collection_id}",
            )
        result = await db.list_documents_by_collection(
            collection_id, page=page, per_page=per_page,
        )
        return result

    # ── Collections HTML form endpoints ──────────────────────────

    @app.post(
        "/collections/create",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def create_collection_form(
        name: str = Form(default=""),
        description: str = Form(default=""),
        parent_id: Optional[str] = Form(default=""),
    ):
        """Create a collection via form POST, then redirect to documents page."""
        name = (name or "").strip()
        if not name:
            return HTMLResponse(
                content=_render_error(
                    "Validation Error",
                    "Collection name is required.",
                ),
                status_code=400,
            )

        pid: Optional[int] = None
        if parent_id and parent_id.strip():
            try:
                pid = int(parent_id.strip())
            except (ValueError, TypeError):
                return HTMLResponse(
                    content=_render_error(
                        "Validation Error",
                        "Parent collection ID must be a number.",
                    ),
                    status_code=400,
                )

        db = get_db()
        if pid is not None:
            parent = await db.get_collection(pid)
            if parent is None:
                return HTMLResponse(
                    content=_render_error(
                        "Not Found",
                        f"Parent collection {pid} not found.",
                    ),
                    status_code=404,
                )

        try:
            await db.create_collection(
                name=name, description=description, parent_id=pid,
            )
        except ValueError as e:
            return HTMLResponse(
                content=_render_error("Validation Error", str(e)),
                status_code=400,
            )

        return RedirectResponse(url="/documents", status_code=303)

    @app.post(
        "/collections/{collection_id}/edit",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def edit_collection_form(
        collection_id: int,
        name: str = Form(default=""),
        description: str = Form(default=""),
        parent_id: Optional[str] = Form(default=""),
    ):
        """Edit a collection via form POST, then redirect to documents page."""
        new_name = (name or "").strip()
        if not new_name:
            return HTMLResponse(
                content=_render_error(
                    "Validation Error",
                    "Collection name is required.",
                ),
                status_code=400,
            )

        pid: Optional[int] = None
        if parent_id and parent_id.strip():
            try:
                pid = int(parent_id.strip())
            except (ValueError, TypeError):
                return HTMLResponse(
                    content=_render_error(
                        "Validation Error",
                        "Parent collection ID must be a number.",
                    ),
                    status_code=400,
                )

        db = get_db()
        existing = await db.get_collection(collection_id)
        if existing is None:
            return HTMLResponse(
                content=_render_error(
                    "Not Found",
                    f"Collection {collection_id} not found.",
                ),
                status_code=404,
            )

        try:
            await db.update_collection(
                collection_id,
                name=new_name,
                description=description,
                parent_id=pid,
            )
        except ValueError as e:
            return HTMLResponse(
                content=_render_error("Validation Error", str(e)),
                status_code=400,
            )

        return RedirectResponse(url="/documents", status_code=303)

    @app.post(
        "/collections/{collection_id}/delete",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def delete_collection_form(collection_id: int):
        """Delete a collection via form POST, then redirect to documents page."""
        db = get_db()
        deleted = await db.delete_collection(collection_id)
        if not deleted:
            return HTMLResponse(
                content=_render_error(
                    "Not Found",
                    f"Collection {collection_id} not found.",
                ),
                status_code=404,
            )
        return RedirectResponse(url="/documents", status_code=303)

    @app.post(
        "/documents/{doc_id}/assign-collection",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def assign_collection_form(
        doc_id: int,
        collection_id: Optional[str] = Form(default=""),
    ):
        """Assign a document to a collection via form POST."""
        try:
            validate_doc_id(doc_id)
        except ValidationError as e:
            return HTMLResponse(
                content=_render_error("Invalid document ID", e.message),
                status_code=400,
            )

        db = get_db()
        doc = await db.get_document(doc_id)
        if doc is None:
            return HTMLResponse(
                content=_render_error(
                    "Not Found",
                    f"Document {doc_id} not found.",
                ),
                status_code=404,
            )

        cid_str = (collection_id or "").strip()
        if not cid_str:
            # No collection selected — remove from current collection
            await db.remove_document_from_collection(doc_id)
        else:
            try:
                cid = int(cid_str)
            except (ValueError, TypeError):
                return HTMLResponse(
                    content=_render_error(
                        "Validation Error",
                        "Collection ID must be a number.",
                    ),
                    status_code=400,
                )
            col = await db.get_collection(cid)
            if col is None:
                return HTMLResponse(
                    content=_render_error(
                        "Not Found",
                        f"Collection {cid} not found.",
                    ),
                    status_code=404,
                )
            await db.assign_document_to_collection(doc_id, cid)

        return RedirectResponse(
            url=f"/documents/{doc_id}", status_code=303,
        )

    return app


# ── Helpers ────────────────────────────────────────────────────


def _get_ext(filename: Optional[str]) -> Optional[str]:
    if not filename:
        return None
    return Path(filename).suffix.lower() or None


def _extract_body(raw: bytes, ext: str, filename: str) -> str:
    """Extract plain text body from raw file bytes."""
    try:
        if ext in (".txt", ".md", ".csv"):
            return raw.decode("utf-8", errors="replace")
        elif ext == ".json":
            data = json.loads(raw.decode("utf-8", errors="replace"))
            return json.dumps(data, ensure_ascii=False, indent=2)
        elif ext == ".xml":
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(raw, "xml")
            return soup.get_text(separator="\n", strip=True)
        elif ext == ".pdf":
            import io
            import pdfplumber
            text_parts: list[str] = []
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)
            return "\n\n".join(text_parts)
        elif ext == ".docx":
            import io
            from docx import Document
            doc = Document(io.BytesIO(raw))
            return "\n".join(p.text for p in doc.paragraphs)
        elif ext in (".html", ".htm"):
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(raw, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            return soup.get_text(separator="\n", strip=True)
        else:
            return raw.decode("utf-8", errors="replace")
    except Exception:
        return raw.decode("utf-8", errors="replace")
app = create_app()


def main():
    """Run the server with uvicorn."""
    import uvicorn
    uvicorn.run(
        "src.web.server:app",
        host=config.server.host,
        port=config.server.port,
        workers=config.server.workers,
        log_level="debug" if config.debug else "info",
    )


if __name__ == "__main__":
    main()
