"""Sanitization layer for secure document processing.

Provides:
- ``SecureDocumentContext`` — wraps document content with metadata isolation
  so that raw body text never leaks into LLM prompts unguarded.
- ``SanitizingSummarizer`` — a drop-in summarizer that sanitizes inputs before
  forwarding to an underlying LLM summarizer, guarding against prompt injection
  and PII exposure.

Sanitization pipeline (applied in order):
1. Unicode NFKC normalization
2. Control character stripping
3. PII redaction (regex-based)
4. Token count check / truncation
5. Character count truncation
"""

from __future__ import annotations

import re
import unicodedata
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

# Control characters to strip (all ASCII C0 + C1 control chars except
# common whitespace: tab, newline, carriage return).
_CONTROL_CHARSET: str = (
    "".join(
        chr(c) for c in range(0, 32)
        if chr(c) not in ("\t", "\n", "\r")
    )
    + "".join(chr(c) for c in range(0x7F, 0xA0))
    + "\ufeff"  # BOM
)
_CONTROL_CHARS_RE = re.compile(
    "[" + re.escape(_CONTROL_CHARSET) + "]"
)


def _estimate_tokens(text: str) -> int:
    """Rough token-count estimate: ~4 chars per token for English text.

    This is a cheap heuristic; for production use, integrate tiktoken
    or the model's native tokenizer.
    """
    return max(1, len(text) // 4)


# ── Sanitization pipeline ──────────────────────────────────────

def apply_nfkc_normalization(text: str) -> str:
    """Apply Unicode NFKC normalization (compatibility decomposition + composition).

    This normalizes lookalike characters, fullwidth forms, ligatures, and
    other Unicode confusables to their canonical equivalents.  Critical for
    preventing Unicode-based prompt injection and ensuring consistent tokenization.
    """
    return unicodedata.normalize("NFKC", text)


def strip_control_characters(text: str) -> str:
    """Remove C0, C1 control characters and BOM, preserving tabs/newlines.

    Control characters can be used for prompt injection (e.g. null bytes,
    escape sequences, right-to-left override markers).
    """
    return _CONTROL_CHARS_RE.sub("", text)


def sanitize_text(
    text: str,
    *,
    nfkc_normalize: bool = True,
    strip_controls: bool = True,
    redact_pii: bool = True,
    max_chars: Optional[int] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """Apply the full sanitization pipeline to a text string.

    Pipeline order:
    1. NFKC normalization
    2. Control character stripping
    3. PII redaction
    4. Token count truncation
    5. Character count truncation

    Args:
        text: Raw input text.
        nfkc_normalize: Apply NFKC normalization (default True).
        strip_controls: Strip C0/C1 control characters (default True).
        redact_pii: Redact detected PII patterns (default True).
        max_chars: Maximum character count after all other transforms.
        max_tokens: Approximate maximum token count (1 token ≈ 4 chars).

    Returns:
        Sanitized text string.
    """
    if nfkc_normalize:
        text = apply_nfkc_normalization(text)

    if strip_controls:
        text = strip_control_characters(text)

    if redact_pii:
        text = _redact_pii(text)

    # Token-based truncation (applied first since it's coarser)
    if max_tokens is not None:
        char_limit = max_tokens * 4  # rough: ~4 chars per token
        if len(text) > char_limit:
            text = text[:char_limit]

    # Character-based truncation (final safety net)
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars]

    return text


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

    def safe_body(
        self,
        *,
        max_chars: int = 16000,
        max_tokens: int = 4000,
        redact_pii: bool = True,
        nfkc_normalize: bool = True,
        strip_controls: bool = True,
    ) -> str:
        """Return a fully sanitized, truncated version of the body safe for LLM prompts.

        Applies the full pipeline: NFKC → control strip → PII redact →
        token truncation → char truncation.
        """
        return sanitize_text(
            self._body,
            nfkc_normalize=nfkc_normalize,
            strip_controls=strip_controls,
            redact_pii=redact_pii,
            max_tokens=max_tokens,
            max_chars=max_chars,
        )

    def safe_prompt_context(
        self,
        *,
        max_chars: int = 16000,
        max_tokens: int = 4000,
    ) -> str:
        """Build a prompt-safe string with metadata + sanitized body."""
        safe = self.safe_body(max_chars=max_chars, max_tokens=max_tokens)
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
    2. Applies NFKC normalization.
    3. Strips control characters.
    4. Redacts PII from the body.
    5. Checks token count and truncates if needed.
    6. Truncates to a configurable character limit.
    7. Only then forwards to the underlying LLM summarizer.
    """

    def __init__(
        self,
        base_summarizer,  # duck-typed: must have .summarize(title, body) -> Optional[str]
        *,
        max_input_chars: int = 16000,
        max_tokens: int = 4000,
        redact_pii: bool = True,
        nfkc_normalize: bool = True,
        strip_controls: bool = True,
    ):
        self._base = base_summarizer
        self._max_input_chars = max_input_chars
        self._max_tokens = max_tokens
        self._redact_pii = redact_pii
        self._nfkc_normalize = nfkc_normalize
        self._strip_controls = strip_controls

    def summarize(self, title: str, body: str) -> Optional[str]:
        """Sanitize inputs, then delegate to the base summarizer."""
        ctx = SecureDocumentContext.from_body(title=title, body=body)
        safe_body = ctx.safe_body(
            max_chars=self._max_input_chars,
            max_tokens=self._max_tokens,
            redact_pii=self._redact_pii,
            nfkc_normalize=self._nfkc_normalize,
            strip_controls=self._strip_controls,
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
