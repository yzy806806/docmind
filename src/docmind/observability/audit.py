"""Authorization audit logging.

Records all access decisions (allowed and denied) for compliance.
Each audit event carries a correlation_id, actor, resource, action, and outcome.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Protocol
from uuid import UUID

import structlog

from docmind.core.correlation import get_correlation_id
from docmind.core.models import AuditEvent

logger = structlog.get_logger(__name__)


class AuditBackend(Protocol):
    """Abstract audit log persistence."""

    async def record(self, event: AuditEvent) -> None:
        ...


class LoggingAuditBackend:
    """Audit backend that writes to structured logging (JSON to stdout)."""

    async def record(self, event: AuditEvent) -> None:
        logger.info(
            "audit_event",
            correlation_id=event.correlation_id,
            event_type=event.event_type,
            actor_id=str(event.actor_id) if event.actor_id else None,
            resource_type=event.resource_type,
            resource_id=str(event.resource_id) if event.resource_id else None,
            action=event.action,
            outcome=event.outcome,
            details=event.details,
            ip_address=event.ip_address,
            timestamp=event.timestamp.isoformat(),
        )


class AuditLogger:
    """High-level audit logging with configurable backends."""

    def __init__(self, backend: AuditBackend | None = None) -> None:
        self._backend = backend or LoggingAuditBackend()

    async def log_auth_event(
        self,
        *,
        actor_id: UUID | None,
        action: str,
        outcome: str,  # "success", "denied", "error"
        resource_type: str | None = None,
        resource_id: UUID | None = None,
        details: dict | None = None,
        ip_address: str | None = None,
    ) -> None:
        event = AuditEvent(
            correlation_id=get_correlation_id(),
            event_type="auth",
            actor_id=actor_id,
            resource_type=resource_type,
            resource_id=resource_id,
            action=action,
            outcome=outcome,
            details=details or {},
            ip_address=ip_address,
            timestamp=datetime.utcnow(),
        )
        await self._backend.record(event)

    async def log_access_granted(
        self,
        actor_id: UUID,
        resource_id: UUID,
        action: str,
        resource_type: str = "document",
    ) -> None:
        await self.log_auth_event(
            actor_id=actor_id,
            action=action,
            outcome="success",
            resource_type=resource_type,
            resource_id=resource_id,
        )

    async def log_access_denied(
        self,
        actor_id: UUID,
        resource_id: UUID,
        action: str,
        reason: str,
        resource_type: str = "document",
    ) -> None:
        await self.log_auth_event(
            actor_id=actor_id,
            action=action,
            outcome="denied",
            resource_type=resource_type,
            resource_id=resource_id,
            details={"reason": reason},
        )


# Global audit logger instance
audit_logger = AuditLogger()
