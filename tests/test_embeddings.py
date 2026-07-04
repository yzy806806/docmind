"""Tests for src.core.embeddings — EmbeddingClient and vector utilities.

Covers:
- Vector serialization/deserialization (round-trip)
- Cosine similarity (pure-Python and numpy paths)
- EmbeddingClient provider selection and fallback
- Remote providers (ollama, openai) with mocked httpx
- Local provider (skipped if sentence-transformers not installed)
- build_embedding_text helper
"""

from __future__ import annotations

import math
from typing import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def embedding_config():
    """Create an EmbeddingConfig with no provider (fallback mode)."""
    from src.core.config import EmbeddingConfig
    return EmbeddingConfig()


@pytest.fixture
def ollama_config():
    """Create an EmbeddingConfig configured for Ollama."""
    from src.core.config import EmbeddingConfig
    return EmbeddingConfig(
        provider="ollama",
        model="nomic-embed-text",
        base_url="http://localhost:11434",
        dim=768,
    )


@pytest.fixture
def openai_config():
    """Create an EmbeddingConfig configured for OpenAI."""
    from src.core.config import EmbeddingConfig
    return EmbeddingConfig(
        provider="openai",
        model="text-embedding-3-small",
        base_url="https://api.openai.com",
        api_key="test-key-123",
        dim=1536,
    )


# ── Import / smoke tests ────────────────────────────────────────


def test_import_embeddings_module() -> None:
    """The embeddings module should be importable."""
    from src.core import embeddings
    assert embeddings is not None


def test_embedding_client_class_exists() -> None:
    """EmbeddingClient class should be importable."""
    from src.core.embeddings import EmbeddingClient
    assert EmbeddingClient is not None


# ── Vector serialization tests ──────────────────────────────────


class TestVectorSerialization:
    def test_serialize_deserialize_roundtrip(self) -> None:
        """serialize_vector → deserialize_vector should round-trip."""
        from src.core.embeddings import serialize_vector, deserialize_vector
        original = [0.1, 0.2, 0.3, 0.4, 0.5]
        blob = serialize_vector(original)
        assert isinstance(blob, bytes)
        assert len(blob) > 0
        recovered = deserialize_vector(blob)
        assert len(recovered) == len(original)
        for a, b in zip(original, recovered):
            assert abs(a - b) < 1e-5

    def test_serialize_empty_vector(self) -> None:
        """Serializing an empty vector should produce bytes (possibly empty)."""
        from src.core.embeddings import serialize_vector, deserialize_vector
        blob = serialize_vector([])
        recovered = deserialize_vector(blob)
        assert recovered == []

    def test_serialize_large_vector(self) -> None:
        """A 384-dim vector (MiniLM size) should serialize and deserialize."""
        from src.core.embeddings import serialize_vector, deserialize_vector
        vec = [float(i) / 384.0 for i in range(384)]
        blob = serialize_vector(vec)
        recovered = deserialize_vector(blob)
        assert len(recovered) == 384
        for a, b in zip(vec, recovered):
            assert abs(a - b) < 1e-5

    def test_deserialize_empty_blob(self) -> None:
        """Deserializing an empty blob should return empty list."""
        from src.core.embeddings import deserialize_vector
        assert deserialize_vector(b"") == []


# ── Cosine similarity tests ─────────────────────────────────────


class TestCosineSimilarity:
    def test_identical_vectors(self) -> None:
        """Cosine similarity of identical vectors should be ~1.0."""
        from src.core.embeddings import cosine_similarity
        vec = [1.0, 2.0, 3.0, 4.0]
        sim = cosine_similarity(vec, vec)
        assert abs(sim - 1.0) < 1e-5

    def test_orthogonal_vectors(self) -> None:
        """Cosine similarity of orthogonal vectors should be ~0.0."""
        from src.core.embeddings import cosine_similarity
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        sim = cosine_similarity(a, b)
        assert abs(sim) < 1e-5

    def test_opposite_vectors(self) -> None:
        """Cosine similarity of opposite vectors should be ~-1.0."""
        from src.core.embeddings import cosine_similarity
        a = [1.0, 2.0, 3.0]
        b = [-1.0, -2.0, -3.0]
        sim = cosine_similarity(a, b)
        assert abs(sim - (-1.0)) < 1e-5

    def test_empty_vectors(self) -> None:
        """Cosine similarity of empty vectors should be 0.0."""
        from src.core.embeddings import cosine_similarity
        assert cosine_similarity([], []) == 0.0

    def test_zero_vector(self) -> None:
        """Cosine similarity with a zero-magnitude vector should be 0.0."""
        from src.core.embeddings import cosine_similarity
        a = [0.0, 0.0, 0.0]
        b = [1.0, 2.0, 3.0]
        assert cosine_similarity(a, b) == 0.0

    def test_different_length_vectors(self) -> None:
        """Vectors of different lengths should return 0.0."""
        from src.core.embeddings import cosine_similarity
        a = [1.0, 2.0]
        b = [1.0, 2.0, 3.0]
        assert cosine_similarity(a, b) == 0.0

    def test_partial_similarity(self) -> None:
        """A vector and its scaled version should have similarity ~1.0."""
        from src.core.embeddings import cosine_similarity
        a = [1.0, 2.0, 3.0]
        b = [2.0, 4.0, 6.0]  # same direction, different magnitude
        sim = cosine_similarity(a, b)
        assert abs(sim - 1.0) < 1e-5


