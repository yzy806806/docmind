# Caching Layer Architecture Specification

**DocMind — Phase 5a**
**Version:** 1.0-draft
**Date:** 2026-07-06

---

## 1. Overview

This document specifies the caching layer architecture for DocMind. The goal is to reduce redundant database queries for read-heavy operations while ensuring cache consistency across all mutation paths.

**Key design decisions:**
- **Pluggable backend**: In-memory dict (default, zero-config) with optional Redis upgrade.
- **No external dependency by default**: Redis is optional; the app works out of the box.
- **Cache-aside pattern**: Application code is responsible for cache reads/writes; the database remains the source of truth.
- **Explicit invalidation**: Every mutation path must invalidate affected cache keys.

---

## 2. Cache Interface Design

### 2.1 Abstract Base Class

```python
from abc import ABC, abstractmethod
from typing import Any, Optional

class CacheBackend(ABC):
    """Abstract cache backend interface."""

    @abstractmethod
    async def get(self, key: str) -> Optional[Any]:
        """Retrieve a value from the cache. Returns None if not found."""
        ...

    @abstractmethod
    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """Store a value in the cache with an optional TTL in seconds."""
        ...

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove a single key from the cache."""
        ...

    @abstractmethod
    async def delete_pattern(self, pattern: str) -> None:
        """Remove all keys matching a glob-style pattern (e.g., 'docs:list:*')."""
        ...

    @abstractmethod
    async def flush(self) -> None:
        """Clear all cached data."""
        ...
```

### 2.2 In-Memory Backend (Default)

```python
import asyncio
from typing import Any, Optional
from collections import OrderedDict
import time

class InMemoryCache(CacheBackend):
    """Thread-safe in-memory cache with TTL support.

    Uses an OrderedDict for LRU eviction when a max_size is reached.
    TTL is checked lazily on get; expired entries are removed then.
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
            if key in self._expires and time.time() > self._expires[key]:
                del self._store[key]
                del self._expires[key]
                return None
            return self._store[key]

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        async with self._lock:
            self._store[key] = value
            if ttl:
                self._expires[key] = time.time() + ttl
            else:
                self._expires.pop(key, None)
            # Simple eviction: if over max_size, remove oldest keys
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
        import fnmatch
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
```

### 2.3 Redis Backend (Optional)

```python
import redis.asyncio as redis
from typing import Any, Optional
import json

class RedisCache(CacheBackend):
    """Redis-backed cache backend.

    Requires ``redis>=5.0`` package. Falls back to in-memory if Redis
    connection fails at startup (with a warning).
    """

    def __init__(self, url: str = "redis://localhost:6379/0"):
        self._client = redis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> Optional[Any]:
        value = await self._client.get(key)
        if value is None:
            return None
        return json.loads(value)

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        serialized = json.dumps(value)
        if ttl:
            await self._client.setex(key, ttl, serialized)
        else:
            await self._client.set(key, serialized)

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def delete_pattern(self, pattern: str) -> None:
        keys = await self._client.keys(pattern)
        if keys:
            await self._client.delete(*keys)

    async def flush(self) -> None:
        await self._client.flushdb()
```

### 2.4 Factory

```python
import os
from typing import Optional

def create_cache_backend() -> CacheBackend:
    """Create a cache backend based on environment configuration.

    Environment variables:
        DOCMIND_CACHE_BACKEND: "memory" (default) or "redis"
        DOCMIND_CACHE_REDIS_URL: Redis connection URL
        DOCMIND_CACHE_MAX_SIZE: Max entries for in-memory cache (default 10000)
    """
    backend = os.environ.get("DOCMIND_CACHE_BACKEND", "memory").lower()
    if backend == "redis":
        url = os.environ.get("DOCMIND_CACHE_REDIS_URL", "redis://localhost:6379/0")
        return RedisCache(url)
    max_size = int(os.environ.get("DOCMIND_CACHE_MAX_SIZE", "10000"))
    return InMemoryCache(max_size=max_size)
```

---

## 3. Cache Key Strategy

### 3.1 Key Naming Convention

Cache keys follow a hierarchical, colon-delimited pattern:

```
<namespace>:<entity>:<operation>:<param_hash>
```

