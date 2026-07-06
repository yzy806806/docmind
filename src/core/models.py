"""Pydantic data models for DocMind API and job queue."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────

class DocumentStatus(str, Enum):
    """Processing status of a document."""
    PENDING = "pending"
    INDEXED = "indexed"
    SUMMARIZED = "summarized"
    ERROR = "error"


class JobState(str, Enum):
    """Lifecycle state of a background processing job."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class SourceType(str, Enum):
    """Supported data source types."""
    LOCAL = "local"
    WEBDAV = "webdav"
    POSTGRESQL = "postgresql"
    API = "api"
    EMAIL = "email"


# ── Document ───────────────────────────────────────────────────

class DocumentRecord(BaseModel):
    """A document indexed in the knowledge base."""
    id: int
    path: str
    source_type: SourceType
    source_name: str
    file_hash: Optional[str] = None
    mtime: float = 0.0
    size: int = 0
    title: str
    ext: str = ""
    mime_type: str = "application/octet-stream"
    summary: Optional[str] = None
    raw_preview: str = ""
    body: str = ""
    status: DocumentStatus = DocumentStatus.PENDING
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DocumentCreate(BaseModel):
    """Payload for submitting a new document."""
    path: str
    source_type: SourceType = SourceType.API
    source_name: str = "api"
    title: str
    ext: str = ""
    mime_type: str = "application/octet-stream"
    body: str = ""
    file_hash: Optional[str] = None
    mtime: float = 0.0
    size: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentStatusResponse(BaseModel):
    """Public status view of a document."""
    id: int
    status: DocumentStatus
    path: str
    title: str
    summary: Optional[str] = None
    ext: str = ""
    mime_type: str = ""
    size: int = 0
    created_at: datetime
    updated_at: datetime


# ── Job ────────────────────────────────────────────────────────

class JobRecord(BaseModel):
    """A background processing job stored in PostgreSQL."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    state: JobState = JobState.PENDING
    document_path: str
    document_title: Optional[str] = None
    source_name: str = "api"
    document_id: Optional[int] = None
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SubmissionAccepted(BaseModel):
    """Response returned after a document is accepted for processing."""
    job_id: str
    status: str = "pending"
    document_path: str


class JobStatusResponse(BaseModel):
    """Public status view of a background job."""
    job_id: str
    status: JobState
    document_id: Optional[int] = None
    error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class BatchSubmissionRequest(BaseModel):
    """Request body for batch document submission."""
    documents: list[BatchDocumentItem] = Field(
        min_length=1, max_length=50
    )


class BatchDocumentItem(BaseModel):
    """A single document reference in a batch submission."""
    path: str
    title: Optional[str] = None
    source_name: str = "api"


# ── Error ──────────────────────────────────────────────────────

class ErrorResponse(BaseModel):
    """Standard error envelope with trace_id for log correlation."""
    error: str
    detail: Optional[str] = None
    trace_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
