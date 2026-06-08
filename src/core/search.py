"""Search engine — multi-stage LLM-powered document search."""
from typing import Optional
from .indexer import Indexer


class SearchEngine:
    """
    Multi-stage search:
    1. FTS5 keyword search (fast, broad)
    2. LLM summary matching (precise, narrow)
    3. Return relevant document snippets
    """

    def __init__(self, indexer: Indexer, llm_client=None):
        self.indexer = indexer
        self.llm = llm_client

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """
        Full search pipeline:
        1. FTS5 returns top-30 candidates
        2. LLM picks top-k most relevant from summaries
        3. Return full documents for those top-k
        """
        # Stage 1: FTS5 broad search
        fts_results = self.indexer.search_fts(query, limit=30)

        if not fts_results:
            return []

        # Stage 2: LLM selection (if LLM configured)
        if self.llm and len(fts_results) > top_k:
            selected = self._llm_select(query, fts_results, top_k)
        else:
            selected = fts_results[:top_k]

        # Stage 3: Return full documents (body will be yielded by caller)
        return selected

    def _llm_select(self, query: str, candidates: list[dict], top_k: int) -> list[dict]:
        """Use LLM to pick the most relevant documents based on summaries."""
        # Build a compact prompt with document summaries
        lines = []
        for i, doc in enumerate(candidates):
            summary = doc.get("summary") or doc.get("raw_preview", "")[:200]
            lines.append(f"[{i}] {doc['title']} — {summary}")

        prompt = f"""You are a document search assistant. Given a user query and a list of document summaries, pick the {top_k} most relevant documents.

User query: {query}

Documents:
{chr(10).join(lines)}

Return ONLY the document numbers, comma-separated (e.g., "0,3,7,12")."""

        try:
            response = self.llm.chat(prompt, max_tokens=50)
            indices = [int(x.strip()) for x in response.split(",") if x.strip().isdigit()]
            return [candidates[i] for i in indices if 0 <= i < len(candidates)]
        except Exception:
            # Fallback: return first top_k
            return candidates[:top_k]

    def search_simple(self, query: str, limit: int = 5) -> list[dict]:
        """Simple FTS5-only search, no LLM."""
        return self.indexer.search_fts(query, limit=limit)
