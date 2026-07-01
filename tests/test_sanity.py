"""Sanity / smoke tests for the DocMind core module.

These tests verify that imports work, basic instantiation succeeds,
and the core public API surface is intact.
"""

from __future__ import annotations

import pytest

# ── 1. Import smoke tests ──────────────────────────────────────


def test_core_imports() -> None:
    """Every top-level core symbol should be importable."""
    from src.core import Indexer, SearchEngine, Extractor, Summarizer, StorageConnector
    assert Indexer is not None
    assert SearchEngine is not None
    assert Extractor is not None
    assert Summarizer is not None
    assert StorageConnector is not None


def test_models_import() -> None:
    """Models module should export key types."""
    from src.core.models import (
        DocumentRecord,
        DocumentCreate,
        DocumentStatus,
        JobRecord,
        JobState,
        SubmissionAccepted,
        JobStatusResponse,
        ErrorResponse,
    )
    assert DocumentStatus.PENDING == "pending"
    assert JobState.PENDING == "pending"
    # Instantiation checks
    doc = DocumentCreate(path="/tmp/test.pdf", title="Test")
    assert doc.path == "/tmp/test.pdf"
    assert doc.source_type == "api"


def test_sanitizer_import() -> None:
    """Sanitizer module should export the new types."""
    from src.core.sanitizer import (
        SecureDocumentContext,
        SanitizingSummarizer,
        redact_pii,
    )
    assert SecureDocumentContext is not None
    assert SanitizingSummarizer is not None
    assert redact_pii is not None


# ── 2. SecureDocumentContext ───────────────────────────────────


class TestSecureDocumentContext:
    def test_safe_body_truncates(self) -> None:
        from src.core.sanitizer import SecureDocumentContext

        ctx = SecureDocumentContext.from_body(
            title="Doc", body="x" * 5000
        )
        safe = ctx.safe_body(max_chars=10)
        assert len(safe) == 10

    def test_safe_body_redacts_email(self) -> None:
        from src.core.sanitizer import SecureDocumentContext

        ctx = SecureDocumentContext.from_body(
            title="Doc",
            body="Contact: alice@example.com for help.",
        )
        safe = ctx.safe_body(redact_pii=True)
        assert "alice@example.com" not in safe
        assert "[REDACTED:EMAIL]" in safe

    def test_safe_body_redacts_phone_cn(self) -> None:
        from src.core.sanitizer import SecureDocumentContext

        ctx = SecureDocumentContext.from_body(
            title="Doc",
            body="Call 13800138000 for support.",
        )
        safe = ctx.safe_body(redact_pii=True)
        assert "13800138000" not in safe
        assert "[REDACTED:PHONE_CN]" in safe

    def test_safe_body_no_redact_when_disabled(self) -> None:
        from src.core.sanitizer import SecureDocumentContext

        ctx = SecureDocumentContext.from_body(
            title="Doc",
            body="Contact alice@example.com",
        )
        safe = ctx.safe_body(redact_pii=False)
        assert "alice@example.com" in safe

    def test_safe_prompt_context_includes_title(self) -> None:
        from src.core.sanitizer import SecureDocumentContext

        ctx = SecureDocumentContext.from_body(
            title="Quarterly Report", body="Revenue grew 20%."
        )
        prompt = ctx.safe_prompt_context()
        assert "Quarterly Report" in prompt
        assert "Revenue grew 20%" in prompt


# ── 3. SanitizingSummarizer ────────────────────────────────────


class FakeSummarizer:
    """Duck-typed summarizer for testing — records calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def summarize(self, title: str, body: str) -> str:
        self.calls.append((title, body))
        return f"Summary of: {title}"


class TestSanitizingSummarizer:
    def test_delegates_to_base(self) -> None:
        from src.core.sanitizer import SanitizingSummarizer

        base = FakeSummarizer()
        ss = SanitizingSummarizer(base, max_input_chars=500)
        result = ss.summarize("Doc A", "Some content here.")
        assert result == "Summary of: Doc A"
        assert len(base.calls) == 1

    def test_redacts_pii_before_delegation(self) -> None:
        from src.core.sanitizer import SanitizingSummarizer

        base = FakeSummarizer()
        ss = SanitizingSummarizer(base, max_input_chars=500, redact_pii=True)
        ss.summarize("Doc", "Email alice@example.com")
        called_body = base.calls[0][1]
        assert "alice@example.com" not in called_body
        assert "[REDACTED:EMAIL]" in called_body

    def test_truncates_before_delegation(self) -> None:
        from src.core.sanitizer import SanitizingSummarizer

        base = FakeSummarizer()
        ss = SanitizingSummarizer(base, max_input_chars=10)
        ss.summarize("Doc", "A" * 100)
        called_body = base.calls[0][1]
        assert len(called_body) <= 10


# ── 4. redact_pii standalone ───────────────────────────────────


def test_redact_pii_standalone() -> None:
    from src.core.sanitizer import redact_pii

    result = redact_pii("User: bob@test.com, IP: 192.168.1.1")
    assert "bob@test.com" not in result
    assert "192.168.1.1" not in result
    assert "[REDACTED:EMAIL]" in result
    assert "[REDACTED:IPV4]" in result


# ── 5. Models serialisation ────────────────────────────────────


def test_error_response_has_trace_id() -> None:
    from src.core.models import ErrorResponse

    err = ErrorResponse(error="Something broke")
    assert err.trace_id is not None
    assert len(err.trace_id) == 36  # UUID string length


def test_submission_accepted_shape() -> None:
    from src.core.models import SubmissionAccepted

    resp = SubmissionAccepted(
        job_id="abc-123", status="pending", document_path="/tmp/doc.pdf"
    )
    data = resp.model_dump()
    assert data["job_id"] == "abc-123"
    assert data["status"] == "pending"
