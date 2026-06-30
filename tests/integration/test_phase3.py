"""Integration tests for DocMind Phase 3 — end-to-end pipeline, multi-format,
citation integrity, chaos/robustness, and Hermes contract tests.

These tests use a temporary SQLite database and sample documents to verify
the full document processing and search pipeline.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Generator

import pytest

# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def sample_docs_dir() -> Path:
    """Path to the sample documents directory."""
    return Path(__file__).parent.parent / "sample_docs"


@pytest.fixture
def tmp_db() -> Generator[tuple[Path, Path], None, None]:
    """Create temporary database files for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        index_db = Path(tmpdir) / "test_docmind.db"
        search_db = Path(tmpdir) / "test_docmind_fts.db"
        yield index_db, search_db


@pytest.fixture
def service(tmp_db):
    """Create a DocMindService with temporary databases."""
    from src.cli.services import DocMindService

    index_db, search_db = tmp_db
    svc = DocMindService(
        index_db_path=str(index_db),
        search_db_path=str(search_db),
    )
    yield svc
    svc.close()


@pytest.fixture
def populated_service(service, sample_docs_dir):
    """A service pre-populated with sample documents."""
    service.ingest_path(str(sample_docs_dir), source_name="test")
    return service


# ── End-to-End Pipeline Tests ───────────────────────────────────


class TestEndToEndPipeline:
    """Verify the full ingest → index → search → summarize → cite pipeline."""

    def test_ingest_directory(self, service, sample_docs_dir):
        """Ingesting a directory should index all supported files."""
        result = service.ingest_path(str(sample_docs_dir), source_name="test")
        assert result["count"] >= 1
        assert result["path"] == str(sample_docs_dir.resolve())

    def test_ingest_updates_stats(self, service, sample_docs_dir):
        """Stats should reflect ingested documents."""
        service.ingest_path(str(sample_docs_dir), source_name="test")
        stats = service.get_stats()
        assert stats["total"] >= 1

    def test_search_after_ingest(self, populated_service):
        """Search should return results after ingestion."""
        results = populated_service.search("machine learning", top_k=5)
        assert len(results) >= 1
        # Should find the ML pipeline document
        titles = [r["title"] for r in results]
        assert any("pipeline" in t.lower() or "machine" in t.lower() for t in titles)

    def test_search_returns_citations(self, populated_service):
        """Search results should include dual-hash citations."""
        results = populated_service.search("machine learning", top_k=5)
        assert len(results) >= 1
        for result in results:
            citation = result.get("citation")
            assert citation is not None, f"No citation in result: {result.get('title')}"
            assert "content_hash" in citation
            assert "structural_hash" in citation
            assert "confidence" in citation
            assert citation["confidence"] in (
                "exact_match", "high", "medium", "low"
            )

    def test_list_documents(self, populated_service):
        """Listing should return all documents."""
        docs = populated_service.list_documents(limit=100)
        assert len(docs) >= 1

    def test_list_filtered_by_source(self, populated_service):
        """Listing with source filter should only return matching docs."""
        docs = populated_service.list_documents(source="test", limit=100)
        assert len(docs) >= 1
        # All should have source "test"
        for doc in docs:
            source = doc.get("source_name", doc.get("source_type", ""))
            assert source == "test" or source == "local"

    def test_get_document(self, populated_service):
        """Getting a document by ID should return its full content."""
        docs = populated_service.list_documents(limit=1)
        assert len(docs) >= 1
        doc_id = docs[0]["id"]

        doc = populated_service.get_document(doc_id)
        assert doc is not None
        assert doc["id"] == doc_id
        assert "title" in doc
        assert "body" in doc
        assert "path" in doc

    def test_get_nonexistent_document(self, service):
        """Getting a non-existent document should raise DocumentNotFoundError."""
        from src.errors import DocumentNotFoundError

        with pytest.raises(DocumentNotFoundError):
            service.get_document(999999)

    def test_summarize_document(self, populated_service):
        """Summarizing a document should generate a summary."""
        docs = populated_service.list_documents(limit=1)
        assert len(docs) >= 1
        doc_id = docs[0]["id"]

        result = populated_service.summarize_document(doc_id)
        assert result["doc_id"] == doc_id
        assert "title" in result
        # Summary might be empty if no LLM, but the call should succeed
        assert "summary" in result

    def test_summarize_force(self, populated_service):
        """Force summarize should regenerate even if cached."""
        docs = populated_service.list_documents(limit=1)
        assert len(docs) >= 1
        doc_id = docs[0]["id"]

        # First call
        result1 = populated_service.summarize_document(doc_id)
        # Second call with force
        result2 = populated_service.summarize_document(doc_id, force=True)
        assert result2["cached"] is False

    def test_stats(self, populated_service):
        """Stats should return valid counts."""
        stats = populated_service.get_stats()
        assert "total" in stats
        assert "pending" in stats
        assert "indexed" in stats
        assert "summarized" in stats
        assert stats["total"] >= 1


