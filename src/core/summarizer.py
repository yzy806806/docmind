"""LLM-powered document summarization with map-reduce chunking, retries, and extractive fallback."""

import hashlib
import re
import time
from typing import Optional


# ── ChunkSummarizer ────────────────────────────────────────────────


class ChunkSummarizer:
    """Map-reduce summarizer that splits long documents into overlapping chunks,
    summarizes each chunk individually, and combines the results.

    Handles retries with idempotent chunk IDs and can produce partial
    summaries when only a fraction of chunks succeed.
    """

    def __init__(
        self,
        llm_client,
        chunk_size: int = 4000,
        chunk_overlap: int = 200,
        min_completion_ratio: float = 0.6,
        max_tokens: int = 8000,
    ):
        self.llm = llm_client
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.min_completion_ratio = min_completion_ratio
        self.max_tokens = max_tokens

    # ── public API ────────────────────────────────────────────────

    def summarize(
        self,
        title: str,
        body: str,
        max_input_chars: int | None = None,
        *,
        include_citations: bool = False,
    ) -> Optional[str]:
        """Summarize a document, using map-reduce for long bodies.

        Returns ``None`` when no LLM client is available or when fewer than
        ``min_completion_ratio`` chunks succeed.
        """
        if not self.llm:
            return None

        if not body or not body.strip():
            return None

        limit = max_input_chars if max_input_chars is not None else self.chunk_size

        if len(body) <= limit:
            return self._single_pass(title, body, limit)

        # ── map-reduce path ─────────────────────────────────────
        chunks = self._chunk_text(body)
        chunk_summaries: list[str] = []
        success_count = 0

        total = len(chunks)
        for idx, chunk in enumerate(chunks):
            chunk_id = _make_chunk_id(title, idx, len(body), chunk)
            summary = self._retry_summarize(title, chunk, chunk_id, idx, total)
            if summary is None:
                chunk_summaries.append(
                    f"[Summary unavailable for section {idx + 1}]"
                )
            else:
                chunk_summaries.append(summary)
                success_count += 1

        ratio = success_count / total if total > 0 else 0
        if ratio < self.min_completion_ratio:
            return None

        final = self._reduce_summaries(title, chunk_summaries)

        if ratio < 1.0:
            final = (
                f"(Partial summary — {success_count}/{total} sections processed)\n"
                f"{final}"
            )

        return final

    # ── internal helpers ──────────────────────────────────────────

    def _single_pass(self, title: str, body: str, limit: int) -> Optional[str]:
        """Summarize a short-enough document in one LLM call."""
        truncated = body[:limit]
        prompt = (
            f"请用中文总结以下文档，2-3句话，"
            f"重点说明：文档内容、关键主题和文档类型"
            f"（如合同、报告、发票、发言稿）。\n\n"
            f"标题：{title}\n\n"
            f"内容：\n{truncated}\n\n"
            f"摘要："
        )
        try:
            response = self.llm.chat(prompt, max_tokens=self.max_tokens)
            return response.strip() if response else None
        except Exception as e:
            print(f"[ChunkSummarizer] LLM call failed: {e}")
            return None

    def _chunk_text(self, text: str) -> list[str]:
        """Split *text* into chunks of ``chunk_size`` with ``chunk_overlap``.

        Prefers splitting on paragraph boundaries (``\\n\\n``), falling back
        to sentence boundaries, and finally to character-level slicing.
        """
        if len(text) <= self.chunk_size:
            return [text]

        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = start + self.chunk_size
            if end >= len(text):
                chunks.append(text[start:])
                break

            # Try paragraph boundary within the overlap window
            candidate = text[start:end]
            split_pos = _find_split_boundary(candidate, self.chunk_overlap)
            actual_end = start + split_pos

            chunks.append(text[start:actual_end])
            # Next chunk starts with overlap, but never before the current start
            start = max(start + 1, actual_end - self.chunk_overlap)

        return chunks

    def _summarize_chunk(
        self, title: str, chunk: str, chunk_idx: int, total_chunks: int
    ) -> str:
        """Summarize one chunk with positional context."""
        prompt = (
            f"你正在阅读标题为'{title}'的长文档的第 {chunk_idx + 1}/{total_chunks} 节。"
            f"请用1-2句话总结本节的关键内容。\n\n"
            f"内容（第 {chunk_idx + 1}/{total_chunks} 节）：\n"
            f"{chunk}\n\n"
            f"摘要："
        )
        response = self.llm.chat(prompt, max_tokens=self.max_tokens)
        return response.strip() if response else ""

    def _reduce_summaries(self, title: str, chunk_summaries: list[str]) -> str:
        """Combine per-chunk summaries into a coherent final summary."""
        joined = "\n---\n".join(
            f"Section {i + 1}: {s}"
            for i, s in enumerate(chunk_summaries)
        )
        prompt = (
            f"以下是标题为'{title}'的长文档各节摘要。"
            f"请将它们合并成一段连贯的中文总结，3-5句话，概括整篇文档。\n\n"
            f"{joined}\n\n"
            f"合并摘要："
        )
        response = self.llm.chat(prompt, max_tokens=self.max_tokens)
        return response.strip() if response else " ".join(chunk_summaries)

    def _retry_summarize(
        self,
        title: str,
        chunk: str,
        chunk_id: str,
        chunk_idx: int,
        total_chunks: int,
        max_retries: int = 3,
    ) -> Optional[str]:
        """Attempt to summarize a chunk, retrying with exponential backoff.

        Returns the summary string on success or ``None`` on total failure.
        """
        for attempt in range(max_retries):
            try:
                result = self._summarize_chunk(title, chunk, chunk_idx, total_chunks)
                if result:
                    return result
            except Exception as e:
                print(
                    f"[ChunkSummarizer] chunk {chunk_id} "
                    f"attempt {attempt + 1}/{max_retries} failed: {e}"
                )
            if attempt < max_retries - 1:
                delay = 2 ** attempt
                time.sleep(delay)
        return None