Where:
- `namespace`: `docmind` (top-level prefix to avoid collisions)
- `entity`: `doc`, `docs`, `search`, `tag`, `collection`, `analytics`, `stats`
- `operation`: `get`, `list`, `search`, `count`, `tree`, `dist`
- `param_hash`: Base32-encoded hash of query parameters (for uniqueness)

### 3.2 Key Patterns by Query Type

| Query Type | Key Pattern | Example |
|------------|-------------|---------|
| Single document | `docmind:doc:get:<doc_id>` | `docmind:doc:get:42` |
| Document list (paginated) | `docmind:docs:list:<source>:<collection_id>:<page>:<per_page>` | `docmind:docs:list::0:1:20` |
| Full-text search | `docmind:search:fts:<query_hash>:<limit>:<collection_id>` | `docmind:search:fts:abc123:20:None` |
| Tag list for doc | `docmind:tag:get:<doc_id>` | `docmind:tag:get:42` |
| All tags (cloud) | `docmind:tag:all` | `docmind:tag:all` |
| Documents by tag | `docmind:docs:by_tag:<tag>:<page>:<per_page>` | `docmind:docs:by_tag:python:1:20` |
| Collection tree | `docmind:collection:tree` | `docmind:collection:tree` |
| Collection counts | `docmind:collection:counts` | `docmind:collection:counts` |
| Single collection | `docmind:collection:get:<id>` | `docmind:collection:get:5` |
| Dashboard stats | `docmind:analytics:stats` | `docmind:analytics:stats` |
| Document growth | `docmind:analytics:growth:<days>` | `docmind:analytics:growth:30` |
| Tag distribution | `docmind:analytics:tag_dist` | `docmind:analytics:tag_dist` |
| Storage stats | `docmind:analytics:storage` | `docmind:analytics:storage` |
| Search stats | `docmind:analytics:search_stats:<days>` | `docmind:analytics:search_stats:30` |
| Popular queries | `docmind:analytics:popular:<limit>` | `docmind:analytics:popular:5` |
| Search trend | `docmind:analytics:search_trend:<days>` | `docmind:analytics:search_trend:30` |
| Chat activity | `docmind:analytics:chat_activity:<days>` | `docmind:analytics:chat_activity:30` |
| Job stats | `docmind:analytics:job_stats` | `docmind:analytics:job_stats` |
| Job list | `docmind:jobs:list:<state>:<page>:<per_page>` | `docmind:jobs:list:pending:1:20` |
| Job detail | `docmind:job:get:<job_id>` | `docmind:job:get:abc-123` |
| Settings | `docmind:settings:all` | `docmind:settings:all` |
| Chat sessions | `docmind:chat:sessions:<limit>` | `docmind:chat:sessions:50` |
| Chat messages | `docmind:chat:messages:<session_id>:<limit>` | `docmind:chat:messages:sess1:200` |

### 3.3 Param Hash for Complex Queries

For keys that include query parameters (search queries, filters), use a deterministic hash:

```python
import hashlib
import json

def _make_key(*parts: str) -> str:
    """Build a cache key from ordered parts."""
    return ":".join(parts)

def _hash_params(**kwargs) -> str:
    """Create a short deterministic hash of keyword arguments."""
    serialized = json.dumps(kwargs, sort_keys=True, ensure_ascii=True)
    return hashlib.blake2b(serialized.encode(), digest_size=8).hexdigest()[:16]
```

---

## 4. TTL Policy per Query Type

| Query Type | TTL | Rationale |
|------------|-----|-----------|
| Single document | 60s | Frequently accessed; short TTL balances freshness and performance |
| Document list | 30s | Lists change often; keep short |
| Full-text search | 120s | Search results are expensive to compute; moderate TTL |
| Tag lists | 300s | Tags change infrequently per document |
| All tags (cloud) | 600s | Tag cloud is relatively stable |
| Documents by tag | 60s | Filtered lists; moderate TTL |
| Collection tree | 600s | Collections are modified rarely |
| Collection counts | 300s | Counts change with document mutations |
| Single collection | 600s | Collections are modified rarely |
| Dashboard stats | 60s | Stats change with every document mutation |
| Document growth | 300s | Historical data; moderately stable |
| Tag distribution | 600s | Tag distribution changes slowly |
| Storage stats | 300s | Storage changes with uploads/deletes |
| Search stats | 300s | Search analytics; moderately stable |
| Popular queries | 600s | Query trends change slowly |
| Search trend | 300s | Trend data; moderately stable |
| Chat activity | 300s | Chat analytics; moderately stable |
| Job stats | 60s | Jobs are dynamic; keep short |
| Job list | 30s | Jobs change state frequently |
| Job detail | 60s | Individual job status changes |
| Settings | 600s | Settings are modified rarely |
| Chat sessions | 60s | Sessions are created/updated frequently |
| Chat messages | 60s | Messages are appended frequently |

