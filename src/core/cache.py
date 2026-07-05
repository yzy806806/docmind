"""Cache abstraction layer for DocMind.

Provides a pluggable cache backend interface with a zero-config
in-memory default and optional Redis support.  Uses the cache-aside
pattern: the database remains the source of truth and application code
is responsible for cache reads/writes and explicit invalidation.

Typical usage::

    from src.core.cache import create_cache_backend

    cache = create_cache_backend()
    await cache.set("docmind:doc:get:42", doc_dict, ttl=60)
    cached = await cache.get("docmind:doc:get:42")

Configuration is driven by environment variables (see ``CacheConfig``
in ``config.py``):
    DOCMIND_CACHE_BACKEND  – "memory" (default) or "redis"
    DOCMIND_CACHE_REDIS_URL – Redis connection URL
    DOCMIND_CACHE_MAX_SIZE  – Max entries for in-memory cache (default 10000)
    DOCMIND_CACHE_ENABLED   – If False, a NoopCache is returned (default True)
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── TTL Configuration ──────────────────────────────────────────


@dataclass(frozen=True)
class CacheTTLConfig:
    """TTL values in seconds for each cache category.

    These are guidelines — callers can pass explicit TTLs, but using
    these named constants ensures consistency across the codebase.
    """

    doc_single: int = 60
    doc_list: int = 30
    search: int = 120
    tag_list: int = 300
    tag_cloud: int = 600
    docs_by_tag: int = 60
    collection_tree: int = 600
    collection_counts: int = 300
    collection_single: int = 600
    dashboard_stats: int = 60
    doc_growth: int = 300
    tag_dist: int = 600
    storage_stats: int = 300
    search_stats: int = 300
    popular_queries: int = 600
    search_trend: int = 300
    chat_activity: int = 300
    job_stats: int = 60
    job_list: int = 30
    job_detail: int = 60
    settings: int = 600
    chat_sessions: int = 60
    chat_messages: int = 60
    doc_by_path: int = 60
    file_type_facets: int = 300
    source_facets: int = 300


# Singleton instance for convenient access
TTL = CacheTTLConfig()


# ── Key Helpers ────────────────────────────────────────────────


def make_key(*parts: Any) -> str:
    """Build a colon-delimited cache key from ordered parts.

    Example::

        make_key("docmind", "doc", "get", 42)  # -> "docmind:doc:get:42"
    """
    return ":".join(str(p) for p in parts)


def hash_params(**kwargs: Any) -> str:
    """Create a short deterministic hash of keyword arguments.

    Used for cache keys that include complex query parameters (search
    queries, filter combinations) where the raw values would be too
    long or contain colons.
    """
    serialized = json.dumps(kwargs, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.blake2b(serialized.encode(), digest_size=8).hexdigest()[:16]


# ── Abstract Base Class ────────────────────────────────────────


class CacheBackend(ABC):
    """Abstract cache backend interface.

    All methods are async to support both local and network backends.
    """

    @abstractmethod
    async def get(self, key: str) -> Optional[Any]:
        """Retrieve a value from the cache. Returns None if not found or expired."""
        ...

    @abstractmethod
    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """Store a value in the cache with an optional TTL in seconds."""
        ...

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove a single key from the cache. No-op if the key doesn't exist."""
        ...

    @abstractmethod
    async def delete_pattern(self, pattern: str) -> None:
        """Remove all keys matching a glob-style pattern (e.g., 'docs:list:*')."""
        ...

    @abstractmethod
    async def flush(self) -> None:
        """Clear all cached data."""
        ...


# ── No-op Backend (for DOCMIND_CACHE_ENABLED=False) ───────────


class NoopCache(CacheBackend):
    """A no-op cache that stores nothing. Used when caching is disabled."""

    async def get(self, key: str) -> Optional[Any]:
        return None

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        pass

    async def delete(self, key: str) -> None:
        pass

    async def delete_pattern(self, pattern: str) -> None:
        pass

    async def flush(self) -> None:
        pass


# ── In-Memory Backend (Default) ────────────────────────────────


class InMemoryCache(CacheBackend):
    """Async in-memory cache with TTL support and LRU eviction.

    Uses a plain dict with an asyncio.Lock for concurrency safety.
    TTL is checked lazily on get; expired entries are removed then.
    When ``max_size`` is reached, the oldest inserted key is evicted
    (FIFO approximation of LRU).
    """

    def __init__(self, max_size: int = 10_000):
        self._store: dict[str, Any] = {}
        self._expires: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._max_size = max_size

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            if key not in self._store:
                return None
            # Lazy TTL expiry check
            if key in self._expires and time.time() > self._expires[key]:
                del self._store[key]
                self._expires.pop(key, None)
                return None
            return self._store[key]

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        async with self._lock:
            self._store[key] = value
            if ttl:
                self._expires[key] = time.time() + ttl
            else:
                self._expires.pop(key, None)
            # Evict oldest entries if over capacity
            while len(self._store) > self._max_size:
                oldest = next(iter(self._store))
                del self._store[oldest]
                self._expires.pop(oldest, None)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)
            self._expires.pop(key, None)

    async def delete_pattern(self, pattern: str) -> None:
        """Remove keys matching a simple glob pattern (* = any chars)."""
        async with self._lock:
            keys = list(self._store.keys())
            for key in keys:
                if fnmatch.fnmatch(key, pattern):
                    del self._store[key]
                    self._expires.pop(key, None)

    async def flush(self) -> None:
        async with self._lock:
            self._store.clear()
            self._expires.clear()

    async def size(self) -> int:
        """Return the current number of cached entries (for diagnostics)."""
        async with self._lock:
            return len(self._store)


