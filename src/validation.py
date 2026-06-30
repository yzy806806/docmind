"""Input validation and sanitization for DocMind.

Covers path safety (canonical resolution, traversal detection), document ID
format validation, file upload safety (magic bytes, size limits), and search
query validation.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from .errors import (
    InvalidQueryError,
    PathTraversalError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
    ValidationError,
)

# ── Path validation ───────────────────────────────────────────────

# Common dangerous path patterns
_PATH_TRAVERSAL_RE = re.compile(r"\.\.(?:/|\\|$)")
_NUL_BYTE_RE = re.compile(r"\x00")


def validate_path(path: str, *, base_dir: Optional[Path] = None) -> Path:
    """Validate and resolve a file path, rejecting traversal attempts.

    Args:
        path: The input path to validate.
        base_dir: Optional base directory to enforce containment within.
                  If provided, the resolved path must be inside base_dir.

    Returns:
        Resolved absolute Path.

    Raises:
        PathTraversalError: If path contains ``..``, NUL bytes, or escapes base_dir.
        ValidationError: If path is empty or invalid.
    """
    if not path or not path.strip():
        raise ValidationError("Path must not be empty")

    stripped = path.strip()

    # Reject NUL bytes
    if _NUL_BYTE_RE.search(stripped):
        raise PathTraversalError(path)

    # Reject path traversal patterns before resolution
    if _PATH_TRAVERSAL_RE.search(stripped):
        raise PathTraversalError(path)

    resolved = Path(stripped).resolve()

    # After resolution, double-check containment
    if base_dir is not None:
        resolved_base = base_dir.resolve()
        try:
            resolved.relative_to(resolved_base)
        except ValueError:
            raise PathTraversalError(path)

    return resolved


def validate_document_path(path: str, base_dir: Path) -> Path:
    """Validate a document path for ingest — must be a file under base_dir.

    Raises:
        PathTraversalError: If path escapes base_dir.
        ValidationError: If path does not point to a file.
    """
    resolved = validate_path(path, base_dir=base_dir)

    if not resolved.is_file():
        raise ValidationError(f"Path is not a file: {path}")

    return resolved


def validate_directory_path(path: str) -> Path:
    """Validate a directory path for scanning.

    Raises:
        ValidationError: If path does not exist or is not a directory.
        PathTraversalError: If path contains traversal patterns.
    """
    if not path or not path.strip():
        raise ValidationError("Directory path must not be empty")

    stripped = path.strip()
    if _PATH_TRAVERSAL_RE.search(stripped):
        raise PathTraversalError(path)

    resolved = Path(stripped).resolve()

    if not resolved.exists():
        raise ValidationError(f"Directory not found: {path}")

    if not resolved.is_dir():
        raise ValidationError(f"Path is not a directory: {path}")

    return resolved


# ── Document ID validation ────────────────────────────────────────

# Doc IDs are positive integers (from SERIAL/INTEGER PRIMARY KEY)
_DOC_ID_RE = re.compile(r"^\d+$")


def validate_doc_id(raw: str | int) -> int:
    """Parse and validate a document ID.

    Raises:
        ValidationError: If the ID is not a valid positive integer.
    """
    if isinstance(raw, int):
        if raw <= 0:
            raise ValidationError(f"Invalid document ID: {raw} (must be > 0)")
        return raw

    raw_str = str(raw).strip()
    if not _DOC_ID_RE.match(raw_str):
        raise ValidationError(f"Invalid document ID: {raw!r} (must be a positive integer)")

    doc_id = int(raw_str)
    if doc_id <= 0:
        raise ValidationError(f"Invalid document ID: {doc_id} (must be > 0)")
    return doc_id


# ── File upload validation ────────────────────────────────────────

# Magic bytes for common document formats
_MAGIC_SIGNATURES: dict[str, bytes] = {
    ".pdf": b"%PDF",
    ".docx": b"PK\x03\x04",
    ".html": b"<!DOCTYPE html",
    ".htm": b"<!DOCTYPE html",
    ".xml": b"<?xml",
}


def validate_file_upload(
    filename: str,
    content: bytes,
    *,
    max_size: int = 100 * 1024 * 1024,  # 100 MB
    allowed_extensions: Optional[set[str]] = None,
) -> str:
    """Validate an uploaded file's name, size, extension, and magic bytes.

    Args:
        filename: Original filename from the upload.
        content: Raw file bytes.
        max_size: Maximum allowed file size in bytes.
        allowed_extensions: Set of allowed extensions (with leading dot).
                            Defaults to DocMind's supported set.

    Returns:
        The normalized lowercase extension (with leading dot).

    Raises:
        PayloadTooLargeError: File exceeds max_size.
        UnsupportedMediaTypeError: Extension not allowed or magic bytes mismatch.
        ValidationError: Empty file or missing filename.
    """
    if not filename:
        raise ValidationError("Filename is required")

    if not content:
        raise ValidationError("File content is empty")

    # Size check
    if len(content) > max_size:
        raise PayloadTooLargeError(
            message=f"File exceeds maximum size of {max_size:,} bytes",
            detail=f"Actual: {len(content):,} bytes, Filename: {filename}",
        )

    # Extension check
    ext = Path(filename).suffix.lower()
    if not ext:
        raise UnsupportedMediaTypeError(
            message=f"File has no extension: {filename}",
        )

    if allowed_extensions is not None and ext not in allowed_extensions:
        raise UnsupportedMediaTypeError(
            message=f"Unsupported file type: {ext}",
            detail=f"Allowed: {sorted(allowed_extensions)}",
        )

    # Magic byte verification (best-effort — not all formats have reliable magic)
    if ext in _MAGIC_SIGNATURES:
        expected_magic = _MAGIC_SIGNATURES[ext]
        if not content.startswith(expected_magic):
            raise UnsupportedMediaTypeError(
                message=f"File content does not match extension {ext}",
                detail=f"Expected magic bytes: {expected_magic!r}, Filename: {filename}",
            )

    return ext


# ── Search query validation ───────────────────────────────────────

_MIN_QUERY_LENGTH = 2
_MAX_QUERY_LENGTH = 1000


def validate_search_query(query: str) -> str:
    """Validate and normalize a search query.

    Raises:
        InvalidQueryError: If query is empty, too short, or too long.
    """
    if not query or not query.strip():
        raise InvalidQueryError(query, detail="Query must not be empty")

    normalized = query.strip()

    if len(normalized) < _MIN_QUERY_LENGTH:
        raise InvalidQueryError(
            query,
            detail=f"Query too short (minimum {_MIN_QUERY_LENGTH} characters)",
        )

    if len(normalized) > _MAX_QUERY_LENGTH:
        raise InvalidQueryError(
            query,
            detail=f"Query too long (maximum {_MAX_QUERY_LENGTH} characters)",
        )

    return normalized


# ── Source name validation ────────────────────────────────────────

_SOURCE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")


def validate_source_name(name: str) -> str:
    """Validate a source name (used for filtering by data source).

    Raises:
        ValidationError: If name is empty or contains invalid characters.
    """
    if not name or not name.strip():
        raise ValidationError("Source name must not be empty")

    stripped = name.strip()

    if not _SOURCE_NAME_RE.match(stripped):
        raise ValidationError(
            f"Invalid source name: {name!r} "
            f"(must be 1-64 chars, alphanumeric, dots, hyphens, underscores)"
        )

    return stripped