### 4.1 TTL Configuration

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class CacheTTLConfig:
    """TTL values in seconds for each cache category."""
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
```

---

## 5. Cache Invalidation Plan

### 5.1 Principle

**Every mutation path must explicitly invalidate all cache keys that could be affected by the change.** The cache is not automatically synchronized with the database.

### 5.2 Mutation Paths and Invalidation

#### 5.2.1 Document Upload (upsert_document)

**Mutations:**
- Creates or updates a document
- May trigger summary generation

**Invalidations:**
```python
await cache.delete_pattern("docmind:docs:list:*")
await cache.delete_pattern("docmind:search:fts:*")
await cache.delete("docmind:analytics:stats")
await cache.delete("docmind:analytics:storage")
await cache.delete("docmind:analytics:tag_dist")
await cache.delete_pattern("docmind:analytics:growth:*")
await cache.delete_pattern("docmind:collection:counts")
await cache.delete_pattern("docmind:tag:all")
```

#### 5.2.2 Document Delete (delete_document)

**Mutations:**
- Deletes a single document by ID
- Cascade-deletes chunks and embeddings

**Invalidations:**
```python
await cache.delete(f"docmind:doc:get:{doc_id}")
await cache.delete_pattern("docmind:docs:list:*")
await cache.delete_pattern("docmind:search:fts:*")
await cache.delete(f"docmind:tag:get:{doc_id}")
await cache.delete_pattern("docmind:docs:by_tag:*")
await cache.delete("docmind:analytics:stats")
await cache.delete("docmind:analytics:storage")
await cache.delete("docmind:analytics:tag_dist")
await cache.delete_pattern("docmind:analytics:growth:*")
await cache.delete_pattern("docmind:collection:counts")
await cache.delete_pattern("docmind:tag:all")
```

#### 5.2.3 Bulk Delete (bulk_delete_documents)

**Mutations:**
- Deletes multiple documents by ID

**Invalidations:**
```python
for doc_id in deleted_ids:
    await cache.delete(f"docmind:doc:get:{doc_id}")
    await cache.delete(f"docmind:tag:get:{doc_id}")
