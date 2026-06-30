"""Sanitization layer for secure document processing.

Provides:
- ``SecureDocumentContext`` — wraps document content with metadata isolation
  so that raw body text never leaks into LLM prompts unguarded.
- ``SanitizingSummarizer`` — a drop-in summarizer that sanitizes inputs before
  forwarding to an underlying LLM summarizer, guarding against prompt injection
  and PII exposure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ── PII patterns ───────────────────────────────────────────────

# Best-effort regex-based PII detection. Not exhaustive — a production
# deployment should layer a dedicated NER model or Presidio on top.
_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL",       re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")),
    ("PHONE_CN",    re.compile(r"1[3-9]\d{9}")),
    ("PHONE_US",    re.compile(r"\+?1?\s*\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")),
    ("SSN",         re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CREDIT_CARD", re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b")),
    ("IPV4",        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("ID_CARD_CN",  re.compile(r"[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]")),
]


# ── SecureDocumentContext ──────────────────────────────────────

@dataclass
class SecureDocumentContext:
    """Isolated document context that never mixes raw body with metadata.

    Design principle: the raw ``body`` is stored separately from the
    ``metadata`` fields. Any code that needs to format a prompt MUST call
    ``safe_prompt_context()``, which applies sanitization.
    """

    title: str
    ext: str = ""
    mime_type: str = "application/octet-stream"
    source_name: str = "api"

    # Internal — never access directly from outside this module.
    _body: str = field(default="", repr=False)
    _metadata: dict = field(default_factory=dict, repr=False)

    # ── Accessors ───────────────────────────────────────────

    @property
    def body(self) -> str:
        """Return the raw body. Prefer ``safe_body()`` for prompt use."""
        return self._body

    def safe_body(self, *, max_chars: int = 3000, redact_pii: bool = True) -> str:
        """Return a sanitized, truncated version of the body safe for LLM prompts.

        - Truncates to ``max_chars``.
        - Optionally redacts PII patterns.
        """
        text = self._body[:max_chars]
        if redact_pii:
            text = _redact_pii(text)
        return text

    def safe_prompt_context(self, *, max_chars: int = 3000) -> str:
        """Build a prompt-safe string with metadata + sanitized body."""
        safe = self.safe_body(max_chars=max_chars)
        return (
            f"Title: {self.title}\n"
            f"Type: {self.ext or 'unknown'} ({self.mime_type})\n"
            f"Source: {self.source_name}\n\n"
            f"Content:\n{safe}"
        )

    # ── Factory ─────────────────────────────────────────────

    @classmethod
    def from_body(
        cls,
        title: str,
        body: str,
        *,
        ext: str = "",
        mime_type: str = "application/octet-stream",
        source_name: str = "api",
        metadata: dict | None = None,
    ) -> "SecureDocumentContext":
        """Create a context from a raw body string."""
        return cls(
            title=title,
            ext=ext,
            mime_type=mime_type,
            source_name=source_name,
            _body=body,
            _metadata=metadata or {},
        )


# ── SanitizingSummarizer ──────────────────────────────────────

class SanitizingSummarizer:
    """Wraps a base ``Summarizer`` with input sanitization.

    Every call to ``summarize()``:
    1. Wraps raw inputs in a ``SecureDocumentContext``.
    2. Redacts PII from the body.
    3. Truncates to a configurable character limit.
    4. Only then forwards to the underlying LLM summarizer.
    """

    def __init__(
        self,
        base_summarizer,  # duck-typed: must have .summarize(title, body) -> Optional[str]
        *,
        max_input_chars: int = 3000,
        redact_pii: bool = True,
    ):
        self._base = base_summarizer
        self._max_input_chars = max_input_chars
        self._redact_pii = redact_pii

    def summarize(self, title: str, body: str) -> Optional[str]:
        """Sanitize inputs, then delegate to the base summarizer."""
        ctx = SecureDocumentContext.from_body(title=title, body=body)
        safe_body = ctx.safe_body(
            max_chars=self._max_input_chars,
            redact_pii=self._redact_pii,
        )
        return self._base.summarize(title, safe_body)

    def batch_summarize(
        self, documents: list[dict], indexer
    ) -> int:
        """Sanitize and summarize a batch, updating the indexer."""
        count = 0
        for doc in documents:
            summary = self.summarize(doc["title"], doc["body"])
            if summary:
                indexer.update_summary(doc["id"], summary)
                count += 1
        return count


# ── Helpers ────────────────────────────────────────────────────

def _redact_pii(text: str) -> str:
    """Replace detected PII patterns with ``[REDACTED:<type>]``."""
    for label, pattern in _PII_PATTERNS:
        text = pattern.sub(f"[REDACTED:{label}]", text)
    return text


def redact_pii(text: str) -> str:
    """Public convenience wrapper — re-exports the PII redaction logic."""
    return _redact_pii(text)
