"""Tests for hybrid search (FTS5 + vector score fusion).

Covers:
- HybridSearchEngine with no embeddings (FTS5-only fallback)
- HybridSearchEngine with embeddings (score fusion)
- Score normalization and weighting
- index_document_embedding integration
- Empty query handling
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    """Provide a temporary database file path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "test_hybrid.db")


@pytest.fixture
async def db(tmp_db_path: str):
    """Create a connected SQLite Database instance."""
    from src.core.db_sqlite import Database
    database = Database(db_path=tmp_db_path)
    await database.connect()
    yield database
    await database.disconnect()


@pytest.fixture
def mock_embed_client():
    """Create a mock EmbeddingClient that returns fake vectors."""
    client = MagicMock()
    client.is_available = MagicMock(return_value=True)
    # Return a simple vector based on text hash for deterministic testing
    def fake_embed(text):
        if not text:
            return []
        # Simple deterministic vector: 3-dim based on text
        return [float(len(text) % 10), float(hash(text) % 100) / 100.0, 0.5]
    client.embed = AsyncMock(side_effect=fake_embed)
    client.embed_batch = AsyncMock(side_effect=lambda texts: [fake_embed(t) for t in texts])
    client.build_embedding_text = MagicMock(
        side_effect=lambda title="", summary="", body="": " ".join(
            filter(None, [title, summary, body[:2000] if body else ""])
        )
    )
    return client


@pytest.fixture
def unavailable_embed_client():
    """Create a mock EmbeddingClient that is not available (no provider)."""
    client = MagicMock()
    client.is_available = MagicMock(return_value=False)
    client.embed = AsyncMock(return_value=[])
    client.build_embedding_text = MagicMock(return_value="")
    return client


# ── Import / smoke tests ────────────────────────────────────────


def test_import_hybrid_search() -> None:
    """HybridSearchEngine should be importable from search module."""
    from src.core.search import HybridSearchEngine
    assert HybridSearchEngine is not None


def test_hybrid_search_engine_init() -> None:
    """HybridSearchEngine should initialize with default params."""
    from src.core.search import HybridSearchEngine
    engine = HybridSearchEngine(db=MagicMock())
    assert engine.vector_weight == 0.6
    assert engine.fts_weight == 0.4
    assert engine.fts_candidate_limit == 30


def test_hybrid_search_engine_weight_clamping() -> None:
    """vector_weight should be clamped to [0, 1]."""
    from src.core.search import HybridSearchEngine
    engine_high = HybridSearchEngine(db=MagicMock(), vector_weight=1.5)
    assert engine_high.vector_weight == 1.0
    assert engine_high.fts_weight == 0.0

    engine_low = HybridSearchEngine(db=MagicMock(), vector_weight=-0.5)
    assert engine_low.vector_weight == 0.0
    assert engine_low.fts_weight == 1.0


# ── FTS-only fallback tests ─────────────────────────────────────