# ── Multi-Format Tests ──────────────────────────────────────────


class TestMultiFormat:
    """Verify document processing across supported formats."""

    def test_markdown_extraction(self, service, sample_docs_dir):
        """Markdown files should be extracted and indexed."""
        service.ingest_path(str(sample_docs_dir / "pipeline_overview.md"))
        docs = service.list_documents()
        assert any("pipeline_overview" in d.get("title", "") for d in docs)

    def test_text_extraction(self, service, sample_docs_dir):
        """Plain text files should be extracted and indexed."""
        service.ingest_path(str(sample_docs_dir / "preprocessing_guide.txt"))
        docs = service.list_documents()
        assert any("preprocessing_guide" in d.get("title", "") for d in docs)

    def test_html_extraction(self, service, sample_docs_dir):
        """HTML files should be extracted and indexed."""
        service.ingest_path(str(sample_docs_dir / "evaluation_metrics.html"))
        docs = service.list_documents()
        assert any("evaluation_metrics" in d.get("title", "") for d in docs)

    def test_markdown_searchable(self, populated_service):
        """Markdown content should be searchable."""
        results = populated_service.search("machine learning pipeline", top_k=5)
        assert any("pipeline" in str(r).lower() for r in results)

    def test_html_content_searchable(self, populated_service):
        """HTML content should be searchable."""
        results = populated_service.search("evaluation metrics", top_k=5)
        assert any(
            "evaluation" in str(r).lower() or "metrics" in str(r).lower()
            for r in results
        )

    def test_text_content_searchable(self, populated_service):
        """Text content should be searchable."""
        results = populated_service.search("preprocessing", top_k=5)
        assert any("preprocessing" in str(r).lower() for r in results)


# ── Citation Integrity Tests ───────────────────────────────────


class TestCitationIntegrity:
    """Verify source tracking and citation confidence."""

    def test_citation_structure(self, populated_service):
        """Every citation should have the required fields."""
        results = populated_service.search("machine learning", top_k=5)
        for result in results:
            citation = result.get("citation", {})
            assert "content_hash" in citation
            assert "structural_hash" in citation
            assert "confidence" in citation
            assert "position_start" in citation
            assert "position_end" in citation

    def test_content_hash_consistency(self, populated_service):
        """Same document should produce same content hash across searches."""
        results1 = populated_service.search("machine learning", top_k=5)
        results2 = populated_service.search("pipeline", top_k=5)

        # Find same doc in both results
        for r1 in results1:
            for r2 in results2:
                if r1["doc_id"] == r2["doc_id"]:
                    assert r1["citation"]["content_hash"] == r2["citation"]["content_hash"]
                    assert r1["citation"]["structural_hash"] == r2["citation"]["structural_hash"]
                    return
        pytest.skip("No overlapping documents found between searches")

    def test_confidence_tier_valid(self, populated_service):
        """Confidence should always be a valid tier."""
        valid_tiers = {"exact_match", "high", "medium", "low"}
        results = populated_service.search("data preprocessing", top_k=10)
        for result in results:
            assert result["citation"]["confidence"] in valid_tiers

    def test_citation_position_range(self, populated_service):
        """Position start/end should be within body bounds."""
        docs = populated_service.list_documents(limit=1)
        assert len(docs) >= 1

        doc = populated_service.get_document(docs[0]["id"])
        body_len = len(doc.get("body", ""))

        # Search with a word we know is in the body
        results = populated_service.search("machine learning pipeline", top_k=1)
        if results:
            citation = results[0]["citation"]
            pos_start = citation["position_start"]
            pos_end = citation["position_end"]
            assert 0 <= pos_start <= body_len
            assert pos_start <= pos_end <= body_len


# ── Chaos / Robustness Tests ───────────────────────────────────


