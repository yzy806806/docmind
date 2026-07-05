"""Tests for src.core.cache — cache abstraction layer.

Covers:
- InMemoryCache: get/set/delete/delete_pattern/flush, TTL expiry, max_size eviction
- NoopCache: all operations are no-ops
- Factory: create_cache_backend with env vars
- Key helpers: make_key, hash_params
- CacheTTLConfig: default values
- Database integration: cache hit/miss, TTL expiry, invalidation on mutations
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path
from typing import Generator

import pytest


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    """Provide a temporary database file path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "test_cache.db")


@pytest.fixture
async def cache():
    """Create an InMemoryCache for testing."""
    from src.core.cache import InMemoryCache

    c = InMemoryCache(max_size=100)
    yield c
    await c.flush()


@pytest.fixture
async def db(tmp_db_path: str):
    """Create a connected Database with an InMemoryCache for testing."""
    from src.core.cache import InMemoryCache
    from src.core.db_sqlite import Database

    test_cache = InMemoryCache(max_size=100)
    database = Database(db_path=tmp_db_path, cache=test_cache)
    await database.connect()
    # Provide access to the cache for assertions
    database._test_cache = test_cache
    yield database
    await database.disconnect()


# ── InMemoryCache Unit Tests ─────────────────────────────────────


class TestInMemoryCache:
    """Unit tests for InMemoryCache backend."""

    async def test_set_and_get(self, cache):
        """Basic set/get round-trip."""
        await cache.set("key1", "value1")
        result = await cache.get("key1")
        assert result == "value1"

    async def test_get_missing_key(self, cache):
        """get returns None for missing keys."""
        result = await cache.get("nonexistent")
        assert result is None

    async def test_set_with_ttl(self, cache):
        """TTL-based expiry works."""
        await cache.set("ephemeral", "data", ttl=0)
        # Immediately expired (ttl=0 means no TTL per the implementation,
        # but let's test with a very short TTL)
        await cache.set("ephemeral2", "data", ttl=1)
        assert await cache.get("ephemeral2") == "data"
        await asyncio.sleep(1.1)
        result = await cache.get("ephemeral2")
        assert result is None

    async def test_set_overwrites_existing(self, cache):
        """Setting an existing key overwrites the value."""
        await cache.set("key", "v1")
        await cache.set("key", "v2")
        assert await cache.get("key") == "v2"

    async def test_set_without_ttl_persists(self, cache):
        """Keys without TTL don't expire."""
        await cache.set("persistent", "value")
        await asyncio.sleep(0.1)
        assert await cache.get("persistent") == "value"

    async def test_delete(self, cache):
        """delete removes a key."""
        await cache.set("key", "value")
        await cache.delete("key")
        assert await cache.get("key") is None

    async def test_delete_missing_key(self, cache):
        """delete on a missing key is a no-op (no error)."""
        await cache.delete("nonexistent")  # should not raise

    async def test_delete_pattern(self, cache):
        """delete_pattern removes all matching keys."""
        await cache.set("docmind:docs:list:1", "a")
        await cache.set("docmind:docs:list:2", "b")
        await cache.set("docmind:doc:get:1", "c")
        await cache.delete_pattern("docmind:docs:list:*")
        assert await cache.get("docmind:docs:list:1") is None
        assert await cache.get("docmind:docs:list:2") is None
        assert await cache.get("docmind:doc:get:1") == "c"

    async def test_delete_pattern_no_matches(self, cache):
        """delete_pattern with no matches is a no-op."""
        await cache.set("key", "value")
        await cache.delete_pattern("nonexistent:*")
        assert await cache.get("key") == "value"

    async def test_flush(self, cache):
        """flush clears all keys."""
        await cache.set("k1", "v1")
        await cache.set("k2", "v2")
        await cache.flush()
        assert await cache.get("k1") is None
        assert await cache.get("k2") is None

    async def test_max_size_eviction(self):
        """Oldest keys are evicted when max_size is exceeded."""
        from src.core.cache import InMemoryCache

        c = InMemoryCache(max_size=3)
        await c.set("k1", "v1")
        await c.set("k2", "v2")
        await c.set("k3", "v3")
        await c.set("k4", "v4")  # should evict k1
        assert await c.get("k1") is None
        assert await c.get("k2") == "v2"
        assert await c.get("k3") == "v3"
        assert await c.get("k4") == "v4"
        assert await c.size() == 3

    async def test_complex_values(self, cache):
        """Cache can store complex Python objects (dicts, lists)."""
        data = {"nested": {"list": [1, 2, 3], "str": "hello"}}
        await cache.set("complex", data)
        result = await cache.get("complex")
        assert result == data
        assert result["nested"]["list"] == [1, 2, 3]

    async def test_ttl_expiry_lazy(self, cache):
        """Expired keys are removed lazily on get."""
        await cache.set("temp", "value", ttl=1)
        assert await cache.get("temp") == "value"
        await asyncio.sleep(1.1)
        # First get returns None and cleans up
        result = await cache.get("temp")
        assert result is None


