"""Embedding client for vector/semantic search.

Supports three embedding providers:
  - 'local':  sentence-transformers (loads model in-process; requires the
              optional ``embeddings`` extra: ``uv sync --extra embeddings``)
  - 'ollama': remote Ollama ``/api/embeddings`` endpoint via httpx
  - 'openai': remote OpenAI-compatible ``/v1/embeddings`` endpoint via httpx

When no provider is configured (or sentence-transformers is not installed
and no remote URL is set), the client returns an empty vector and search
gracefully falls back to FTS5-only keyword matching.

Vector math (cosine similarity, normalization) uses numpy when available
and falls back to pure-Python otherwise, so the module works even without
numpy installed.
"""

from __future__ import annotations

import logging
import struct
from typing import Any, Optional

import httpx

from .config import EmbeddingConfig

logger = logging.getLogger(__name__)

# Try importing numpy (optional — pure-Python fallback otherwise)
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False
    np = None  # type: ignore[assignment]

# Try importing sentence-transformers (optional — heavy dep)
try:
    from sentence_transformers import SentenceTransformer  # type: ignore[import-untyped]
    _HAS_ST = True
except ImportError:
    _HAS_ST = False
    SentenceTransformer = None  # type: ignore[assignment]


# ── Vector serialization ──────────────────────────────────────


def serialize_vector(vec: list[float]) -> bytes:
    """Serialize a float vector to a compact binary BLOB.

    Uses little-endian 32-bit floats (numpy float32 compatible).
    A 384-dim vector takes 1.5 KB.
    """
    if _HAS_NUMPY:
        arr = np.asarray(vec, dtype=np.float32)
        return arr.tobytes()
    # Pure-Python fallback: struct pack
    return struct.pack(f"<{len(vec)}f", *vec)