class TestRobustness:
    """Verify resilience under edge cases."""

    def test_search_empty_query(self, service):
        """Empty query should raise ValidationError."""
        from src.errors import ValidationError

        with pytest.raises(ValidationError):
            service.search("")

    def test_search_very_long_query(self, service):
        """Very long query should be rejected."""
        from src.errors import ValidationError

        long_query = "x" * 2000
        with pytest.raises(ValidationError):
            service.search(long_query)

    def test_ingest_nonexistent_path(self, service):
        """Ingesting a non-existent path should raise an error."""
        from src.errors import ValidationError

        with pytest.raises(ValidationError):
            service.ingest_path("/nonexistent/path/12345")

    def test_get_nonexistent_document(self, service):
        """Getting a document that doesn't exist should raise."""
        from src.errors import DocumentNotFoundError

        with pytest.raises(DocumentNotFoundError):
            service.get_document(999999)

    def test_invalid_doc_id_string(self, service):
        """Invalid doc_id format should raise ValidationError."""
        from src.errors import ValidationError

        with pytest.raises(ValidationError):
            service.get_document("not_a_number")

    def test_negative_doc_id(self, service):
        """Negative doc_id should raise ValidationError."""
        from src.errors import ValidationError

        with pytest.raises(ValidationError):
            service.get_document(-1)

    def test_duplicate_ingest_idempotent(self, service, sample_docs_dir):
        """Ingesting the same directory twice should be idempotent."""
        result1 = service.ingest_path(str(sample_docs_dir), source_name="test")
        result2 = service.ingest_path(str(sample_docs_dir), source_name="test")

        # Second ingest should report 0 new documents (hash-based dedup)
        assert result1["count"] >= 1
        assert result2["count"] == 0

    def test_search_no_results(self, service):
        """Search on empty index should return empty list."""
        results = service.search("nonexistent term xyzzy12345")
        assert results == []

    def test_service_close_idempotent(self, service):
        """Closing service twice should not error."""
        service.close()
        service.close()  # Should be safe


# ── Hermes Contract Tests ──────────────────────────────────────


class TestHermesContract:
    """Verify that kb_* tool signatures match the Hermes contract."""

    def test_kb_search_contract(self, populated_service):
        """kb_search should accept query and top_k, return results with citations."""
        from src.hermes_plugin import kb_search

        result = kb_search("machine learning", top_k=3)

        assert isinstance(result, dict)
        assert "results" in result
        assert "total" in result
        assert isinstance(result["results"], list)

        if result["results"]:
            first = result["results"][0]
            assert "doc_id" in first
            assert "title" in first
            assert "citation" in first

    def test_kb_list_contract(self, populated_service):
        """kb_list should accept source filter, return documents."""
        from src.hermes_plugin import kb_list

        result = kb_list(source="test")
        assert isinstance(result, dict)
        assert "documents" in result
        assert "total" in result
        assert isinstance(result["documents"], list)

    def test_kb_list_all_sources(self, populated_service):
        """kb_list with no source should return all documents."""
        from src.hermes_plugin import kb_list
        import src.cli.services as svc_mod

        orig = svc_mod._service
        try:
            svc_mod._service = populated_service
            result = kb_list()
        finally:
            svc_mod._service = orig

        assert result["total"] >= 1

    def test_kb_read_contract(self, populated_service):
        """kb_read should accept doc_id, return full document."""
        from src.hermes_plugin import kb_read
        from src.cli.services import _service

        docs = populated_service.list_documents(limit=1)
        assert len(docs) >= 1

        # Temporarily use the test's service
        import src.cli.services as svc_mod
        orig = svc_mod._service
        try:
            svc_mod._service = populated_service
            result = kb_read(docs[0]["id"])
        finally:
            svc_mod._service = orig

        assert isinstance(result, dict)
        assert "title" in result
        assert "body" in result
        assert "truncated" in result

    def test_kb_read_nonexistent(self):
        """kb_read on nonexistent doc should return error dict."""
        from src.hermes_plugin import kb_read

        result = kb_read(999999)
        assert "error" in result

    def test_kb_read_with_chunk_limit(self, populated_service):
        """kb_read should respect chunk_limit."""
        from src.hermes_plugin import kb_read

        docs = populated_service.list_documents(limit=1)
        assert len(docs) >= 1

        result = kb_read(docs[0]["id"], chunk_limit=100)
        body = result.get("body", "")
        assert len(body) <= 100 + 1  # +1 for the "…" truncation marker

    def test_kb_ingest_contract(self, sample_docs_dir):
        """kb_ingest should accept a path, return count."""
        from src.hermes_plugin import kb_ingest

        result = kb_ingest(str(sample_docs_dir))
        assert isinstance(result, dict)
        assert "count" in result
        assert "path" in result
        assert result.get("status") == "ok" or "error" in result

    def test_tool_registry_complete(self):
        """All four kb_* tools should be in the registry."""
        from src.hermes_plugin import get_registered_tools

        tools = get_registered_tools()
        assert "kb_search" in tools
        assert "kb_list" in tools
        assert "kb_read" in tools
        assert "kb_ingest" in tools

        # Each tool should have function, description, parameters
        for name, tool in tools.items():
            assert "function" in tool, f"{name} missing function"
            assert "description" in tool, f"{name} missing description"
            assert "parameters" in tool, f"{name} missing parameters"

    def test_tool_functions_callable(self):
        """All registered tool functions should be callable."""
        from src.hermes_plugin import get_registered_tools

        tools = get_registered_tools()
        for name, tool in tools.items():
            assert callable(tool["function"]), f"{name} function is not callable"


