"""Tests for src.core.detector — LLM-based document type auto-detection.

Covers:
- DocumentDetector keyword-based heuristic (all document types)
- DocumentDetector LLM path (mocked LLMClient)
- Fallback behavior when LLM is not configured or fails
- _parse_llm_response edge cases (quotes, extra text, unknown types)
- Non-text / short body handling
- build_detection_prompt structure
- DOCUMENT_TYPES and KEYWORD_MAP taxonomy completeness
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.detector import (
    DOCUMENT_TYPES,
    KEYWORD_MAP,
    DETECTION_SYSTEM_PROMPT,
    build_detection_prompt,
    DocumentDetector,
)
from src.core.llm_client import LLMConfig, LLMClient


# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def detector_no_llm():
    """DocumentDetector with no LLM client (keyword-only mode)."""
    return DocumentDetector(llm_client=None)


@pytest.fixture
def detector_with_llm():
    """DocumentDetector with a mocked LLM client that appears configured."""
    llm_config = LLMConfig(provider="openai", api_key="fake-key")
    client = LLMClient(llm_config)
    # Mock the internal methods so no real HTTP call is made
    client._get_client = AsyncMock()
    client._openai_url = MagicMock(return_value="http://fake/v1/chat/completions")
    client._openai_headers = MagicMock(return_value={})
    return DocumentDetector(llm_client=client)


# ── Taxonomy Tests ────────────────────────────────────────────────


class TestTaxonomy:
    """Test DOCUMENT_TYPES and KEYWORD_MAP completeness."""

    def test_document_types_has_other(self):
        """'other' must always be in the taxonomy (fallback)."""
        assert "other" in DOCUMENT_TYPES

    def test_keyword_map_keys_are_valid_types(self):
        """Every keyword map key must be a valid document type."""
        for key in KEYWORD_MAP:
            assert key in DOCUMENT_TYPES, f"KEYWORD_MAP has unknown type: {key}"

    def test_keyword_map_has_keywords(self):
        """Each type has at least one keyword."""
        for key, keywords in KEYWORD_MAP.items():
            assert len(keywords) > 0, f"No keywords for type: {key}"


# ── Prompt Construction Tests ─────────────────────────────────────


class TestBuildDetectionPrompt:
    """Test build_detection_prompt."""

    def test_returns_system_and_user(self):
        """Prompt has system + user messages."""
        messages = build_detection_prompt("Test Doc", "body text", ".pdf")
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    def test_system_prompt_lists_types(self):
        """System prompt mentions all valid types."""
        messages = build_detection_prompt("T", "B", ".txt")
        sys_content = messages[0]["content"]
        for type_key in DOCUMENT_TYPES:
            assert type_key in sys_content

    def test_user_prompt_contains_title_and_body(self):
        """User message includes the title and body excerpt."""
        messages = build_detection_prompt("My Invoice", "Amount due: $500", ".pdf")
        user_content = messages[1]["content"]
        assert "My Invoice" in user_content
        assert "Amount due: $500" in user_content
        assert ".pdf" in user_content


# ── Keyword Detection Tests ───────────────────────────────────────


class TestKeywordDetection:
    """Test _detect_keyword heuristic."""

    def test_invoice(self, detector_no_llm):
        body = "INVOICE #12345\nAmount Due: $500.00\nBill To: Customer\nPayment Terms: Net 30"
        result = detector_no_llm._detect_keyword("Invoice 2024", body)
        assert result == "invoice"

    def test_contract(self, detector_no_llm):
        body = "AGREEMENT\nWhereas the parties hereby agree to the terms and conditions..."
        result = detector_no_llm._detect_keyword("Service Contract", body)
        assert result == "contract"

    def test_resume(self, detector_no_llm):
        body = "Work Experience\nEducation\nSkills\nReferences available upon request"
        result = detector_no_llm._detect_keyword("John Doe - Resume", body)
        assert result == "resume"

    def test_email(self, detector_no_llm):
        body = "From: sender@example.com\nTo: recipient@example.com\nSubject: Meeting\nDear John,"
        result = detector_no_llm._detect_keyword("Email", body)
        assert result == "email"

    def test_research_paper(self, detector_no_llm):
        body = "Abstract\nMethodology\nReferences\nCitation: Smith et al. (2023)\nDOI: 10.1234/abc"
        result = detector_no_llm._detect_keyword("Research Paper", body)
        assert result == "research_paper"

    def test_report(self, detector_no_llm):
        body = "Executive Summary\nFindings\nRecommendations\nAppendix A\nQuarterly Report"
        result = detector_no_llm._detect_keyword("Q3 Report", body)
        assert result == "report"

    def test_receipt(self, detector_no_llm):
        body = "Thank you!\nCash: $20.00\nChange: $5.00\nVisa ****1234\nQty: 2"
        result = detector_no_llm._detect_keyword("Receipt", body)
        assert result == "receipt"

    def test_letter(self, detector_no_llm):
        body = "Dear Sir,\n\nI am writing to...\n\nSincerely,\nJohn Doe"
        result = detector_no_llm._detect_keyword("Cover Letter", body)
        assert result == "letter"

    def test_form(self, detector_no_llm):
        body = "Please print clearly\nDate of Birth: ___\nSignature: ___\nCheck box if applicable"
        result = detector_no_llm._detect_keyword("Application Form", body)
        assert result == "form"

    def test_no_match_returns_other(self, detector_no_llm):
        body = "This is a random document with no matching keywords whatsoever."
        result = detector_no_llm._detect_keyword("Random", body)
        assert result == "other"

    def test_empty_body_returns_other(self, detector_no_llm):
        result = detector_no_llm._detect_keyword("Untitled", "")
        assert result == "other"

    def test_title_weighted_higher(self, detector_no_llm):
        """Title keywords are weighted 3x body keywords."""
        body = "some generic text without keywords"
        # Title has 'invoice' which should dominate
        result = detector_no_llm._detect_keyword("Invoice", body)
        assert result == "invoice"


# ── LLM Response Parsing Tests ────────────────────────────────────


class TestParseLLMResponse:
    """Test _parse_llm_response."""

    def test_exact_match(self, detector_no_llm):
        assert detector_no_llm._parse_llm_response("invoice") == "invoice"

    def test_with_whitespace(self, detector_no_llm):
        assert detector_no_llm._parse_llm_response("  invoice  ") == "invoice"

    def test_with_quotes(self, detector_no_llm):
        assert detector_no_llm._parse_llm_response("'invoice'") == "invoice"
        assert detector_no_llm._parse_llm_response('"invoice"') == "invoice"

    def test_with_backticks(self, detector_no_llm):
        assert detector_no_llm._parse_llm_response("`invoice`") == "invoice"

    def test_with_trailing_period(self, detector_no_llm):
        assert detector_no_llm._parse_llm_response("invoice.") == "invoice"

    def test_case_insensitive(self, detector_no_llm):
        assert detector_no_llm._parse_llm_response("INVOICE") == "invoice"
        assert detector_no_llm._parse_llm_response("Invoice") == "invoice"

    def test_embedded_in_sentence(self, detector_no_llm):
        """Type key found within a longer response."""
        result = detector_no_llm._parse_llm_response("This is an invoice document")
        assert result == "invoice"

    def test_empty_response(self, detector_no_llm):
        assert detector_no_llm._parse_llm_response("") == "other"

    def test_unknown_type(self, detector_no_llm):
        assert detector_no_llm._parse_llm_response("unknown_type") == "other"

    def test_none_response(self, detector_no_llm):
        assert detector_no_llm._parse_llm_response(None) == "other"


# ── Detect (Async) Tests ──────────────────────────────────────────


class TestDetectAsync:
    """Test the async detect() method."""

    @pytest.mark.asyncio
    async def test_detect_short_body_uses_keyword(self, detector_no_llm):
        """Short body (<50 chars) always uses keyword heuristic."""
        result = await detector_no_llm.detect("Invoice", "Amount due $50", ext=".pdf")
        assert result == "invoice"

    @pytest.mark.asyncio
    async def test_detect_empty_body(self, detector_no_llm):
        """Empty body returns 'other'."""
        result = await detector_no_llm.detect("Untitled", "", ext=".txt")
        assert result == "other"

    @pytest.mark.asyncio
    async def test_detect_no_llm_uses_keyword(self, detector_no_llm):
        """Without LLM, keyword heuristic is used even for long bodies."""
        body = "INVOICE\nAmount Due: $500.00\nBill To: Customer\nPayment Terms: Net 30\n" * 5
        result = await detector_no_llm.detect("Invoice 2024", body, ext=".pdf")
        assert result == "invoice"

    @pytest.mark.asyncio
    async def test_detect_with_llm_success(self, detector_with_llm):
        """LLM-configured detector uses LLM path for long bodies."""
        body = "This is a lengthy invoice document. " * 20
        # Mock the _detect_llm method to return 'invoice'
        detector_with_llm._detect_llm = AsyncMock(return_value="invoice")
        result = await detector_with_llm.detect("Invoice", body, ext=".pdf")
        assert result == "invoice"
        detector_with_llm._detect_llm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_detect_llm_failure_falls_back(self, detector_with_llm):
        """LLM failure falls back to keyword heuristic."""
        body = "INVOICE\nAmount Due: $500.00\nBill To: Customer\n" * 10
        detector_with_llm._detect_llm = AsyncMock(side_effect=Exception("LLM timeout"))
        result = await detector_with_llm.detect("Invoice 2024", body, ext=".pdf")
        # Should fall back to keyword and still detect 'invoice'
        assert result == "invoice"

    @pytest.mark.asyncio
    async def test_detect_short_body_skips_llm(self, detector_with_llm):
        """Short body skips LLM even when configured."""
        detector_with_llm._detect_llm = AsyncMock(return_value="invoice")
        result = await detector_with_llm.detect("Doc", "short", ext=".txt")
        # Should use keyword, not LLM
        detector_with_llm._detect_llm.assert_not_awaited()


# ── Detection Method Property Tests ───────────────────────────────


class TestDetectionMethod:
    """Test detection_method property."""

    def test_method_keyword_no_llm(self, detector_no_llm):
        assert detector_no_llm.detection_method == "keyword"

    def test_method_llm_when_configured(self, detector_with_llm):
        assert detector_with_llm.detection_method == "llm"

    def test_method_keyword_when_not_configured(self):
        """LLM client present but not configured → keyword."""
        client = LLMClient(LLMConfig())  # Empty config → not configured
        d = DocumentDetector(llm_client=client)
        assert d.detection_method == "keyword"


# ── DB Integration Tests ──────────────────────────────────────────


class TestDBDocumentType:
    """Test DB methods: update_document_type, get_documents_by_type, get_document_type_facet."""

    @pytest.fixture
    async def db(self):
        import tempfile
        from pathlib import Path
        from src.core.cache import InMemoryCache
        from src.core.db_sqlite import Database

        tmpdir = tempfile.mkdtemp()
        db_path = str(Path(tmpdir) / "test_detector.db")
        test_cache = InMemoryCache(max_size=100)
        database = Database(db_path=db_path, cache=test_cache)
        await database.connect()
        yield database
        await database.disconnect()

    @pytest.mark.asyncio
    async def test_update_document_type(self, db):
        """update_document_type sets the type."""
        doc_id = await db.save_document(
            path="/test/doc1.pdf",
            source_type="api", source_name="test",
            title="Test Invoice", ext=".pdf",
            mime_type="application/pdf",
            body="This is an invoice for services rendered.",
        )
        result = await db.update_document_type(doc_id, "invoice")
        assert result is True

        doc = await db.get_document(doc_id)
        assert doc["document_type"] == "invoice"

    @pytest.mark.asyncio
    async def test_update_document_type_not_found(self, db):
        """update_document_type returns False for non-existent doc."""
        result = await db.update_document_type(99999, "invoice")
        assert result is False

    @pytest.mark.asyncio
    async def test_get_documents_by_type(self, db):
        """get_documents_by_type filters by type."""
        # Create docs of different types
        for i, dtype in enumerate(["invoice", "contract", "invoice", "report"]):
            await db.save_document(
                path=f"/test/doc_{i}.txt",
                source_type="api", source_name="test",
                title=f"Doc {i}", ext=".txt",
                mime_type="text/plain", body=f"body {i}" * 20,
                document_type=dtype,
            )

        invoices = await db.get_documents_by_type("invoice")
        assert len(invoices) == 2
        assert all(d["document_type"] == "invoice" for d in invoices)

    @pytest.mark.asyncio
    async def test_get_document_type_facet(self, db):
        """get_document_type_facet returns counts."""
        for i, dtype in enumerate(["invoice", "invoice", "contract", "report", "other"]):
            await db.save_document(
                path=f"/test/facet_{i}.txt",
                source_type="api", source_name="test",
                title=f"Doc {i}", ext=".txt",
                mime_type="text/plain", body=f"body {i}" * 20,
                document_type=dtype,
            )

        facets = await db.get_document_type_facet()
        facet_dict = {f["value"]: f["count"] for f in facets}
        assert facet_dict.get("invoice") == 2
        assert facet_dict.get("contract") == 1
        assert facet_dict.get("report") == 1
        assert facet_dict.get("other") == 1

    @pytest.mark.asyncio
    async def test_save_document_with_type(self, db):
        """save_document accepts and stores document_type."""
        doc_id = await db.save_document(
            path="/test/typed_doc.txt",
            source_type="api", source_name="test",
            title="Contract", ext=".txt",
            mime_type="text/plain", body="agreement terms" * 20,
            document_type="contract",
        )
        doc = await db.get_document(doc_id)
        assert doc["document_type"] == "contract"

    @pytest.mark.asyncio
    async def test_default_type_is_other(self, db):
        """Documents saved without document_type get 'other'."""
        doc_id = await db.save_document(
            path="/test/default_doc.txt",
            source_type="api", source_name="test",
            title="Default", ext=".txt",
            mime_type="text/plain", body="some text" * 20,
        )
        doc = await db.get_document(doc_id)
        assert doc["document_type"] == "other"
