"""LLM-based document type auto-detection.

Classifies documents into predefined categories (invoice, contract,
resume, email, etc.) using an LLM when configured, with a keyword-based
heuristic fallback when no LLM is available or the LLM call fails.

Integration: called during document ingestion after upsert_document()
to populate the ``document_type`` column.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from .llm_client import LLMClient

logger = logging.getLogger(__name__)

# ── Document Type Taxonomy ────────────────────────────────────────

# Predefined document types: type_key -> display_name
DOCUMENT_TYPES: dict[str, str] = {
    "invoice": "Invoice",
    "contract": "Contract",
    "resume": "Resume",
    "email": "Email",
    "research_paper": "Research Paper",
    "report": "Report",
    "receipt": "Receipt",
    "letter": "Letter",
    "form": "Form",
    "presentation": "Presentation",
    "spreadsheet": "Spreadsheet",
    "manual": "Manual",
    "article": "Article",
    "other": "Other",
}

# Keywords for the fallback heuristic: type_key -> list of keywords.
# Matched case-insensitively in the document title + body.
KEYWORD_MAP: dict[str, list[str]] = {
    "invoice": [
        "invoice", "amount due", "billing", "remit", "payment terms",
        "subtotal", "tax", "total due", "bill to", "ship to",
    ],
    "contract": [
        "agreement", "parties", "whereas", "hereby", "terms and conditions",
        "contractor", "obligations", "warranties", "liability",
        "effective date", "termination",
    ],
    "resume": [
        "curriculum vitae", "work experience", "education", "skills",
        "references", "professional summary", "employment history",
        "qualifications", "career objective",
    ],
    "email": [
        "from:", "to:", "subject:", "sent:", "dear", "regards",
        "cc:", "bcc:", "forwarded", "re:",
    ],
    "research_paper": [
        "abstract", "methodology", "references", "citation",
        "hypothesis", "data analysis", "conclusion",
        "keywords:", "doi", "et al",
    ],
    "report": [
        "executive summary", "findings", "recommendations",
        "methodology", "analysis", "stakeholders", "kpi",
        "quarterly", "annual report", "appendix",
    ],
    "receipt": [
        "cash", "card", "change", "thank you", "subtotal",
        "visa", "mastercard", "transaction", "purchase",
        "qty", "unit price",
    ],
    "letter": [
        "dear", "sincerely", "regards", "to whom it may concern",
        "yours truly", "best regards", "respectfully",
    ],
    "form": [
        "check box", "checkbox", "fill in", "date of birth",
        "social security", "please print", "applicant",
        "signature", "date signed",
    ],
    "presentation": [
        "slides", "agenda", "key takeaways", "overview",
        "presentation", "deck", "talking points",
    ],
    "spreadsheet": [
        "sheet1", "cell", "formula", "row", "column",
        "pivot table", "vlookup", "sum(", "=a1",
    ],
    "manual": [
        "instructions", "step 1", "warning", "caution",
        "troubleshooting", "user guide", "operating",
        "assembly", "maintenance",
    ],
    "article": [
        "byline", "published", "journalist", "paragraph",
        "editorial", "correspondent",
    ],
}

# Minimum body length to attempt LLM detection (shorter → keyword only).
_MIN_BODY_FOR_LLM = 50


# ── Prompt Construction ───────────────────────────────────────────

DETECTION_SYSTEM_PROMPT = (
    "You are a document classification assistant. Classify the document "
    "into exactly one of these types: invoice, contract, resume, email, "
    "research_paper, report, receipt, letter, form, presentation, "
    "spreadsheet, manual, article, other.\n\n"
    "Respond with ONLY the type key (e.g. 'invoice'), no explanation, "
    "no punctuation, no extra text."
)


def build_detection_prompt(
    title: str,
    body_excerpt: str,
    ext: str,
) -> list[dict[str, str]]:
    """Build chat-style messages for LLM document type classification.

    Args:
        title: Document title.
        body_excerpt: First N characters of the document body.
        ext: File extension (e.g. '.pdf').

    Returns:
        List of {"role": ..., "content": ...} message dicts.
    """
    user_msg = (
        f"Classify the following document.\n\n"
        f"Title: {title}\n"
        f"Extension: {ext}\n"
        f"Body (first {len(body_excerpt)} chars):\n"
        f"{body_excerpt}\n\n"
        f"Type key:"
    )
    return [
        {"role": "system", "content": DETECTION_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]


# ── DocumentDetector ──────────────────────────────────────────────


class DocumentDetector:
    """LLM-powered document type detection with keyword fallback.

    Args:
        llm_client: An LLMClient instance. If None or not configured,
            keyword-based heuristic is used.
        max_body_chars: Maximum body characters to send to the LLM.
    """

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        max_body_chars: int = 2000,
    ) -> None:
        self.llm = llm_client
        self.max_body_chars = max_body_chars

    # ── Public API ────────────────────────────────────────────────

    async def detect(
        self,
        title: str,
        body: str,
        *,
        ext: str = "",
    ) -> str:
        """Detect the document type.

        Returns a type_key string from DOCUMENT_TYPES.
        Falls back to keyword heuristic when LLM is not configured,
        the body is too short, or the LLM call fails.

        Args:
            title: Document title.
            body: Full document body text.
            ext: File extension including the dot (e.g. '.pdf').

        Returns:
            A type key like 'invoice', 'contract', or 'other'.
        """
        body = body or ""

        # If body is too short for meaningful LLM analysis, use keywords.
        if len(body.strip()) < _MIN_BODY_FOR_LLM:
            return self._detect_keyword(title, body)

        # If LLM is not configured, use keyword heuristic.
        if self.llm is None or not self.llm.is_configured:
            return self._detect_keyword(title, body)

        # Try LLM detection.
        try:
            return await self._detect_llm(title, body, ext)
        except Exception:
            logger.exception(
                "LLM document detection failed, falling back to keyword heuristic"
            )
            return self._detect_keyword(title, body)

    @property
    def detection_method(self) -> str:
        """Return the detection method that would be used ('llm' or 'keyword')."""
        if self.llm is not None and self.llm.is_configured:
            return "llm"
        return "keyword"

    # ── LLM Detection ─────────────────────────────────────────────

    async def _detect_llm(
        self, title: str, body: str, ext: str
    ) -> str:
        """Run LLM-based classification.

        Sends a classification prompt with the first max_body_chars
        of the body. Parses and validates the response.
        """
        body_excerpt = body[: self.max_body_chars]
        messages = build_detection_prompt(title, body_excerpt, ext)

        # Use the LLM client's internal _call_openai/_call_ollama
        # via a direct generate-like call. Since LLMClient.generate()
        # is designed for RAG (takes question + context), we call
        # the underlying provider methods directly.
        import httpx

        if self.llm is None:
            return "other"

        config = self.llm.config
        client = await self.llm._get_client()

        if config.provider == "ollama":
            payload = {
                "model": config.model,
                "messages": messages,
                "stream": False,
                "options": {
                    "num_predict": 50,  # Very short — just a type key
                    "temperature": 0.0,  # Deterministic classification
                },
            }
            url = self.llm._ollama_url()
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            raw = data.get("message", {}).get("content", "")
        else:
            # OpenAI-compatible
            payload = {
                "model": config.model,
                "messages": messages,
                "max_tokens": config.max_tokens,
                "temperature": 0.0,
            }
            url = self.llm._openai_url()
            headers = self.llm._openai_headers()
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            message = data["choices"][0]["message"]
            raw = message.get("content", "") or ""
            if not raw:
                raw = message.get("reasoning_content", "") or ""

        return self._parse_llm_response(raw)

    def _parse_llm_response(self, response: str) -> str:
        """Extract and validate the type_key from the LLM response.

        The LLM is instructed to respond with only the type key, but
        we handle common variations (extra text, quotes, etc.).
        """
        if not response:
            return "other"

        text = response.strip().lower()

        # Remove common wrapping: quotes, backticks, periods
        text = text.strip("'\"`.,;:!?")

        # Check for exact match first
        if text in DOCUMENT_TYPES:
            return text

        # Try to find a valid type key within the response
        for type_key in DOCUMENT_TYPES:
            if type_key in text:
                return type_key

        # LLM returned something unrecognized
        logger.warning("LLM returned unrecognized type: %r", response)
        return "other"

    # ── Keyword Fallback ──────────────────────────────────────────

    def _detect_keyword(self, title: str, body: str) -> str:
        """Keyword-based document type detection heuristic.

        Scores each type by counting keyword occurrences in the
        title + body (case-insensitive). Returns the highest-scoring
        type, or 'other' if all scores are zero.

        Title keywords are weighted 3x (title is more indicative).
        """
        title_lower = (title or "").lower()
        body_lower = (body or "").lower()
        combined = f"{title_lower} {body_lower}"

        best_type = "other"
        best_score = 0

        for type_key, keywords in KEYWORD_MAP.items():
            score = 0
            for kw in keywords:
                # Count occurrences in combined text
                count = combined.count(kw)
                if count > 0:
                    # Title matches weighted 3x
                    title_count = title_lower.count(kw)
                    body_count = body_lower.count(kw)
                    score += title_count * 3 + body_count * 1

            if score > best_score:
                best_score = score
                best_type = type_key

        return best_type
