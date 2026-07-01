"""Unified error hierarchy for docmind."""

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """Domain-specific error codes for structured error responses."""

    # General
    INTERNAL_ERROR = "INTERNAL_ERROR"
    NOT_FOUND = "NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    FEATURE_DISABLED = "FEATURE_DISABLED"

    # Auth
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    INVALID_TOKEN = "INVALID_TOKEN"

    # Storage
    DOCUMENT_TOO_LARGE = "DOCUMENT_TOO_LARGE"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    STORAGE_INTEGRITY_ERROR = "STORAGE_INTEGRITY_ERROR"
    DOCUMENT_LOCKED = "DOCUMENT_LOCKED"

    # Processing
    PROCESSING_FAILED = "PROCESSING_FAILED"
    PARSER_UNAVAILABLE = "PARSER_UNAVAILABLE"


class DocMindError(Exception):
    """Base exception for all docmind errors."""

    def __init__(
        self,
        message: str,
        code: ErrorCode = ErrorCode.INTERNAL_ERROR,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


class NotFoundError(DocMindError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, ErrorCode.NOT_FOUND, details)


class AuthorizationError(DocMindError):
    def __init__(self, message: str, code: ErrorCode = ErrorCode.FORBIDDEN, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code, details)


class StorageError(DocMindError):
    def __init__(self, message: str, code: ErrorCode = ErrorCode.INTERNAL_ERROR, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code, details)


class FeatureDisabledError(DocMindError):
    def __init__(self, feature: str) -> None:
        super().__init__(
            f"Feature '{feature}' is not enabled",
            ErrorCode.FEATURE_DISABLED,
            {"feature": feature},
        )
