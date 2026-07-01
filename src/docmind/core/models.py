"""Core domain models for docmind."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class DocumentStatus(str, Enum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    ARCHIVED = "archived"


class Permission(str, Enum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


class Document(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    content_hash: str
    mime_type: str
    size_bytes: int
    status: DocumentStatus = DocumentStatus.UPLOADED
    owner_id: UUID
    tenant_id: UUID | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class DocumentVersion(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    document_id: UUID
    version_number: int
    content_hash: str
    size_bytes: int
    mime_type: str
    created_by: UUID
    created_at: datetime = Field(default_factory=datetime.utcnow)
    comment: str = ""


class PermissionEntry(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    document_id: UUID
    user_id: UUID
    permission: Permission
    granted_by: UUID
    granted_at: datetime = Field(default_factory=datetime.utcnow)


class AuditEvent(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    correlation_id: str
    event_type: str
    actor_id: UUID | None = None
    resource_type: str | None = None
    resource_id: UUID | None = None
    action: str
    outcome: str  # "success" | "denied" | "error"
    details: dict = Field(default_factory=dict)
    ip_address: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
