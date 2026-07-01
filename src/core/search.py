"""Multi-round semantic search engine with dual-hash citation tracking.

Pipeline:
  1. Keyword FTS search (fast, broad recall via SearchBackend)
  2. LLM summary matching (precise relevance ranking)
  3. Source citation with dual-hash confidence model
  4. Position-anchored citations for exact excerpt locations

Confidence tiers (dual-hash model):
  - EXACT_MATCH: Both content_hash AND structural_hash match → byte-identical
  - HIGH:        Content hash matches, structural differs → same text, layout changed
  - MEDIUM:      Structural hash matches, content differs → same structure, content updated
  - LOW:         Neither hash matches → best-effort keyword match
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .search_backend import SearchBackend, SearchResult, SearchResults


# ── Citation confidence ────────────────────────────────────────


class CitationConfidence(str, Enum):
    """Dual-hash confidence tier for source citations."""

    EXACT_MATCH = "exact_match"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class DualHashCitation:
    """A citation anchored by a dual-hash (content + structural) model.

    Two independent hashes provide robust citation tracking:
      - content_hash: Hash of the text content (position-independent)
      - structural_hash: Hash of the document structure (headings, paragraphs, tables)
    """

    doc_id: int
    path: str
    content_hash: str
    structural_hash: str
    confidence: str  # one of CitationConfidence values
    snippet: str
    position_start: int = 0
    position_end: int = 0

    @classmethod
    def from_search_result(
        cls,
        result: SearchResult,
        query: str,
        content_hash: Optional[str] = None,
        structural_hash: Optional[str] = None,
    ) -> "DualHashCitation":
        """Create a citation from a search result.

        Computes content and structural hashes from the document body.
        """
        body = result.body

        # Content hash: hash the full body text (canonical form: stripped, lowered)
        canonical = " ".join(body.lower().split())
        computed_content = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        # Structural hash: hash the structure (headings, paragraph lengths, table shapes)
        structural_features = cls._extract_structural_features(body)
        computed_structural = hashlib.sha256(
            structural_features.encode("utf-8")
        ).hexdigest()

        # Determine confidence tier
        content_match = (content_hash is None) or (content_hash == computed_content)
        struct_match = (structural_hash is None) or (structural_hash == computed_structural)

        if content_match and struct_match:
            confidence = CitationConfidence.EXACT_MATCH
        elif content_match:
            confidence = CitationConfidence.HIGH
        elif struct_match:
            confidence = CitationConfidence.MEDIUM
        else:
            confidence = CitationConfidence.LOW

        # Find position of best match in body
        pos_start, pos_end = cls._find_match_position(body, query)

        return cls(
            doc_id=result.doc_id,
            path=result.path,
            content_hash=computed_content,
            structural_hash=computed_structural,
            confidence=confidence.value,
            snippet=result.snippet,
            position_start=pos_start,
            position_end=pos_end,
        )

    @staticmethod
    def _extract_structural_features(text: str) -> str:
        """Extract structural features from document text.

        Features: heading markers (#), paragraph count, line lengths,
        table-like structures (delimited by |), list markers (-, *, 1.).
        """
        lines = text.split("\n")
        features: list[str] = []

        # Count structural elements
        heading_count = sum(1 for line in lines if line.strip().startswith("#"))
        list_count = sum(
            1
            for line in lines
            if re.match(r"^\s*[-*+]\s", line) or re.match(r"^\s*\d+[.)]\s", line)
        )
        table_lines = sum(1 for line in lines if "|" in line and line.count("|") >= 2)
        para_lengths = [
            str(len(line)) for line in lines if line.strip() and not line.startswith("#")
        ]

        features.append(f"headings:{heading_count}")
        features.append(f"lists:{list_count}")
        features.append(f"table_lines:{table_lines}")
        features.append(f"para_pattern:{','.join(para_lengths[:20])}")  # first 20 para lengths

        return "|".join(features)

    @staticmethod
    def _find_match_position(body: str, query: str) -> tuple[int, int]:
        """Find the character position range of the best query match in body."""
        query_lower = query.lower()
        body_lower = body.lower()

        # Try exact match first
        idx = body_lower.find(query_lower)
        if idx >= 0:
            return idx, idx + len(query)

        # Try individual query terms
        terms = query_lower.split()
        best_pos = 0
        best_end = 0
        best_terms_matched = 0

        for term in terms:
            term_idx = body_lower.find(term)
            if term_idx >= 0:
                if best_terms_matched == 0:
                    best_pos = term_idx
                best_end = max(best_end, term_idx + len(term))
                best_terms_matched += 1

        return best_pos, best_end


# ── Search engine ──────────────────────────────────────────────


class SearchEngine:
    """Multi-stage semantic search engine.

    Stage 1: Keyword FTS via SearchBackend (fast, high recall)
    Stage 2: LLM relevance ranking on FTS results (precise)
    Stage 3: Dual-hash citation generation for each result

    If no LLM is available, falls back to FTS-only ranking.
    """

    def __init__(
        self,
        backend: SearchBackend,
        llm_client: Any = None,
        *,
        fts_candidate_limit: int = 30,
    ):
        self.backend = backend
        self.llm = llm_client
        self.fts_candidate_limit = fts_candidate_limit

    def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        include_citations: bool = True,
    ) -> list[dict[str, Any]]:
        """Full multi-stage search pipeline.

        Args:
            query: User search query
            top_k: Number of top results to return
            include_citations: Whether to generate dual-hash citations

        Returns:
            List of dicts with keys: doc_id, path, title, summary, body,
            snippet, rank, citation (DualHashCitation if include_citations=True)
        """
        # Stage 1: FTS broad recall
        fts_results: SearchResults = self.backend.search(
            query, limit=self.fts_candidate_limit
        )

        if not fts_results.results:
            return []

        candidates = fts_results.results

        # Stage 2: LLM relevance ranking (if LLM available and candidates > top_k)
        if self.llm and len(candidates) > top_k:
            ranked = self._llm_rank(query, candidates, top_k)
        else:
            ranked = candidates[:top_k]

        # Stage 3: Generate citations
        output: list[dict[str, Any]] = []
        for result in ranked:
            entry: dict[str, Any] = {
                "doc_id": result.doc_id,
                "path": result.path,
                "title": result.title,
                "summary": result.summary,
                "body": result.body,
                "snippet": result.snippet,
                "rank": result.rank,
            }

            if include_citations:
                citation = DualHashCitation.from_search_result(result, query)
                entry["citation"] = {
                    "content_hash": citation.content_hash,
                    "structural_hash": citation.structural_hash,
                    "confidence": citation.confidence,
                    "position_start": citation.position_start,
                    "position_end": citation.position_end,
                }

            output.append(entry)

        return output

    def search_simple(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Simple FTS-only search without LLM or citations.

        Returns list of dicts with: doc_id, path, title, summary, body, snippet, rank.
        """
        fts_results = self.backend.search(query, limit=limit)

        return [
            {
                "doc_id": r.doc_id,
                "path": r.path,
                "title": r.title,
                "summary": r.summary,
                "body": r.body,
                "snippet": r.snippet,
                "rank": r.rank,
            }
            for r in fts_results.results
        ]

    def _llm_rank(
        self,
        query: str,
        candidates: list[SearchResult],
        top_k: int,
    ) -> list[SearchResult]:
        """Use LLM to rank candidates by relevance to the query.

        Falls back to top-N by FTS rank on LLM failure.
        """
        # Build a compact prompt with document summaries and snippets
        lines: list[str] = []
        for i, doc in enumerate(candidates):
            summary_text = doc.summary or ""
            snippet_text = doc.snippet or ""
            # Truncate for prompt tokens
            combined = f"{doc.title}: {summary_text} | {snippet_text}"
            lines.append(f"[{i}] {combined[:300]}")

        prompt = (
            f"You are a document search assistant. Given a user query and "
            f"a list of document summaries, pick the {top_k} most relevant "
            f"documents.\n\n"
            f"User query: {query}\n\n"
            f"Documents:\n" + "\n".join(lines) + "\n\n"
            f"Return ONLY the document numbers, comma-separated "
            f'(e.g., "0,3,7,12").'
        )

        try:
            response = self.llm.chat(prompt, max_tokens=50)
            indices = [
                int(x.strip())
                for x in response.split(",")
                if x.strip().lstrip("-").isdigit()
            ]
            valid_indices = [i for i in indices if 0 <= i < len(candidates)]
            if valid_indices:
                return [candidates[i] for i in valid_indices[:top_k]]
        except Exception:
            pass

        # Fallback: return top-N by FTS rank
        return candidates[:top_k]

    @staticmethod
    def compute_dual_hash(body: str) -> tuple[str, str]:
        """Compute (content_hash, structural_hash) for a document body.

        Useful for pre-computing hashes at index time to compare later.
        """
        canonical = " ".join(body.lower().split())
        content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        structural_hash = DualHashCitation._extract_structural_features(body)
        structural_hash = hashlib.sha256(
            structural_hash.encode("utf-8")
        ).hexdigest()
        return content_hash, structural_hash
