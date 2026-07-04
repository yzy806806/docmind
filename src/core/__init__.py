"""DocMind core engine."""

from .indexer import Indexer
from .search import SearchEngine
from .search import CitationConfidence, DualHashCitation
from .extractor import Extractor
from .summarizer import Summarizer
from .storage import StorageConnector
from .sanitizer import (
    SecureDocumentContext,
    SanitizingSummarizer,
    redact_pii,
    sanitize_text,
)
from .search_backend import (
    SearchBackend,
    SearchResult,
    SearchResults,
    SQLiteSearchBackend,
    PostgresSearchBackend,
    create_backend,
)
from .parser_sandbox import ParserSandbox, RlimitSandbox
from .db_sqlite import Database
from .db_sqlite import Database as SqliteDatabase
from .job_queue import JobQueue
from .config import config, Config

__all__ = [
    "Indexer",
    "SearchEngine",
    "CitationConfidence",
    "DualHashCitation",
    "Extractor",
    "Summarizer",
    "StorageConnector",
    "SecureDocumentContext",
    "SanitizingSummarizer",
    "redact_pii",
    "sanitize_text",
    "SearchBackend",
    "SearchResult",
    "SearchResults",
    "SQLiteSearchBackend",
    "PostgresSearchBackend",
    "create_backend",
    "ParserSandbox",
    "RlimitSandbox",
    "Database",
    "SqliteDatabase",
    "JobQueue",
    "config",
    "Config",
]