# ── NoopCache Tests ──────────────────────────────────────────────


class TestNoopCache:
    """Unit tests for NoopCache backend."""

    async def test_noop_get_always_none(self):
        from src.core.cache import NoopCache

        c = NoopCache()
        assert await c.get("anything") is None

    async def test_noop_set_does_nothing(self):
        from src.core.cache import NoopCache

        c = NoopCache()
        await c.set("key", "value", ttl=60)
        assert await c.get("key") is None

    async def test_noop_delete(self):
        from src.core.cache import NoopCache

        c = NoopCache()
        await c.delete("key")  # should not raise

    async def test_noop_delete_pattern(self):
        from src.core.cache import NoopCache

        c = NoopCache()
        await c.delete_pattern("pattern:*")  # should not raise

    async def test_noop_flush(self):
        from src.core.cache import NoopCache

        c = NoopCache()
        await c.flush()  # should not raise


# ── Factory Tests ────────────────────────────────────────────────


class TestCreateCacheBackend:
    """Tests for the create_cache_backend factory."""

    def test_default_memory(self):
        """Default backend is InMemoryCache."""
        from src.core.cache import InMemoryCache, create_cache_backend

        # Clear env vars that might interfere
        env_backup = {
            k: os.environ.pop(k, None)
            for k in [
                "DOCMIND_CACHE_BACKEND",
                "DOCMIND_CACHE_ENABLED",
                "DOCMIND_CACHE_MAX_SIZE",
                "DOCMIND_CACHE_REDIS_URL",
            ]
        }
        try:
            backend = create_cache_backend()
            assert isinstance(backend, InMemoryCache)
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v

    def test_disabled_returns_noop(self):
        """When enabled=False, returns NoopCache."""
        from src.core.cache import NoopCache, create_cache_backend

        backend = create_cache_backend(enabled=False)
        assert isinstance(backend, NoopCache)

    def test_explicit_memory(self):
        """Explicit backend='memory' returns InMemoryCache."""
        from src.core.cache import InMemoryCache, create_cache_backend

        backend = create_cache_backend(backend="memory", enabled=True)
        assert isinstance(backend, InMemoryCache)

    def test_redis_backend(self):
        """backend='redis' returns RedisCache (without connecting)."""
        from src.core.cache import RedisCache, create_cache_backend

        backend = create_cache_backend(
            backend="redis", enabled=True, redis_url="redis://localhost:6379/0"
        )
        assert isinstance(backend, RedisCache)

    def test_max_size_from_env(self):
        """max_size is read from environment."""
        from src.core.cache import InMemoryCache, create_cache_backend

        env_backup = {
            k: os.environ.pop(k, None)
            for k in [
                "DOCMIND_CACHE_BACKEND",
                "DOCMIND_CACHE_ENABLED",
                "DOCMIND_CACHE_MAX_SIZE",
            ]
        }
        try:
            os.environ["DOCMIND_CACHE_MAX_SIZE"] = "500"
            os.environ["DOCMIND_CACHE_ENABLED"] = "true"
            backend = create_cache_backend()
            assert isinstance(backend, InMemoryCache)
            assert backend._max_size == 500
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                elif k in os.environ:
                    del os.environ[k]


