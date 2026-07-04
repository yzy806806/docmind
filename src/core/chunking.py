"""Document chunking for improved search and RAG retrieval.

Splits document text into semantically meaningful chunks so that:
  - Search returns the most relevant passage, not the entire document.
  - LLM context in chat includes only the pertinent chunk (fewer tokens).
  - Vector embeddings are per-chunk, giving finer-grained semantic matching.

Chunking strategies (applied in priority order):
  1. Paragraph-based — split on double newlines, merge small paragraphs.
  2. Sentence-based fallback — split on . ! ? for paragraphs exceeding
     ``max_chunk_size``.
  3. Sliding window — for very long unbroken text with no sentence
     boundaries, fall back to a character-level sliding window with overlap.

Each chunk is a dict with keys:
  - text: the chunk content
  - start_char: character offset in the original document
  - end_char: exclusive end offset
  - chunk_index: sequential index (0-based)
  - token_count: rough estimate (chars / 4)
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .config import ChunkingConfig


# Rough estimate: ~4 characters per token for English text.
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    """Estimate token count from character count (~4 chars/token)."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


class TextChunker:
    """Split document text into chunks for granular search and retrieval.

    Usage::

        chunker = TextChunker()  # uses defaults from ChunkingConfig
        chunks = chunker.chunk(long_document_text)
        for c in chunks:
            print(c["chunk_index"], c["start_char"], c["text"][:50])

    The chunker is stateless and thread-safe after construction.
    """

    # Regex for splitting on sentence boundaries (. ! ? followed by space/end)
    _SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

    def __init__(self, config: Optional[ChunkingConfig] = None):
        self._config = config or ChunkingConfig()

    @property
    def max_chunk_size(self) -> int:
        return self._config.chunk_size

    @property
    def overlap(self) -> int:
        return self._config.chunk_overlap

    @property
    def min_chunk_size(self) -> int:
        return self._config.min_chunk_size

    def chunk(
        self,
        text: str,
        max_chunk_size: Optional[int] = None,
        overlap: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Chunk text into a list of chunk dicts.

        Args:
            text: The document text to chunk.
            max_chunk_size: Override config chunk_size for this call.
            overlap: Override config chunk_overlap for this call.

        Returns:
            List of dicts with keys: text, start_char, end_char,
            chunk_index, token_count. Returns an empty list for empty
            or whitespace-only input.
        """
        if not text or not text.strip():
            return []

        max_sz = max_chunk_size or self.max_chunk_size
        ov = overlap if overlap is not None else self.overlap

        # Guard against nonsensical parameters
        if max_sz <= 0:
            max_sz = self._config.chunk_size
        if ov < 0:
            ov = 0
        if ov >= max_sz:
            ov = max(0, max_sz // 4)

        raw_chunks = self._split_into_raw_chunks(text, max_sz)

        # Apply overlap and build final chunk dicts with position metadata
        return self._build_chunks_with_metadata(text, raw_chunks, max_sz, ov)

    # ── Internal splitting strategies ────────────────────────────

    def _split_into_raw_chunks(
        self, text: str, max_sz: int
    ) -> list[tuple[str, int, int]]:
        """Split text into raw (text, start, end) segments.

        Tries paragraph-based splitting first, then sentence-based
        fallback for oversized paragraphs, and finally a sliding
        window for unbreakable text.
        """
        # Step 1: Split on paragraph boundaries (double newlines)
        paragraphs = self._split_paragraphs(text)

        raw: list[tuple[str, int, int]] = []
        for para_text, para_start, para_end in paragraphs:
            if len(para_text) <= max_sz:
                raw.append((para_text, para_start, para_end))
            else:
                # Step 2: Paragraph is too big — split by sentences
                sentences = self._split_sentences(para_text, para_start)
                if len(sentences) > 1:
                    # Multiple sentences — merge them respecting max_sz
                    merged = self._merge_segments(
                        sentences, max_sz, self.min_chunk_size
                    )
                    # Check if any merged segment is still > max_sz
                    final: list[tuple[str, int, int]] = []
                    for seg_text, seg_start, seg_end in merged:
                        if len(seg_text) > max_sz:
                            # Single sentence too big — sliding window
                            final.extend(
                                self._sliding_window(seg_text, seg_start, max_sz)
                            )
                        else:
                            final.append((seg_text, seg_start, seg_end))
                    raw.extend(final)
                else:
                    # No sentence boundaries (or single sentence) — sliding window
                    raw.extend(
                        self._sliding_window(para_text, para_start, max_sz)
                    )

        # Final pass: merge tiny chunks that are below min_chunk_size
        # (unless they're the only content)
        return self._merge_small(raw, max_sz, self.min_chunk_size)

    def _split_paragraphs(
        self, text: str
    ) -> list[tuple[str, int, int]]:
        """Split text on double newlines, preserving character offsets.

        Single newlines within a paragraph are preserved (they may be
        line breaks within the same logical paragraph).
        """
        result: list[tuple[str, int, int]] = []
        # Split on 2+ consecutive newlines (with optional whitespace between)
        pattern = re.compile(r"\n\s*\n")
        pos = 0
        for match in pattern.finditer(text):
            end = match.start()
            para = text[pos:end]
            if para.strip():
                result.append((para, pos, end))
            pos = match.end()
        # Last paragraph
        if pos < len(text):
            para = text[pos:]
            if para.strip():
                result.append((para, pos, len(text)))
        return result

    def _split_sentences(
        self, text: str, base_offset: int
    ) -> list[tuple[str, int, int]]:
        """Split text on sentence boundaries (. ! ? followed by whitespace).

        Returns segments with absolute character offsets.
        """
        result: list[tuple[str, int, int]] = []
        pos = 0
        for match in self._SENTENCE_SPLIT_RE.finditer(text):
            end = match.start()
            sentence = text[pos:end]
            if sentence.strip():
                result.append((sentence, base_offset + pos, base_offset + end))
            pos = match.end()
        # Last sentence
        if pos < len(text):
            sentence = text[pos:]
            if sentence.strip():
                result.append(
                    (sentence, base_offset + pos, base_offset + len(text))
                )
        return result

    def _merge_segments(
        self,
        segments: list[tuple[str, int, int]],
        max_sz: int,
        min_sz: int,
    ) -> list[tuple[str, int, int]]:
        """Merge adjacent small segments until they approach max_chunk_size.

        Segments that are already > max_sz are kept as-is (they'll be
        handled by the sliding window if needed — but since we got here
        via sentence splitting, individual sentences should rarely
        exceed max_sz).
        """
        if not segments:
            return []

        merged: list[tuple[str, int, int]] = []
        buf_text = ""
        buf_start = -1
        buf_end = -1

        for seg_text, seg_start, seg_end in segments:
            if buf_text and len(buf_text) + 1 + len(seg_text) > max_sz:
                # Flush buffer
                merged.append((buf_text, buf_start, buf_end))
                buf_text = ""
                buf_start = -1
                buf_end = -1

            if not buf_text:
                buf_text = seg_text
                buf_start = seg_start
                buf_end = seg_end
            else:
                buf_text += "\n" + seg_text
                buf_end = seg_end

        if buf_text:
            merged.append((buf_text, buf_start, buf_end))

        return merged

    def _merge_small(
        self,
        raw: list[tuple[str, int, int]],
        max_sz: int,
        min_sz: int,
    ) -> list[tuple[str, int, int]]:
        """Merge consecutive chunks smaller than min_sz into neighbors.

        Only merges when:
        - A chunk is smaller than min_sz AND
        - Merging with the neighbor won't exceed max_sz

        This prevents creating many tiny chunks while respecting the
        max chunk size limit. If merging isn't possible (would exceed
        max_sz), the small chunk is kept as-is.
        """
        if len(raw) <= 1:
            return raw

        result: list[tuple[str, int, int]] = []
        for seg in raw:
            seg_text, seg_start, seg_end = seg
            # Try to merge small segments into the previous one
            if (
                result
                and len(seg_text) < min_sz
                and len(result[-1][0]) + 1 + len(seg_text) <= max_sz
            ):
                prev_text, prev_start, _ = result[-1]
                result[-1] = (
                    prev_text + "\n" + seg_text,
                    prev_start,
                    seg_end,
                )
            elif (
                result
                and len(result[-1][0]) < min_sz
                and len(result[-1][0]) + 1 + len(seg_text) <= max_sz
            ):
                # Previous was small — merge current into it
                prev_text, prev_start, _ = result[-1]
                result[-1] = (
                    prev_text + "\n" + seg_text,
                    prev_start,
                    seg_end,
                )
            else:
                result.append(seg)

        return result

    def _sliding_window(
        self, text: str, base_offset: int, max_sz: int
    ) -> list[tuple[str, int, int]]:
        """Character-level sliding window for unbreakable text.

        Overlap is max_sz // 4 by default for the sliding window.
        """
        ov = max_sz // 4
        step = max_sz - ov
        if step <= 0:
            step = max_sz

        result: list[tuple[str, int, int]] = []
        pos = 0
        while pos < len(text):
            end = min(pos + max_sz, len(text))
            chunk_text = text[pos:end]
            result.append(
                (chunk_text, base_offset + pos, base_offset + end)
            )
            if end >= len(text):
                break
            pos += step
        return result

    # ── Metadata building ─────────────────────────────────────────

    def _build_chunks_with_metadata(
        self,
        original_text: str,
        raw_chunks: list[tuple[str, int, int]],
        max_sz: int,
        ov: int,
    ) -> list[dict[str, Any]]:
        """Apply overlap between chunks and build final dicts with metadata.

        Overlap is achieved by extending each chunk's end boundary into
        the next chunk's start by ``ov`` characters (when possible).
        """
        if not raw_chunks:
            return []

        chunks: list[dict[str, Any]] = []
        for i, (text, start, end) in enumerate(raw_chunks):
            # Apply overlap: extend the end of this chunk into the next
            # chunk's territory by ``ov`` chars (if there is a next chunk).
            if ov > 0 and i < len(raw_chunks) - 1:
                next_start = raw_chunks[i + 1][1]
                extended_end = min(next_start + ov, len(original_text))
                # Only extend if it actually adds content from the next chunk
                if extended_end > end:
                    chunk_text = original_text[start:extended_end]
                    chunk_end = extended_end
                else:
                    chunk_text = text
                    chunk_end = end
            else:
                chunk_text = text
                chunk_end = end

            chunks.append({
                "text": chunk_text,
                "start_char": start,
                "end_char": chunk_end,
                "chunk_index": i,
                "token_count": _estimate_tokens(chunk_text),
            })

        return chunks


# ── Convenience function ─────────────────────────────────────────


def chunk_text(
    text: str,
    max_chunk_size: int = 500,
    overlap: int = 50,
    min_chunk_size: int = 100,
) -> list[dict[str, Any]]:
    """Chunk text using default configuration.

    Convenience wrapper around TextChunker for one-off use.

    Args:
        text: Document text to chunk.
        max_chunk_size: Maximum characters per chunk.
        overlap: Overlap characters between adjacent chunks.
        min_chunk_size: Minimum chunk size (smaller chunks are merged).

    Returns:
        List of chunk dicts (see TextChunker.chunk).
    """
    cfg = ChunkingConfig(
        chunk_size=max_chunk_size,
        chunk_overlap=overlap,
        min_chunk_size=min_chunk_size,
    )
    return TextChunker(cfg).chunk(text)