# ── ExtractiveFallbackSummarizer ───────────────────────────────────


class ExtractiveFallbackSummarizer:
    """LLM-free extractive summarization based on sentence scoring.

    Used as a fallback when the LLM is unavailable or all retries fail.
    """

    _SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+", re.UNICODE)

    def summarize(self, title: str, body: str) -> str:
        """Produce an extractive summary of *body* without an LLM.

        Algorithm:
        1. Split into sentences.
        2. Score each sentence by position, keyword density, and length.
        3. Return the top 5 sentences, or first 500 chars if too few.
        """
        if not body or not body.strip():
            return ""

        sentences = self._split_sentences(body)
        if not sentences:
            return body[:500].strip()

        if len(sentences) <= 5:
            return " ".join(sentences)

        title_words = set(title.lower().split())
        scores = [self._score_sentence(i, s, len(sentences), title_words) for i, s in enumerate(sentences)]

        # Pick top 5
        indexed = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        top_indices = sorted(i for i, _ in indexed[:5])
        summary = " ".join(sentences[i] for i in top_indices)

        return summary or body[:500].strip()

    def _split_sentences(self, text: str) -> list[str]:
        """Split text into sentences, filtering out empty strings and
        whitespace-only fragments."""
        parts = self._SENTENCE_RE.split(text)
        return [p.strip() for p in parts if p.strip()]

    def _score_sentence(
        self, idx: int, sentence: str, total: int, title_words: set[str]
    ) -> float:
        """Score a sentence for extractive summarization."""
        score = 0.0

        # ── Position bonus: first 20% of sentences ─────────────
        if total > 0 and idx < max(1, total * 0.2):
            score += 2.0

        # ── Keyword density ────────────────────────────────────
        words = sentence.lower().split()
        if words:
            keyword_hits = sum(1 for w in words if w in title_words)
            score += (keyword_hits / len(words)) * 3.0

            # Capitalized terms (proper nouns, acronyms)
            cap_count = sum(1 for w in words if w and w[0].isupper())
            score += (cap_count / len(words)) * 2.0

            # Presence of numbers
            num_count = sum(1 for w in words if any(ch.isdigit() for ch in w))
            score += (num_count / len(words)) * 1.5

        # ── Length penalty ─────────────────────────────────────
        length = len(sentence)
        if length < 20:
            score -= 2.0
        elif length > 500:
            score -= 1.0

        return score


