"""Correlation ID management — UUIDv4 with redaction rules.

Design spec (motion-df6d1ead4cab):
- Every request gets a UUIDv4 correlation_id
- Correlation ID propagates through all async tasks, DB queries, audit logs
- Redaction rules for PII in log context
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from uuid import UUID, uuid4

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")


def generate_correlation_id() -> str:
    return str(uuid4())


def set_correlation_id(cid: str) -> None:
    correlation_id_var.set(cid)


def get_correlation_id() -> str:
    cid = correlation_id_var.get()
    if not cid:
        cid = generate_correlation_id()
        correlation_id_var.set(cid)
    return cid


# PII redaction patterns
_PII_PATTERNS: list[tuple[str, str]] = [
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "[EMAIL]"),
    (r"\b(?:\d{3}[-.]?){2}\d{4}\b", "[PHONE]"),
    (r"\b\d{3}-\d{2}-\d{4}\b", "[SSN]"),
    (r"bearer\s+[A-Za-z0-9._~+/-]+=*", "bearer [REDACTED_TOKEN]"),
]


def redact_pii(text: str) -> str:
    """Redact PII from log messages."""
    for pattern, replacement in _PII_PATTERNS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def is_valid_correlation_id(cid: str) -> bool:
    try:
        UUID(cid)
        return True
    except (ValueError, AttributeError):
        return False
