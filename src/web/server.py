"""DocMind web server — FastAPI application.

Exposes the full document processing and knowledge base API:

REST:
- GET  /                         Dashboard with stats
- GET  /search?q=                Search page with results + citations
- GET  /documents                List all documents
- GET  /documents/<id>           Document detail with summary
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
from ..core.db import Database
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


# ── App factory ────────────────────────────────────────────────


def create_app() -> FastAPI:
    app = FastAPI(
        title="DocMind Document Knowledge Base",
        version="0.1.0",
        description="AI-powered enterprise document knowledge base",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # ── Lifespan ────────────────────────────────────────────

    @app.on_event("startup")
    async def startup() -> None:
        global _db, _queue
        _db = Database(
            dsn=config.database.dsn,
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

    @app.on_event("shutdown")
    async def shutdown() -> None:
        global _db
        if _db:
            await _db.disconnect()
            _db = None
        logger.info("DocMind server shut down")

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
            async with db.connection() as conn:
                total_row = await conn.fetchrow(
                    "SELECT COUNT(*) as c FROM documents"
                )
                pending_row = await conn.fetchrow(
                    "SELECT COUNT(*) as c FROM documents WHERE status = 'pending'"
                )
                indexed_row = await conn.fetchrow(
                    "SELECT COUNT(*) as c FROM documents WHERE status = 'indexed'"
                )
                summarized_row = await conn.fetchrow(
                    "SELECT COUNT(*) as c FROM documents WHERE status = 'summarized'"
                )
                job_row = await conn.fetchrow(
                    "SELECT COUNT(*) as c FROM jobs WHERE state IN ('pending', 'processing')"
                )

                stats = {
                    "total": total_row["c"] if total_row else 0,
                    "pending": pending_row["c"] if pending_row else 0,
                    "indexed": indexed_row["c"] if indexed_row else 0,
                    "summarized": summarized_row["c"] if summarized_row else 0,
                    "active_jobs": job_row["c"] if job_row else 0,
                }

                # Get recent documents
                recent_rows = await conn.fetch(
                    """SELECT id, title, status, ext, created_at
                       FROM documents
                       ORDER BY created_at DESC
                       LIMIT 10"""
                )
                recent = [dict(r) for r in recent_rows]
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
            rows = await db.fulltext_search(validated_q, limit=20)
            results = [dict(r) for r in rows]
        except Exception:
            pass

        html = _render_search_results(validated_q, results)
        return HTMLResponse(content=html)

    @app.get("/documents", response_class=HTMLResponse, include_in_schema=False)
    async def list_documents_page(
        source: str = Query(default=""),
        limit: int = Query(default=100, le=500),
    ):
        """List all indexed documents."""
        db = get_db()
        try:
            async with db.connection() as conn:
                if source:
                    rows = await conn.fetch(
                        """SELECT id, title, status, ext, mime_type,
                                  source_name, source_type, size, created_at
                           FROM documents
                           WHERE source_name = $1 OR source_type = $1
                           ORDER BY created_at DESC
                           LIMIT $2""",
                        source, limit,
                    )
                else:
                    rows = await conn.fetch(
                        """SELECT id, title, status, ext, mime_type,
                                  source_name, source_type, size, created_at
                           FROM documents
                           ORDER BY created_at DESC
                           LIMIT $1""",
                        limit,
                    )
                documents = [dict(r) for r in rows]
        except Exception:
            documents = []

        html = _render_documents_list(documents, source)
        return HTMLResponse(content=html)

    @app.get("/documents/{doc_id}", response_class=HTMLResponse, include_in_schema=False)
    async def document_detail(doc_id: int):
        """Document detail page with summary."""
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

        html = _render_document_detail(doc)
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

    # ── WebSocket ───────────────────────────────────────────

    @app.websocket("/chat")
    async def chat_endpoint(websocket: WebSocket):
        """Real-time Q&A with citation tracking."""
        await handle_chat(websocket)

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
    """Render a base HTML page with minimal styling."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} — DocMind</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: #f5f5f5; color: #333; line-height: 1.6; }}
        .container {{ max-width: 960px; margin: 0 auto; padding: 20px; }}
        header {{ background: #1a1a2e; color: white; padding: 16px 24px; }}
        header h1 {{ font-size: 1.5em; }}
        header nav {{ margin-top: 8px; }}
        header nav a {{ color: #a8dadc; text-decoration: none; margin-right: 16px; }}
        header nav a:hover {{ text-decoration: underline; }}
        .card {{ background: white; border-radius: 8px; padding: 20px; margin: 16px 0;
                 box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
                 gap: 16px; margin: 16px 0; }}
        .stat {{ background: white; border-radius: 8px; padding: 20px; text-align: center;
                 box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .stat-value {{ font-size: 2em; font-weight: bold; color: #1a1a2e; }}
        .stat-label {{ font-size: 0.85em; color: #666; margin-top: 4px; }}
        table {{ width: 100%; border-collapse: collapse; margin: 16px 0; }}
        th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #eee; }}
        th {{ background: #f8f8f8; font-weight: 600; }}
        tr:hover {{ background: #fafafa; }}
        .search-box {{ display: flex; gap: 8px; }}
        .search-box input {{ flex: 1; padding: 10px 14px; border: 2px solid #ddd;
                            border-radius: 6px; font-size: 1em; }}
        .search-box button {{ padding: 10px 24px; background: #1a1a2e; color: white;
                              border: none; border-radius: 6px; cursor: pointer; font-size: 1em; }}
        .search-box button:hover {{ background: #2d2d4e; }}
        .result {{ margin: 16px 0; padding: 16px; background: white; border-radius: 8px;
                   box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        .result h3 {{ color: #1a1a2e; }}
        .result h3 a {{ color: inherit; text-decoration: none; }}
        .result h3 a:hover {{ text-decoration: underline; }}
        .snippet {{ color: #555; margin: 8px 0; }}
        .meta {{ font-size: 0.85em; color: #888; }}
        .badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px;
                 font-size: 0.75em; font-weight: 600; }}
        .badge-indexed {{ background: #e3f2fd; color: #1565c0; }}
        .badge-summarized {{ background: #e8f5e9; color: #2e7d32; }}
        .badge-pending {{ background: #fff3e0; color: #e65100; }}
        .badge-error {{ background: #ffebee; color: #c62828; }}
        .error {{ background: #ffebee; color: #c62828; padding: 12px 16px;
                 border-radius: 6px; margin: 12px 0; }}
        .success {{ background: #e8f5e9; color: #2e7d32; padding: 12px 16px;
                   border-radius: 6px; margin: 12px 0; }}
        .upload-form {{ background: white; border-radius: 8px; padding: 24px;
                       box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .upload-form input[type="file"] {{ margin: 12px 0; }}
        .upload-form button {{ padding: 10px 24px; background: #1a1a2e; color: white;
                               border: none; border-radius: 6px; cursor: pointer; }}
        .doc-detail h2 {{ color: #1a1a2e; margin-bottom: 16px; }}
        .doc-detail .field {{ margin: 8px 0; }}
        .doc-detail .field-label {{ font-weight: 600; color: #555; }}
        .doc-detail pre {{ background: #f5f5f5; padding: 16px; border-radius: 6px;
                          overflow-x: auto; font-size: 0.9em; white-space: pre-wrap; }}
        footer {{ text-align: center; padding: 24px; color: #888; font-size: 0.85em; }}
        {extra_head}
    </style>
</head>
<body>
    <header>
        <div class="container">
            <h1>📚 DocMind</h1>
            <nav>
                <a href="/">Dashboard</a>
                <a href="/search">Search</a>
                <a href="/documents">Documents</a>
                <a href="/upload">Upload</a>
                <a href="/docs">API Docs</a>
            </nav>
        </div>
    </header>
    <div class="container">
        {content}
    </div>
    <footer>DocMind v0.1.0 — AI-Powered Document Knowledge Base</footer>
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


def _render_documents_list(documents: list[dict], source: str) -> str:
    rows = ""
    for doc in documents:
        status_class = f"badge-{doc.get('status', 'pending')}"
        rows += f"""
        <tr>
            <td><a href="/documents/{doc['id']}">[{doc['id']}] {doc.get('title', 'Untitled')}</a></td>
            <td><span class="badge {status_class}">{doc.get('status', '')}</span></td>
            <td>{doc.get('source_name', doc.get('source_type', ''))}</td>
            <td>{doc.get('ext', '')}</td>
            <td>{_fmt_date(doc.get('created_at', ''))}</td>
        </tr>"""

    content = f"""
    <div class="card">
        <h2>Documents {'— ' + source if source else ''}</h2>
        {'<table><tr><th>Document</th><th>Status</th><th>Source</th><th>Type</th><th>Date</th></tr>' + rows + '</table>' if documents else '<p>No documents found.</p>'}
    </div>
    """
    return _base_page("Documents", content)


def _render_document_detail(doc: dict) -> str:
    status_class = f"badge-{doc.get('status', 'pending')}"
    body_preview = (doc.get("body", "") or "")[:2000]
    if len(doc.get("body", "") or "") > 2000:
        body_preview += "\n... (truncated)"

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

        {'<h3>Summary</h3><p>' + (doc.get('summary') or '<em>No summary available</em>') + '</p>' if doc.get('summary') else ''}

        <h3>Content Preview</h3>
        <pre>{_escape(body_preview)}</pre>
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