# ── Key Helper Tests ─────────────────────────────────────────────


class TestKeyHelpers:
    """Tests for make_key and hash_params."""

    def test_make_key_simple(self):
        from src.core.cache import make_key

        assert make_key("a", "b", "c") == "a:b:c"

    def test_make_key_with_int(self):
        from src.core.cache import make_key

        assert make_key("docmind", "doc", "get", 42) == "docmind:doc:get:42"

    def test_make_key_single(self):
        from src.core.cache import make_key

        assert make_key("only") == "only"

    def test_hash_params_deterministic(self):
        from src.core.cache import hash_params

        h1 = hash_params(a=1, b="hello")
        h2 = hash_params(a=1, b="hello")
        assert h1 == h2

    def test_hash_params_order_independent(self):
        from src.core.cache import hash_params

        h1 = hash_params(a=1, b="hello")
        h2 = hash_params(b="hello", a=1)
        assert h1 == h2

    def test_hash_params_different_values(self):
        from src.core.cache import hash_params

        h1 = hash_params(a=1)
        h2 = hash_params(a=2)
        assert h1 != h2

    def test_hash_params_length(self):
        from src.core.cache import hash_params

        h = hash_params(query="test")
        assert len(h) == 16


# ── CacheTTLConfig Tests ─────────────────────────────────────────


class TestCacheTTLConfig:
    """Tests for CacheTTLConfig default values."""

    def test_default_ttls(self):
        from src.core.cache import CacheTTLConfig

        config = CacheTTLConfig()
        assert config.doc_single == 60
        assert config.doc_list == 30
        assert config.search == 120
        assert config.tag_cloud == 600
        assert config.dashboard_stats == 60
        assert config.settings == 600

    def test_ttls_in_valid_range(self):
        from src.core.cache import CacheTTLConfig

        config = CacheTTLConfig()
        # All TTLs should be between 30 and 600 seconds
        for attr in dir(config):
            if attr.startswith("_"):
                continue
            val = getattr(config, attr)
            if isinstance(val, int) and not attr.startswith("__"):
                assert 30 <= val <= 600, f"{attr}={val} not in [30, 600]"


# ── Database Cache Integration Tests ─────────────────────────────


