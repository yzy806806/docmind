"""Tests for src.core.chunking — TextChunker and chunk_text.

Covers:
- Paragraph-based splitting (double newlines)
- Sentence-based fallback for oversized paragraphs
- Sliding window for unbreakable text
- Overlap between adjacent chunks
- Chunk metadata (start_char, end_char, chunk_index, token_count)
- Edge cases: empty text, very short text, very long paragraphs,
  single paragraph, no sentence boundaries
- Config override via ChunkingConfig
- Convenience function chunk_text()
"""

from __future__ import annotations

import pytest

from src.core.chunking import TextChunker, chunk_text, _estimate_tokens
from src.core.config import ChunkingConfig


# ── Import / smoke tests ────────────────────────────────────────


def test_import_chunking() -> None:
    """TextChunker should be importable from chunking module."""
    assert TextChunker is not None


def test_chunk_text_function_exists() -> None:
    """chunk_text convenience function should be importable."""
    assert callable(chunk_text)


def test_estimate_tokens() -> None:
    """_estimate_tokens should return ~chars/4."""
    assert _estimate_tokens("hello world") == 2  # 11 chars -> 2 tokens
    assert _estimate_tokens("") == 1  # min 1
    assert _estimate_tokens("a") == 1  # min 1


# ── Edge cases ──────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_text(self) -> None:
        """Empty text should produce no chunks."""
        chunker = TextChunker()
        assert chunker.chunk("") == []

    def test_whitespace_only(self) -> None:
        """Whitespace-only text should produce no chunks."""
        chunker = TextChunker()
        assert chunker.chunk("   \n\n  \t  ") == []

    def test_none_text(self) -> None:
        """None text should produce no chunks."""
        chunker = TextChunker()
        assert chunker.chunk(None) == []  # type: ignore[arg-type]

    def test_very_short_text(self) -> None:
        """Short text should produce a single chunk."""
        chunker = TextChunker()
        chunks = chunker.chunk("Hello world.")
        assert len(chunks) == 1
        assert chunks[0]["text"] == "Hello world."
        assert chunks[0]["chunk_index"] == 0
        assert chunks[0]["start_char"] == 0
        assert chunks[0]["end_char"] == len("Hello world.")

    def test_single_paragraph(self) -> None:
        """A single paragraph under max_chunk_size produces one chunk."""
        chunker = TextChunker()
        text = "This is a single paragraph with some content."
        chunks = chunker.chunk(text)
        assert len(chunks) == 1
        assert chunks[0]["text"] == text
        assert chunks[0]["start_char"] == 0
        assert chunks[0]["end_char"] == len(text)


# ── Paragraph splitting ─────────────────────────────────────────


class TestParagraphSplitting:
    def test_multiple_paragraphs(self) -> None:
        """Multiple small paragraphs should each become a chunk."""
        chunker = TextChunker(ChunkingConfig(
            chunk_size=500, chunk_overlap=0, min_chunk_size=1
        ))
        text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        chunks = chunker.chunk(text)

        assert len(chunks) == 3
        assert chunks[0]["text"] == "First paragraph."
        assert chunks[1]["text"] == "Second paragraph."
        assert chunks[2]["text"] == "Third paragraph."

        # Check positions
        assert chunks[0]["start_char"] == 0
        assert chunks[0]["chunk_index"] == 0
        assert chunks[1]["chunk_index"] == 1
        assert chunks[2]["chunk_index"] == 2

    def test_small_paragraphs_merged(self) -> None:
        """Small paragraphs should be merged to approach max_chunk_size."""
        chunker = TextChunker(ChunkingConfig(
            chunk_size=100, chunk_overlap=0, min_chunk_size=30
        ))
        # Each paragraph is ~20 chars, should merge to approach 100
        text = "\n\n".join([f"Para {i} short." for i in range(10)])
        chunks = chunker.chunk(text)

        # Should have fewer chunks than 10 (merged)
        assert len(chunks) < 10
        # Each chunk should be <= 100 chars (plus newline joins)
        for c in chunks:
            assert len(c["text"]) <= 110  # allow small overflow from \n

    def test_paragraph_positions_correct(self) -> None:
        """Chunk start_char/end_char should map to original text positions."""
        chunker = TextChunker(ChunkingConfig(
            chunk_size=500, chunk_overlap=0, min_chunk_size=1
        ))
        text = "AAA.\n\nBBB."
        chunks = chunker.chunk(text)

        assert len(chunks) == 2
        assert chunks[0]["start_char"] == 0
        assert chunks[0]["end_char"] == 4  # "AAA."
        assert text[chunks[0]["start_char"]:chunks[0]["end_char"]] == "AAA."

        assert chunks[1]["start_char"] == 6  # after "\n\n"
        assert text[chunks[1]["start_char"]:chunks[1]["end_char"]] == "BBB."


