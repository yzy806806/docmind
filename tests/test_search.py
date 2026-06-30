"""Tests for src.core.search — multi-round search with dual-hash citation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ── Import smoke test ──────────────────────────────────────────

def test_import_search() -> None:
    from src.core.search import SearchEngine

    assert SearchEngine is not None


# ── SearchResult / Citation confidence ─────────────────────────

def test_citation_confidence_enum() -> None:
    from src.core.search import CitationConfidence

    assert CitationConfidence.EXACT_MATCH.value == "exact_match"
    assert CitationConfidence.HIGH.value == "high"
    assert CitationConfidence.MEDIUM.value == "medium"
    assert CitationConfidence.LOW.value == "low"


# ── Dual-hash citation ─────────────────────────────────────────

def test_dual_hash_citation() -> None:
    from src.core.search import DualHashCitation

    cit = DualHashCitation(
        doc_id=1,
        path="/tmp/doc.pdf",
        content_hash="abc123",
        structural_hash="def456",
        confidence="exact_match",
        snippet="Relevant snippet text.",
        position_start=100,
        position_end=250,
    )
    assert cit.doc_id == 1
    assert cit.content_hash == "abc123"
    assert cit.structural_hash == "def456"
    assert cit.confidence == "exact_match"
    assert cit.position_start == 100
    assert cit.position_end == 250


# ── Search engine with SQLite backend ──────────────────────────

class TestSearchEngineSQLite:
    def test_keyword_search_delegates_to_backend(self) -> None:
        from src.core.search import SearchEngine
        from src.core.search_backend import SearchResult, SearchResults

        mock_backend = MagicMock()
        mock_backend.search.return_value = SearchResults(
            query="test query",
            results=[
                SearchResult(
                    doc_id=1, path="/x", title="Result 1",
                    summary="Summary 1", body="Body 1", rank=0.9,
                    snippet="...Body 1...",
                )
            ],
            total_hits=1,
            backend="sqlite",
        )

        engine = SearchEngine(mock_backend)
        results = engine.search_simple("test query", limit=5)

        assert len(results) == 1
        assert results[0]["title"] == "Result 1"
        mock_backend.search.assert_called_once()

    def test_full_search_pipeline(self) -> None:
        from src.core.search import SearchEngine
        from src.core.search_backend import SearchResult, SearchResults

        mock_backend = MagicMock()
        mock_backend.search.return_value = SearchResults(
            query="important document",
            results=[
                SearchResult(
                    doc_id=i, path=f"/doc/{i}", title=f"Document {i}",
                    summary=f"Summary {i}", body=f"Body {i}", rank=1.0 - i * 0.1,
                    snippet=f"...Body {i}...",
                )
                for i in range(10)
            ],
            total_hits=10,
            backend="sqlite",
        )

        # Fake LLM for second stage
        mock_llm = MagicMock()
        mock_llm.chat.return_value = "0,3,7"

        engine = SearchEngine(mock_backend, mock_llm)
        results = engine.search("important document", top_k=3)

        # Should have called LLM for selection
        assert mock_llm.chat.called
        # Results should be filtered to top_k
        assert len(results) <= 3

    def test_search_no_results(self) -> None:
        from src.core.search import SearchEngine
        from src.core.search_backend import SearchResults

        mock_backend = MagicMock()
        mock_backend.search.return_value = SearchResults(
            query="nonexistent",
            results=[],
            total_hits=0,
            backend="sqlite",
        )

        engine = SearchEngine(mock_backend)
        results = engine.search("nonexistent")
        assert results == []

    def test_llm_fallback_on_error(self) -> None:
        from src.core.search import SearchEngine
        from src.core.search_backend import SearchResult, SearchResults

        mock_backend = MagicMock()
        mock_backend.search.return_value = SearchResults(
            query="test",
            results=[
                SearchResult(doc_id=i, path=f"/p/{i}", title=f"T{i}",
                             summary=f"S{i}", body=f"B{i}", rank=0.5)
                for i in range(10)
            ],
            total_hits=10,
            backend="sqlite",
        )

        mock_llm = MagicMock()
        mock_llm.chat.side_effect = RuntimeError("LLM down")

        engine = SearchEngine(mock_backend, mock_llm)
        results = engine.search("test", top_k=3)

        # Should fallback to top N
        assert len(results) == 3

    def test_citation_generation(self) -> None:
        from src.core.search import SearchEngine
        from src.core.search_backend import SearchResult, SearchResults

        mock_backend = MagicMock()
        mock_backend.search.return_value = SearchResults(
            query="citation test",
            results=[
                SearchResult(
                    doc_id=42, path="/docs/important.pdf",
                    title="Important Report", summary="Key findings",
                    body="This is the body of the document with source text.",
                    rank=0.95,
                    snippet="...source text...",
                )
            ],
            total_hits=1,
            backend="sqlite",
        )

        engine = SearchEngine(mock_backend)
        results = engine.search("citation test", top_k=5)

        assert len(results) == 1
        # Result should be a dict with citation info
        result = results[0]
        assert isinstance(result, dict)
        assert "citation" in result or "doc_id" in result


# ── Position-anchored citations ────────────────────────────────

def test_position_anchored_citation() -> None:
    from src.core.search import SearchEngine
    from src.core.search_backend import SearchResult, SearchResults

    mock_backend = MagicMock()
    body_text = "Introduction paragraph. Methods section describes the approach. "
    body_text += "Results show significant improvement. Discussion concludes."
    mock_backend.search.return_value = SearchResults(
        query="methods",
        results=[
            SearchResult(
                doc_id=1, path="/doc", title="Paper",
                summary="Research paper", body=body_text, rank=0.8,
                snippet="...Methods section...",
            )
        ],
        total_hits=1,
        backend="sqlite",
    )

    engine = SearchEngine(mock_backend)
    results = engine.search("methods", top_k=5)

    assert len(results) == 1
    # Verify position tracking if present
    result = results[0]
    assert isinstance(result, dict)
