"""Structured JSON logging with correlation IDs and trace context manager.

Provides:
- JSON-formatted log records for machine-readability
- Correlation IDs (trace_id) propagated across requests
- ``traced`` context manager for automatic trace_id injection
- Configuration via DOCMIND_LOG_LEVEL and DOCMIND_LOG_FORMAT env vars
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional


# ── Correlation ID context ────────────────────────────────────────

_trace_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "trace_id", default=None
)


def get_trace_id() -> str:
    """Return the current correlation ID, generating one if absent."""
    tid = _trace_id.get()
    if tid is None:
        tid = str(uuid.uuid4())
        _trace_id.set(tid)
    return tid


def set_trace_id(trace_id: str) -> None:
    """Set the correlation ID for the current context."""
    _trace_id.set(trace_id)


@contextmanager
def traced(trace_id: Optional[str] = None):
    """Context manager that sets a correlation ID for all logs within.

    Usage::

        with traced("req-abc123"):
            logger.info("Processing request")
            do_work()
    """
    token = _trace_id.set(trace_id or str(uuid.uuid4()))
    try:
        yield
    finally:
        _trace_id.reset(token)


# ── JSON Formatter ────────────────────────────────────────────────


class JSONFormatter(logging.Formatter):
    """Emit log records as JSON lines with correlation IDs."""

    def __init__(self, *, service_name: str = "docmind"):
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record as a JSON line."""
        log_entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "service": self.service_name,
            "message": record.getMessage(),
            "trace_id": _trace_id.get(),
        }

        # Include exception info if present
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = {
                "type": type(record.exc_info[1]).__name__,
                "message": str(record.exc_info[1]),
            }

        # Include extra fields passed via `extra=` kwarg
        for key in ("doc_id", "job_id", "query", "source", "duration_ms", "path"):
            val = getattr(record, key, None)
            if val is not None:
                log_entry[key] = val

        return json.dumps(log_entry, default=str)


# ── Logger setup ──────────────────────────────────────────────────

_log_configured = False


def setup_logging(
    *,
    level: Optional[str] = None,
    log_format: Optional[str] = None,
    service_name: str = "docmind",
) -> None:
    """Configure the root logger for DocMind.

    Call once at process startup. Subsequent calls are no-ops.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR). Defaults to
               ``DOCMIND_LOG_LEVEL`` env var or ``INFO``.
        log_format: ``'json'`` or ``'text'``. Defaults to
                    ``DOCMIND_LOG_FORMAT`` env var or ``'json'``.
        service_name: Service name included in each log entry.
    """
    global _log_configured
    if _log_configured:
        return
    _log_configured = True

    if level is None:
        level = os.environ.get("DOCMIND_LOG_LEVEL", "INFO").upper()
    if log_format is None:
        log_format = os.environ.get("DOCMIND_LOG_FORMAT", "json").lower()

    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))

    # Remove existing handlers
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stderr)

    if log_format == "json":
        handler.setFormatter(JSONFormatter(service_name=service_name))
    else:
        fmt = (
            "%(asctime)s [%(levelname)s] %(name)s "
            "[trace_id=%(trace_id)s] %(message)s"
        )
        handler.setFormatter(logging.Formatter(fmt))

    root.addHandler(handler)

    # Quiet noisy third-party loggers
    for noisy in ("uvicorn.access", "asyncio", "asyncpg"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a logger with the correlation ID automatically attached."""
    logger = logging.getLogger(name)
    return logger


# ── Timing utility ────────────────────────────────────────────────


@contextmanager
def log_duration(logger: logging.Logger, operation: str, **extra):
    """Log the duration of a block with structured metadata.

    Usage::

        with log_duration(logger, "pdf_extraction", path="/data/doc.pdf"):
            text = extract_pdf(path)
    """
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.info(
            "%s completed in %.1fms", operation, elapsed_ms,
            extra={"duration_ms": round(elapsed_ms, 1), **extra},
        )