# ── EmbeddingClient tests ───────────────────────────────────────


class TestEmbeddingClient:
    def test_no_provider_returns_empty(self, embedding_config) -> None:
        """With no provider configured, is_available() should be False."""
        from src.core.embeddings import EmbeddingClient
        client = EmbeddingClient(embedding_config)
        assert client.is_available() is False

    @pytest.mark.asyncio
    async def test_embed_no_provider_returns_empty(self, embedding_config) -> None:
        """embed() with no provider should return empty list."""
        from src.core.embeddings import EmbeddingClient
        client = EmbeddingClient(embedding_config)
        vec = await client.embed("test text")
        assert vec == []

    @pytest.mark.asyncio
    async def test_embed_empty_text_returns_empty(self, ollama_config) -> None:
        """embed() with empty text should return empty list."""
        from src.core.embeddings import EmbeddingClient
        client = EmbeddingClient(ollama_config)
        vec = await client.embed("")
        assert vec == []

    @pytest.mark.asyncio
    async def test_embed_whitespace_text_returns_empty(self, ollama_config) -> None:
        """embed() with whitespace-only text should return empty list."""
        from src.core.embeddings import EmbeddingClient
        client = EmbeddingClient(ollama_config)
        vec = await client.embed("   ")
        assert vec == []

    def test_ollama_provider_is_available(self, ollama_config) -> None:
        """Ollama provider should be available."""
        from src.core.embeddings import EmbeddingClient
        client = EmbeddingClient(ollama_config)
        assert client.is_available() is True

    def test_openai_provider_is_available(self, openai_config) -> None:
        """OpenAI provider should be available."""
        from src.core.embeddings import EmbeddingClient
        client = EmbeddingClient(openai_config)
        assert client.is_available() is True

    def test_provider_property(self, ollama_config, openai_config) -> None:
        """provider property should return the configured provider string."""
        from src.core.embeddings import EmbeddingClient
        assert EmbeddingClient(ollama_config).provider == "ollama"
        assert EmbeddingClient(openai_config).provider == "openai"

    def test_dim_property(self, ollama_config, openai_config) -> None:
        """dim property should return the configured dimension."""
        from src.core.embeddings import EmbeddingClient
        assert EmbeddingClient(ollama_config).dim == 768
        assert EmbeddingClient(openai_config).dim == 1536

    @pytest.mark.asyncio
    async def test_embed_batch_no_provider(self, embedding_config) -> None:
        """embed_batch with no provider should return empty vectors."""
        from src.core.embeddings import EmbeddingClient
        client = EmbeddingClient(embedding_config)
        result = await client.embed_batch(["a", "b", "c"])
        assert len(result) == 3
        for vec in result:
            assert vec == []

    @pytest.mark.asyncio
    async def test_embed_batch_empty_input(self, ollama_config) -> None:
        """embed_batch with empty input should return empty list."""
        from src.core.embeddings import EmbeddingClient
        client = EmbeddingClient(ollama_config)
        result = await client.embed_batch([])
        assert result == []


# ── Ollama provider tests (mocked) ──────────────────────────────


class TestOllamaProvider:
    @pytest.mark.asyncio
    async def test_embed_ollama_success(self, ollama_config) -> None:
        """Ollama embed should return a vector from the API response."""
        from src.core.embeddings import EmbeddingClient

        client = EmbeddingClient(ollama_config)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"embedding": [0.1, 0.2, 0.3]}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            vec = await client.embed("hello world")
            assert vec == [0.1, 0.2, 0.3]
            mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_embed_ollama_failure_returns_empty(self, ollama_config) -> None:
        """Ollama embed on API failure should return empty list."""
        from src.core.embeddings import EmbeddingClient

        client = EmbeddingClient(ollama_config)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            vec = await client.embed("hello world")
            assert vec == []

    @pytest.mark.asyncio
    async def test_embed_ollama_correct_url(self, ollama_config) -> None:
        """Ollama embed should call the correct endpoint URL."""
        from src.core.embeddings import EmbeddingClient

        client = EmbeddingClient(ollama_config)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"embedding": [0.1]}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await client.embed("test")
            call_args = mock_client.post.call_args
            url = call_args[0][0] if call_args[0] else call_args[1].get("url", "")
            assert "/api/embeddings" in url
            assert "localhost:11434" in url


