"""DocMind error hierarchy with HTTP status code mappings.

Every user-facing error in DocMind inherits from DocMindError, which carries
a machine-readable code, a human-readable message, and an HTTP status code.
This enables consistent error serialization across REST, CLI, and Hermes plugin.
"""

from __future__ import annotations

from typing import Optional


class DocMindError(Exception):
    """Base exception for all DocMind errors.

    Attributes:
        code: Machine-readable error code (e.g. ``NOT_FOUND``, ``VALIDATION_ERROR``).
        message: Human-readable error message.
        status_code: HTTP status code for REST responses. Default 500.
        detail: Optional additional context for debugging.
    """

    code: str = "INTERNAL_ERROR"
    status_code: int = 500

    def __init__(
        self,
        message: str = "",
        *,
        detail: Optional[str] = None,
        code: Optional[str] = None,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.message = message or self.__class__.__doc__ or ""
        self.detail = detail
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code

    def to_dict(self) -> dict:
        """Serialize to a dict suitable for JSON error responses."""
        result: dict = {
            "error": self.code,
            "message": self.message,
        }
        if self.detail:
            result["detail"] = self.detail
        return result


# ── 4xx Client Errors ─────────────────────────────────────────────


class ValidationError(DocMindError):
    """Input validation failed."""
    code = "VALIDATION_ERROR"
    status_code = 400


class NotFoundError(DocMindError):
    """Requested resource was not found."""
    code = "NOT_FOUND"
    status_code = 404


class UnsupportedMediaTypeError(DocMindError):
    """File format is not supported."""
    code = "UNSUPPORTED_MEDIA_TYPE"
    status_code = 415


class PayloadTooLargeError(DocMindError):
    """Upload exceeds the maximum allowed size."""
    code = "PAYLOAD_TOO_LARGE"
    status_code = 413


class ConflictError(DocMindError):
    """Resource already exists or state conflict."""
    code = "CONFLICT"
    status_code = 409


class AuthenticationError(DocMindError):
    """Authentication required or failed."""
    code = "AUTHENTICATION_ERROR"
    status_code = 401


class RateLimitError(DocMindError):
    """Too many requests."""
    code = "RATE_LIMIT"
    status_code = 429


# ── Document-specific errors ─────────────────────────────────────


class DocumentNotFoundError(NotFoundError):
    """The requested document does not exist in the knowledge base."""
    code = "DOCUMENT_NOT_FOUND"

    def __init__(self, doc_id: int | str, *, detail: Optional[str] = None) -> None:
        super().__init__(
            message=f"Document not found: {doc_id}",
            detail=detail,
        )


class DocumentProcessingError(DocMindError):
    """Document processing pipeline failed."""
    code = "DOCUMENT_PROCESSING_ERROR"
    status_code = 422


class ExtractionError(DocumentProcessingError):
    """Failed to extract text from a document file."""
    code = "EXTRACTION_ERROR"

    def __init__(self, path: str, *, detail: Optional[str] = None) -> None:
        super().__init__(
            message=f"Failed to extract text from: {path}",
            detail=detail,
        )


class IndexingError(DocumentProcessingError):
    """Failed to index a document."""
    code = "INDEXING_ERROR"


class SummarizationError(DocumentProcessingError):
    """Failed to summarize a document."""
    code = "SUMMARIZATION_ERROR"


# ── Source / Ingest errors ────────────────────────────────────────


class IngestError(DocMindError):
    """Document ingestion failed."""
    code = "INGEST_ERROR"
    status_code = 422

    def __init__(
        self, path: str, *, detail: Optional[str] = None
    ) -> None:
        super().__init__(
            message=f"Ingestion failed for: {path}",
            detail=detail,
        )


class SourceUnavailableError(DocMindError):
    """External data source is unreachable."""
    code = "SOURCE_UNAVAILABLE"
    status_code = 502

    def __init__(
        self, source_name: str, *, detail: Optional[str] = None
    ) -> None:
        super().__init__(
            message=f"Source unavailable: {source_name}",
            detail=detail,
        )


class PathTraversalError(ValidationError):
    """Path traversal attack detected."""
    code = "PATH_TRAVERSAL"

    def __init__(self, path: str) -> None:
        super().__init__(
            message=f"Path traversal detected in: {path}",
        )


# ── Search errors ─────────────────────────────────────────────────


class SearchError(DocMindError):
    """Search operation failed."""
    code = "SEARCH_ERROR"
    status_code = 500


class InvalidQueryError(ValidationError):
    """Search query is malformed or too short."""
    code = "INVALID_QUERY"

    def __init__(self, query: str, *, detail: Optional[str] = None) -> None:
        super().__init__(
            message=f"Invalid search query: {query!r}",
            detail=detail,
        )


# ── Configuration errors ──────────────────────────────────────────


class ConfigurationError(DocMindError):
    """DocMind misconfigured — check environment variables."""
    code = "CONFIGURATION_ERROR"
    status_code = 500


# ── Job errors ────────────────────────────────────────────────────


class JobNotFoundError(NotFoundError):
    """The requested job does not exist."""
    code = "JOB_NOT_FOUND"

    def __init__(self, job_id: str, *, detail: Optional[str] = None) -> None:
        super().__init__(
            message=f"Job not found: {job_id}",
            detail=detail,
        )


class JobFailedError(DocMindError):
    """A background job has failed."""
    code = "JOB_FAILED"
    status_code = 500

    def __init__(
        self,
        job_id: str,
        *,
        detail: Optional[str] = None,
    ) -> None:
        super().__init__(
            message=f"Job {job_id} failed",
            detail=detail,
        )


# ── HTTP status code mapping ─────────────────────────────────────

# Map DocMindError subclasses to HTTP status codes for FastAPI exception handlers.
# Usage: status_code = ERROR_STATUS_MAP.get(type(exc), 500)
ERROR_STATUS_MAP: dict[type[DocMindError], int] = {
    ValidationError: 400,
    NotFoundError: 404,
    DocumentNotFoundError: 404,
    JobNotFoundError: 404,
    AuthenticationError: 401,
    ConflictError: 409,
    PayloadTooLargeError: 413,
    UnsupportedMediaTypeError: 415,
    DocumentProcessingError: 422,
    IngestError: 422,
    ExtractionError: 422,
    IndexingError: 422,
    SummarizationError: 422,
    RateLimitError: 429,
    SourceUnavailableError: 502,
    ConfigurationError: 500,
    SearchError: 500,
    JobFailedError: 500,
    DocMindError: 500,
}
