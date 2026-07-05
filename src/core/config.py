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
    """SQLite database settings for standalone operation."""

    path: str = field(
        default_factory=lambda: _env(
            "DOCMIND_DATABASE_PATH",
            "data/docmind.db",
        )
    )
    pool_min_size: int = field(
        default_factory=lambda: _env_int("DOCMIND_DB_POOL_MIN", 1)
    )
    pool_max_size: int = field(
        default_factory=lambda: _env_int("DOCMIND_DB_POOL_MAX", 5)
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
class EmbeddingConfig:
    """Embedding provider settings for vector/semantic search.

    Supports three providers:
      - 'local':  sentence-transformers (loads model in-process; heavy dep)
      - 'ollama': remote Ollama /api/embeddings endpoint (lightweight)
      - 'openai': remote OpenAI-compatible /v1/embeddings endpoint

    When provider is empty or sentence-transformers is not installed and
    no remote provider is configured, search gracefully falls back to
    FTS5-only (no vector embeddings are generated).
    """

    provider: str = field(
        default_factory=lambda: _env("DOCMIND_EMBEDDING_PROVIDER", "")
    )
    model: str = field(
        default_factory=lambda: _env(
            "DOCMIND_EMBEDDING_MODEL", "all-MiniLM-L6-v2"
        )
    )
    base_url: str = field(
        default_factory=lambda: _env("DOCMIND_EMBEDDING_BASE_URL", "")
    )
    api_key: str = field(
        default_factory=lambda: _env("DOCMIND_EMBEDDING_API_KEY", "")
    )
    dim: int = field(
        default_factory=lambda: _env_int("DOCMIND_EMBEDDING_DIM", 384)
    )
    timeout_seconds: float = field(
        default_factory=lambda: float(_env("DOCMIND_EMBEDDING_TIMEOUT", "30.0"))
    )
    # Weight of vector score in hybrid fusion (0.0 = FTS only, 1.0 = vector only)
    hybrid_vector_weight: float = field(
        default_factory=lambda: float(_env("DOCMIND_HYBRID_VECTOR_WEIGHT", "0.6"))
    )


@dataclass
class ChunkingConfig:
    """Document chunking settings for granular search and RAG retrieval.

    Documents are split into chunks so that search returns the most
    relevant passage (not the whole document) and LLM context includes
    only the pertinent chunk (fewer tokens).
    """

    chunk_size: int = field(
        default_factory=lambda: _env_int("DOCMIND_CHUNK_SIZE", 500)
    )
    chunk_overlap: int = field(
        default_factory=lambda: _env_int("DOCMIND_CHUNK_OVERLAP", 50)
    )
    min_chunk_size: int = field(
        default_factory=lambda: _env_int("DOCMIND_CHUNK_MIN_SIZE", 100)
    )


@dataclass
class LLMConfig:
    """LLM provider settings for RAG answer generation.

    Supports OpenAI-compatible APIs (openai, openai-compat) and Ollama.
    When provider is empty, the chat falls back to extractive answers.
    """

    provider: str = field(
        default_factory=lambda: _env("DOCMIND_LLM_PROVIDER", "")
    )
    model: str = field(
        default_factory=lambda: _env("DOCMIND_LLM_MODEL", "gpt-4o-mini")
    )
    api_key: str = field(
        default_factory=lambda: _env("DOCMIND_LLM_API_KEY", "")
    )
    base_url: str = field(
        default_factory=lambda: _env("DOCMIND_LLM_BASE_URL", "")
    )
    max_tokens: int = field(
        default_factory=lambda: _env_int("DOCMIND_LLM_MAX_TOKENS", 1000)
    )
    temperature: float = field(
        default_factory=lambda: float(_env("DOCMIND_LLM_TEMPERATURE", "0.3"))
    )
    timeout_seconds: float = field(
        default_factory=lambda: float(_env("DOCMIND_LLM_TIMEOUT", "30.0"))
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
class CacheConfig:
    """Cache layer settings for query result caching.

    The cache is integrated at the Database layer to reduce redundant
    SQLite queries for read-heavy operations (dashboard, search,
    document lists, analytics).

    Backend selection:
      - 'memory': In-process dict with TTL + LRU eviction (default, zero-config)
      - 'redis':  Optional external Redis backend for multi-process deployments

    When ``enabled`` is False, a NoopCache is used — all cache operations
    become no-ops with zero overhead.
    """
    enabled: bool = field(
        default_factory=lambda: _env_bool("DOCMIND_CACHE_ENABLED", True)
    )
    backend: str = field(
        default_factory=lambda: _env("DOCMIND_CACHE_BACKEND", "memory")
    )
    redis_url: str = field(
        default_factory=lambda: _env(
            "DOCMIND_CACHE_REDIS_URL", "redis://localhost:6379/0"
        )
    )
    max_size: int = field(
        default_factory=lambda: _env_int("DOCMIND_CACHE_MAX_SIZE", 10000)
    )


@dataclass
class AutoDetectionConfig:
    """LLM-based document type auto-detection settings.

    When enabled, documents are classified by type during ingestion
    using the configured LLM provider. If no LLM is configured, a
    keyword-based heuristic is used instead.

    The detection uses the existing LLMConfig for provider settings —
    no separate LLM endpoint is needed.
    """

    enabled: bool = field(
        default_factory=lambda: _env_bool("DOCMIND_AUTODETECT_ENABLED", True)
    )
    max_body_chars: int = field(
        default_factory=lambda: _env_int("DOCMIND_AUTODETECT_MAX_BODY_CHARS", 2000)
    )


@dataclass
class AuthConfig:
    """Authentication settings for the web UI and REST API.

    A simple opt-in API-key/password authentication scheme for the
    self-hosted single-user deployment. When ``enabled`` is False (the
    default), all requests pass through unchallenged — preserving the
    current open behaviour. When enabled, every request must present
    either a valid signed session cookie (web UI) or an ``X-API-Key``
    header matching the configured ``api_key`` (programmatic API).

    The ``api_key`` doubles as the login password: users enter it on
    the login page to obtain a session cookie. It can be set via the
    ``DOCMIND_AUTH_API_KEY`` env var or generated on first enable from
    the settings page (stored in the DB ``settings`` table).
    """

    enabled: bool = field(
        default_factory=lambda: _env_bool("DOCMIND_AUTH_ENABLED", False)
    )
    api_key: str = field(
        default_factory=lambda: _env("DOCMIND_AUTH_API_KEY", "")
    )
    session_secret: str = field(
        default_factory=lambda: _env("DOCMIND_AUTH_SESSION_SECRET", "")
    )
    session_expiry_hours: int = field(
        default_factory=lambda: _env_int("DOCMIND_AUTH_SESSION_EXPIRY_HOURS", 24)
    )


@dataclass
class Config:
    """Top-level configuration aggregator."""

    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    sanitizer: SanitizerConfig = field(default_factory=SanitizerConfig)
    job_queue: JobQueueConfig = field(default_factory=JobQueueConfig)
    document_limits: DocumentLimits = field(default_factory=DocumentLimits)
    llm: LLMConfig = field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    auto_detection: AutoDetectionConfig = field(default_factory=AutoDetectionConfig)
    debug: bool = field(
        default_factory=lambda: _env_bool("DOCMIND_DEBUG", False)
    )


# Global singleton — instantiated once at process start.
config = Config()
