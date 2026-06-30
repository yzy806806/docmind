"""DocMind web server — FastAPI application.

Exposes the document processing API as defined in docs/openapi.yaml:
- POST /documents/submit  — upload a single document
- POST /documents/batch   — submit multiple documents
- GET  /documents/{id}/status — query document processing status
- GET  /jobs/{id}         — query background job status
"""

from __future__ import annotations

import logging
import mimetypes
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse

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

logger = logging.getLogger(__name__)

# ── State ──────────────────────────────────────────────────────

# These are initialized in the lifespan handler and stored in app.state.
# In production, wire them through dependency injection instead of globals.
_db: Optional[Database] = None
_queue: Optional[JobQueue] = None


def get_db() -> Database:
    """Return the global Database instance (must have been initialized)."""
    if _db is None:
        raise RuntimeError("Database not initialized")
    return _db


def get_queue() -> JobQueue:
    """Return the global JobQueue instance (must have been initialized)."""
    if _queue is None:
        raise RuntimeError("JobQueue not initialized")
    return _queue


# ── App factory ────────────────────────────────────────────────

def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    app = FastAPI(
        title="DocMind Document Processing API",
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
        logger.info("DocMind server started on %s:%d", config.server.host, config.server.port)

    @app.on_event("shutdown")
    async def shutdown() -> None:
        global _db
        if _db:
            await _db.disconnect()
            _db = None
        logger.info("DocMind server shut down")

    # ── Error handlers ──────────────────────────────────────

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                error=exc.detail or "HTTP error",
                trace_id=str(uuid.uuid4()),
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
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

    # ── Routes ──────────────────────────────────────────────

    @app.get("/health", include_in_schema=False)
    async def health_check():
        """Liveness probe."""
        return {"status": "ok"}

    @app.post(
        "/documents/submit",
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
        """Upload a document file for async processing.

        Returns a ``job_id`` immediately. Poll ``GET /jobs/{job_id}`` for status.
        """
        # Validate extension
        ext = _get_ext(file.filename)
        if ext and ext not in config.document_limits.supported_extensions:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"Unsupported file type: {ext}. Supported: {sorted(config.document_limits.supported_extensions)}",
            )

        # Read file content
        raw = await file.read()

        # Size check
        if len(raw) > config.document_limits.max_file_size_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File exceeds maximum size of {config.document_limits.max_file_size_bytes} bytes",
            )

        display_title = title or file.filename or "untitled"
        mime_type = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"

        # Extract body text from raw bytes
        body = _extract_body(raw, ext or "", file.filename or "")

        # Create document record
        doc = DocumentCreate(
            path=f"/submissions/{file.filename or 'unknown'}",
            source_name=source_name,
            title=display_title,
            ext=ext or "",
            mime_type=mime_type,
            body=body,
            size=len(raw),
        )

        # Persist document and enqueue job
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
        "/documents/batch",
        response_model=dict,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["documents"],
        summary="Submit multiple documents in one request",
    )
    async def batch_submit_documents(body: BatchSubmissionRequest):
        """Accept a batch of document references. Returns one job per document."""
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
        "/documents/{doc_id}/status",
        response_model=DocumentStatusResponse,
        tags=["documents"],
        summary="Get document processing status",
    )
    async def get_document_status(doc_id: int):
        """Query the current status of a document by its internal ID."""
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
        "/jobs/{job_id}",
        response_model=JobStatusResponse,
        tags=["jobs"],
        summary="Get background job status",
    )
    async def get_job_status(job_id: str):
        """Poll this endpoint after submitting documents.

        A job transitions through:
        ``pending`` → ``processing`` → ``completed`` | ``failed``.

        Once ``completed``, the response includes ``document_id``.
        """
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
    """Extract lowercase file extension from a filename."""
    if not filename:
        return None
    return Path(filename).suffix.lower() or None


def _extract_body(raw: bytes, ext: str, filename: str) -> str:
    """Extract plain text body from raw file bytes based on extension."""
    try:
        if ext in (".txt", ".md", ".csv"):
            return raw.decode("utf-8", errors="replace")
        elif ext == ".json":
            import json as _json
            data = _json.loads(raw.decode("utf-8", errors="replace"))
            return _json.dumps(data, ensure_ascii=False, indent=2)
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