class TestDatabaseCacheIntegration:
    """Integration tests for cache-aside pattern in Database."""

    async def test_document_cache_hit(self, db):
        """get_document caches results on first fetch."""
        doc_id = await db.save_document(
            path="/test/doc.txt",
            source_type="local",
            source_name="test",
            title="Test Doc",
            ext=".txt",
            mime_type="text/plain",
            body="Hello world",
        )
        # First fetch — miss, populates cache
        doc1 = await db.get_document(doc_id)
        assert doc1 is not None
        assert doc1["title"] == "Test Doc"

        # Check cache was populated
        cached = await db._test_cache.get(f"docmind:doc:get:{doc_id}")
        assert cached is not None
        assert cached["title"] == "Test Doc"

        # Second fetch — should come from cache
        doc2 = await db.get_document(doc_id)
        assert doc2 is not None
        assert doc2["title"] == "Test Doc"

    async def test_document_cache_invalidation_on_delete(self, db):
        """delete_document invalidates the cache."""
        doc_id = await db.save_document(
            path="/test/delete.txt",
            source_type="local",
            source_name="test",
            title="Delete Me",
            ext=".txt",
            mime_type="text/plain",
            body="Goodbye",
        )
        # Populate cache
        await db.get_document(doc_id)
        assert await db._test_cache.get(f"docmind:doc:get:{doc_id}") is not None

        # Delete
        await db.delete_document(doc_id)
        # Cache should be invalidated
        assert await db._test_cache.get(f"docmind:doc:get:{doc_id}") is None

    async def test_document_cache_invalidation_on_summary(self, db):
        """update_summary invalidates the document cache."""
        doc_id = await db.save_document(
            path="/test/summary.txt",
            source_type="local",
            source_name="test",
            title="Summary Test",
            ext=".txt",
            mime_type="text/plain",
            body="Content",
        )
        await db.get_document(doc_id)  # populate cache
        assert await db._test_cache.get(f"docmind:doc:get:{doc_id}") is not None

        await db.update_summary(doc_id, "New summary")
        assert await db._test_cache.get(f"docmind:doc:get:{doc_id}") is None

    async def test_stats_cache(self, db):
        """get_stats caches results."""
        await db.save_document(
            path="/test/stats.txt",
            source_type="local",
            source_name="test",
            title="Stats",
            ext=".txt",
            mime_type="text/plain",
            body="x",
        )
        await db.get_stats()
        assert await db._test_cache.get("docmind:analytics:stats") is not None

    async def test_stats_invalidation_on_document_save(self, db):
        """save_document invalidates stats cache."""
        await db.save_document(
            path="/test/stats_inv1.txt",
            source_type="local",
            source_name="test",
            title="Doc1",
            ext=".txt",
            mime_type="text/plain",
            body="x",
        )
        await db.get_stats()  # populate cache
        assert await db._test_cache.get("docmind:analytics:stats") is not None

        await db.save_document(
            path="/test/stats_inv2.txt",
            source_type="local",
            source_name="test",
            title="Doc2",
            ext=".txt",
            mime_type="text/plain",
            body="y",
        )
        assert await db._test_cache.get("docmind:analytics:stats") is None

    async def test_search_cache(self, db):
        """search_documents caches results."""
        await db.save_document(
            path="/test/search.txt",
            source_type="local",
            source_name="test",
            title="Searchable",
            ext=".txt",
            mime_type="text/plain",
            body="unique search term",
        )
        await db.search_documents("unique")
        # Check that some search key was cached
        # (We can't predict the exact key due to hashing, so check store size)
        assert await db._test_cache.size() > 0

    async def test_tag_cache_invalidation(self, db):
        """add_tag and remove_tag invalidate tag caches."""
        doc_id = await db.save_document(
            path="/test/tags.txt",
            source_type="local",
            source_name="test",
            title="Tagged",
            ext=".txt",
            mime_type="text/plain",
            body="x",
        )
        await db.get_tags(doc_id)  # populate cache
        assert await db._test_cache.get(f"docmind:tag:get:{doc_id}") is not None

        await db.add_tag(doc_id, "python")
        assert await db._test_cache.get(f"docmind:tag:get:{doc_id}") is None

        # Re-populate and test remove
        await db.get_tags(doc_id)
        assert await db._test_cache.get(f"docmind:tag:get:{doc_id}") is not None

        await db.remove_tag(doc_id, "python")
        assert await db._test_cache.get(f"docmind:tag:get:{doc_id}") is None

    async def test_all_tags_cache(self, db):
        """get_all_tags caches results."""
        doc_id = await db.save_document(
            path="/test/alltags.txt",
            source_type="local",
            source_name="test",
            title="AllTags",
            ext=".txt",
            mime_type="text/plain",
            body="x",
        )
        await db.add_tag(doc_id, "tag1")
        await db.get_all_tags()
        assert await db._test_cache.get("docmind:tag:all") is not None

    async def test_collection_cache(self, db):
        """Collection reads are cached."""
        col_id = await db.create_collection("TestCol")
        await db.get_collection(col_id)
        assert await db._test_cache.get(f"docmind:collection:get:{col_id}") is not None

        await db.update_collection(col_id, name="UpdatedCol")
        assert await db._test_cache.get(f"docmind:collection:get:{col_id}") is None

    async def test_collection_tree_cache(self, db):
        """list_collections_tree is cached."""
        await db.create_collection("TreeCol")
        await db.list_collections_tree()
        assert await db._test_cache.get("docmind:collection:tree") is not None

        await db.create_collection("NewCol2")
        assert await db._test_cache.get("docmind:collection:tree") is None

    async def test_settings_cache(self, db):
        """get_all_settings caches results, set_setting invalidates."""
        await db.set_setting("key1", "value1")
        await db.get_all_settings()
        assert await db._test_cache.get("docmind:settings:all") is not None

        await db.set_setting("key2", "value2")
        assert await db._test_cache.get("docmind:settings:all") is None

    async def test_settings_cache_invalidation_on_delete(self, db):
        """delete_setting invalidates settings cache."""
        await db.set_setting("deletable", "yes")
        await db.get_all_settings()
        assert await db._test_cache.get("docmind:settings:all") is not None

        await db.delete_setting("deletable")
        assert await db._test_cache.get("docmind:settings:all") is None

    async def test_job_cache_invalidation(self, db):
        """Job state changes invalidate job caches."""
        job = await db.create_job("/test/job.txt", document_title="JobDoc")
        await db.get_job(job.id)
        assert await db._test_cache.get(f"docmind:job:get:{job.id}") is not None

        await db.update_job_status(job.id, "processing")
        assert await db._test_cache.get(f"docmind:job:get:{job.id}") is None

    async def test_chat_cache_invalidation(self, db):
        """Chat mutations invalidate chat caches."""
        session = await db.create_chat_session()
        # create_chat_session invalidates sessions cache
        assert await db._test_cache.get("docmind:chat:sessions:50") is None

        # Populate cache
        await db.list_chat_sessions()
        assert await db._test_cache.get("docmind:chat:sessions:50") is not None

        # save_chat_message should invalidate
        await db.save_chat_message(session["id"], "user", "Hello")
        assert await db._test_cache.get("docmind:chat:sessions:50") is None

        # Re-populate
        await db.list_chat_sessions()
        assert await db._test_cache.get("docmind:chat:sessions:50") is not None

        await db.delete_chat_session(session["id"])
        assert await db._test_cache.get("docmind:chat:sessions:50") is None

    async def test_document_list_cache(self, db):
        """list_documents_paginated caches results."""
        await db.save_document(
            path="/test/list1.txt",
            source_type="local",
            source_name="test",
            title="List1",
            ext=".txt",
            mime_type="text/plain",
            body="x",
        )
        await db.list_documents_paginated(page=1, per_page=20)
        # Should have cached something (key is hashed, so check size > 0)
        assert await db._test_cache.size() > 0

    async def test_assign_collection_invalidates(self, db):
        """assign_document_to_collection invalidates caches."""
        doc_id = await db.save_document(
            path="/test/assign.txt",
            source_type="local",
            source_name="test",
            title="Assign",
            ext=".txt",
            mime_type="text/plain",
            body="x",
        )
        col_id = await db.create_collection("AssignCol")
        await db.get_document(doc_id)  # populate cache
        assert await db._test_cache.get(f"docmind:doc:get:{doc_id}") is not None

        await db.assign_document_to_collection(doc_id, col_id)
        assert await db._test_cache.get(f"docmind:doc:get:{doc_id}") is None

    async def test_remove_from_collection_invalidates(self, db):
        """remove_document_from_collection invalidates caches."""
        doc_id = await db.save_document(
            path="/test/remove.txt",
            source_type="local",
            source_name="test",
            title="Remove",
            ext=".txt",
            mime_type="text/plain",
            body="x",
        )
        col_id = await db.create_collection("RemoveCol")
        await db.assign_document_to_collection(doc_id, col_id)
        await db.get_document(doc_id)  # populate cache
        assert await db._test_cache.get(f"docmind:doc:get:{doc_id}") is not None

        await db.remove_document_from_collection(doc_id)
        assert await db._test_cache.get(f"docmind:doc:get:{doc_id}") is None

    async def test_delete_collection_invalidates(self, db):
        """delete_collection invalidates collection caches."""
        col_id = await db.create_collection("DeleteCol")
        await db.get_collection(col_id)
        assert await db._test_cache.get(f"docmind:collection:get:{col_id}") is not None

        await db.delete_collection(col_id)
        assert await db._test_cache.get(f"docmind:collection:get:{col_id}") is None

    async def test_storage_stats_cache(self, db):
        """get_storage_stats caches results."""
        await db.save_document(
            path="/test/storage.txt",
            source_type="local",
            source_name="test",
            title="Storage",
            ext=".txt",
            mime_type="text/plain",
            body="x",
            size=100,
        )
        await db.get_storage_stats()
        assert await db._test_cache.get("docmind:analytics:storage") is not None

        # Mutate a document — should invalidate storage stats
        await db.save_document(
            path="/test/storage2.txt",
            source_type="local",
            source_name="test",
            title="Storage2",
            ext=".txt",
            mime_type="text/plain",
            body="y",
            size=200,
        )
        assert await db._test_cache.get("docmind:analytics:storage") is None

    async def test_job_stats_cache(self, db):
        """get_job_stats caches results."""
        await db.create_job("/test/jobstats.txt", document_title="JobStats")
        await db.get_job_stats()
        assert await db._test_cache.get("docmind:analytics:job_stats") is not None

    async def test_facets_cache(self, db):
        """File type and source facets are cached."""
        await db.save_document(
            path="/test/facets.txt",
            source_type="local",
            source_name="test",
            title="Facets",
            ext=".txt",
            mime_type="text/plain",
            body="x",
        )
        await db.get_file_type_facets()
        assert await db._test_cache.get("docmind:analytics:file_type_facets") is not None

        await db.get_source_facets()
        assert await db._test_cache.get("docmind:analytics:source_facets") is not None

    async def test_chat_activity_cache(self, db):
        """get_chat_activity caches results."""
        session = await db.create_chat_session()
        await db.save_chat_message(session["id"], "user", "test")
        await db.get_chat_activity(30)
        assert await db._test_cache.get("docmind:analytics:chat_activity:30") is not None