# ── Redis Backend (Optional) ───────────────────────────────────


class RedisCache(CacheBackend):
    """Redis-backed cache backend.

    Requires the ``redis>=5.0`` package.  Values are JSON-serialized
    to support arbitrary Python objects (dicts, lists, etc.).

    Falls back to ``InMemoryCache`` if the Redis connection fails at
    startup — logged as a warning so operators know.
    """

    def __init__(self, url: str = "redis://localhost:6379/0"):
        self._url = url
        self._client = None  # lazy init
        self._fallback: Optional[InMemoryCache] = None

    def _get_client(self):
        """Lazily import redis and create the client."""
        if self._fallback is not None:
            return None  # already fallen back

        if self._client is None:
            try:
                import redis.asyncio as redis  # noqa: PLC0415

                self._client = redis.from_url(self._url, decode_responses=True)
            except ImportError:
                logger.warning(
                    "Redis package not installed — falling back to InMemoryCache. "
                    "Install with: pip install redis"
                )
                self._fallback = InMemoryCache()
            except Exception as e:
                logger.warning(
                    "Failed to create Redis client (%s) — falling back to InMemoryCache",
                    e,
                )
                self._fallback = InMemoryCache()
        return self._client

    async def get(self, key: str) -> Optional[Any]:
        client = self._get_client()
        if client is None:
            return await self._fallback.get(key)  # type: ignore[union-attr]
        try:
            value = await client.get(key)
            if value is None:
                return None
            return json.loads(value)
        except Exception as e:
            logger.warning("Redis get failed for key %s: %s", key, e)
            return None

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        client = self._get_client()
        if client is None:
            await self._fallback.set(key, value, ttl=ttl)  # type: ignore[union-attr]
            return
        try:
            serialized = json.dumps(value, default=str)
            if ttl:
                await client.setex(key, ttl, serialized)
            else:
                await client.set(key, serialized)
        except Exception as e:
            logger.warning("Redis set failed for key %s: %s", key, e)

    async def delete(self, key: str) -> None:
        client = self._get_client()
        if client is None:
            await self._fallback.delete(key)  # type: ignore[union-attr]
            return
        try:
            await client.delete(key)
        except Exception as e:
            logger.warning("Redis delete failed for key %s: %s", key, e)

    async def delete_pattern(self, pattern: str) -> None:
        client = self._get_client()
        if client is None:
            await self._fallback.delete_pattern(pattern)  # type: ignore[union-attr]
            return
        try:
            keys = await client.keys(pattern)
            if keys:
                await client.delete(*keys)
        except Exception as e:
            logger.warning("Redis delete_pattern failed for %s: %s", pattern, e)

    async def flush(self) -> None:
        client = self._get_client()
        if client is None:
            await self._fallback.flush()  # type: ignore[union-attr]
            return
        try:
            await client.flushdb()
        except Exception as e:
            logger.warning("Redis flush failed: %s", e)


# ── Factory ────────────────────────────────────────────────────


def create_cache_backend(
    *,
    backend: Optional[str] = None,
    redis_url: Optional[str] = None,
    max_size: Optional[int] = None,
    enabled: Optional[bool] = None,
) -> CacheBackend:
    """Create a cache backend based on configuration.

    Args:
        backend: "memory" or "redis". If None, reads DOCMIND_CACHE_BACKEND env.
        redis_url: Redis connection URL. If None, reads DOCMIND_CACHE_REDIS_URL env.
        max_size: Max entries for in-memory cache. If None, reads DOCMIND_CACHE_MAX_SIZE env.
        enabled: If False, returns a NoopCache. If None, reads DOCMIND_CACHE_ENABLED env.

    Returns:
        A CacheBackend instance.
    """
    import os

    if enabled is None:
        val = os.environ.get("DOCMIND_CACHE_ENABLED", "true").strip().lower()
        enabled = val not in ("0", "false", "no")

    if not enabled:
        logger.info("Cache is disabled (DOCMIND_CACHE_ENABLED=false) — using NoopCache")
        return NoopCache()

    if backend is None:
        backend = os.environ.get("DOCMIND_CACHE_BACKEND", "memory").lower()

    if backend == "redis":
        if redis_url is None:
            redis_url = os.environ.get(
                "DOCMIND_CACHE_REDIS_URL", "redis://localhost:6379/0"
            )
        logger.info("Using Redis cache backend: %s", redis_url)
        return RedisCache(url=redis_url)

    if max_size is None:
        max_size = int(os.environ.get("DOCMIND_CACHE_MAX_SIZE", "10000"))

    logger.info("Using in-memory cache backend (max_size=%d)", max_size)
    return InMemoryCache(max_size=max_size)
