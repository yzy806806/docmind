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
- POST /upload                   File upload form
- POST /api/v1/documents/submit  Programmatic document submission
- POST /api/v1/documents/batch   Batch document submission
- GET  /api/v1/documents/{id}/status  Document processing status
- GET  /api/v1/jobs/{id}         Background job status

WebSocket:
- WS   /chat                     Real-time Q&A with citation tracking
"""

from __future__ import annotations

import json
import logging
import mimetypes
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse

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
from .chat import handle_chat

logger = logging.getLogger(__name__)

# ── State ──────────────────────────────────────────────────────

_db: Optional[Database] = None
_queue: Optional[JobQueue] = None


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
    _queue = JobQueue(_db)
    logger.info(
        "DocMind server started on %s:%d",
        config.server.host,
        config.server.port,
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
        description="AI-powered enterprise document knowledge base",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

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

    # ── Web UI Routes ───────────────────────────────────────

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard():
        """Dashboard page with knowledge base statistics."""
        db = get_db()
        try:
            stats = await db.get_stats()

            # Get recent documents
            recent = await db.list_documents(limit=10)
        except Exception:
            stats = {
                "total": 0, "pending": 0, "indexed": 0,
                "summarized": 0, "active_jobs": 0,
            }
            recent = []

        html = _render_dashboard(stats, recent)
        return HTMLResponse(content=html)

    @app.get("/search", response_class=HTMLResponse, include_in_schema=False)
    async def search_page(q: str = Query(default="", description="Search query")):
        """Search page with results and citations."""
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

        html = _render_search_results(validated_q, results)
        return HTMLResponse(content=html)

    @app.get("/documents", response_class=HTMLResponse, include_in_schema=False)
    async def list_documents_page(
        source: str = Query(default=""),
        tag: str = Query(default=""),
        page: int = Query(default=1, ge=1),
        per_page: int = Query(default=20, ge=1, le=100),
    ):
        """List documents with pagination, optional tag/source filtering."""
        db = get_db()
        try:
            if tag:
                # Filter by tag — get all docs with this tag, then paginate manually
                tag_docs = await db.get_documents_by_tag(tag)
                total = len(tag_docs)
                total_pages = (total + per_page - 1) // per_page if per_page > 0 else 0
                offset = (page - 1) * per_page
                documents = tag_docs[offset : offset + per_page]
            else:
                result = await db.list_documents_paginated(
                    page=page, per_page=per_page, source=source if source else None
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

        html = _render_documents_list(
            documents, source, page, per_page, total, total_pages,
            tags_map=tags_map, all_tags=all_tags, active_tag=tag,
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
        html = _render_document_detail(doc, tags)
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

    @app.post("/upload", response_class=HTMLResponse, include_in_schema=False)
    async def upload_page(file: UploadFile = File(None)):
        """File upload form handler."""
        if file is None:
            return HTMLResponse(content=_render_upload_form())

        # Validate and process
        ext = Path(file.filename or "").suffix.lower()
        if ext and ext not in config.document_limits.supported_extensions:
            return HTMLResponse(
                content=_render_upload_form(
                    error=f"Unsupported file type: {ext}"
                ),
            )

        raw = await file.read()
        if len(raw) > config.document_limits.max_file_size_bytes:
            return HTMLResponse(
                content=_render_upload_form(
                    error=f"File too large (max {config.document_limits.max_file_size_bytes:,} bytes)"
                ),
            )

        display_title = file.filename or "untitled"
        mime_type = (
            file.content_type
            or mimetypes.guess_type(file.filename or "")[0]
            or "application/octet-stream"
        )

        body = _extract_body(raw, ext or "", file.filename or "")

        db = get_db()
        queue = get_queue()

        try:
            doc_id = await db.upsert_document(
                path=f"/uploads/{file.filename or 'unknown'}",
                source_type="api",
                source_name="web-upload",
                title=display_title,
                ext=ext or "",
                mime_type=mime_type,
                body=body,
                size=len(raw),
            )

            job = await queue.enqueue(
                document_path=f"/uploads/{file.filename or 'unknown'}",
                document_title=display_title,
                source_name="web-upload",
            )

            return HTMLResponse(
                content=_render_upload_success(display_title, doc_id, job.id)
            )
        except Exception as e:
            logger.exception("Upload failed")
            return HTMLResponse(
                content=_render_upload_form(error=str(e)),
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
        "/api/v1/documents/{doc_id}",
        tags=["documents"],
        summary="Delete a document and its FTS index entry",
    )
    async def delete_document_api(doc_id: int):
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

    @app.get("/chat", response_class=HTMLResponse, include_in_schema=False)
    async def chat_page():
        """Chat page with WebSocket client for real-time Q&A."""
        html = _render_chat_page()
        return HTMLResponse(content=html)

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
    ):
        """Save LLM settings to the DB and reload the in-memory config.

        Security note: if the submitted api_key field is the masked
        placeholder (``****`` prefix), the existing stored key is kept
        unchanged — the user only sees the masked value in the form.
        """
        db = get_db()

        # ── Persist each field ──────────────────────────────────
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

        # ── Reload the in-memory LLMConfig from DB ──────────────
        # Re-read all settings (including the just-saved ones) and apply
        # them to the global config singleton. The LLMClient in chat.py
        # constructs from config.llm on each WebSocket connection, so
        # the new values take effect on the next chat request.
        saved_settings = await db.get_all_settings()
        _reload_llm_config_from_db(saved_settings)

        # Redirect back to /settings?saved=1 to show success banner
        html = _render_settings_redirect()
        return HTMLResponse(
            content=html,
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/settings?saved=1"},
        )

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
    async def list_chat_sessions(limit: int = Query(default=50, ge=1, le=200)):
        """Return recent chat sessions (newest first) with preview + timestamps."""
        db = get_db()
        sessions = await db.list_chat_sessions(limit=limit)
        return {"sessions": sessions, "count": len(sessions)}

    @app.get(
        "/api/v1/chat/sessions/{session_id}/messages",
        tags=["chat"],
        summary="Get full message history for a chat session",
    )
    async def get_chat_messages(session_id: str):
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
    async def delete_chat_session_api(session_id: str):
        """Delete a chat session. Returns 404 if not found."""
        db = get_db()
        deleted = await db.delete_chat_session(session_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No chat session with id {session_id}",
            )
        return {"id": session_id, "deleted": True}

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
    async def batch_submit_documents(body: BatchSubmissionRequest):
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
    async def get_document_status(doc_id: int):
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
    async def get_job_status(job_id: str):
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


# ── HTML Templates (inline, minimal) ────────────────────────────


def _base_page(title: str, content: str, extra_head: str = "") -> str:
    """Render a base HTML page with dark-mode and responsive styling.

    Uses CSS custom properties (variables) for theming. A JavaScript
    toggle in the nav bar switches between light and dark, and the
    preference is persisted in localStorage under ``docmind-theme``.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} — DocMind</title>
    <style>
        :root {{
            --bg: #f5f5f5;
            --surface: #ffffff;
            --text: #333333;
            --text-muted: #666666;
            --text-faint: #888888;
            --header-bg: #1a1a2e;
            --header-text: #ffffff;
            --nav-link: #a8dadc;
            --border: #eeeeee;
            --table-header-bg: #f8f8f8;
            --hover-bg: #fafafa;
            --primary: #1a1a2e;
            --primary-hover: #2d2d4e;
            --input-border: #dddddd;
            --code-bg: #f5f5f5;
            --badge-indexed-bg: #e3f2fd; --badge-indexed-text: #1565c0;
            --badge-summarized-bg: #e8f5e9; --badge-summarized-text: #2e7d32;
            --badge-pending-bg: #fff3e0; --badge-pending-text: #e65100;
            --badge-error-bg: #ffebee; --badge-error-text: #c62828;
            --error-bg: #ffebee; --error-text: #c62828;
            --success-bg: #e8f5e9; --success-text: #2e7d32;
            --shadow: 0 2px 4px rgba(0,0,0,0.1);
            --shadow-sm: 0 1px 3px rgba(0,0,0,0.1);
        }}
        [data-theme="dark"] {{
            --bg: #1a1a2e;
            --surface: #16213e;
            --text: #e0e0e0;
            --text-muted: #b0b0b0;
            --text-faint: #888888;
            --header-bg: #0f0f23;
            --header-text: #e0e0e0;
            --nav-link: #a8dadc;
            --border: #2a2a4a;
            --table-header-bg: #1e1e3a;
            --hover-bg: #1e1e3a;
            --primary: #4a4a6a;
            --primary-hover: #5a5a7a;
            --input-border: #2a2a4a;
            --code-bg: #0d0d1f;
            --badge-indexed-bg: #1a3a5a; --badge-indexed-text: #64b5f6;
            --badge-summarized-bg: #1a3a2a; --badge-summarized-text: #81c784;
            --badge-pending-bg: #3a2a1a; --badge-pending-text: #ffb74d;
            --badge-error-bg: #3a1a1a; --badge-error-text: #ef5350;
            --error-bg: #3a1a1a; --error-text: #ef5350;
            --success-bg: #1a3a2a; --success-text: #81c784;
            --shadow: 0 2px 4px rgba(0,0,0,0.3);
            --shadow-sm: 0 1px 3px rgba(0,0,0,0.3);
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: var(--bg); color: var(--text); line-height: 1.6;
                transition: background 0.2s, color 0.2s; }}
        .container {{ max-width: 960px; margin: 0 auto; padding: 20px; }}
        header {{ background: var(--header-bg); color: var(--header-text); padding: 16px 24px; }}
        .header-row {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; }}
        header h1 {{ font-size: 1.5em; }}
        header nav {{ margin-top: 8px; display: flex; flex-wrap: wrap; align-items: center; gap: 4px 0; }}
        header nav a {{ color: var(--nav-link); text-decoration: none; margin-right: 16px; }}
        header nav a:hover {{ text-decoration: underline; }}
        .theme-toggle {{
            background: none; border: 1px solid var(--nav-link); border-radius: 6px;
            color: var(--nav-link); padding: 4px 10px; cursor: pointer;
            font-size: 1.1em; margin-left: 8px; line-height: 1;
        }}
        .theme-toggle:hover {{ background: rgba(168,218,220,0.15); }}
        .nav-toggle {{
            display: none; background: none; border: none; color: var(--header-text);
            font-size: 1.5em; cursor: pointer; padding: 4px 8px;
        }}
        .card {{ background: var(--surface); border-radius: 8px; padding: 20px; margin: 16px 0;
                 box-shadow: var(--shadow); }}
        .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
                 gap: 16px; margin: 16px 0; }}
        .stat {{ background: var(--surface); border-radius: 8px; padding: 20px; text-align: center;
                 box-shadow: var(--shadow); }}
        .stat-value {{ font-size: 2em; font-weight: bold; color: var(--primary); }}
        .stat-label {{ font-size: 0.85em; color: var(--text-muted); margin-top: 4px; }}
        table {{ width: 100%; border-collapse: collapse; margin: 16px 0; }}
        th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); }}
        th {{ background: var(--table-header-bg); font-weight: 600; }}
        tr:hover {{ background: var(--hover-bg); }}
        .search-box {{ display: flex; gap: 8px; }}
        .search-box input {{ flex: 1; padding: 10px 14px; border: 2px solid var(--input-border);
                            border-radius: 6px; font-size: 1em; background: var(--surface); color: var(--text); }}
        .search-box button {{ padding: 10px 24px; background: var(--primary); color: var(--header-text);
                              border: none; border-radius: 6px; cursor: pointer; font-size: 1em; }}
        .search-box button:hover {{ background: var(--primary-hover); }}
        .result {{ margin: 16px 0; padding: 16px; background: var(--surface); border-radius: 8px;
                   box-shadow: var(--shadow-sm); }}
        .result h3 {{ color: var(--primary); }}
        .result h3 a {{ color: inherit; text-decoration: none; }}
        .result h3 a:hover {{ text-decoration: underline; }}
        .snippet {{ color: var(--text-muted); margin: 8px 0; }}
        .meta {{ font-size: 0.85em; color: var(--text-faint); }}
        .badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px;
                 font-size: 0.75em; font-weight: 600; }}
        .badge-indexed {{ background: var(--badge-indexed-bg); color: var(--badge-indexed-text); }}
        .badge-summarized {{ background: var(--badge-summarized-bg); color: var(--badge-summarized-text); }}
        .badge-pending {{ background: var(--badge-pending-bg); color: var(--badge-pending-text); }}
        .badge-error {{ background: var(--badge-error-bg); color: var(--badge-error-text); }}
        .tag-pill {{ display: inline-block; padding: 2px 10px; border-radius: 14px;
                     font-size: 0.75em; font-weight: 500; text-decoration: none;
                     background: var(--surface); color: var(--text);
                     border: 1px solid var(--input-border); margin: 2px; }}
        .tag-pill:hover {{ background: var(--primary); color: var(--header-text); }}
        .tag-pill .tag-remove {{ margin-left: 6px; text-decoration: none; color: var(--badge-error-text);
                                  font-weight: bold; }}
        .tag-pill .tag-remove:hover {{ color: var(--error-text); }}
        .tag-cloud {{ background: var(--surface); border-radius: 8px; padding: 16px;
                      box-shadow: var(--shadow); margin-top: 16px; }}
        .tag-cloud h3 {{ margin-top: 0; color: var(--primary); }}
        .tag-cloud-items {{ display: flex; flex-wrap: wrap; gap: 6px; }}
        .tag-cloud-item {{ display: inline-flex; align-items: center; gap: 4px;
                           padding: 4px 12px; border-radius: 14px; font-size: 0.85em;
                           text-decoration: none; background: var(--code-bg); color: var(--text);
                           border: 1px solid var(--input-border); }}
        .tag-cloud-item:hover {{ background: var(--primary); color: var(--header-text); }}
        .tag-cloud-item .tag-count {{ font-size: 0.8em; opacity: 0.7; }}
        .tag-cloud-item.active {{ background: var(--primary); color: var(--header-text); font-weight: 600; }}
        .tag-input-row {{ display: flex; gap: 8px; margin-top: 8px; }}
        .tag-input-row input {{ flex: 1; padding: 8px 12px; border-radius: 6px;
                                border: 1px solid var(--input-border); background: var(--input-bg); color: var(--text); }}
        .tag-input-row button {{ padding: 8px 16px; border-radius: 6px; border: none;
                                  background: var(--primary); color: var(--header-text); cursor: pointer; }}
        .doc-tags {{ margin: 12px 0; }}
        .doc-tags .field-label {{ font-weight: 600; color: var(--text-muted); margin-right: 8px; }}
        .error {{ background: var(--error-bg); color: var(--error-text); padding: 12px 16px;
                 border-radius: 6px; margin: 12px 0; }}
        .success {{ background: var(--success-bg); color: var(--success-text); padding: 12px 16px;
                   border-radius: 6px; margin: 12px 0; }}
        .upload-form {{ background: var(--surface); border-radius: 8px; padding: 24px;
                       box-shadow: var(--shadow); }}
        .upload-form input[type="file"] {{ margin: 12px 0; }}
        .upload-form button {{ padding: 10px 24px; background: var(--primary); color: var(--header-text);
                               border: none; border-radius: 6px; cursor: pointer; }}
        .doc-detail h2 {{ color: var(--primary); margin-bottom: 16px; }}
        .doc-detail .field {{ margin: 8px 0; }}
        .doc-detail .field-label {{ font-weight: 600; color: var(--text-muted); }}
        .doc-detail pre {{ background: var(--code-bg); padding: 16px; border-radius: 6px;
                          overflow-x: auto; font-size: 0.9em; white-space: pre-wrap; color: var(--text); }}
        .doc-actions {{ margin-top: 20px; display: flex; gap: 12px; }}
        .btn-delete {{ padding: 10px 24px; background: var(--badge-error-bg); color: var(--badge-error-text);
                       border: 1px solid var(--badge-error-text); border-radius: 6px; cursor: pointer; font-size: 1em; }}
        .btn-delete:hover {{ background: var(--error-bg); }}
        .pagination {{ display: flex; justify-content: center; align-items: center; gap: 8px; margin: 20px 0; flex-wrap: wrap; }}
        .pagination a, .pagination span {{
            padding: 6px 12px; border-radius: 6px; text-decoration: none;
            border: 1px solid var(--input-border); color: var(--text); background: var(--surface);
        }}
        .pagination a:hover {{ background: var(--hover-bg); }}
        .pagination .current {{ background: var(--primary); color: var(--header-text); border-color: var(--primary); }}
        .pagination .disabled {{ color: var(--text-faint); opacity: 0.5; cursor: default; }}
        .pagination-info {{ text-align: center; color: var(--text-muted); font-size: 0.85em; margin-bottom: 8px; }}
        .chat-box {{ display: flex; flex-direction: column; gap: 8px; }}
        .chat-messages {{ min-height: 300px; max-height: 500px; overflow-y: auto; border: 1px solid var(--border);
                         border-radius: 6px; padding: 12px; background: var(--code-bg); }}
        .chat-msg {{ margin: 6px 0; padding: 8px 14px; border-radius: 10px; max-width: 85%; word-wrap: break-word; }}
        .chat-msg.user {{ color: var(--header-text); background: var(--primary); margin-left: auto; text-align: right; }}
        .chat-msg.bot {{ color: var(--text); background: var(--surface); border: 1px solid var(--border); margin-right: auto; }}
        .chat-msg.error {{ color: var(--badge-error-text); background: var(--badge-error-bg); margin-right: auto; }}
        .chat-msg.typing {{ color: var(--text-faint); font-style: italic; }}
        .typing-indicator {{ display: inline-block; animation: blink 1.4s infinite; }}
        @keyframes blink {{ 0%, 100% {{ opacity: 0.2; }} 50% {{ opacity: 1; }} }}
        .chat-input-row {{ display: flex; gap: 8px; }}
        .chat-input-row input {{ flex: 1; padding: 10px 14px; border: 2px solid var(--input-border);
                                 border-radius: 6px; font-size: 1em; background: var(--surface); color: var(--text); }}
        .chat-input-row button {{ padding: 10px 24px; background: var(--primary); color: var(--header-text);
                                  border: none; border-radius: 6px; cursor: pointer; }}
        .chat-input-row button:disabled {{ opacity: 0.5; cursor: default; }}
        .chat-status {{ font-size: 0.85em; color: var(--text-faint); }}
        .citations-panel {{ margin-top: 12px; }}
        .citations-panel h3 {{ color: var(--primary); font-size: 1em; }}
        .citation-item {{ font-size: 0.85em; margin: 4px 0; padding: 4px 8px;
                         border-left: 3px solid var(--primary); color: var(--text-muted); }}
        /* Chat history sidebar */
        .chat-layout {{ display: flex; gap: 16px; align-items: flex-start; margin: 16px 0; }}
        .chat-sidebar {{ width: 260px; flex-shrink: 0; background: var(--surface); border-radius: 8px;
                         box-shadow: var(--shadow); padding: 12px; max-height: 600px; overflow-y: auto; }}
        .chat-sidebar-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
        .chat-sidebar-header h3 {{ font-size: 1em; color: var(--primary); }}
        .btn-new-chat {{ background: var(--primary); color: var(--header-text); border: none;
                         border-radius: 6px; padding: 4px 10px; cursor: pointer; font-size: 0.85em; }}
        .btn-new-chat:hover {{ background: var(--primary-hover); }}
        .chat-session-list {{ display: flex; flex-direction: column; gap: 4px; }}
        .chat-session-item {{ position: relative; padding: 8px 10px; border-radius: 6px; cursor: pointer;
                              border: 1px solid transparent; }}
        .chat-session-item:hover {{ background: var(--hover-bg); }}
        .chat-session-item.active {{ background: var(--hover-bg); border-color: var(--primary); }}
        .chat-session-title {{ font-weight: 600; font-size: 0.9em; color: var(--text);
                               white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding-right: 20px; }}
        .chat-session-preview {{ font-size: 0.8em; color: var(--text-faint);
                                white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px; }}
        .chat-session-del {{ position: absolute; top: 4px; right: 4px; background: none; border: none;
                             color: var(--text-faint); cursor: pointer; font-size: 1.2em; line-height: 1;
                             padding: 2px 6px; border-radius: 4px; }}
        .chat-session-del:hover {{ color: var(--badge-error-text); background: var(--badge-error-bg); }}
        .chat-main {{ flex: 1; min-width: 0; }}
        /* Settings form */
        .settings-field {{ margin: 16px 0; }}
        .settings-field label {{ display: block; margin-bottom: 4px; }}
        .settings-field input[type="text"],
        .settings-field input[type="password"],
        .settings-field select {{ width: 100%; padding: 8px 12px; border: 2px solid var(--input-border);
                                  border-radius: 6px; font-size: 1em; background: var(--surface); color: var(--text); }}
        .settings-field input[type="range"] {{ width: 100%; }}
        .settings-hint {{ font-size: 0.85em; color: var(--text-faint); margin-top: 4px; }}
        .settings-actions {{ margin-top: 20px; display: flex; gap: 12px; align-items: center; }}
        .btn-save {{ padding: 10px 24px; background: var(--primary); color: var(--header-text);
                     border: none; border-radius: 6px; cursor: pointer; font-size: 1em; }}
        .btn-save:hover {{ background: var(--primary-hover); }}
        .btn-cancel {{ color: var(--text-muted); text-decoration: none; }}
        .btn-cancel:hover {{ text-decoration: underline; }}
        footer {{ text-align: center; padding: 24px; color: var(--text-faint); font-size: 0.85em; }}
        /* Mobile responsive */
        @media (max-width: 640px) {{
            .container {{ padding: 12px; }}
            .header-row {{ flex-direction: column; align-items: flex-start; }}
            .nav-toggle {{ display: block; }}
            header nav {{ display: none; flex-direction: column; width: 100%; }}
            header nav.open {{ display: flex; }}
            header nav a {{ margin-right: 0; margin-bottom: 8px; display: block; }}
            .stats {{ grid-template-columns: 1fr; }}
            .search-box {{ flex-direction: column; }}
            .search-box button {{ width: 100%; }}
            table {{ font-size: 0.85em; }}
            th, td {{ padding: 6px 8px; }}
            .doc-actions {{ flex-direction: column; }}
            .chat-input-row {{ flex-direction: column; }}
            .chat-input-row button {{ width: 100%; }}
            .chat-layout {{ flex-direction: column; }}
            .chat-sidebar {{ width: 100%; max-height: 200px; }}
        }}
        {extra_head}
    </style>
