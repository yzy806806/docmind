"""Tests for src.core.search_backend — abstract SearchBackend + implementations."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


# ── Import smoke test ──────────────────────────────────────────

def test_import_search_backend() -> None:
    from src.core.search_backend import (
        SearchBackend,
        SearchResult,
        SearchResults,
        SQLiteSearchBackend,
        PostgresSearchBackend,
        create_backend,
    )

    assert SearchBackend is not None
    assert SearchResult is not None
    assert SearchResults is not None
    assert SQLiteSearchBackend is not None
    assert PostgresSearchBackend is not None
    assert create_backend is not None


# ── SearchResult / SearchResults dataclasses ───────────────────

def test_search_result_creation() -> None:
    from src.core.search_backend import SearchResult

    sr = SearchResult(
        doc_id=1,
        path="/tmp/doc.pdf",
        title="Test Doc",
        summary="A summary",
        body="Full body text",
        rank=0.85,
        snippet="...body...",
    )
    assert sr.doc_id == 1
    assert sr.rank == 0.85
    assert sr.snippet == "...body..."


def test_search_results_creation() -> None:
    from src.core.search_backend import SearchResult, SearchResults

    results = [SearchResult(doc_id=1, path="/x", title="T", summary=None, body="B", rank=1.0)]
    sr = SearchResults(query="test query", results=results, total_hits=1, backend="sqlite")
    assert sr.query == "test query"
    assert sr.total_hits == 1
    assert sr.backend == "sqlite"
    assert len(sr.results) == 1


# ── Abstract base class ────────────────────────────────────────

def test_cannot_instantiate_abstract_backend() -> None:
    from src.core.search_backend import SearchBackend

    with pytest.raises(TypeError):
        SearchBackend()  # type: ignore[abstract]


# ── SQLiteSearchBackend ────────────────────────────────────────

@pytest.fixture
def sqlite_backend() -> "SQLiteSearchBackend":
    from src.core.search_backend import SQLiteSearchBackend

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    path = tmp.name
    backend = SQLiteSearchBackend(db_path=path)
    yield backend
    backend.close()
    Path(path).unlink()


def test_sqlite_backend_creation(sqlite_backend) -> None:
    from src.core.search_backend import SQLiteSearchBackend

    assert isinstance(sqlite_backend, SQLiteSearchBackend)


def test_sqlite_index_and_search(sqlite_backend) -> None:
    backend = sqlite_backend

    backend.index_document(
        doc_id=1,
        path="/docs/report.pdf",
        title="Annual Report 2024",
        summary="Financial report for fiscal year 2024",
        body="Revenue increased by 20% compared to the previous year. "
        "New product lines contributed significantly to growth.",
    )

    backend.index_document(
        doc_id=2,
        path="/docs/guide.pdf",
        title="User Guide",
        summary="Getting started guide",
        body="This guide covers installation, configuration, and basic usage.",
    )

    results = backend.search("revenue growth", limit=5)
    assert results.backend == "sqlite"
    assert results.total_hits >= 1
    assert any("Annual Report" in r.title for r in results.results)


def test_sqlite_search_no_results(sqlite_backend) -> None:
    backend = sqlite_backend

    results = backend.search("nonexistent_term_xyzzy", limit=5)
    assert results.total_hits == 0
    assert len(results.results) == 0


def test_sqlite_delete_document(sqlite_backend) -> None:
    backend = sqlite_backend

    backend.index_document(1, "/x", "T", "S", "body text here")
    results_before = backend.search("body", limit=5)
    assert results_before.total_hits >= 1

    backend.delete_document(1)
    results_after = backend.search("body", limit=5)
    assert results_after.total_hits == 0


def test_sqlite_reindex_updates(sqlite_backend) -> None:
    backend = sqlite_backend

    backend.index_document(1, "/x", "Old Title", "Old summary", "old body")
    backend.index_document(1, "/x", "New Title", "New summary", "new body content")

    results = backend.search("new body", limit=5)
    assert results.total_hits >= 1
    # Old content should not be found
    results_old = backend.search("old body", limit=5)
    assert results_old.total_hits == 0


# ── create_backend factory ─────────────────────────────────────

def test_create_backend_sqlite() -> None:
    from src.core.search_backend import SQLiteSearchBackend, create_backend

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    path = tmp.name

    try:
        backend = create_backend("sqlite", db_path=path)
        assert isinstance(backend, SQLiteSearchBackend)
        backend.close()
    finally:
        Path(path).unlink()


def test_create_backend_unknown() -> None:
    from src.core.search_backend import create_backend

    with pytest.raises(ValueError, match="Unknown backend type"):
        create_backend("mongodb")
