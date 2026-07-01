"""Application configuration via pydantic-settings."""

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """DocMind configuration loaded from environment and .env file."""

    model_config = SettingsConfigDict(
        env_prefix="DOCMIND_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    app_name: str = "docmind"
    debug: bool = False
    secret_key: str = "change-me-in-production"

    # Database
    database_url: str = "postgresql+asyncpg://docmind:docmind@localhost:5432/docmind"
    database_pool_size: int = 20
    database_max_overflow: int = 10

    # Content-addressable storage
    storage_backend: str = "local"  # local | s3
    storage_path: Path = Path("/var/lib/docmind/documents")

    # Search
    search_backend: str = "fts5"  # fts5 | pgvector
    search_fts_table: str = "document_fts"

    # AI / LLM
    llm_provider: str = "openai"
    llm_api_key: Optional[str] = None
    llm_model: str = "gpt-4o-mini"
    llm_max_tokens: int = 4096
    llm_temperature: float = 0.1
    llm_circuit_breaker_threshold: int = 5
    llm_circuit_breaker_recovery: float = 30.0  # seconds

    # Processing
    max_document_size_mb: int = 100
    parser_timeout_seconds: int = 30
    parser_sandbox_enabled: bool = True

    # RBAC
    rbac_default_role: str = "viewer"
    multi_user_enabled: bool = False  # Feature flag, default OFF

    # Observability
    otel_enabled: bool = True
    otel_exporter_endpoint: Optional[str] = None
    log_level: str = "INFO"
    log_format: str = "json"  # json | console

    # Rate limiting
    rate_limit_enabled: bool = True
    rate_limit_requests_per_minute: int = 60


settings = Settings()
