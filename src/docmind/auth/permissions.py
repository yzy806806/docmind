"""
PermissionService interface and contract definition.

This module defines the PermissionService protocol that all RBAC
implementations must satisfy. Contract tests are in tests/contract/.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PermissionRole(str, Enum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


@dataclass(frozen=True)
class PermissionCheck:
    """Result of a permission check."""

    allowed: bool
    role: Optional[PermissionRole] = None
    reason: str = ""

    @classmethod
    def deny(cls, reason: str = "Access denied") -> PermissionCheck:
        return cls(allowed=False, reason=reason)

    @classmethod
    def allow(cls, role: PermissionRole) -> PermissionCheck:
        return cls(allowed=True, role=role)


@dataclass
class PermissionEntry:
    """A single permission record."""

    user_id: uuid.UUID
    document_id: uuid.UUID
    role: PermissionRole
    created_at: Optional[str] = None


# ── PermissionService Protocol ────────────────────────────────────────────


class PermissionService(ABC):
    """
    Database-backed RBAC service for document-level access control.

    CONTRACT (enforced by contract tests):
      1. check_permission(user_id, document_id, required_role) -> PermissionCheck
         - Returns allow if user has >= required_role (owner > editor > viewer)
         - Returns deny if user has no permission record for this document
         - Raises ValueError for invalid UUIDs
         - Never returns None

      2. grant(user_id, document_id, role) -> PermissionEntry
         - Upserts: existing record is updated to new role
         - Raises ValueError if role is invalid
         - Must be idempotent (granting same role twice is a no-op)

      3. revoke(user_id, document_id) -> bool
         - Returns True if a record was deleted
         - Returns False if no matching record existed
         - Does not raise for missing permissions

      4. list_for_document(document_id) -> list[PermissionEntry]
         - Returns empty list if no permissions (not None)
         - Ordered by role hierarchy desc (owners first)

      5. list_for_user(user_id) -> list[PermissionEntry]
         - Returns empty list if no permissions (not None)
         - Ordered by created_at desc (most recent first)

      ALL METHODS are async and must be called with await.
    """

    @abstractmethod
    async def check_permission(
        self,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
        required_role: PermissionRole,
    ) -> PermissionCheck:
        """Check if a user has at least the required role on a document."""
        ...

    @abstractmethod
    async def grant(
        self,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
        role: PermissionRole,
    ) -> PermissionEntry:
        """Grant (or update) a permission for a user on a document."""
        ...

    @abstractmethod
    async def revoke(
        self,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> bool:
        """Revoke a user's permission on a document. Returns True if removed."""
        ...

    @abstractmethod
    async def list_for_document(
        self, document_id: uuid.UUID
    ) -> list[PermissionEntry]:
        """List all permissions for a document."""
        ...

    @abstractmethod
    async def list_for_user(
        self, user_id: uuid.UUID
    ) -> list[PermissionEntry]:
        """List all permissions for a user."""
        ...


# ── Role hierarchy helper ─────────────────────────────────────────────────

ROLE_HIERARCHY: dict[PermissionRole, int] = {
    PermissionRole.OWNER: 3,
    PermissionRole.EDITOR: 2,
    PermissionRole.VIEWER: 1,
}


def has_sufficient_role(
    user_role: PermissionRole,
    required_role: PermissionRole,
) -> bool:
    """Check if user_role is at least as high as required_role in the hierarchy."""
    return ROLE_HIERARCHY.get(user_role, 0) >= ROLE_HIERARCHY.get(required_role, 0)
