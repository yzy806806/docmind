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
from .chunking import TextChunker


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


# ── Hybrid (FTS5 + vector) search ──────────────────────────────


class HybridSearchEngine:
    """Combines FTS5 keyword search with vector semantic search.

    Score fusion: weighted combination of normalized FTS5 BM25 score
    and cosine similarity. Falls back to FTS5-only when no embedding
    provider is available or no embeddings are stored.

    Usage::

        from src.core.embeddings import EmbeddingClient
        from src.core.db_sqlite import Database

        db = Database("data/docmind.db")
        await db.connect()
        embed_client = EmbeddingClient(config.embedding)
        engine = HybridSearchEngine(db=db, embed_client=embed_client)
        results = await engine.search("how to train a model", top_k=5)
    """

    def __init__(
        self,
        db: Any,
        embed_client: Any = None,
        *,
        vector_weight: float = 0.6,
        fts_candidate_limit: int = 30,
    ):
        """Initialize the hybrid search engine.

        Args:
            db: A Database instance (db_sqlite.Database) with
                search_documents() and search_similar() methods.
            embed_client: An EmbeddingClient instance. If None or
                unavailable, search falls back to FTS5-only.
            vector_weight: Weight of vector score in fusion (0.0–1.0).
                Final score = (1 - w) * fts_score_norm + w * vector_score.
            fts_candidate_limit: Max FTS results to retrieve as candidates.
        """
        self.db = db
        self.embed_client = embed_client
        self.vector_weight = max(0.0, min(1.0, vector_weight))
        self.fts_weight = 1.0 - self.vector_weight
        self.fts_candidate_limit = fts_candidate_limit

    async def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        vector_weight: float | None = None,
    ) -> list[dict[str, Any]]:
        """Run hybrid search combining FTS5 and vector similarity.

        Returns a list of dicts with keys: doc_id, path, title, summary,
        body, snippet, rank (fused score), fts_score, vector_score.

        Args:
            query: Search query string.
            top_k: Maximum number of results to return.
            vector_weight: Optional per-query override of the vector
                weight (0.0–1.0).  When ``None`` (default), the weight
                configured at construction time (``self.vector_weight``)
                is used.  This allows callers to tune the FTS/vector
                balance per query without re-instantiating the engine.
        """
        if not query or not query.strip():
            return []

        # Resolve effective weights (per-query override or constructor default)
        eff_vw = self.vector_weight
        if vector_weight is not None:
            eff_vw = max(0.0, min(1.0, vector_weight))
        eff_fts_w = 1.0 - eff_vw

        # Stage 1: FTS5 keyword search (broad recall)
        fts_results = await self.db.search_documents(
            query, limit=self.fts_candidate_limit
        )

        # Check if vector search is available
        vector_available = (
            self.embed_client is not None
            and self.embed_client.is_available()
            and await self._has_any_embeddings()
        )

        if not vector_available:
            # Fallback: FTS-only
            return self._format_fts_results(fts_results, top_k)

        # Stage 2: Vector semantic search
        query_vec = await self.embed_client.embed(query)
        if not query_vec:
            return self._format_fts_results(fts_results, top_k)

        similar = await self.db.search_similar(query_vec, top_k=max(top_k, 20))

        # Build lookup for vector scores
        vector_scores: dict[int, float] = {
            s["doc_id"]: s["similarity"] for s in similar
        }

        # Stage 3: Score fusion
        # Collect all candidate doc_ids from both FTS and vector results
        fts_doc_ids = {r["id"] for r in fts_results}
        vec_doc_ids = set(vector_scores.keys())
        all_doc_ids = fts_doc_ids | vec_doc_ids

        # Normalize FTS scores (BM25 scores can vary wildly)
        fts_score_map: dict[int, float] = {}
        if fts_results:
            max_fts = max((r.get("rank", 0) for r in fts_results), default=1.0)
            min_fts = min((r.get("rank", 0) for r in fts_results), default=0.0)
            fts_range = max_fts - min_fts if max_fts > min_fts else 1.0
            for r in fts_results:
                # Normalize to [0, 1]
                normalized = (r.get("rank", 0) - min_fts) / fts_range
                fts_score_map[r["id"]] = normalized

        # Build fused results
        fused: list[dict[str, Any]] = []
        for doc_id in all_doc_ids:
            fts_score = fts_score_map.get(doc_id, 0.0)
            vec_score = vector_scores.get(doc_id, 0.0)
            fused_score = (
                eff_fts_w * fts_score + eff_vw * vec_score
            )
            fused.append({
                "doc_id": doc_id,
                "fts_score": fts_score,
                "vector_score": vec_score,
                "rank": fused_score,
            })

        # Sort by fused score descending
        fused.sort(key=lambda x: x["rank"], reverse=True)
        top = fused[:top_k]

        # Enrich with document metadata
        results: list[dict[str, Any]] = []
        for item in top:
            doc = await self.db.get_document(item["doc_id"])
            if doc is None:
                continue
            results.append({
                "doc_id": item["doc_id"],
                "path": doc.get("path", ""),
                "title": doc.get("title", ""),
                "summary": doc.get("summary"),
                "body": doc.get("body", ""),
                "snippet": (doc.get("body") or "")[:300],
                "rank": item["rank"],
                "fts_score": item["fts_score"],
                "vector_score": item["vector_score"],
            })

        return results

    async def _has_any_embeddings(self) -> bool:
        """Check if any documents have stored embeddings."""
        try:
            count = await self.db.get_document_count_with_embeddings()
            return count > 0
        except Exception:
            return False

    @staticmethod
    def _format_fts_results(
        fts_results: list[dict[str, Any]],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """Format FTS results as hybrid-style output (FTS-only fallback)."""
        output: list[dict[str, Any]] = []
        for r in fts_results[:top_k]:
            output.append({
                "doc_id": r.get("id", r.get("doc_id")),
                "path": r.get("path", ""),
                "title": r.get("title", ""),
                "summary": r.get("summary"),
                "body": r.get("body", ""),
                "snippet": (r.get("body") or "")[:300],
                "rank": r.get("rank", 0.0),
                "fts_score": r.get("rank", 0.0),
                "vector_score": 0.0,
            })
        return output

    # ── Chunk-level search ───────────────────────────────────────

    async def search_chunks(
        self,
        query: str,
        top_k: int = 5,
        *,
        vector_weight: float | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid search at the chunk level.

        Combines FTS5 keyword search on chunk content with vector
        similarity on chunk embeddings. Results are returned as
        individual chunks (not grouped by document), each enriched
        with the parent document's title and path.

        Returns a list of dicts with keys: doc_id, title, path,
        chunk_id, chunk_index, chunk_content, snippet, rank (fused
        score), fts_score, vector_score.

        Args:
            query: Search query string.
            top_k: Maximum number of chunks to return.
            vector_weight: Optional per-query override of the vector
                weight (0.0–1.0).  When ``None`` (default), the weight
                configured at construction time is used.
        """
        if not query or not query.strip():
            return []

        # Resolve effective weights (per-query override or constructor default)
        eff_vw = self.vector_weight
        if vector_weight is not None:
            eff_vw = max(0.0, min(1.0, vector_weight))
        eff_fts_w = 1.0 - eff_vw

        # Stage 1: FTS5 on chunk content
        fts_chunks = await self.db.search_chunks_fts(
            query, top_k=max(top_k, 20)
        )

        # Check if vector search is available at chunk level
        vector_available = (
            self.embed_client is not None
            and self.embed_client.is_available()
            and await self._has_any_chunk_embeddings()
        )

        if not vector_available:
            return await self._format_chunk_fts_results(fts_chunks, top_k)

        # Stage 2: Vector search on chunk embeddings
        query_vec = await self.embed_client.embed(query)
        if not query_vec:
            return await self._format_chunk_fts_results(fts_chunks, top_k)

        similar_chunks = await self.db.search_chunks_similar(
            query_vec, top_k=max(top_k, 20)
        )

        # Build score lookups
        vec_scores: dict[int, float] = {
            c["id"]: c["similarity"] for c in similar_chunks
        }

        # Normalize FTS scores
        fts_score_map: dict[int, float] = {}
        if fts_chunks:
            max_fts = max((c.get("rank", 0) for c in fts_chunks), default=1.0)
            min_fts = min((c.get("rank", 0) for c in fts_chunks), default=0.0)
            fts_range = max_fts - min_fts if max_fts > min_fts else 1.0
            for c in fts_chunks:
                normalized = (c.get("rank", 0) - min_fts) / fts_range
                fts_score_map[c["id"]] = normalized

        # Score fusion
        all_chunk_ids = set(fts_score_map.keys()) | set(vec_scores.keys())
        fused: list[dict[str, Any]] = []
        for chunk_id in all_chunk_ids:
            fts_score = fts_score_map.get(chunk_id, 0.0)
            vec_score = vec_scores.get(chunk_id, 0.0)
            fused_score = (
                eff_fts_w * fts_score + eff_vw * vec_score
            )
            fused.append({
                "chunk_id": chunk_id,
                "fts_score": fts_score,
                "vector_score": vec_score,
                "rank": fused_score,
            })

        fused.sort(key=lambda x: x["rank"], reverse=True)
        top = fused[:top_k]

        # Build a lookup for chunk metadata from both FTS and vector results
        chunk_lookup: dict[int, dict[str, Any]] = {}
        for c in fts_chunks:
            chunk_lookup[c["id"]] = c
        for c in similar_chunks:
            if c["id"] not in chunk_lookup:
                chunk_lookup[c["id"]] = c

        # Enrich with document metadata
        results: list[dict[str, Any]] = []
        for item in top:
            chunk = chunk_lookup.get(item["chunk_id"], {})
            doc_id = chunk.get("doc_id", 0)
            doc = await self.db.get_document(doc_id) if doc_id else None
            content = chunk.get("content", "")
            results.append({
                "doc_id": doc_id,
                "title": doc.get("title", "") if doc else "",
                "path": doc.get("path", "") if doc else "",
                "chunk_id": item["chunk_id"],
                "chunk_index": chunk.get("chunk_index", 0),
                "chunk_content": content,
                "snippet": content[:300],
                "rank": item["rank"],
                "fts_score": item["fts_score"],
                "vector_score": item["vector_score"],
            })

        return results

    async def _has_any_chunk_embeddings(self) -> bool:
        """Check if any chunks have stored embeddings."""
        try:
            count = await self.db.get_chunk_count_with_embeddings()
            return count > 0
        except Exception:
            return False

    @staticmethod
    async def _format_chunk_fts_results(
        fts_chunks: list[dict[str, Any]],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """Format chunk FTS results as chunk-level output (FTS-only fallback).

        This is a static method but needs to be async because it's called
        from an async context and may be overridden.
        """
        output: list[dict[str, Any]] = []
        for c in fts_chunks[:top_k]:
            content = c.get("content", "")
            output.append({
                "doc_id": c.get("doc_id", 0),
                "title": "",
                "path": "",
                "chunk_id": c.get("id", 0),
                "chunk_index": c.get("chunk_index", 0),
                "chunk_content": content,
                "snippet": content[:300],
                "rank": c.get("rank", 0.0),
                "fts_score": c.get("rank", 0.0),
                "vector_score": 0.0,
            })
        return output

    async def index_document_embedding(
        self,
        doc_id: int,
        title: str = "",
        summary: str = "",
        body: str = "",
    ) -> None:
        """Generate and store embeddings for a document.

        Chunks the document body and generates per-chunk embeddings for
        granular retrieval. Also stores a document-level embedding (the
        mean of chunk embeddings) for backward-compatible document search.

        Skips silently if no embedding provider is available.
        """
        if self.embed_client is None or not self.embed_client.is_available():
            return

        # Chunk the document body
        chunker = TextChunker()
        chunks = chunker.chunk(body or "")

        if not chunks:
            # Fall back to whole-document embedding (no body to chunk)
            text = self.embed_client.build_embedding_text(title, summary, body)
            if not text:
                return
            vec = await self.embed_client.embed(text)
            if vec:
                await self.db.save_embedding(doc_id, vec)
            return

        # Save chunks to the database (without embeddings yet)
        await self.db.save_chunks(doc_id, chunks)

        # Generate per-chunk embeddings
        chunk_texts = [c["text"] for c in chunks]
        chunk_embeddings = await self.embed_client.embed_batch(chunk_texts)

        # Save each chunk's embedding
        saved_chunks = await self.db.get_chunks(doc_id)
        for i, chunk_row in enumerate(saved_chunks):
            if i < len(chunk_embeddings) and chunk_embeddings[i]:
                await self.db.save_chunk_embedding(
                    chunk_row["id"], chunk_embeddings[i]
                )

        # Document-level embedding = mean of chunk embeddings
        valid_vecs = [v for v in chunk_embeddings if v]
        if valid_vecs:
            dim = len(valid_vecs[0])
            mean_vec = [
                sum(v[j] for v in valid_vecs) / len(valid_vecs)
                for j in range(dim)
            ]
            await self.db.save_embedding(doc_id, mean_vec)