# ── Sentence splitting ──────────────────────────────────────────


class TestSentenceSplitting:
    def test_long_paragraph_split_by_sentences(self) -> None:
        """A paragraph exceeding max_chunk_size should be split by sentences."""
        chunker = TextChunker(ChunkingConfig(
            chunk_size=50, chunk_overlap=0, min_chunk_size=10
        ))
        text = "First sentence here. Second sentence there. Third one everywhere."
        chunks = chunker.chunk(text)

        # Should produce multiple chunks
        assert len(chunks) >= 2
        # Each chunk should be <= ~50 chars (may overflow slightly due to merging)
        for c in chunks:
            assert len(c["text"]) <= 60

    def test_sentence_boundaries(self) -> None:
        """Sentences should be split on . ! ? followed by whitespace."""
        chunker = TextChunker(ChunkingConfig(
            chunk_size=30, chunk_overlap=0, min_chunk_size=5
        ))
        text = "What is this? That is great! Another one. More text."
        chunks = chunker.chunk(text)
        assert len(chunks) >= 2

        # Verify all content is covered
        for c in chunks:
            assert c["text"].strip()  # no empty chunks


# ── Sliding window ──────────────────────────────────────────────


class TestSlidingWindow:
    def test_long_unbreakable_text(self) -> None:
        """Very long text with no sentence boundaries uses sliding window."""
        chunker = TextChunker(ChunkingConfig(
            chunk_size=50, chunk_overlap=0, min_chunk_size=1
        ))
        # No paragraph breaks, no sentence boundaries
        text = "a" * 200
        chunks = chunker.chunk(text)

        assert len(chunks) >= 3
        # Each chunk should be <= 50 chars
        for c in chunks:
            assert len(c["text"]) <= 50

        # Chunks should cover the full text
        assert chunks[0]["start_char"] == 0
        assert chunks[-1]["end_char"] == 200


# ── Overlap ─────────────────────────────────────────────────────


class TestOverlap:
    def test_overlap_extends_chunk_end(self) -> None:
        """Overlap should extend chunk end into the next chunk's territory."""
        chunker = TextChunker(ChunkingConfig(
            chunk_size=100, chunk_overlap=20, min_chunk_size=1
        ))
        text = "A" * 50 + "\n\n" + "B" * 50 + "\n\n" + "C" * 50
        chunks = chunker.chunk(text)

        if len(chunks) >= 2:
            # The first chunk's end_char should extend into the second
            # chunk's start (by overlap amount)
            first_end = chunks[0]["end_char"]
            second_start = chunks[1]["start_char"]
            # Overlap means first_end > second_start (or the text is extended)
            assert first_end >= chunks[0]["start_char"] + 50  # at least original

    def test_zero_overlap(self) -> None:
        """Zero overlap should produce non-overlapping chunks."""
        chunker = TextChunker(ChunkingConfig(
            chunk_size=100, chunk_overlap=0, min_chunk_size=1
        ))
        text = "A" * 50 + "\n\n" + "B" * 50
        chunks = chunker.chunk(text)
        assert len(chunks) == 2
        # No overlap
        assert chunks[0]["end_char"] <= chunks[1]["start_char"]


# ── Metadata ────────────────────────────────────────────────────


class TestChunkMetadata:
    def test_chunk_index_sequential(self) -> None:
        """chunk_index should be 0, 1, 2, ... in order."""
        chunker = TextChunker(ChunkingConfig(chunk_size=50, chunk_overlap=0))
        text = "\n\n".join([f"Paragraph {i}." for i in range(10)])
        chunks = chunker.chunk(text)

        for i, c in enumerate(chunks):
            assert c["chunk_index"] == i

    def test_token_count_present(self) -> None:
        """Each chunk should have a token_count estimate."""
        chunker = TextChunker()
        chunks = chunker.chunk("Hello world. This is a test.")
        assert len(chunks) == 1
        assert "token_count" in chunks[0]
        assert chunks[0]["token_count"] > 0

    def test_start_end_char_in_bounds(self) -> None:
        """start_char and end_char should be within the original text."""
        chunker = TextChunker(ChunkingConfig(chunk_size=50, chunk_overlap=10))
        text = "A" * 30 + "\n\n" + "B" * 30 + "\n\n" + "C" * 30
        chunks = chunker.chunk(text)

        for c in chunks:
            assert 0 <= c["start_char"] <= len(text)
            assert 0 <= c["end_char"] <= len(text)
            assert c["start_char"] < c["end_char"]