# ── OpenAI provider tests (mocked) ──────────────────────────────


class TestOpenAIProvider:
    @pytest.mark.asyncio
    async def test_embed_openai_success(self, openai_config) -> None:
        """OpenAI embed should return a vector from the API response."""
        from src.core.embeddings import EmbeddingClient

        client = EmbeddingClient(openai_config)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            vec = await client.embed("hello world")
            assert vec == [0.1, 0.2, 0.3]

    @pytest.mark.asyncio
    async def test_embed_openai_failure_returns_empty(self, openai_config) -> None:
        """OpenAI embed on API failure should return empty list."""
        from src.core.embeddings import EmbeddingClient

        client = EmbeddingClient(openai_config)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("Auth failed"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            vec = await client.embed("hello world")
            assert vec == []

    @pytest.mark.asyncio
    async def test_embed_openai_api_key_header(self, openai_config) -> None:
        """OpenAI embed should include Authorization header with API key."""
        from src.core.embeddings import EmbeddingClient

        client = EmbeddingClient(openai_config)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [{"index": 0, "embedding": [0.1]}]
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await client.embed("test")
            call_args = mock_client.post.call_args
            headers = call_args[1].get("headers", {})
            assert headers.get("Authorization") == "Bearer test-key-123"

    @pytest.mark.asyncio
    async def test_embed_batch_openai_success(self, openai_config) -> None:
        """OpenAI batch embed should return vectors in input order."""
        from src.core.embeddings import EmbeddingClient

        client = EmbeddingClient(openai_config)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [
                {"index": 1, "embedding": [0.2, 0.3]},
                {"index": 0, "embedding": [0.1, 0.2]},
            ]
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await client.embed_batch(["first", "second"])
            assert len(result) == 2
            assert result[0] == [0.1, 0.2]  # index 0
            assert result[1] == [0.2, 0.3]  # index 1


# ── Local provider tests (skipped if sentence-transformers not installed) ──


class TestLocalProvider:
    def test_local_provider_availability(self) -> None:
        """Local provider availability depends on sentence-transformers."""
        from src.core.embeddings import EmbeddingClient, _HAS_ST
        from src.core.config import EmbeddingConfig
        config = EmbeddingConfig(provider="local", model="all-MiniLM-L6-v2")
        client = EmbeddingClient(config)
        assert client.is_available() == _HAS_ST

    @pytest.mark.skipif(
        not pytest.importorskip("sentence_transformers", reason="sentence-transformers not installed"),
        reason="sentence-transformers not installed",
    )
    @pytest.mark.asyncio
    async def test_embed_local_returns_vector(self) -> None:
        """Local embed should return a non-empty vector when ST is installed."""
        from src.core.embeddings import EmbeddingClient
        from src.core.config import EmbeddingConfig
        config = EmbeddingConfig(provider="local", model="all-MiniLM-L6-v2")
        client = EmbeddingClient(config)
        vec = await client.embed("machine learning")
        assert len(vec) > 0
        assert len(vec) == config.dim


# ── build_embedding_text tests ──────────────────────────────────


class TestBuildEmbeddingText:
    def test_all_fields(self, embedding_config) -> None:
        """build_embedding_text should combine title, summary, body."""
        from src.core.embeddings import EmbeddingClient
        client = EmbeddingClient(embedding_config)
        text = client.build_embedding_text(
            title="Machine Learning",
            summary="An overview of ML techniques.",
            body="Deep learning is a subset of machine learning.",
        )
        assert "Machine Learning" in text
        assert "overview of ML" in text
        assert "Deep learning" in text

    def test_empty_fields(self, embedding_config) -> None:
        """build_embedding_text with all empty should return empty string."""
        from src.core.embeddings import EmbeddingClient
        client = EmbeddingClient(embedding_config)
        text = client.build_embedding_text()
        assert text == ""

    def test_only_title(self, embedding_config) -> None:
        """build_embedding_text with only title should return title."""
        from src.core.embeddings import EmbeddingClient
        client = EmbeddingClient(embedding_config)
        text = client.build_embedding_text(title="Just a Title")
        assert text == "Just a Title"

    def test_body_truncated(self, embedding_config) -> None:
        """build_embedding_text should truncate body to 2000 chars."""
        from src.core.embeddings import EmbeddingClient
        client = EmbeddingClient(embedding_config)
        long_body = "x" * 5000
        text = client.build_embedding_text(title="T", body=long_body)
        # Title (2) + space (1) + body (2000) = 2003
        assert len(text) <= 2003
        assert "x" * 2000 in text