# ── Config Integration Tests ─────────────────────────────────────


class TestCacheConfig:
    """Tests for CacheConfig in config.py."""

    def test_cache_config_defaults(self):
        from src.core.config import CacheConfig

        config = CacheConfig()
        assert config.enabled is True
        assert config.backend == "memory"
        assert config.max_size == 10000
        assert "redis://localhost:6379/0" in config.redis_url

    def test_cache_config_in_main_config(self):
        from src.core.config import Config

        config = Config()
        assert hasattr(config, "cache")
        assert config.cache.enabled is True
        assert config.cache.backend == "memory"

    def test_cache_config_env_override(self):
        """CacheConfig reads from environment variables."""
        from src.core.config import CacheConfig

        env_backup = {
            k: os.environ.pop(k, None)
            for k in ["DOCMIND_CACHE_ENABLED", "DOCMIND_CACHE_BACKEND", "DOCMIND_CACHE_MAX_SIZE"]
        }
        try:
            os.environ["DOCMIND_CACHE_ENABLED"] = "false"
            os.environ["DOCMIND_CACHE_BACKEND"] = "redis"
            os.environ["DOCMIND_CACHE_MAX_SIZE"] = "5000"
            config = CacheConfig()
            assert config.enabled is False
            assert config.backend == "redis"
            assert config.max_size == 5000
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                elif k in os.environ:
                    del os.environ[k]