class TestFTSFallback:
    @pytest.mark.asyncio
    async def test_search_empty_query(self, db, mock_embed_client) -> None:
        """Empty query should return empty results."""
        from src.core.search import HybridSearchEngine
        engine = HybridSearchEngine(db=db, embed_client=mock_embed_client)
        results = await engine.search("", top_k=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_search_whitespace_query(self, db, mock_embed_client) -> None:
        """Whitespace-only query should return empty results."""
        from src.core.search import HybridSearchEngine
        engine = HybridSearchEngine(db=db, embed_client=mock_embed_client)
        results = await engine.search("   ", top_k=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_search_no_embeddings_fallback(
        self, db, unavailable_embed_client
    ) -> None:
        """Without embeddings, search should fall back to FTS-only."""
        from src.core.search import HybridSearchEngine

        # Add a document
        await db.save_document(
            path="/docs/ml.txt",
            source_type="local",
            source_name="test",
            title="Machine Learning Guide",
            ext=".txt",
            mime_type="text/plain",
            body="Machine learning is a subset of artificial intelligence.",
        )

        engine = HybridSearchEngine(db=db, embed_client=unavailable_embed_client)
        results = await engine.search("machine learning", top_k=5)

        assert len(results) >= 1
        assert results[0]["title"] == "Machine Learning Guide"
        assert results[0]["vector_score"] == 0.0
        assert "fts_score" in results[0]

    @pytest.mark.asyncio
    async def test_search_no_results(self, db, mock_embed_client) -> None:
        """Search with no matching documents should return empty list."""
        from src.core.search import HybridSearchEngine

        # Add a document that won't match
        await db.save_document(
            path="/docs/cooking.txt",
            source_type="local",
            source_name="test",
            title="Cooking Guide",
            ext=".txt",
            mime_type="text/plain",
            body="How to make pasta from scratch.",
        )

        engine = HybridSearchEngine(db=db, embed_client=mock_embed_client)
        results = await engine.search("quantum physics", top_k=5)
        assert results == []


# ── Hybrid search with embeddings ───────────────────────────────


class TestHybridSearch:
    @pytest.mark.asyncio
    async def test_search_with_embeddings(
        self, db, mock_embed_client
    ) -> None:
        """Search with embeddings should return fused results."""
        from src.core.search import HybridSearchEngine

        # Add documents
        doc_id1 = await db.save_document(
            path="/docs/ml.txt",
            source_type="local",
            source_name="test",
            title="Machine Learning",
            ext=".txt",
            mime_type="text/plain",
            body="Machine learning models and training.",
        )
        doc_id2 = await db.save_document(
            path="/docs/cooking.txt",
            source_type="local",
            source_name="test",
            title="Cooking Recipes",
            ext=".txt",
            mime_type="text/plain",
            body="Pasta and sauce recipes.",
        )

        # Manually store embeddings
        await db.save_embedding(doc_id1, [1.0, 0.5, 0.3])
        await db.save_embedding(doc_id2, [0.1, 0.2, 0.9])

        engine = HybridSearchEngine(db=db, embed_client=mock_embed_client)
        results = await engine.search("machine learning", top_k=5)

        assert len(results) >= 1
        # Results should have fused scores
        for r in results:
            assert "rank" in r
            assert "fts_score" in r
            assert "vector_score" in r
            assert r["rank"] >= 0.0

    @pytest.mark.asyncio
    async def test_search_top_k_limit(self, db, mock_embed_client) -> None:
        """Search should respect top_k limit."""
        from src.core.search import HybridSearchEngine

        # Add multiple documents
        for i in range(10):
            doc_id = await db.save_document(
                path=f"/docs/doc_{i}.txt",
                source_type="local",
                source_name="test",
                title=f"Document {i}",
                ext=".txt",
                mime_type="text/plain",
                body=f"Content about topic {i}.",
            )
            await db.save_embedding(doc_id, [float(i), 0.5, 0.3])

        engine = HybridSearchEngine(db=db, embed_client=mock_embed_client)
        results = await engine.search("topic", top_k=3)
        assert len(results) <= 3

    @pytest.mark.asyncio
    async def test_search_vector_only_results(
        self, db, mock_embed_client
    ) -> None:
        """Documents found by vector but not FTS should appear in results."""
        from src.core.search import HybridSearchEngine

        # Add a document that won't match FTS for "quantum"
        doc_id = await db.save_document(
            path="/docs/physics.txt",
            source_type="local",
            source_name="test",
            title="Physics Notes",
            ext=".txt",
            mime_type="text/plain",
            body="Quantum mechanics and particle physics.",
        )
        # Store an embedding that's very similar to query embedding
        await db.save_embedding(doc_id, [5.0, 0.8, 0.5])

        # mock_embed_client returns [len(text)%10, hash%100/100, 0.5]
        # For "quantum" (7 chars): [7.0, X, 0.5]
        # Our stored embedding [5.0, 0.8, 0.5] won't match exactly but will
        # have non-zero similarity

        engine = HybridSearchEngine(db=db, embed_client=mock_embed_client)
        results = await engine.search("quantum", top_k=5)

        # Should find the document via vector search even if FTS matches
        # (FTS will match "quantum" in body, so this tests both paths)
        assert len(results) >= 1
        doc_ids = [r["doc_id"] for r in results]
        assert doc_id in doc_ids

    @pytest.mark.asyncio
    async def test_search_fusion_weighting(
        self, db, mock_embed_client
    ) -> None:
        """Fused score should be weighted combination of FTS and vector."""
        from src.core.search import HybridSearchEngine

        doc_id = await db.save_document(
            path="/docs/test.txt",
            source_type="local",
            source_name="test",
            title="Test Doc",
            ext=".txt",
            mime_type="text/plain",
            body="Machine learning test document.",
        )
        await db.save_embedding(doc_id, [1.0, 0.5, 0.3])

        # With vector_weight=0.0, rank should equal fts_score (vector ignored in fusion)
        engine_fts = HybridSearchEngine(
            db=db, embed_client=mock_embed_client, vector_weight=0.0
        )
        results_fts = await engine_fts.search("machine learning", top_k=1)
        if results_fts:
            r = results_fts[0]
            assert abs(r["rank"] - r["fts_score"]) < 1e-5

        # With vector_weight=1.0, rank should equal vector_score (fts ignored in fusion)
        engine_vec = HybridSearchEngine(
            db=db, embed_client=mock_embed_client, vector_weight=1.0
        )
        results_vec = await engine_vec.search("machine learning", top_k=1)
        if results_vec:
            r = results_vec[0]
            assert abs(r["rank"] - r["vector_score"]) < 1e-5

        # With balanced weight 0.5, rank should be average of fts and vector
        engine_bal = HybridSearchEngine(
            db=db, embed_client=mock_embed_client, vector_weight=0.5
        )
        results_bal = await engine_bal.search("machine learning", top_k=1)
        if results_bal:
            r = results_bal[0]
            expected = 0.5 * r["fts_score"] + 0.5 * r["vector_score"]
            assert abs(r["rank"] - expected) < 1e-5

    @pytest.mark.asyncio
    async def test_search_per_query_vector_weight_override(
        self, db, mock_embed_client
    ) -> None:
        """Per-query vector_weight should override constructor default."""
        from src.core.search import HybridSearchEngine

        doc_id = await db.save_document(
            path="/docs/override_test.txt",
            source_type="local",
            source_name="test",
            title="Override Test",
            ext=".txt",
            mime_type="text/plain",
            body="Machine learning test document for weight override.",
        )
        await db.save_embedding(doc_id, [1.0, 0.5, 0.3])

        # Engine constructed with vector_weight=0.5
        engine = HybridSearchEngine(
            db=db, embed_client=mock_embed_client, vector_weight=0.5
        )

        # Override to 0.0 → rank should equal fts_score
        results_fts = await engine.search(
            "machine learning", top_k=1, vector_weight=0.0
        )
        assert len(results_fts) >= 1
        r = results_fts[0]
        assert abs(r["rank"] - r["fts_score"]) < 1e-5

        # Override to 1.0 → rank should equal vector_score
        results_vec = await engine.search(
            "machine learning", top_k=1, vector_weight=1.0
        )
        assert len(results_vec) >= 1
        r = results_vec[0]
        assert abs(r["rank"] - r["vector_score"]) < 1e-5

        # Override to 0.3 → rank should be 0.7*fts + 0.3*vec
        results_custom = await engine.search(
            "machine learning", top_k=1, vector_weight=0.3
        )
        assert len(results_custom) >= 1
        r = results_custom[0]
        expected = 0.7 * r["fts_score"] + 0.3 * r["vector_score"]
        assert abs(r["rank"] - expected) < 1e-5

    @pytest.mark.asyncio
    async def test_search_per_query_vector_weight_none_uses_default(
        self, db, mock_embed_client
    ) -> None:
        """vector_weight=None should use the constructor default."""
        from src.core.search import HybridSearchEngine

        doc_id = await db.save_document(
            path="/docs/default_test.txt",
            source_type="local",
            source_name="test",
            title="Default Test",
            ext=".txt",
            mime_type="text/plain",
            body="Machine learning test for default weight.",
        )
        await db.save_embedding(doc_id, [1.0, 0.5, 0.3])

        # Constructor vector_weight=0.8
        vw = 0.8
        engine = HybridSearchEngine(
            db=db, embed_client=mock_embed_client, vector_weight=vw
        )

        # Calling with vector_weight=None (or omitted) should use 0.8
        results = await engine.search("machine learning", top_k=1)
        assert len(results) >= 1
        r = results[0]
        expected = (1.0 - vw) * r["fts_score"] + vw * r["vector_score"]
        assert abs(r["rank"] - expected) < 1e-5

    @pytest.mark.asyncio
    async def test_search_per_query_vector_weight_clamped(
        self, db, mock_embed_client
    ) -> None:
        """Out-of-range vector_weight should be clamped to [0, 1]."""
        from src.core.search import HybridSearchEngine

        doc_id = await db.save_document(
            path="/docs/clamp_test.txt",
            source_type="local",
            source_name="test",
            title="Clamp Test",
            ext=".txt",
            mime_type="text/plain",
            body="Machine learning clamp test document.",
        )
        await db.save_embedding(doc_id, [1.0, 0.5, 0.3])

        engine = HybridSearchEngine(
            db=db, embed_client=mock_embed_client, vector_weight=0.5
        )

        # 1.5 → clamped to 1.0, so rank == vector_score
        results_high = await engine.search(
            "machine learning", top_k=1, vector_weight=1.5
        )
        assert len(results_high) >= 1
        r = results_high[0]
        assert abs(r["rank"] - r["vector_score"]) < 1e-5

        # -0.5 → clamped to 0.0, so rank == fts_score
        results_low = await engine.search(
            "machine learning", top_k=1, vector_weight=-0.5
        )
        assert len(results_low) >= 1
        r = results_low[0]
        assert abs(r["rank"] - r["fts_score"]) < 1e-5

    @pytest.mark.asyncio
    async def test_search_per_query_weight_does_not_mutate_engine(
        self, db, mock_embed_client
    ) -> None:
        """A per-query override must not change the engine's stored weights."""
        from src.core.search import HybridSearchEngine

        doc_id = await db.save_document(
            path="/docs/mutate_test.txt",
            source_type="local",
            source_name="test",
            title="Mutate Test",
            ext=".txt",
            mime_type="text/plain",
            body="Machine learning mutation test.",
        )
        await db.save_embedding(doc_id, [1.0, 0.5, 0.3])

        engine = HybridSearchEngine(
            db=db, embed_client=mock_embed_client, vector_weight=0.5
        )
        original_vw = engine.vector_weight
        original_fts_w = engine.fts_weight

        # Override per-query
        await engine.search("machine learning", top_k=1, vector_weight=0.9)

        # Engine's stored weights must be unchanged
        assert engine.vector_weight == original_vw
        assert engine.fts_weight == original_fts_w


# ── Vector storage in Database tests ────────────────────────────


class TestVectorStorage:
    @pytest.mark.asyncio
    async def test_save_and_get_embedding(self, db) -> None:
        """save_embedding and get_embedding should round-trip."""
        doc_id = await db.save_document(
            path="/docs/vec.txt",
            source_type="local",
            source_name="test",
            title="Vector Doc",
            ext=".txt",
            mime_type="text/plain",
            body="Some content.",
        )
        vec = [0.1, 0.2, 0.3, 0.4]
        await db.save_embedding(doc_id, vec)
        recovered = await db.get_embedding(doc_id)
        assert len(recovered) == len(vec)
        for a, b in zip(vec, recovered):
            assert abs(a - b) < 1e-5

    @pytest.mark.asyncio
    async def test_get_embedding_not_found(self, db) -> None:
        """get_embedding for non-existent doc should return empty list."""
        result = await db.get_embedding(99999)
        assert result == []

    @pytest.mark.asyncio
    async def test_save_embedding_upsert(self, db) -> None:
        """save_embedding should update existing embedding (upsert)."""
        doc_id = await db.save_document(
            path="/docs/upsert_vec.txt",
            source_type="local",
            source_name="test",
            title="Upsert Vec",
            ext=".txt",
            mime_type="text/plain",
            body="Content.",
        )
        await db.save_embedding(doc_id, [1.0, 2.0, 3.0])
        await db.save_embedding(doc_id, [4.0, 5.0, 6.0])
        recovered = await db.get_embedding(doc_id)
        assert len(recovered) == 3
        assert abs(recovered[0] - 4.0) < 1e-5

    @pytest.mark.asyncio
    async def test_save_empty_embedding_noop(self, db) -> None:
        """save_embedding with empty vector should be a no-op."""
        doc_id = await db.save_document(
            path="/docs/empty_vec.txt",
            source_type="local",
            source_name="test",
            title="Empty Vec",
            ext=".txt",
            mime_type="text/plain",
            body="Content.",
        )
        await db.save_embedding(doc_id, [])
        assert await db.has_embedding(doc_id) is False

    @pytest.mark.asyncio
    async def test_has_embedding(self, db) -> None:
        """has_embedding should return True after saving, False before."""
        doc_id = await db.save_document(
            path="/docs/has_vec.txt",
            source_type="local",
            source_name="test",
            title="Has Vec",
            ext=".txt",
            mime_type="text/plain",
            body="Content.",
        )
        assert await db.has_embedding(doc_id) is False
        await db.save_embedding(doc_id, [0.1, 0.2])
        assert await db.has_embedding(doc_id) is True

    @pytest.mark.asyncio
    async def test_delete_embedding(self, db) -> None:
        """delete_embedding should remove the embedding."""
        doc_id = await db.save_document(
            path="/docs/del_vec.txt",
            source_type="local",
            source_name="test",
            title="Del Vec",
            ext=".txt",
            mime_type="text/plain",
            body="Content.",
        )
        await db.save_embedding(doc_id, [0.1, 0.2])
        assert await db.delete_embedding(doc_id) is True
        assert await db.has_embedding(doc_id) is False
        # Second delete should return False
        assert await db.delete_embedding(doc_id) is False

    @pytest.mark.asyncio
    async def test_search_similar(self, db) -> None:
        """search_similar should return sorted results by similarity."""
        doc_id1 = await db.save_document(
            path="/docs/sim1.txt",
            source_type="local",
            source_name="test",
            title="Similar 1",
            ext=".txt",
            mime_type="text/plain",
            body="Content.",
        )
        doc_id2 = await db.save_document(
            path="/docs/sim2.txt",
            source_type="local",
            source_name="test",
            title="Similar 2",
            ext=".txt",
            mime_type="text/plain",
            body="Content.",
        )
        # doc1 has identical vector to query, doc2 is orthogonal
        await db.save_embedding(doc_id1, [1.0, 0.0, 0.0])
        await db.save_embedding(doc_id2, [0.0, 1.0, 0.0])

        results = await db.search_similar([1.0, 0.0, 0.0], top_k=5)
        assert len(results) == 2
        # doc1 should be more similar than doc2
        assert results[0]["doc_id"] == doc_id1
        assert results[0]["similarity"] > results[1]["similarity"]
        assert abs(results[0]["similarity"] - 1.0) < 1e-5

    @pytest.mark.asyncio
    async def test_search_similar_empty_query(self, db) -> None:
        """search_similar with empty embedding should return empty list."""
        results = await db.search_similar([], top_k=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_search_similar_no_embeddings(self, db) -> None:
        """search_similar with no stored embeddings should return empty list."""
        results = await db.search_similar([1.0, 2.0, 3.0], top_k=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_search_similar_top_k(self, db) -> None:
        """search_similar should respect top_k limit."""
        for i in range(5):
            doc_id = await db.save_document(
                path=f"/docs/topk_{i}.txt",
                source_type="local",
                source_name="test",
                title=f"TopK {i}",
                ext=".txt",
                mime_type="text/plain",
                body="Content.",
            )
            await db.save_embedding(doc_id, [float(i), 0.0, 0.0])

        results = await db.search_similar([1.0, 0.0, 0.0], top_k=2)
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_embedding_count(self, db) -> None:
        """get_document_count_with_embeddings should count stored embeddings."""
        assert await db.get_document_count_with_embeddings() == 0
        doc_id = await db.save_document(
            path="/docs/count_vec.txt",
            source_type="local",
            source_name="test",
            title="Count Vec",
            ext=".txt",
            mime_type="text/plain",
            body="Content.",
        )
        await db.save_embedding(doc_id, [0.1, 0.2])
        assert await db.get_document_count_with_embeddings() == 1

    @pytest.mark.asyncio
    async def test_embedding_cascade_delete(self, db) -> None:
        """Deleting a document should cascade-delete its embedding."""
        doc_id = await db.save_document(
            path="/docs/cascade.txt",
            source_type="local",
            source_name="test",
            title="Cascade",
            ext=".txt",
            mime_type="text/plain",
            body="Content.",
        )
        await db.save_embedding(doc_id, [0.1, 0.2])
        assert await db.has_embedding(doc_id) is True

        await db.delete_document(doc_id)
        assert await db.has_embedding(doc_id) is False


# ── index_document_embedding tests ─────────────────────────────


class TestIndexEmbedding:
    @pytest.mark.asyncio
    async def test_index_document_embedding(
        self, db, mock_embed_client
    ) -> None:
        """index_document_embedding should generate and store embedding."""
        from src.core.search import HybridSearchEngine

        doc_id = await db.save_document(
            path="/docs/index_vec.txt",
            source_type="local",
            source_name="test",
            title="Index Vec",
            ext=".txt",
            mime_type="text/plain",
            body="Content for embedding.",
        )

        engine = HybridSearchEngine(db=db, embed_client=mock_embed_client)
        await engine.index_document_embedding(
            doc_id=doc_id,
            title="Index Vec",
            summary="A summary.",
            body="Content for embedding.",
        )

        assert await db.has_embedding(doc_id) is True
        vec = await db.get_embedding(doc_id)
        assert len(vec) > 0

    @pytest.mark.asyncio
    async def test_index_embedding_no_provider(
        self, db, unavailable_embed_client
    ) -> None:
        """index_document_embedding should skip when no provider available."""
        from src.core.search import HybridSearchEngine

        doc_id = await db.save_document(
            path="/docs/no_provider.txt",
            source_type="local",
            source_name="test",
            title="No Provider",
            ext=".txt",
            mime_type="text/plain",
            body="Content.",
        )

        engine = HybridSearchEngine(
            db=db, embed_client=unavailable_embed_client
        )
        await engine.index_document_embedding(
            doc_id=doc_id, title="No Provider", body="Content."
        )

        assert await db.has_embedding(doc_id) is False

    @pytest.mark.asyncio
    async def test_index_embedding_empty_text(
        self, db, mock_embed_client
    ) -> None:
        """index_document_embedding with empty text should skip."""
        from src.core.search import HybridSearchEngine

        doc_id = await db.save_document(
            path="/docs/empty_text.txt",
            source_type="local",
            source_name="test",
            title="",
            ext=".txt",
            mime_type="text/plain",
            body="",
        )

        engine = HybridSearchEngine(db=db, embed_client=mock_embed_client)
        await engine.index_document_embedding(doc_id=doc_id)

        assert await db.has_embedding(doc_id) is False


# ── on_document_saved hook tests ────────────────────────────────


class TestDocumentSavedHook:
    @pytest.mark.asyncio
    async def test_hook_called_on_save(self, db) -> None:
        """on_document_saved callback should be called after save_document."""
        called = asyncio.Event()
        call_args = {}

        async def hook(doc_id, path, title, summary, body):
            call_args.update(doc_id=doc_id, path=path, title=title,
                             summary=summary, body=body)
            called.set()

        db.on_document_saved = hook
        doc_id = await db.save_document(
            path="/docs/hook.txt",
            source_type="local",
            source_name="test",
            title="Hook Test",
            ext=".txt",
            mime_type="text/plain",
            body="Body for hook.",
        )

        # Wait for the async hook to fire
        try:
            await asyncio.wait_for(called.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass

        assert call_args.get("doc_id") == doc_id
        assert call_args.get("title") == "Hook Test"

    @pytest.mark.asyncio
    async def test_hook_error_tolerated(self, db) -> None:
        """A failing on_document_saved hook should not break save_document."""

        async def bad_hook(doc_id, path, title, summary, body):
            raise RuntimeError("Hook failed!")

        db.on_document_saved = bad_hook
        doc_id = await db.save_document(
            path="/docs/bad_hook.txt",
            source_type="local",
            source_name="test",
            title="Bad Hook",
            ext=".txt",
            mime_type="text/plain",
            body="Body.",
        )
        assert doc_id > 0  # Document was still saved

    @pytest.mark.asyncio
    async def test_no_hook_no_error(self, db) -> None:
        """save_document should work fine with no hook set."""
        db.on_document_saved = None
        doc_id = await db.save_document(
            path="/docs/no_hook.txt",
            source_type="local",
            source_name="test",
            title="No Hook",
            ext=".txt",
            mime_type="text/plain",
            body="Body.",
        )
        assert doc_id > 0


# ── Chunk-level search tests ────────────────────────────────────


class TestChunkLevelSearch:
    """Tests for HybridSearchEngine.search_chunks()."""

    @pytest.mark.asyncio
    async def test_search_chunks_fts_only(
        self, db, unavailable_embed_client
    ) -> None:
        """search_chunks should return FTS-only results when no embeddings."""
        from src.core.search import HybridSearchEngine

        doc_id = await db.save_document(
            path="/docs/chunk_fts.txt",
            source_type="api",
            source_name="test",
            title="Chunk FTS Doc",
            ext=".txt",
            mime_type="text/plain",
            body="Content",
        )
        chunks = [
            {"text": "Machine learning is powerful", "start_char": 0,
             "end_char": 28, "chunk_index": 0, "token_count": 7},
            {"text": "Cooking recipes are fun", "start_char": 28,
             "end_char": 52, "chunk_index": 1, "token_count": 6},
        ]
        await db.save_chunks(doc_id, chunks)

        engine = HybridSearchEngine(
            db=db, embed_client=unavailable_embed_client
        )
        results = await engine.search_chunks("machine learning", top_k=5)

        assert len(results) >= 1
        assert results[0]["chunk_content"] == "Machine learning is powerful"
        assert results[0]["doc_id"] == doc_id
        assert results[0]["chunk_index"] == 0
        assert "rank" in results[0]
        assert "fts_score" in results[0]

    @pytest.mark.asyncio
    async def test_search_chunks_empty_query(self, db) -> None:
        """search_chunks should return empty for empty query."""
        from src.core.search import HybridSearchEngine

        engine = HybridSearchEngine(db=db)
        results = await engine.search_chunks("", top_k=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_search_chunks_no_chunks(self, db) -> None:
        """search_chunks should return empty when no chunks exist."""
        from src.core.search import HybridSearchEngine

        engine = HybridSearchEngine(db=db)
        results = await engine.search_chunks("anything", top_k=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_search_chunks_with_embeddings(
        self, db, mock_embed_client
    ) -> None:
        """search_chunks should fuse FTS + vector scores."""
        from src.core.search import HybridSearchEngine

        doc_id = await db.save_document(
            path="/docs/chunk_hybrid.txt",
            source_type="api",
            source_name="test",
            title="Hybrid Chunk Doc",
            ext=".txt",
            mime_type="text/plain",
            body="Content",
        )
        chunks = [
            {"text": "Neural networks learn patterns", "start_char": 0,
             "end_char": 31, "chunk_index": 0, "token_count": 8},
            {"text": "Weather forecast tomorrow", "start_char": 31,
             "end_char": 56, "chunk_index": 1, "token_count": 6},
        ]
        await db.save_chunks(doc_id, chunks)
        retrieved = await db.get_chunks(doc_id)

        # Save embeddings
        await db.save_chunk_embedding(retrieved[0]["id"], [1.0, 0.0, 0.0])
        await db.save_chunk_embedding(retrieved[1]["id"], [0.0, 1.0, 0.0])

        engine = HybridSearchEngine(
            db=db, embed_client=mock_embed_client, vector_weight=0.5
        )
        results = await engine.search_chunks("neural", top_k=5)

        assert len(results) >= 1
        # The neural network chunk should be in the results
        found = any("neural" in r["chunk_content"].lower() for r in results)
        assert found

    @pytest.mark.asyncio
    async def test_search_chunks_per_query_vector_weight_override(
        self, db, mock_embed_client
    ) -> None:
        """search_chunks should honour per-query vector_weight override."""
        from src.core.search import HybridSearchEngine

        doc_id = await db.save_document(
            path="/docs/chunk_override.txt",
            source_type="api",
            source_name="test",
            title="Chunk Override Doc",
            ext=".txt",
            mime_type="text/plain",
            body="Content",
        )
        chunks = [
            {"text": "Neural networks learn patterns", "start_char": 0,
             "end_char": 31, "chunk_index": 0, "token_count": 8},
            {"text": "Weather forecast tomorrow", "start_char": 31,
             "end_char": 56, "chunk_index": 1, "token_count": 6},
        ]
        await db.save_chunks(doc_id, chunks)
        retrieved = await db.get_chunks(doc_id)

        await db.save_chunk_embedding(retrieved[0]["id"], [1.0, 0.0, 0.0])
        await db.save_chunk_embedding(retrieved[1]["id"], [0.0, 1.0, 0.0])

        # Engine constructed with vector_weight=0.5
        engine = HybridSearchEngine(
            db=db, embed_client=mock_embed_client, vector_weight=0.5
        )

        # Override to 0.0 → rank should equal fts_score
        results_fts = await engine.search_chunks(
            "neural", top_k=5, vector_weight=0.0
        )
        assert len(results_fts) >= 1
        for r in results_fts:
            assert abs(r["rank"] - r["fts_score"]) < 1e-5

        # Override to 1.0 → rank should equal vector_score
        results_vec = await engine.search_chunks(
            "neural", top_k=5, vector_weight=1.0
        )
        assert len(results_vec) >= 1
        for r in results_vec:
            assert abs(r["rank"] - r["vector_score"]) < 1e-5

    @pytest.mark.asyncio
    async def test_index_document_embedding_creates_chunks(
        self, db, mock_embed_client
    ) -> None:
        """index_document_embedding should chunk and embed per-chunk."""
        from src.core.search import HybridSearchEngine
        from src.core.chunking import TextChunker
        from src.core.config import ChunkingConfig

        # Use long enough paragraphs to avoid merging (default min_chunk_size=100)
        body = (
            "This is a detailed paragraph about artificial intelligence "
            "and machine learning. It covers neural networks, deep learning, "
            "and natural language processing in great detail.\n\n"
            "This second paragraph discusses cooking recipes and culinary "
            "arts. It explores various cuisines, cooking techniques, and "
            "ingredient selection for gourmet meals."
        )
        doc_id = await db.save_document(
            path="/docs/auto_chunk.txt",
            source_type="api",
            source_name="test",
            title="Auto Chunk",
            ext=".txt",
            mime_type="text/plain",
            body=body,
        )

        engine = HybridSearchEngine(
            db=db, embed_client=mock_embed_client
        )
        await engine.index_document_embedding(
            doc_id=doc_id,
            title="Auto Chunk",
            summary="",
            body=body,
        )

        # Verify chunks were created
        chunk_count = await db.get_chunk_count(doc_id)
        assert chunk_count >= 2

        # Verify chunk embeddings were stored
        emb_count = await db.get_chunk_count_with_embeddings()
        assert emb_count >= 2

        # Verify document-level embedding (mean of chunks) was stored
        doc_emb = await db.get_embedding(doc_id)
        assert len(doc_emb) > 0

    @pytest.mark.asyncio
    async def test_index_document_embedding_no_provider(self, db) -> None:
        """index_document_embedding should skip when no provider."""
        from src.core.search import HybridSearchEngine

        doc_id = await db.save_document(
            path="/docs/no_provider.txt",
            source_type="api",
            source_name="test",
            title="No Provider",
            ext=".txt",
            mime_type="text/plain",
            body="Some content here.",
        )

        # No embed client
        engine = HybridSearchEngine(db=db, embed_client=None)
        await engine.index_document_embedding(
            doc_id=doc_id,
            title="No Provider",
            body="Some content here.",
        )

        # No chunks should be created
        assert await db.get_chunk_count(doc_id) == 0

    @pytest.mark.asyncio
    async def test_index_document_embedding_empty_body(
        self, db, mock_embed_client
    ) -> None:
        """index_document_embedding should handle empty body gracefully."""
        from src.core.search import HybridSearchEngine

        doc_id = await db.save_document(
            path="/docs/empty_body.txt",
            source_type="api",
            source_name="test",
            title="Empty Body",
            ext=".txt",
            mime_type="text/plain",
            body="",
        )

        engine = HybridSearchEngine(
            db=db, embed_client=mock_embed_client
        )
        await engine.index_document_embedding(
            doc_id=doc_id,
            title="Empty Body",
            summary="A summary",
            body="",
        )

        # No chunks, no embedding
        assert await db.get_chunk_count(doc_id) == 0