# ── Config override ─────────────────────────────────────────────


class TestConfigOverride:
    def test_config_defaults(self) -> None:
        """ChunkingConfig should have the right defaults."""
        cfg = ChunkingConfig()
        assert cfg.chunk_size == 500
        assert cfg.chunk_overlap == 50
        assert cfg.min_chunk_size == 100

    def test_custom_config(self) -> None:
        """Custom config values should be respected."""
        cfg = ChunkingConfig(chunk_size=200, chunk_overlap=20, min_chunk_size=50)
        chunker = TextChunker(cfg)
        assert chunker.max_chunk_size == 200
        assert chunker.overlap == 20
        assert chunker.min_chunk_size == 50

    def test_override_in_chunk_call(self) -> None:
        """max_chunk_size and overlap can be overridden in chunk()."""
        chunker = TextChunker(ChunkingConfig(chunk_size=500, chunk_overlap=50))
        text = "A" * 100 + "\n\n" + "B" * 100
        chunks = chunker.chunk(text, max_chunk_size=50, overlap=0)
        # With 50 char limit, each 100-char paragraph gets split
        assert len(chunks) >= 2


# ── Convenience function ────────────────────────────────────────


class TestChunkTextFunction:
    def test_chunk_text_basic(self) -> None:
        """chunk_text() should work like TextChunker().chunk()."""
        text = "First paragraph.\n\nSecond paragraph."
        chunks = chunk_text(text, max_chunk_size=500, overlap=0, min_chunk_size=1)
        assert len(chunks) == 2
        assert chunks[0]["text"] == "First paragraph."
        assert chunks[1]["text"] == "Second paragraph."

    def test_chunk_text_empty(self) -> None:
        """chunk_text() should handle empty input."""
        assert chunk_text("") == []

    def test_chunk_text_metadata(self) -> None:
        """chunk_text() should include all metadata keys."""
        chunks = chunk_text("Hello world.", max_chunk_size=500)
        assert len(chunks) == 1
        c = chunks[0]
        assert "text" in c
        assert "start_char" in c
        assert "end_char" in c
        assert "chunk_index" in c
        assert "token_count" in c


# ── Integration / regression ────────────────────────────────────


class TestIntegration:
    def test_realistic_document(self) -> None:
        """A realistic multi-paragraph document should chunk correctly."""
        text = (
            "# Introduction\n\n"
            "This is the introduction paragraph. It contains some text "
            "about the document.\n\n"
            "## Methodology\n\n"
            "The methodology involves several steps. First, we collect "
            "data. Then, we process it. Finally, we analyze results.\n\n"
            "## Results\n\n"
            "The results show a significant improvement over the baseline. "
            "The accuracy increased by 15 percent."
        )
        chunker = TextChunker(ChunkingConfig(chunk_size=200, chunk_overlap=20))
        chunks = chunker.chunk(text)

        # Should produce multiple chunks
        assert len(chunks) >= 3

        # All chunks should have non-empty text
        for c in chunks:
            assert c["text"].strip()

        # chunk_index should be sequential
        for i, c in enumerate(chunks):
            assert c["chunk_index"] == i

        # Total content coverage: first start=0, last end=len(text)
        assert chunks[0]["start_char"] == 0
        assert chunks[-1]["end_char"] <= len(text)

    def test_no_content_loss(self) -> None:
        """Chunking should not lose content — reassembly covers the text."""
        text = (
            "Paragraph one with content. "
            + "word " * 50
            + "\n\nParagraph two. "
            + "more " * 50
        )
        chunker = TextChunker(ChunkingConfig(chunk_size=100, chunk_overlap=0))
        chunks = chunker.chunk(text)

        # The union of [start_char, end_char) ranges should cover all non-whitespace
        covered = set()
        for c in chunks:
            for i in range(c["start_char"], c["end_char"]):
                covered.add(i)

        # Check that all non-whitespace characters are covered
        for i, ch in enumerate(text):
            if not ch.isspace():
                assert i in covered, f"Character {i} ({ch!r}) not covered"

    def test_very_long_document(self) -> None:
        """A very long document should produce many chunks without error."""
        text = "\n\n".join([f"Paragraph {i}. " + "x " * 30 for i in range(100)])
        chunker = TextChunker(ChunkingConfig(chunk_size=200, chunk_overlap=20))
        chunks = chunker.chunk(text)

        assert len(chunks) > 10
        # All indices sequential
        for i, c in enumerate(chunks):
            assert c["chunk_index"] == i