</head>
<body>
    <header>
        <div class="container">
            <div class="header-row">
                <h1>📚 DocMind</h1>
                <button class="nav-toggle" onclick="document.querySelector('header nav').classList.toggle('open')">☰</button>
                <button class="theme-toggle" onclick="toggleTheme()" title="Toggle dark mode">🌙</button>
            </div>
            <nav>
                <a href="/">Dashboard</a>
                <a href="/search">Search</a>
                <a href="/documents">Documents</a>
                <a href="/upload">Upload</a>
                <a href="/chat">Chat</a>
                <a href="/settings">Settings</a>
                <a href="/docs">API Docs</a>
            </nav>
        </div>
    </header>
    <div class="container">
        {content}
    </div>
    <footer>DocMind v0.1.0 — AI-Powered Document Knowledge Base</footer>
    <script>
        (function() {{
            var t = localStorage.getItem('docmind-theme') || 'light';
            document.documentElement.setAttribute('data-theme', t);
            updateToggleIcon(t);
        }})();
        function toggleTheme() {{
            var cur = document.documentElement.getAttribute('data-theme') || 'light';
            var next = cur === 'dark' ? 'light' : 'dark';
            document.documentElement.setAttribute('data-theme', next);
            localStorage.setItem('docmind-theme', next);
            updateToggleIcon(next);
        }}
        function updateToggleIcon(theme) {{
            var btn = document.querySelector('.theme-toggle');
            if (btn) btn.textContent = theme === 'dark' ? '☀️' : '🌙';
        }}
    </script>