# ── Validation Tests ───────────────────────────────────────────


class TestValidation:
    """Test the validation module independently."""

    def test_validate_search_query(self):
        from src.validation import validate_search_query
        from src.errors import InvalidQueryError

        assert validate_search_query("hello") == "hello"
        assert validate_search_query("  hello world  ") == "hello world"

        with pytest.raises(InvalidQueryError):
            validate_search_query("")

        with pytest.raises(InvalidQueryError):
            validate_search_query("a")  # too short

    def test_validate_doc_id(self):
        from src.validation import validate_doc_id
        from src.errors import ValidationError

        assert validate_doc_id(42) == 42
        assert validate_doc_id("42") == 42

        with pytest.raises(ValidationError):
            validate_doc_id("abc")

        with pytest.raises(ValidationError):
            validate_doc_id(-1)

        with pytest.raises(ValidationError):
            validate_doc_id(0)

    def test_validate_source_name(self):
        from src.validation import validate_source_name
        from src.errors import ValidationError

        assert validate_source_name("local") == "local"
        assert validate_source_name("my-source_v1") == "my-source_v1"

        with pytest.raises(ValidationError):
            validate_source_name("")

        with pytest.raises(ValidationError):
            validate_source_name("has spaces")

    def test_validate_path_traversal(self):
        from src.validation import validate_path
        from src.errors import PathTraversalError, ValidationError

        # Valid paths
        result = validate_path(".")
        assert result is not None

        # Traversal
        with pytest.raises(PathTraversalError):
            validate_path("../etc/passwd")

        with pytest.raises(PathTraversalError):
            validate_path("..")

    def test_validate_directory_path(self):
        from src.validation import validate_directory_path
        from src.errors import ValidationError, PathTraversalError

        # Existing directory
        result = validate_directory_path(".")
        assert result.is_dir()

        # Non-existent
        with pytest.raises(ValidationError):
            validate_directory_path("/nonexistent/dir/abc123")

        # Traversal
        with pytest.raises(PathTraversalError):
            validate_directory_path("..")


# ── Formatter Tests ────────────────────────────────────────────


class TestFormatters:
    """Test CLI output formatters."""

    def test_json_format(self):
        from src.cli.formatters import format_output

        data = {"key": "value", "count": 42}
        output = format_output(data, fmt="json")
        parsed = json.loads(output)
        assert parsed == data

    def test_table_format(self):
        from src.cli.formatters import format_output

        data = {"Name": "DocMind", "Version": "0.1.0"}
        output = format_output(data, fmt="table")
        assert "Name" in output
        assert "DocMind" in output

    def test_list_table_format(self):
        from src.cli.formatters import format_output

        data = [
            {"id": 1, "title": "Doc A"},
            {"id": 2, "title": "Doc B"},
        ]
        output = format_output(data, fmt="table")
        assert "Doc A" in output
        assert "Doc B" in output

    def test_search_results_format(self):
        from src.cli.formatters import format_search_results

        results = [
            {
                "doc_id": 1,
                "title": "Test Doc",
                "snippet": "sample text",
                "path": "/data/test.txt",
                "citation": {"confidence": "exact_match"},
            }
        ]
        output = format_search_results(results, 1, "test query", fmt="table")
        assert "test query" in output
        assert "Test Doc" in output
        assert "EXACT" in output

    def test_format_document(self):
        from src.cli.formatters import format_document

        doc = {
            "id": 42,
            "title": "Test Document",
            "path": "/data/test.md",
            "status": "indexed",
            "source_type": "local",
            "ext": ".md",
            "mime_type": "text/markdown",
            "summary": "A test document.",
            "body": "Full content here.",
        }
        output = format_document(doc, fmt="table")
        assert "Test Document" in output
        assert "42" in output
        assert "A test document" in output