await cache.delete_pattern("docmind:docs:list:*")
await cache.delete_pattern("docmind:search:fts:*")
await cache.delete_pattern("docmind:docs:by_tag:*")
await cache.delete("docmind:analytics:stats")
await cache.delete("docmind:analytics:storage")
await cache.delete("docmind:analytics:tag_dist")
await cache.delete_pattern("docmind:analytics:growth:*")
await cache.delete_pattern("docmind:collection:counts")
await cache.delete_pattern("docmind:tag:all")
```

#### 5.2.4 Tag Add (add_tag)

**Mutations:**
- Adds a tag to a document

**Invalidations:**
```python
await cache.delete(f"docmind:tag:get:{doc_id}")
await cache.delete("docmind:tag:all")
await cache.delete_pattern("docmind:docs:by_tag:*")
await cache.delete("docmind:analytics:tag_dist")
await cache.delete("docmind:analytics:stats")
```

#### 5.2.5 Tag Remove (remove_tag)

**Mutations:**
- Removes a tag from a document

**Invalidations:**
```python
await cache.delete(f"docmind:tag:get:{doc_id}")
await cache.delete("docmind:tag:all")
await cache.delete_pattern("docmind:docs:by_tag:*")
await cache.delete("docmind:analytics:tag_dist")
await cache.delete("docmind:analytics:stats")
```

#### 5.2.6 Summary Update (update_summary)

**Mutations:**
- Updates a document's summary and status

**Invalidations:**
```python
await cache.delete(f"docmind:doc:get:{doc_id}")
await cache.delete_pattern("docmind:search:fts:*")
await cache.delete("docmind:analytics:stats")
```

#### 5.2.7 Collection Create (create_collection)

**Mutations:**
- Creates a new collection

**Invalidations:**
```python
await cache.delete("docmind:collection:tree")
await cache.delete_pattern("docmind:collection:get:*")
await cache.delete("docmind:analytics:stats")
```

#### 5.2.8 Collection Update (update_collection)

**Mutations:**
- Updates a collection's name, description, or parent

**Invalidations:**
```python
await cache.delete(f"docmind:collection:get:{collection_id}")
await cache.delete("docmind:collection:tree")
await cache.delete_pattern("docmind:docs:list:*")
await cache.delete("docmind:analytics:stats")
```

#### 5.2.9 Collection Delete (delete_collection)

**Mutations:**
- Deletes a collection and unassigns its documents

**Invalidations:**
```python
await cache.delete(f"docmind:collection:get:{collection_id}")
await cache.delete("docmind:collection:tree")
await cache.delete_pattern("docmind:collection:counts")
await cache.delete_pattern("docmind:docs:list:*")
await cache.delete_pattern("docmind:search:fts:*")
await cache.delete("docmind:analytics:stats")
await cache.delete("docmind:analytics:storage")
```

#### 5.2.10 Assign Document to Collection (assign_document_to_collection)

**Mutations:**
- Sets a document's collection_id

**Invalidations:**
```python
await cache.delete(f"docmind:doc:get:{doc_id}")
await cache.delete_pattern("docmind:docs:list:*")
await cache.delete_pattern("docmind:collection:counts")
await cache.delete("docmind:analytics:stats")
```

#### 5.2.11 Remove Document from Collection (remove_document_from_collection)

**Mutations:**
- Sets a document's collection_id to NULL

**Invalidations:**
```python
await cache.delete(f"docmind:doc:get:{doc_id}")
await cache.delete_pattern("docmind:docs:list:*")
await cache.delete_pattern("docmind:collection:counts")
await cache.delete("docmind:analytics:stats")
```

#### 5.2.12 Settings Update (set_setting / settings_save)

**Mutations:**
- Updates application settings

**Invalidations:**
```python
await cache.delete("docmind:settings:all")
```

#### 5.2.13 Chat Session Mutations

**Mutations:**
- Create, delete chat sessions
- Add messages

**Invalidations:**
```python
# After creating/deleting a session:
await cache.delete_pattern("docmind:chat:sessions:*")

# After adding a message:
await cache.delete(f"docmind:chat:messages:{session_id}:*")
await cache.delete_pattern("docmind:chat:sessions:*")
await cache.delete("docmind:analytics:chat_activity")
```

#### 5.2.14 Job State Changes

**Mutations:**
- Job creation, state transitions

**Invalidations:**
```python
# After job creation/state change:
await cache.delete_pattern("docmind:jobs:list:*")
await cache.delete(f"docmind:job:get:{job_id}")
await cache.delete("docmind:analytics:job_stats")
await cache.delete("docmind:analytics:stats")
```

---

## 6. Integration Points

### 6.1 Database Layer Integration

The cache should be integrated at the `Database` class level, not in `server.py`. This keeps the caching transparent to route handlers.

```python
class Database:
    def __init__(self, ..., cache: Optional[CacheBackend] = None):
        self._cache = cache or create_cache_backend()
        # ...

    async def get_document(self, doc_id: int) -> Optional[dict]:
        key = f"docmind:doc:get:{doc_id}"
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        # ... fetch from DB ...
        await self._cache.set(key, result, ttl=CacheTTLConfig.doc_single)
        return result
```

### 6.2 Configuration

Add to `config.py`:

```python
@dataclass
class CacheConfig:
    """Cache layer settings."""
    backend: str = field(
        default_factory=lambda: _env("DOCMIND_CACHE_BACKEND", "memory")
    )
    redis_url: str = field(
        default_factory=lambda: _env("DOCMIND_CACHE_REDIS_URL", "redis://localhost:6379/0")
    )
    max_size: int = field(
        default_factory=lambda: _env_int("DOCMIND_CACHE_MAX_SIZE", 10000)
    )
    enabled: bool = field(
        default_factory=lambda: _env_bool("DOCMIND_CACHE_ENABLED", True)
    )
