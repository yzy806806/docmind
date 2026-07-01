"""FastAPI dependency injection for docmind."""

from __future__ import annotations

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from docmind.auth.permissions import PermissionService
from docmind.config import settings
from docmind.observability.audit import AuditLogger, audit_logger


async def get_audit_logger() -> AuditLogger:
    return audit_logger


# PermissionService requires a real store; placeholder for now.
# Will be wired up when DB-backed PermissionStore is implemented.
# async def get_permission_service(...) -> PermissionService:
#     ...