</body>
</html>"""


def _render_dashboard(stats: dict, recent: list[dict]) -> str:
    recent_rows = ""
    for doc in recent:
        status_class = f"badge-{doc.get('status', 'pending')}"
        recent_rows += f"""
        <tr>
            <td><a href="/documents/{doc['id']}">[{doc['id']}] {doc.get('title', 'Untitled')}</a></td>
            <td><span class="badge {status_class}">{doc.get('status', '')}</span></td>
            <td>{doc.get('ext', '')}</td>
            <td>{_fmt_date(doc.get('created_at', ''))}</td>
        </tr>"""

    content = f"""
    <div class="stats">
        <div class="stat">
            <div class="stat-value">{stats['total']}</div>
            <div class="stat-label">Total Documents</div>
        </div>
        <div class="stat">
            <div class="stat-value">{stats['indexed'] + stats['summarized']}</div>
            <div class="stat-label">Processed</div>
        </div>
        <div class="stat">
            <div class="stat-value">{stats['summarized']}</div>
            <div class="stat-label">Summarized</div>
        </div>
        <div class="stat">
            <div class="stat-value">{stats['active_jobs']}</div>
            <div class="stat-label">Active Jobs</div>
        </div>
    </div>

    <div class="card">
        <h2>Quick Search</h2>
        <form action="/search" method="get" class="search-box">
            <input type="text" name="q" placeholder="Search your documents..." required>
            <button type="submit">Search</button>
        </form>
    </div>

    <div class="card">
        <h2>Recent Documents</h2>
        {'<table><tr><th>Document</th><th>Status</th><th>Type</th><th>Date</th></tr>' + recent_rows + '</table>' if recent else '<p>No documents indexed yet. <a href="/upload">Upload one</a> to get started.</p>'}
    </div>
    """
    return _base_page("Dashboard", content)


def _render_search_form(error: str = "") -> str:
    error_html = f'<div class="error">{error}</div>' if error else ""
    content = f"""
    <div class="card">
        <h2>Search Documents</h2>
        {error_html}
        <form action="/search" method="get" class="search-box">
            <input type="text" name="q" placeholder="Enter your search query..." required autofocus>
            <button type="submit">Search</button>
        </form>
    </div>
    """
    return _base_page("Search", content)


def _render_search_results(query: str, results: list[dict]) -> str:
    results_html = ""
    if results:
        for r in results:
            doc_id = r.get("id", "?")
            title = r.get("title", "Untitled")
            snippet = r.get("snippet", r.get("raw_preview", ""))
            summary = r.get("summary", "")
            status_val = r.get("status", "pending")
            rank = r.get("rank", 0)

            results_html += f"""
            <div class="result">
                <h3><a href="/documents/{doc_id}">[{doc_id}] {title}</a></h3>
                <div class="meta">
                    Status: <span class="badge badge-{status_val}">{status_val}</span>
                    {' | Score: ' + f'{rank:.2f}' if rank else ''}
                </div>
                {'<div class="snippet"><strong>Summary:</strong> ' + summary + '</div>' if summary else ''}
                {'<div class="snippet">' + (snippet[:300] or '') + '</div>' if snippet else ''}
            </div>"""
    else:
        results_html = "<p>No results found. Try different keywords.</p>"

    content = f"""
    <div class="card">
        <h2>Search Results</h2>
        <form action="/search" method="get" class="search-box">
            <input type="text" name="q" value="{_escape(query)}" required>
            <button type="submit">Search</button>
        </form>
    </div>

    <p>Found {len(results)} result(s) for: <strong>{_escape(query)}</strong></p>
    {results_html}
    """
    return _base_page(f"Search: {query}", content)


def _render_documents_list(
    documents: list[dict],
    source: str,
    page: int = 1,
    per_page: int = 20,
    total: int = 0,
    total_pages: int = 0,
    *,
    tags_map: dict[int, list[str]] | None = None,
    all_tags: list[dict] | None = None,
    active_tag: str = "",
) -> str:
    tags_map = tags_map or {}
    all_tags = all_tags or []
    rows = ""
    for doc in documents:
        status_class = f"badge-{doc.get('status', 'pending')}"
        doc_tags = tags_map.get(doc["id"], [])
        tag_badges = ""
        if doc_tags:
            tag_badges = '<div class="doc-tags">' + "".join(
                f'<a href="/documents?tag={_escape(t)}" class="tag-pill">{_escape(t)}</a>'
                for t in doc_tags
            ) + "</div>"
        rows += f"""
        <tr>
            <td><a href="/documents/{doc['id']}">[{doc['id']}] {doc.get('title', 'Untitled')}</a></td>
            <td><span class="badge {status_class}">{doc.get('status', '')}</span></td>
            <td>{doc.get('source_name', doc.get('source_type', ''))}</td>
            <td>{doc.get('ext', '')}</td>
            <td>{_fmt_date(doc.get('created_at', ''))}</td>
            <td>{tag_badges}</td>
        </tr>"""

    source_param = f"&source={_escape(source)}" if source else ""
    tag_param = f"&tag={_escape(active_tag)}" if active_tag else ""
    pagination_html = _render_pagination(
        page, per_page, total, total_pages, source_param + tag_param
    )

    start = (page - 1) * per_page + 1 if total > 0 else 0
    end = min(page * per_page, total)

    # Tag cloud sidebar
    tag_cloud_html = ""
    if all_tags:
        tag_items = ""
        for t in all_tags:
            tag_name = t["tag"]
            count = t["count"]
            active_class = " active" if tag_name == active_tag else ""
            tag_items += (
                f'<a href="/documents?tag={_escape(tag_name)}" '
                f'class="tag-cloud-item{active_class}">'
                f'{_escape(tag_name)} <span class="tag-count">({count})</span></a>'
            )
        tag_cloud_html = f"""
        <div class="tag-cloud">
            <h3>Tags</h3>
            <div class="tag-cloud-items">
                {tag_items}
            </div>
            {'<p style="margin-top:8px;"><a href="/documents">← Show all documents</a></p>' if active_tag else ''}
        </div>"""

    filter_label = ""
    if active_tag:
        filter_label = f" — tag: {_escape(active_tag)}"
    elif source:
        filter_label = f" — {_escape(source)}"

    # Add a Tags column header if any document has tags
    tags_col_header = "<th>Tags</th>" if tags_map else ""

    content = f"""
    <div class="card">
        <h2>Documents{filter_label}</h2>
        <div class="pagination-info">Showing {start}–{end} of {total} document(s)</div>
        {'<table><tr><th>Document</th><th>Status</th><th>Source</th><th>Type</th><th>Date</th>' + tags_col_header + '</tr>' + rows + '</table>' if documents else '<p>No documents found.</p>'}
    </div>
    {tag_cloud_html}
    {pagination_html}
    """
    return _base_page("Documents", content)


def _render_document_detail(doc: dict, tags: list[str] | None = None) -> str:
    tags = tags or []
    status_class = f"badge-{doc.get('status', 'pending')}"
    body_preview = (doc.get("body", "") or "")[:2000]
    if len(doc.get("body", "") or "") > 2000:
        body_preview += "\n... (truncated)"

    # Build tag badges with remove buttons
    tag_badges_html = ""
    if tags:
        tag_badges_html = '<div class="doc-tags">'
        for t in tags:
            tag_badges_html += (
                f'<span class="tag-pill">{_escape(t)}'
                f'<form action="/documents/{doc.get("id", "?")}/tags/{_escape(t)}/delete" '
                f'method="post" style="display:inline;">'
                f'<button type="submit" class="tag-remove" title="Remove tag">✕</button>'
                f'</form></span>'
            )
        tag_badges_html += "</div>"

    tag_section = f"""
    <div class="doc-tags-section">
        <div class="field"><span class="field-label">Tags:</span>
            {tag_badges_html if tag_badges_html else '<em>No tags yet</em>'}
        </div>
        <form action="/documents/{doc.get('id', '?')}/tags" method="post" class="tag-input-row">
            <input type="text" name="tag" placeholder="Add a tag…" required maxlength="50">
            <button type="submit">Add Tag</button>
        </form>
    </div>"""

    content = f"""
    <div class="card doc-detail">
        <h2>{doc.get('title', 'Untitled')}</h2>
        <div class="field"><span class="field-label">ID:</span> {doc.get('id', '?')}</div>
        <div class="field"><span class="field-label">Status:</span> <span class="badge {status_class}">{doc.get('status', '')}</span></div>
        <div class="field"><span class="field-label">Path:</span> {doc.get('path', '')}</div>
        <div class="field"><span class="field-label">Source:</span> {doc.get('source_name', doc.get('source_type', ''))}</div>
        <div class="field"><span class="field-label">Type:</span> {doc.get('ext', '')} ({doc.get('mime_type', '')})</div>
        <div class="field"><span class="field-label">Size:</span> {_fmt_size(doc.get('size', 0))}</div>
        <div class="field"><span class="field-label">Created:</span> {_fmt_date(doc.get('created_at', ''))}</div>
        <div class="field"><span class="field-label">Updated:</span> {_fmt_date(doc.get('updated_at', ''))}</div>

        {tag_section}

        {'<h3>Summary</h3><p>' + (doc.get('summary') or '<em>No summary available</em>') + '</p>' if doc.get('summary') else ''}

        <h3>Content Preview</h3>
        <pre>{_escape(body_preview)}</pre>

        <div class="doc-actions">
            <form action="/documents/{doc.get('id', '?')}/delete" method="post"
                  onsubmit="return confirm('Are you sure you want to delete document {doc.get('id', '?')}? This cannot be undone.');">
                <button type="submit" class="btn-delete">🗑 Delete Document</button>
            </form>
        </div>
    </div>
    """
    return _base_page(doc.get('title', 'Document Detail'), content)


def _render_upload_form(error: str = "") -> str:
    error_html = f'<div class="error">{error}</div>' if error else ""
    content = f"""
    <div class="upload-form">
        <h2>Upload Document</h2>
        {error_html}
        <form action="/upload" method="post" enctype="multipart/form-data">
            <p><input type="file" name="file" required></p>
            <p><button type="submit">Upload & Index</button></p>
        </form>
        <p style="margin-top: 16px; color: #888; font-size: 0.9em;">
            Supported formats: PDF, DOCX, HTML, MD, TXT, CSV, JSON, XML
        </p>
    </div>
    """
    return _base_page("Upload", content)


def _render_upload_success(title: str, doc_id: int, job_id: str) -> str:
    content = f"""
    <div class="success">
        <h2>✅ Upload Successful</h2>
        <p><strong>{_escape(title)}</strong> has been uploaded and queued for processing.</p>
        <p>Document ID: <a href="/documents/{doc_id}">{doc_id}</a></p>
        <p>Job ID: {job_id}</p>
    </div>
    <p><a href="/upload">Upload another</a> | <a href="/documents">View all documents</a></p>
    """
    return _base_page("Upload Success", content)


def _render_pagination(
    page: int,
    per_page: int,
    total: int,
    total_pages: int,
    extra_params: str = "",
) -> str:
    """Render pagination navigation with prev/next and page numbers."""
    if total_pages <= 1:
        return ""

    base = f"?per_page={per_page}{extra_params}"

    parts: list[str] = ['<div class="pagination">']

    # Prev button
    if page > 1:
        parts.append(f'<a href="{base}&page={page - 1}">← Prev</a>')
    else:
        parts.append('<span class="disabled">← Prev</span>')

    # Page numbers (show up to 7 pages with ellipsis)
    max_show = 7
    if total_pages <= max_show:
        for p in range(1, total_pages + 1):
            if p == page:
                parts.append(f'<span class="current">{p}</span>')
            else:
                parts.append(f'<a href="{base}&page={p}">{p}</a>')
    else:
        # Show first, last, and pages around current
        half = max_show // 2
        start_page = max(1, page - half)
        end_page = min(total_pages, page + half)
        if start_page > 1:
            parts.append(f'<a href="{base}&page=1">1</a>')
            if start_page > 2:
                parts.append('<span class="disabled">…</span>')
        for p in range(start_page, end_page + 1):
            if p == page:
                parts.append(f'<span class="current">{p}</span>')
            else:
                parts.append(f'<a href="{base}&page={p}">{p}</a>')
        if end_page < total_pages:
            if end_page < total_pages - 1:
                parts.append('<span class="disabled">…</span>')
            parts.append(f'<a href="{base}&page={total_pages}">{total_pages}</a>')

    # Next button
    if page < total_pages:
        parts.append(f'<a href="{base}&page={page + 1}">Next →</a>')
    else:
        parts.append('<span class="disabled">Next →</span>')

    parts.append("</div>")
    return "\n".join(parts)


def _render_delete_success(doc_id: int) -> str:
    content = f"""
    <div class="success">
        <h2>🗑 Document Deleted</h2>
        <p>Document <strong>{doc_id}</strong> has been deleted from the knowledge base.</p>
    </div>
    <p><a href="/documents">← Back to Documents</a></p>
    """
    return _base_page("Document Deleted", content)


def _render_chat_page() -> str:
    content = """
    <div class="chat-layout">
        <aside class="chat-sidebar" id="chat-sidebar">
            <div class="chat-sidebar-header">
                <h3>Conversations</h3>
                <button id="new-chat-btn" class="btn-new-chat" title="Start a new chat">+ New</button>
            </div>
            <div class="chat-session-list" id="chat-session-list">
                <p class="pagination-info">Loading...</p>
            </div>
        </aside>
        <div class="card chat-main">
            <h2 id="chat-title">Chat with Your Documents</h2>
            <p class="pagination-info">Ask questions and get AI-powered answers with citation tracking.</p>
            <div class="chat-box">
                <div class="chat-messages" id="chat-messages">
                    <div class="chat-msg bot">Connecting...</div>
                </div>
                <div class="chat-input-row">
                    <input type="text" id="chat-input" placeholder="Ask a question..."
                           onkeydown="if(event.key==='Enter')sendChat()" autofocus>
                    <button id="chat-send-btn" onclick="sendChat()">Send</button>
                </div>
                <div class="chat-status" id="chat-status">Disconnected</div>
            </div>
            <div class="citations-panel" id="citations-panel" style="display:none;">
                <h3>Citations</h3>
                <div id="citations-list"></div>
            </div>
        </div>
    </div>
    <script>
        var ws = null;
        var citations = [];
        var currentAnswer = '';
        var isStreaming = false;
        var sendBtn, inputField;
        var currentSessionId = null;
        var sessionTitle = 'New Chat';

        function getQueryParam(name) {
            var params = new URLSearchParams(window.location.search);
            return params.get(name);
        }

        function getWsUrl() {
            var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
            var url = proto + '//' + location.host + '/chat';
            if (currentSessionId) {
                url += '?session_id=' + encodeURIComponent(currentSessionId);
            }
            return url;
        }

        function setSession(id, title) {
            currentSessionId = id;
            sessionTitle = title || 'New Chat';
            var titleEl = document.getElementById('chat-title');
            if (titleEl) titleEl.textContent = sessionTitle;
            // Update URL without reload
            var newUrl = window.location.pathname;
            if (id) newUrl += '?session=' + encodeURIComponent(id);
            history.replaceState({}, '', newUrl);
        }

        function clearMessages() {
            document.getElementById('chat-messages').innerHTML = '';
            citations = [];
            currentAnswer = '';
            document.getElementById('citations-panel').style.display = 'none';
            document.getElementById('citations-list').innerHTML = '';
        }

        function connectChat() {
            ws = new WebSocket(getWsUrl());
            ws.onopen = function() {
                document.getElementById('chat-status').textContent = 'Connected';
            };
            ws.onclose = function() {
                document.getElementById('chat-status').textContent = 'Disconnected';
                addMsg('bot', 'Disconnected. Reconnecting in 3s...');
                setTimeout(connectChat, 3000);
            };
            ws.onerror = function() {
                document.getElementById('chat-status').textContent = 'Error';
            };
            ws.onmessage = function(event) {
                var msg = JSON.parse(event.data);
                handleChatMessage(msg);
            };
        }
        function sendChat() {
            inputField = document.getElementById('chat-input');
            sendBtn = document.getElementById('chat-send-btn');
            var text = inputField.value.trim();
            if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
            addMsg('user', text);
            ws.send(JSON.stringify({type: 'question', text: text}));
            inputField.value = '';
            inputField.disabled = true;
            sendBtn.disabled = true;
            citations = [];
            currentAnswer = '';
            isStreaming = false;
            document.getElementById('citations-panel').style.display = 'none';
            document.getElementById('citations-list').innerHTML = '';
            showTypingIndicator();
        }
        function showTypingIndicator() {
            var box = document.getElementById('chat-messages');
            var div = document.createElement('div');
            div.className = 'chat-msg bot typing';
            div.id = 'typing-indicator-msg';
            div.innerHTML = 'Thinking<span class="typing-indicator">...</span>';
            box.appendChild(div);
            box.scrollTop = box.scrollHeight;
        }
        function removeTypingIndicator() {
            var el = document.getElementById('typing-indicator-msg');
            if (el) el.remove();
        }
        function handleChatMessage(msg) {
            switch(msg.type) {
                case 'connected':
                    if (msg.session_id) {
                        setSession(msg.session_id, msg.title);
                        loadSessionList();
                    }
                    break;
                case 'history':
                    clearMessages();
                    if (msg.messages && msg.messages.length) {
                        msg.messages.forEach(function(m) {
                            addMsg(m.role === 'user' ? 'user' : 'bot', m.content);
                            if (m.role === 'assistant' && m.citations && m.citations.length) {
                                citations = m.citations;
                                renderCitations();
                            }
                        });
                    }
                    break;
                case 'citation:added':
                    citations.push(msg);
                    renderCitations();
                    break;
                case 'answer:chunk':
                    removeTypingIndicator();
                    appendChunk(msg.text);
                    break;
                case 'answer:done':
                    removeTypingIndicator();
                    if (msg.text && msg.text !== currentAnswer) {
                        var box = document.getElementById('chat-messages');
                        var lastBot = box.querySelector('.chat-msg.bot:last-child');
                        if (lastBot && lastBot.dataset.streaming === 'true') {
                            lastBot.textContent = msg.text;
                            currentAnswer = msg.text;
                        } else {
                            addMsg('bot', msg.text);
                        }
                    }
                    if (msg.session_id && msg.session_id !== currentSessionId) {
                        setSession(msg.session_id, msg.title);
                    }
                    isStreaming = false;
                    inputField = document.getElementById('chat-input');
                    sendBtn = document.getElementById('chat-send-btn');
                    inputField.disabled = false;
                    sendBtn.disabled = false;
                    inputField.focus();
                    renderCitations();
                    loadSessionList();
                    break;
                case 'error':
                    removeTypingIndicator();
                    addMsg('error', msg.message);
                    inputField = document.getElementById('chat-input');
                    sendBtn = document.getElementById('chat-send-btn');
                    inputField.disabled = false;
                    sendBtn.disabled = false;
                    break;
                case 'pong':
                    break;
            }
        }
        function addMsg(cls, text) {
            var div = document.createElement('div');
            div.className = 'chat-msg ' + cls;
            div.textContent = text;
            document.getElementById('chat-messages').appendChild(div);
            var box = document.getElementById('chat-messages');
            box.scrollTop = box.scrollHeight;
        }
        function appendChunk(text) {
            currentAnswer += text;
            var box = document.getElementById('chat-messages');
            var lastBot = box.querySelector('.chat-msg.bot:last-child');
            if (lastBot && lastBot.dataset.streaming === 'true') {
                lastBot.textContent = currentAnswer;
            } else {
                currentAnswer = text;
                addMsg('bot', currentAnswer);
                var last = box.querySelector('.chat-msg.bot:last-child');
                if (last) last.dataset.streaming = 'true';
            }
            box.scrollTop = box.scrollHeight;
        }
        function renderCitations() {
            if (citations.length === 0) return;
            var panel = document.getElementById('citations-panel');
            var list = document.getElementById('citations-list');
            list.innerHTML = citations.map(function(c) {
                return '<div class="citation-item"><strong>[' + c.ref + ']</strong> ' +
                       '<a href="/documents/' + c.doc_id + '">' + c.title + '</a>' +
                       ' (confidence: ' + (c.confidence || 'low') + ')</div>';
            }).join('');
            panel.style.display = 'block';
        }
        function loadSessionList() {
            fetch('/api/v1/chat/sessions?limit=30')
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    var listEl = document.getElementById('chat-session-list');
                    if (!data.sessions || data.sessions.length === 0) {
                        listEl.innerHTML = '<p class="pagination-info">No conversations yet.</p>';
                        return;
                    }
                    listEl.innerHTML = data.sessions.map(function(s) {
                        var active = (s.id === currentSessionId) ? ' active' : '';
                        var title = s.title || 'New Chat';
                        var preview = s.preview || '';
                        var safeTitle = title.replace(/</g, '&lt;').replace(/>/g, '&gt;');
                        var safePreview = preview.replace(/</g, '&lt;').replace(/>/g, '&gt;');
                        return '<div class="chat-session-item' + active + '" ' +
                               'onclick="loadSession(\\'' + s.id + '\\', \\'' + safeTitle.replace(/'/g, "\\\\'") + '\\')">' +
                               '<div class="chat-session-title">' + safeTitle + '</div>' +
                               '<div class="chat-session-preview">' + safePreview + '</div>' +
                               '<button class="chat-session-del" title="Delete" ' +
                               'onclick="deleteSession(event, \\'' + s.id + '\\')">&times;</button>' +
                               '</div>';
                    }).join('');
                })
                .catch(function() {
                    document.getElementById('chat-session-list').innerHTML =
                        '<p class="pagination-info">Could not load sessions.</p>';
                });
        }
        function loadSession(id, title) {
            setSession(id, title);
            clearMessages();
            // Reconnect WebSocket with the new session_id
            if (ws) { try { ws.close(); } catch(e) {} }
            connectChat();
        }
        function deleteSession(event, id) {
            event.stopPropagation();
            if (!confirm('Delete this conversation? This cannot be undone.')) return;
            fetch('/api/v1/chat/sessions/' + encodeURIComponent(id), {method: 'DELETE'})
                .then(function(r) { if (!r.ok) throw new Error('delete failed'); return r.json(); })
                .then(function() {
                    if (id === currentSessionId) {
                        window.location.href = '/chat';
                    } else {
                        loadSessionList();
                    }
                })
                .catch(function() { alert('Failed to delete session.'); });
        }
        function startNewChat() {
            window.location.href = '/chat';
        }
        document.getElementById('new-chat-btn').addEventListener('click', startNewChat);
        // On load: pick up ?session=xxx if present
        (function() {
            var sid = getQueryParam('session');
            if (sid) { currentSessionId = sid; }
            loadSessionList();
            connectChat();
        })();
    </script>
    """
    return _base_page("Chat", content)


# ── Settings page renderer ──────────────────────────────────────


def _mask_api_key(key: str) -> str:
    """Mask an API key for display, showing only the last 4 characters.

    Examples:
        "sk-abc123XYZ" -> "****XYZ"
        "ab"            -> "****ab"   (short keys still masked)
        ""              -> ""         (empty shows nothing)

    SECURITY: This is the ONLY function that produces a value safe to
    embed in HTML. Never send the raw key to the browser.
    """
    if not key:
        return ""
    if len(key) <= 4:
        return "****" + key
    return "****" + key[-4:]


def _render_settings_page(settings: dict[str, str], *, success: bool = False) -> str:
    """Render the LLM settings page.

    Args:
        settings: A {key: value} dict from db.get_all_settings().
        success: When True, show a green "Settings saved" banner.
    """
    provider = settings.get("llm_provider", "")
    model = settings.get("llm_model", "")
    raw_api_key = settings.get("llm_api_key", "")
    base_url = settings.get("llm_base_url", "")
    max_tokens = settings.get("llm_max_tokens", "1000")
    temperature = settings.get("llm_temperature", "0.3")
    chat_fallback = settings.get("llm_chat_fallback", "1")

    masked_key = _mask_api_key(raw_api_key)
    # The form's api_key field shows the masked value as a placeholder hint.
    # The actual value attribute is left empty so the browser never has the
    # raw key; if the user wants to change it they type a new one.
    fallback_checked = "checked" if chat_fallback == "1" else ""

    show_base_url = provider in ("openai-compat", "ollama")
    base_url_row_display = "block" if show_base_url else "none"

    success_html = (
        '<div class="success">✅ Settings saved. The new LLM configuration '
        "is now active — chat will use it on the next request.</div>"
        if success
        else ""
    )

    content = f"""
    <div class="card">
        <h2>⚙️ LLM Settings</h2>
        <p class="pagination-info">Configure the language model used for chat answers and document summarization. Settings persist across restarts.</p>
        {success_html}
        <form action="/settings" method="post" id="settings-form">
            <div class="settings-field">
                <label for="provider"><strong>LLM Provider</strong></label>
                <select name="provider" id="provider" onchange="toggleBaseUrl()">
                    <option value="" {'' if provider else 'selected'}>(none — extractive fallback)</option>
                    <option value="openai" {'selected' if provider == 'openai' else ''}>OpenAI</option>
                    <option value="openai-compat" {'selected' if provider == 'openai-compat' else ''}>OpenAI-compatible (vLLM, LM Studio, etc.)</option>
                    <option value="ollama" {'selected' if provider == 'ollama' else ''}>Ollama (local)</option>
                </select>
            </div>

            <div class="settings-field">
                <label for="model"><strong>Model Name</strong></label>
                <input type="text" name="model" id="model" value="{_escape(model)}"
                       placeholder="e.g. gpt-4o-mini, llama3, qwen2.5">
            </div>

            <div class="settings-field">
                <label for="api_key"><strong>API Key</strong></label>
                <input type="password" name="api_key" id="api_key"
                       value="{_escape(masked_key)}"
                       placeholder="Leave as-is to keep current key, or type a new one">
                <p class="settings-hint">Current key: {_escape(masked_key) if masked_key else '(not set)'} — only the last 4 characters are shown for security.</p>
            </div>

            <div class="settings-field" id="base_url_row" style="display:{base_url_row_display}">
                <label for="base_url"><strong>Base URL</strong></label>
                <input type="text" name="base_url" id="base_url" value="{_escape(base_url)}"
                       placeholder="https://api.openai.com/v1 or http://localhost:11434">
                <p class="settings-hint">Required for OpenAI-compatible and Ollama providers.</p>
            </div>

            <div class="settings-field">
                <label for="max_tokens"><strong>Max Tokens</strong> <span id="max_tokens_val">{_escape(max_tokens)}</span></label>
                <input type="range" name="max_tokens" id="max_tokens" min="100" max="4000" step="100"
                       value="{_escape(max_tokens)}" oninput="document.getElementById('max_tokens_val').textContent=this.value">
            </div>

            <div class="settings-field">
                <label for="temperature"><strong>Temperature</strong> <span id="temperature_val">{_escape(temperature)}</span></label>
                <input type="range" name="temperature" id="temperature" min="0.0" max="1.0" step="0.05"
                       value="{_escape(temperature)}" oninput="document.getElementById('temperature_val').textContent=this.value">
            </div>

            <div class="settings-field">
                <label>
                    <input type="checkbox" name="chat_fallback" value="1" {fallback_checked}>
                    <strong>Chat Fallback</strong> — if the LLM call fails, use an extractive answer from search snippets instead of erroring.
                </label>
            </div>

            <div class="settings-actions">
                <button type="submit" class="btn-save">💾 Save Settings</button>
                <a href="/" class="btn-cancel">Cancel</a>
            </div>
        </form>
    </div>
    <script>
        function toggleBaseUrl() {{
            var p = document.getElementById('provider').value;
            var row = document.getElementById('base_url_row');
            row.style.display = (p === 'openai-compat' || p === 'ollama') ? 'block' : 'none';
        }}
        // Run once on load to sync the base URL row visibility
        toggleBaseUrl();
    </script>
    """
    return _base_page("Settings", content)


def _render_settings_redirect() -> str:
    """Render a minimal HTML page with a meta-refresh redirect.

    Used as the body of a 302 response so that even clients which don't
    follow the Location header (e.g. some test clients) still land on
    the settings page.
    """
    return (
        '<!DOCTYPE html><html><head>'
        '<meta http-equiv="refresh" content="0; url=/settings?saved=1">'
        '<title>Redirecting…</title></head>'
        '<body>Settings saved. <a href="/settings?saved=1">Continue</a>.</body>'
        '</html>'
    )


def _reload_llm_config_from_db(settings: dict[str, str]) -> None:
    """Reload the in-memory LLMConfig from DB-stored settings.

    This mutates the global ``config.llm`` dataclass in place so that
    the next ``LLMClient(config.llm)`` construction (which happens per
    WebSocket chat connection in ``chat.py``) picks up the new values.
    Existing in-flight LLMClient instances are unaffected; they will be
    closed and replaced on the next chat connection.

    Args:
        settings: A {key: value} dict freshly read from the DB. Pass
            the same dict that was just saved so no extra DB round-trip
            is needed. Keys not present fall back to the current config
            value, so calling this with an empty dict is a no-op.
    """
    from ..core.config import config

    config.llm.provider = settings.get("llm_provider", config.llm.provider)
    config.llm.model = settings.get("llm_model", config.llm.model)
    # API key: only override if a real (non-masked) value is stored.
    # Masked values (**** prefix) come from the form's display field and
    # should never overwrite the real key in config.
    stored_key = settings.get("llm_api_key")
    if stored_key and not stored_key.startswith("****"):
        config.llm.api_key = stored_key
    config.llm.base_url = settings.get("llm_base_url", config.llm.base_url)

    try:
        config.llm.max_tokens = int(
            settings.get("llm_max_tokens", config.llm.max_tokens)
        )
    except (ValueError, TypeError):
        pass

    try:
        config.llm.temperature = float(
            settings.get("llm_temperature", config.llm.temperature)
        )
    except (ValueError, TypeError):
        pass


def _render_error(title: str, message: str) -> str:
    content = f"""
    <div class="error">
        <h2>{_escape(title)}</h2>
        <p>{_escape(message)}</p>
    </div>
    <p><a href="/">Back to Dashboard</a></p>
    """
    return _base_page(f"Error: {title}", content)


# ── Template utilities ──────────────────────────────────────────


def _escape(text: str) -> str:
    """Basic HTML escaping."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _fmt_date(date_val) -> str:
    """Format a datetime value for display."""
    if not date_val:
        return ""
    if isinstance(date_val, datetime):
        return date_val.strftime("%Y-%m-%d %H:%M")
    try:
        # Try ISO format
        return str(date_val)[:19].replace("T", " ")
    except Exception:
        return str(date_val)


def _fmt_size(size: int) -> str:
    """Format bytes as human-readable size."""
    s = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if s < 1024:
            return f"{s:.1f} {unit}"
        s /= 1024
    return f"{s:.1f} TB"


# ── Main entrypoint ────────────────────────────────────────────

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