def deserialize_vector(blob: bytes) -> list[float]:
    """Deserialize a binary BLOB back to a list of floats."""
    if not blob:
        return []
    if _HAS_NUMPY:
        arr = np.frombuffer(blob, dtype=np.float32)
        return arr.tolist()
    # Pure-Python fallback: struct unpack
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Returns a float in [-1, 1], or 0.0 if either vector is empty or
    has zero magnitude.
    """
    if not a or not b or len(a) != len(b):
        return 0.0

    if _HAS_NUMPY:
        arr_a = np.asarray(a, dtype=np.float32)
        arr_b = np.asarray(b, dtype=np.float32)
        norm_a = np.linalg.norm(arr_a)
        norm_b = np.linalg.norm(arr_b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(arr_a, arr_b) / (norm_a * norm_b))

    # Pure-Python fallback
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / ((norm_a ** 0.5) * (norm_b ** 0.5))


# ── Embedding client ──────────────────────────────────────────


class EmbeddingClient:
    """Async embedding client supporting local and remote providers.

    Usage::

        client = EmbeddingClient(config.embedding)
        vec = await client.embed("some text")
        if client.is_available():
            # use vec for semantic search
        else:
            # fall back to FTS5-only

    The client is stateless after construction (local models are loaded
    lazily on first ``embed`` call to avoid importing torch at startup).
    """

    def __init__(self, config: Optional[EmbeddingConfig] = None):
        self._config = config or EmbeddingConfig()
        self._model: Any = None  # lazily-loaded SentenceTransformer
        self._model_loaded = False

    @property
    def provider(self) -> str:
        """Active provider string ('local', 'ollama', 'openai', or '')."""
        return self._config.provider.strip().lower()

    @property
    def dim(self) -> int:
        """Expected embedding dimensionality."""
        return self._config.dim

    def is_available(self) -> bool:
        """Check whether an embedding provider is available.

        Returns True if a remote provider is configured, or if the local
        sentence-transformers library is installed.
        """
        p = self.provider
        if p in ("ollama", "openai"):
            return True
        if p == "local":
            return _HAS_ST
        return False

    async def embed(self, text: str) -> list[float]:
        """Generate an embedding vector for a single text.

        Returns an empty list if no provider is available.
        """
        if not text or not text.strip():
            return []

        p = self.provider
        if p == "local":
            return await self._embed_local(text)
        elif p == "ollama":
            return await self._embed_ollama(text)
        elif p == "openai":
            return await self._embed_openai(text)
        else:
            logger.debug("No embedding provider configured — returning empty vector")
            return []

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts.

        Returns a list of embedding vectors (one per input text).
        Empty input texts produce empty vectors.
        """
        if not texts:
            return []

        p = self.provider
        if p == "local":
            return await self._embed_batch_local(texts)
        elif p == "ollama":
            # Ollama doesn't have a batch endpoint — embed one at a time
            results = []
            for text in texts:
                results.append(await self._embed_ollama(text))
            return results
        elif p == "openai":
            return await self._embed_batch_openai(texts)
        else:
            return [[] for _ in texts]

    def build_embedding_text(
        self,
        title: str = "",
        summary: str = "",
        body: str = "",
    ) -> str:
        """Build the text used to generate a document's embedding.

        Combines title + summary + first N chars of body to produce a
        rich representation for semantic matching.
        """
        parts: list[str] = []
        if title and title.strip():
            parts.append(title.strip())
        if summary and summary.strip():
            parts.append(summary.strip())
        if body and body.strip():
            # Use first 2000 chars of body to stay within model limits
            parts.append(body.strip()[:2000])
        return " ".join(parts) if parts else ""

    # ── Local (sentence-transformers) ──────────────────────────

    def _load_local_model(self) -> None:
        """Lazily load the sentence-transformers model."""
        if self._model_loaded:
            return
        if not _HAS_ST:
            raise ImportError(
                "sentence-transformers is not installed. "
                "Install with: uv sync --extra embeddings"
            )
        logger.info("Loading sentence-transformers model: %s", self._config.model)
        self._model = SentenceTransformer(self._config.model)
        self._model_loaded = True

    async def _embed_local(self, text: str) -> list[float]:
        """Embed using local sentence-transformers model."""
        try:
            self._load_local_model()
            # SentenceTransformer.encode is sync — run in executor
            import asyncio
            loop = asyncio.get_event_loop()
            vec = await loop.run_in_executor(
                None, lambda: self._model.encode(text, normalize_embeddings=True)
            )
            return vec.tolist()
        except Exception as e:
            logger.error("Local embedding failed: %s", e)
            return []

    async def _embed_batch_local(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch using local sentence-transformers model."""
        try:
            self._load_local_model()
            import asyncio
            loop = asyncio.get_event_loop()
            vecs = await loop.run_in_executor(
                None,
                lambda: self._model.encode(texts, normalize_embeddings=True),
            )
            return [v.tolist() for v in vecs]
        except Exception as e:
            logger.error("Local batch embedding failed: %s", e)
            return [[] for _ in texts]

    # ── Ollama ─────────────────────────────────────────────────

    async def _embed_ollama(self, text: str) -> list[float]:
        """Embed using Ollama's /api/embeddings endpoint."""
        base_url = self._config.base_url or "http://localhost:11434"
        url = f"{base_url.rstrip('/')}/api/embeddings"
        payload = {"model": self._config.model, "prompt": text}
        try:
            async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                return data.get("embedding", [])
        except Exception as e:
            logger.error("Ollama embedding failed: %s", e)
            return []

    # ── OpenAI ─────────────────────────────────────────────────

    async def _embed_openai(self, text: str) -> list[float]:
        """Embed using OpenAI-compatible /v1/embeddings endpoint."""
        base_url = self._config.base_url or "https://api.openai.com"
        url = f"{base_url.rstrip('/')}/v1/embeddings"
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        payload = {"model": self._config.model, "input": text}
        try:
            async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                embeddings = data.get("data", [])
                if embeddings and len(embeddings) > 0:
                    return embeddings[0].get("embedding", [])
                return []
        except Exception as e:
            logger.error("OpenAI embedding failed: %s", e)
            return []

    async def _embed_batch_openai(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch using OpenAI-compatible endpoint."""
        base_url = self._config.base_url or "https://api.openai.com"
        url = f"{base_url.rstrip('/')}/v1/embeddings"
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        payload = {"model": self._config.model, "input": texts}
        try:
            async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                embeddings = data.get("data", [])
                # Sort by index to preserve input order
                embeddings.sort(key=lambda x: x.get("index", 0))
                return [e.get("embedding", []) for e in embeddings]
        except Exception as e:
            logger.error("OpenAI batch embedding failed: %s", e)
            return [[] for _ in texts]
