"""Configuration loader for DocMind.

Loads configuration from environment variables with sensible defaults.
For production, override via DOCMIND_* environment variables or a .env file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _env(key: str, default: str = "") -> str:
    """Read an environment variable with a default."""
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    """Read an integer environment variable."""
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    """Read a boolean environment variable (truthy: 1, true, yes)."""
    val = os.environ.get(key, "").strip().lower()
    if val in ("1", "true", "yes"):
        return True
    if val in ("0", "false", "no"):
        return False
    return default


@dataclass
class DatabaseConfig:
    """PostgreSQL connection settings."""

    dsn: str = field(
        default_factory=lambda: _env(
            "DOCMIND_DATABASE_DSN",
            "postgresql://docmind:docmind@localhost:5432/docmind",
        )
    )
    pool_min_size: int = field(
        default_factory=lambda: _env_int("DOCMIND_DB_POOL_MIN", 2)
    )
    pool_max_size: int = field(
        default_factory=lambda: _env_int("DOCMIND_DB_POOL_MAX", 10)
    )


@dataclass
class ServerConfig:
    """HTTP server settings."""

    host: str = field(
        default_factory=lambda: _env("DOCMIND_HOST", "0.0.0.0")
    )
    port: int = field(
        default_factory=lambda: _env_int("DOCMIND_PORT", 8080)
    )
    workers: int = field(
        default_factory=lambda: _env_int("DOCMIND_WORKERS", 1)
    )
    api_prefix: str = "/api/v1"


@dataclass
class SanitizerConfig:
    """Input sanitization settings."""

    max_input_chars: int = field(
        default_factory=lambda: _env_int("DOCMIND_SANITIZER_MAX_CHARS", 16000)
    )
    max_tokens: int = field(
        default_factory=lambda: _env_int("DOCMIND_SANITIZER_MAX_TOKENS", 4000)
    )
    redact_pii: bool = field(
        default_factory=lambda: _env_bool("DOCMIND_SANITIZER_REDACT_PII", True)
    )
    nfkc_normalize: bool = field(
        default_factory=lambda: _env_bool("DOCMIND_SANITIZER_NFKC", True)
    )
    strip_control_chars: bool = field(
        default_factory=lambda: _env_bool("DOCMIND_SANITIZER_STRIP_CONTROL", True)
    )


@dataclass
class JobQueueConfig:
    """Background job queue settings."""

    poll_interval_seconds: float = field(
        default_factory=lambda: float(_env("DOCMIND_QUEUE_POLL_INTERVAL", "2.0"))
    )
    max_retries: int = field(
        default_factory=lambda: _env_int("DOCMIND_QUEUE_MAX_RETRIES", 3)
    )
    worker_count: int = field(
        default_factory=lambda: _env_int("DOCMIND_QUEUE_WORKERS", 2)
    )


@dataclass
class DocumentLimits:
    """Document processing limits."""

    max_file_size_bytes: int = field(
        default_factory=lambda: _env_int(
            "DOCMIND_MAX_FILE_SIZE", 100 * 1024 * 1024
        )  # 100 MB default
    )
    max_batch_size: int = field(
        default_factory=lambda: _env_int("DOCMIND_MAX_BATCH_SIZE", 50)
    )
    supported_extensions: set[str] = field(
        default_factory=lambda: {
            ".txt", ".md", ".pdf", ".docx", ".html", ".htm",
            ".csv", ".json", ".xml",
        }
    )


@dataclass
class Config:
    """Top-level configuration aggregator."""

    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    sanitizer: SanitizerConfig = field(default_factory=SanitizerConfig)
    job_queue: JobQueueConfig = field(default_factory=JobQueueConfig)
    document_limits: DocumentLimits = field(default_factory=DocumentLimits)
    debug: bool = field(
        default_factory=lambda: _env_bool("DOCMIND_DEBUG", False)
    )


# Global singleton — instantiated once at process start.
config = Config()