```

### 6.3 Dependency

Redis support is **optional**. The application must work without `redis` installed. Use lazy imports:

```python
def _import_redis():
    try:
        import redis.asyncio as redis
        return redis
    except ImportError:
        raise ImportError(
            "Redis cache backend requires 'redis' package. "
            "Install with: pip install redis"
        )
```

---

## 7. Acceptance Criteria

1. **Interface implemented**: `CacheBackend` ABC with `InMemoryCache` and `RedisCache` implementations.
2. **Zero-config default**: In-memory cache works without any external dependencies or configuration.
3. **Cache key strategy**: All 24 query types have documented key patterns.
4. **TTL policy**: Each query type has a defined TTL between 30s and 600s.
5. **Invalidation coverage**: All 14 mutation paths have explicit invalidation rules.
6. **Integration**: Cache is wired into `Database` class, not route handlers.
7. **Tests**: Unit tests for both backends, integration tests for cache hit/miss, TTL expiry, and invalidation.
8. **Documentation**: This spec is committed to `docs/architecture/caching.md`.

---

## 8. Open Questions

1. Should we implement cache warming (pre-populate on startup)?
2. Should we add cache metrics (hit rate, miss rate) for observability?
3. Should we support cache stampede protection (e.g., single-flight for hot keys)?

---

## 9. Appendix: Query-to-Key Mapping

| # | Query | Cache Key | TTL |
|---|-------|-----------|-----|
| 1 | `get_document(doc_id)` | `docmind:doc:get:{doc_id}` | 60s |
| 2 | `list_documents_paginated(...)` | `docmind:docs:list:{source}:{collection_id}:{page}:{per_page}` | 30s |
| 3 | `search_documents(query, ...)` | `docmind:search:fts:{hash}:{limit}:{collection_id}` | 120s |
| 4 | `get_tags(doc_id)` | `docmind:tag:get:{doc_id}` | 300s |
| 5 | `get_all_tags()` | `docmind:tag:all` | 600s |
| 6 | `get_documents_by_tag(tag)` | `docmind:docs:by_tag:{tag}:{page}:{per_page}` | 60s |
| 7 | `list_collections_tree()` | `docmind:collection:tree` | 600s |
| 8 | `get_collection_counts()` | `docmind:collection:counts` | 300s |
| 9 | `get_collection(id)` | `docmind:collection:get:{id}` | 600s |
| 10 | `get_stats()` | `docmind:analytics:stats` | 60s |
| 11 | `get_document_growth(days)` | `docmind:analytics:growth:{days}` | 300s |
| 12 | `get_tag_distribution()` | `docmind:analytics:tag_dist` | 600s |
| 13 | `get_storage_stats()` | `docmind:analytics:storage` | 300s |
| 14 | `get_search_stats(days)` | `docmind:analytics:search_stats:{days}` | 300s |
| 15 | `get_popular_queries(limit)` | `docmind:analytics:popular:{limit}` | 600s |
| 16 | `get_search_trend(days)` | `docmind:analytics:search_trend:{days}` | 300s |
| 17 | `get_chat_activity(days)` | `docmind:analytics:chat_activity:{days}` | 300s |
| 18 | `get_job_stats()` | `docmind:analytics:job_stats` | 60s |
| 19 | `list_jobs_paginated(...)` | `docmind:jobs:list:{state}:{page}:{per_page}` | 30s |
| 20 | `get_job(job_id)` | `docmind:job:get:{job_id}` | 60s |
| 21 | `get_all_settings()` | `docmind:settings:all` | 600s |
| 22 | `list_chat_sessions(limit)` | `docmind:chat:sessions:{limit}` | 60s |
| 23 | `get_chat_history(session_id, limit)` | `docmind:chat:messages:{session_id}:{limit}` | 60s |
| 24 | `get_document_by_path(path)` | `docmind:doc:by_path:{path_hash}` | 60s |
