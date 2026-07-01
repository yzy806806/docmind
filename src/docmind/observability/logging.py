"""Structured logging with structlog + correlation ID propagation."""

from __future__ import annotations

import logging
import sys

import structlog

from docmind.config import settings
from docmind.core.correlation import get_correlation_id, redact_pii


def correlation_id_processor(
    logger: logging.Logger, method_name: str, event_dict: dict
) -> dict:
    """Inject correlation_id into every log event."""
    event_dict["correlation_id"] = get_correlation_id()
    return event_dict


def redact_processor(
    logger: logging.Logger, method_name: str, event_dict: dict
) -> dict:
    """Redact PII from log messages."""
    if "event" in event_dict:
        event_dict["event"] = redact_pii(event_dict["event"])
    return event_dict


def configure_logging() -> None:
    """Configure structlog for docmind with correlation IDs and PII redaction."""

    # Standard logging bridge
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.DEBUG if settings.debug else logging.INFO,
    )

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            correlation_id_processor,
            redact_processor,
            structlog.processors.UnicodeDecoder(),
            structlog.dev.ConsoleRenderer() if settings.debug else structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name or __name__)