# ── Summarizer (backward-compatible facade) ─────────────────────────


class Summarizer:
    """Backward-compatible document summarization facade.

    Wraps ``ChunkSummarizer`` for LLM-powered map-reduce summarization
    with TPM rate limiting, falling back to ``ExtractiveFallbackSummarizer``
    when the LLM is unavailable or fails.
    """

    def __init__(
        self,
        llm_client=None,
        tpm_limit: int = 5,
        chunk_size: int = 4000,
        max_tokens: int = 8000,
    ):
        self.llm = llm_client
        self.tpm_limit = tpm_limit
        self._last_call_time = 0.0

        self._chunk_summarizer = ChunkSummarizer(
            llm_client=self.llm,
            chunk_size=chunk_size,
            max_tokens=max_tokens,
        )
        self._extractive = ExtractiveFallbackSummarizer()

    # ── rate limiting ─────────────────────────────────────────────

    def _rate_limit(self):
        """Enforce TPM (tokens per minute) limit between LLM calls."""
        if self.tpm_limit <= 0:
            return
        min_interval = 60.0 / self.tpm_limit
        elapsed = time.monotonic() - self._last_call_time
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)

    def _mark_call(self):
        self._last_call_time = time.monotonic()

    # ── public API ────────────────────────────────────────────────

    def summarize(
        self,
        title: str,
        body: str,
        max_input_chars: int = 2000,
        *,
        include_citations: bool = False,
    ) -> Optional[str]:
        """Generate a concise summary, falling back to extractive on failure.

        Returns ``None`` only when the LLM path fails and the extractive
        fallback also produces nothing useful (empty body).
        """
        if not body or not body.strip():
            return None

        # Try LLM path first
        if self.llm:
            self._rate_limit()
            try:
                result = self._chunk_summarizer.summarize(
                    title, body, max_input_chars
                )
                self._mark_call()
                if result:
                    return result
            except Exception as e:
                print(f"[Summarizer] LLM summarization failed: {e}")
                self._mark_call()

        # ── Extractive fallback ──────────────────────────────────
        fallback = self._extractive.summarize(title, body)
        return fallback if fallback else None

    def batch_summarize(self, documents: list[dict], indexer) -> int:
        """Summarize a batch of documents, updating the indexer."""
        count = 0
        for doc in documents:
            summary = self.summarize(doc["title"], doc["body"])
            if summary:
                indexer.update_summary(doc["id"], summary)
                count += 1
        return count


# ── Helpers ────────────────────────────────────────────────────────


def _make_chunk_id(title: str, chunk_idx: int, body_len: int, chunk: str) -> str:
    """Generate a deterministic, idempotent chunk ID.

    ``sha256(title:chunk_idx:len(body):hash(chunk))`` truncated to 16 hex chars.
    """
    chunk_hash = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
    raw = f"{title}:{chunk_idx}:{body_len}:{chunk_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _find_split_boundary(candidate: str, overlap: int) -> int:
    """Find the best split point in *candidate* near its end.

    Prefers paragraph boundaries (double newline), then sentence endings,
    then a single newline. Returns an index relative to the start of
    *candidate*.
    """
    # Search window: last <overlap> chars (but at least 1 char)
    window_start = max(0, len(candidate) - overlap)
    search_in = candidate[window_start:]

    # 1. Paragraph boundary (\n\n)
    para_match = list(re.finditer(r"\n\n", search_in))
    if para_match:
        last = para_match[-1]
        return window_start + last.start() + 2

    # 2. Sentence boundary (.!? followed by space or newline)
    sent_match = list(re.finditer(r"[.!?]\s+", search_in))
    if sent_match:
        last = sent_match[-1]
        return window_start + last.start() + 1

    # 3. Single newline
    nl_match = list(re.finditer(r"\n", search_in))
    if nl_match:
        last = nl_match[-1]
        return window_start + last.start() + 1

    # 4. Fallback: just split exactly at chunk_size
    return len(candidate)
