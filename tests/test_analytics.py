"""Tests for analytics dashboard: search logging, analytics methods, and web routes.

Covers:
- Search log table CRUD (log_search, get_search_stats, get_popular_queries, get_search_trend)
- Document growth analytics (get_document_growth)
- Tag distribution (get_tag_distribution)
- Storage stats (get_storage_stats)
- Chat activity (get_chat_activity)
- Job statistics (get_job_stats)
- Dashboard page rendering with charts
- Analytics page rendering with date range
- JSON API endpoint GET /api/v1/analytics
- Search route logs searches
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Generator

import pytest


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    """Provide a temporary database file path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "test_analytics.db")


@pytest.fixture
async def db(tmp_db_path: str):
    """Create a connected SQLite Database instance."""
    from src.core.db_sqlite import Database

    database = Database(db_path=tmp_db_path)
    await database.connect()
    yield database
    await database.disconnect()


@pytest.fixture
async def asgi_client(tmp_db_path: str):
    """Create an httpx.AsyncClient backed by the ASGI app."""
    import httpx
    from src.core.db_sqlite import Database
    from src.web import server

    db = Database(db_path=tmp_db_path)
    await db.connect()

    # Insert test documents with various types and statuses
    for i in range(10):
        await db.save_document(
            path=f"/docs/test_{i}.txt",
            source_type="api",
            source_name="test-source",
            title=f"Test Document {i}",
            ext=".txt",
            mime_type="text/plain",
            body=f"Body of document {i} with searchable text.",
            size=1000 + i * 100,
            status="indexed" if i % 2 == 0 else "pending",
        )
    for i in range(5):
        await db.save_document(
            path=f"/docs/report_{i}.pdf",
            source_type="api",
            source_name="test-source",
            title=f"PDF Report {i}",
            ext=".pdf",
            mime_type="application/pdf",
            body=f"PDF content {i} important data.",
            size=5000 + i * 500,
            status="summarized",
        )

    # Add tags
    for i in range(5):
        await db.add_tag(doc_id=i + 1, tag=f"tag-{i}")
    await db.add_tag(doc_id=1, tag="important")
    await db.add_tag(doc_id=2, tag="important")

    # Log some searches
    await db.log_search("test query", 5)
    await db.log_search("test query", 3)
    await db.log_search("pdf report", 2)
    await db.log_search("important data", 10)

    # Create chat sessions and messages
    session = await db.create_chat_session(title="Test Chat")
    await db.save_chat_message(session["id"], "user", "Hello there")
    await db.save_chat_message(session["id"], "assistant", "Hi! How can I help?")

    # Create some jobs — create_job always starts as 'pending',
    # then complete_job / fail_job change the state.
    job1 = await db.create_job(
        document_path="/docs/test_0.txt",
        document_title="Test Document 0",
    )
    # Document IDs start at 1; test_0.txt is the first doc saved
    await db.complete_job(job1.id, 1)

    job2 = await db.create_job(
        document_path="/docs/test_1.txt",
        document_title="Test Document 1",
    )
    await db.fail_job(job2.id, "Processing timeout")

    from unittest.mock import AsyncMock, MagicMock

    mock_queue = MagicMock()
    mock_queue.enqueue = AsyncMock(return_value=MagicMock(id="test-job-id"))

    original_db = server._db
    original_queue = server._queue
    server._db = db
    server._queue = mock_queue

    app = server.create_app()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await db.disconnect()
    server._db = original_db
    server._queue = original_queue


# ── Search Log CRUD Tests ────────────────────────────────────────


class TestSearchLog:
    """Tests for search logging methods."""

    async def test_log_search_returns_id(self, db):
        """log_search should return a positive row id."""
        log_id = await db.log_search("hello", 5)
        assert log_id > 0

    async def test_log_search_with_session(self, db):
        """log_search should accept an optional session parameter."""
        log_id = await db.log_search("world", 3, session="sess-123")
        assert log_id > 0

    async def test_log_search_zero_results(self, db):
        """log_search should handle zero results."""
        log_id = await db.log_search("nonexistent", 0)
        assert log_id > 0

    async def test_get_search_stats_empty(self, db):
        """get_search_stats should return zeros when no searches logged."""
        stats = await db.get_search_stats(days=30)
        assert stats["total_searches"] == 0
        assert stats["avg_results"] == 0.0
        assert stats["unique_queries"] == 0

    async def test_get_search_stats_with_data(self, db):
        """get_search_stats should return correct aggregate stats."""
        await db.log_search("alpha", 5)
        await db.log_search("alpha", 3)
        await db.log_search("beta", 10)

        stats = await db.get_search_stats(days=30)
        assert stats["total_searches"] == 3
        assert stats["unique_queries"] == 2
        assert stats["avg_results"] == 6.0  # (5+3+10)/3

    async def test_get_popular_queries_empty(self, db):
        """get_popular_queries should return empty list when no searches."""
        result = await db.get_popular_queries(limit=10)
        assert result == []

    async def test_get_popular_queries_with_data(self, db):
        """get_popular_queries should return queries sorted by count."""
        await db.log_search("popular", 5)
        await db.log_search("popular", 3)
        await db.log_search("popular", 2)
        await db.log_search("rare", 1)

        result = await db.get_popular_queries(limit=10)
        assert len(result) == 2
        assert result[0]["query"] == "popular"
        assert result[0]["count"] == 3
        assert result[1]["query"] == "rare"
        assert result[1]["count"] == 1

    async def test_get_popular_queries_limit(self, db):
        """get_popular_queries should respect the limit parameter."""
        for i in range(15):
            await db.log_search(f"query-{i}", 1)

        result = await db.get_popular_queries(limit=5)
        assert len(result) == 5

    async def test_get_search_trend_empty(self, db):
        """get_search_trend should return empty list when no searches."""
        result = await db.get_search_trend(days=30)
        assert result == []

    async def test_get_search_trend_with_data(self, db):
        """get_search_trend should return daily counts."""
        await db.log_search("today1", 1)
        await db.log_search("today2", 2)
        await db.log_search("today3", 3)

        result = await db.get_search_trend(days=30)
        assert len(result) >= 1
        # Today's searches should be grouped
        today_total = sum(r["count"] for r in result)
        assert today_total == 3


# ── Analytics Methods Tests ──────────────────────────────────────


class TestDocumentGrowth:
    """Tests for get_document_growth."""

    async def test_document_growth_with_data(self, db):
        """get_document_growth should return daily document counts."""
        for i in range(5):
            await db.save_document(
                path=f"/docs/growth_{i}.txt",
                source_type="api",
                source_name="test",
                title=f"Growth Doc {i}",
                ext=".txt",
                mime_type="text/plain",
                body="content",
                size=100,
                status="indexed",
            )

        result = await db.get_document_growth(days=30)
        assert len(result) >= 1
        total = sum(r["count"] for r in result)
        assert total >= 5
        # Each entry should have date and count
        for entry in result:
            assert "date" in entry
            assert "count" in entry
            assert isinstance(entry["count"], int)

    async def test_document_growth_empty(self, db):
        """get_document_growth should return empty list when no docs."""
        result = await db.get_document_growth(days=30)
        assert result == []


class TestTagDistribution:
    """Tests for get_tag_distribution."""

    async def test_tag_distribution_with_data(self, db):
        """get_tag_distribution should return tags with counts."""
        doc_id = await db.save_document(
            path="/docs/tagged.txt",
            source_type="api",
            source_name="test",
            title="Tagged Doc",
            ext=".txt",
            mime_type="text/plain",
            body="content",
            status="indexed",
        )
        await db.add_tag(doc_id, "python")
        await db.add_tag(doc_id, "web")
        await db.add_tag(doc_id, "python")  # duplicate, should be ignored

        result = await db.get_tag_distribution()
        tags = {r["tag"]: r["count"] for r in result}
        assert tags.get("python") == 1
        assert tags.get("web") == 1

    async def test_tag_distribution_empty(self, db):
        """get_tag_distribution should return empty list when no tags."""
        result = await db.get_tag_distribution()
        assert result == []


class TestStorageStats:
    """Tests for get_storage_stats."""

    async def test_storage_stats_with_data(self, db):
        """get_storage_stats should return correct storage info."""
        await db.save_document(
            path="/docs/big.txt",
            source_type="api",
            source_name="test",
            title="Big Doc",
            ext=".txt",
            mime_type="text/plain",
            body="x" * 100,
            size=10000,
            status="indexed",
        )
        await db.save_document(
            path="/docs/small.pdf",
            source_type="api",
            source_name="test",
            title="Small PDF",
            ext=".pdf",
            mime_type="application/pdf",
            body="y" * 50,
            size=5000,
            status="indexed",
        )

        result = await db.get_storage_stats()
        assert result["total_size"] == 15000
        assert result["doc_count"] == 2
        assert result["avg_doc_size"] == 7500.0
        assert ".txt" in result["by_type"]
        assert ".pdf" in result["by_type"]
        assert result["by_type"][".txt"] == 10000
        assert result["by_type"][".pdf"] == 5000

    async def test_storage_stats_empty(self, db):
        """get_storage_stats should return zeros when no documents."""
        result = await db.get_storage_stats()
        assert result["total_size"] == 0
        assert result["doc_count"] == 0
        assert result["by_type"] == {}


class TestChatActivity:
    """Tests for get_chat_activity."""

    async def test_chat_activity_with_data(self, db):
        """get_chat_activity should return daily message counts."""
        session = await db.create_chat_session(title="Test")
        await db.save_chat_message(session["id"], "user", "Hello")
        await db.save_chat_message(session["id"], "assistant", "Hi")
        await db.save_chat_message(session["id"], "user", "Question?")

        result = await db.get_chat_activity(days=30)
        assert len(result) >= 1
        total = sum(r["message_count"] for r in result)
        assert total == 3

    async def test_chat_activity_empty(self, db):
        """get_chat_activity should return empty list when no messages."""
        result = await db.get_chat_activity(days=30)
        assert result == []


class TestJobStats:
    """Tests for get_job_stats."""

    async def test_job_stats_with_data(self, db):
        """get_job_stats should return correct job statistics."""
        # Create documents first (complete_job requires a valid document_id FK)
        doc_a = await db.save_document(
            path="/docs/a.txt", source_type="api", source_name="test",
            title="Doc A", ext=".txt", mime_type="text/plain",
            body="content a", status="indexed",
        )
        doc_b = await db.save_document(
            path="/docs/b.txt", source_type="api", source_name="test",
            title="Doc B", ext=".txt", mime_type="text/plain",
            body="content b", status="indexed",
        )
        await db.save_document(
            path="/docs/c.txt", source_type="api", source_name="test",
            title="Doc C", ext=".txt", mime_type="text/plain",
            body="content c", status="indexed",
        )

        job1 = await db.create_job(
            document_path="/docs/a.txt",
            document_title="Doc A",
        )
        await db.complete_job(job1.id, doc_a)

        job2 = await db.create_job(
            document_path="/docs/b.txt",
            document_title="Doc B",
        )
        await db.complete_job(job2.id, doc_b)

        job3 = await db.create_job(
            document_path="/docs/c.txt",
            document_title="Doc C",
        )
        await db.fail_job(job3.id, "Timeout error")

        result = await db.get_job_stats()
        assert result["total"] == 3
        assert result["by_state"]["completed"] == 2
        assert result["by_state"]["failed"] == 1
        # success_rate = 2/(2+1) * 100 = 66.67
        assert result["success_rate"] == pytest.approx(66.67, rel=0.1)
        assert len(result["recent_failures"]) == 1
        assert result["recent_failures"][0]["document_title"] == "Doc C"
        assert "Timeout" in result["recent_failures"][0]["error"]

    async def test_job_stats_empty(self, db):
        """get_job_stats should return zeros when no jobs."""
        result = await db.get_job_stats()
        assert result["total"] == 0
        assert result["success_rate"] == 0.0
        assert result["recent_failures"] == []
        assert result["by_state"] == {}


# ── Web Route Tests ──────────────────────────────────────────────


class TestDashboardRendering:
    """Tests for the enhanced dashboard page."""

    async def test_dashboard_returns_200(self, asgi_client):
        """GET / should return 200 with HTML."""
        resp = await asgi_client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    async def test_dashboard_has_charts(self, asgi_client):
        """Dashboard should contain SVG chart elements."""
        resp = await asgi_client.get("/")
        html = resp.text
        assert "<svg" in html
        assert "chart-svg" in html

    async def test_dashboard_has_analytics_sections(self, asgi_client):
        """Dashboard should contain analytics section headings."""
        resp = await asgi_client.get("/")
        html = resp.text
        assert "文档增长" in html
        assert "搜索趋势" in html
        assert "对话活跃度" in html
        assert "存储分类" in html
        assert "热门搜索" in html or "暂无" in html
        assert "任务统计" in html

    async def test_dashboard_has_analytics_link(self, asgi_client):
        """Dashboard should have a link to the full analytics page."""
        resp = await asgi_client.get("/")
        html = resp.text
        assert 'href="/analytics"' in html

    async def test_dashboard_has_nav_link(self, asgi_client):
        """Nav bar should have Analytics link."""
        resp = await asgi_client.get("/")
        html = resp.text
        assert "分析" in html

    async def test_dashboard_dark_mode_compatible(self, asgi_client):
        """Dashboard should link external stylesheet with CSS variables for dark mode."""
        resp = await asgi_client.get("/")
        html = resp.text
        assert "/static/css/styles.css" in html
        assert "/static/js/theme.js" in html


class TestAnalyticsPageRendering:
    """Tests for the dedicated analytics page."""

    async def test_analytics_page_returns_200(self, asgi_client):
        """GET /analytics should return 200 with HTML."""
        resp = await asgi_client.get("/analytics")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    async def test_analytics_page_has_title(self, asgi_client):
        """Analytics page should have the Analytics title."""
        resp = await asgi_client.get("/analytics")
        assert "分析" in resp.text

    async def test_analytics_page_has_date_range(self, asgi_client):
        """Analytics page should have date range selector."""
        resp = await asgi_client.get("/analytics")
        html = resp.text
        assert "date-range-selector" in html
        assert "7 days" in html
        assert "30 days" in html
        assert "90 days" in html

    async def test_analytics_page_with_days_param(self, asgi_client):
        """GET /analytics?days=7 should return 200 and show active range."""
        resp = await asgi_client.get("/analytics?days=7")
        assert resp.status_code == 200
        html = resp.text
        assert "7 days" in html
        # The 7-day link should be active
        assert 'href="/analytics?days=7"' in html
        assert "active" in html

    async def test_analytics_page_has_charts(self, asgi_client):
        """Analytics page should contain SVG charts."""
        resp = await asgi_client.get("/analytics")
        html = resp.text
        assert "<svg" in html

    async def test_analytics_page_has_export_link(self, asgi_client):
        """Analytics page should link to the JSON export endpoint."""
        resp = await asgi_client.get("/analytics")
        html = resp.text
        assert "/api/v1/analytics" in html

    async def test_analytics_page_has_detailed_tables(self, asgi_client):
        """Analytics page should have detailed data tables."""
        resp = await asgi_client.get("/analytics")
        html = resp.text
        assert "存储详情" in html
        assert "全部标签" in html

    async def test_analytics_page_invalid_days(self, asgi_client):
        """GET /analytics?days=0 should return 422 (validation error)."""
        resp = await asgi_client.get("/analytics?days=0")
        assert resp.status_code == 422

    async def test_analytics_page_days_too_large(self, asgi_client):
        """GET /analytics?days=400 should return 422 (validation error)."""
        resp = await asgi_client.get("/analytics?days=400")
        assert resp.status_code == 422


class TestAnalyticsAPI:
    """Tests for the JSON analytics API endpoint."""

    async def test_api_returns_200(self, asgi_client):
        """GET /api/v1/analytics should return 200 JSON."""
        resp = await asgi_client.get("/api/v1/analytics")
        assert resp.status_code == 200
        assert "application/json" in resp.headers.get("content-type", "")

    async def test_api_has_all_keys(self, asgi_client):
        """Analytics JSON should contain all expected keys."""
        resp = await asgi_client.get("/api/v1/analytics")
        data = resp.json()
        assert "days" in data
        assert "stats" in data
        assert "document_growth" in data
        assert "tag_distribution" in data
        assert "storage" in data
        assert "search_stats" in data
        assert "popular_queries" in data
        assert "search_trend" in data
        assert "chat_activity" in data
        assert "job_stats" in data

    async def test_api_with_days_param(self, asgi_client):
        """GET /api/v1/analytics?days=7 should return days=7."""
        resp = await asgi_client.get("/api/v1/analytics?days=7")
        data = resp.json()
        assert data["days"] == 7

    async def test_api_stats_has_counts(self, asgi_client):
        """Analytics JSON stats should have document counts."""
        resp = await asgi_client.get("/api/v1/analytics")
        data = resp.json()
        stats = data["stats"]
        assert "total" in stats
        assert "pending" in stats
        assert "indexed" in stats
        assert "summarized" in stats
        assert stats["total"] > 0

    async def test_api_storage_has_by_type(self, asgi_client):
        """Analytics JSON storage should have by_type breakdown."""
        resp = await asgi_client.get("/api/v1/analytics")
        data = resp.json()
        storage = data["storage"]
        assert "total_size" in storage
        assert "by_type" in storage
        assert "avg_doc_size" in storage
        assert "doc_count" in storage

    async def test_api_job_stats(self, asgi_client):
        """Analytics JSON job_stats should have state breakdown."""
        resp = await asgi_client.get("/api/v1/analytics")
        data = resp.json()
        job_stats = data["job_stats"]
        assert "by_state" in job_stats
        assert "total" in job_stats
        assert "success_rate" in job_stats
        assert "recent_failures" in job_stats


class TestSearchRouteLogging:
    """Tests that the /search route logs searches."""

    async def test_search_logs_query(self, asgi_client, tmp_db_path):
        """GET /search?q=test should log the search query."""
        # Perform a search
        resp = await asgi_client.get("/search?q=test")
        assert resp.status_code == 200

        # Verify the search was logged
        from src.core.db_sqlite import Database

        db = Database(db_path=tmp_db_path)
        await db.connect()
        try:
            popular = await db.get_popular_queries(limit=10)
            queries = [p["query"] for p in popular]
            assert "test" in queries
        finally:
            await db.disconnect()

    async def test_search_empty_not_logged(self, asgi_client, tmp_db_path):
        """GET /search (no query) should not log anything."""
        resp = await asgi_client.get("/search")
        assert resp.status_code == 200

        from src.core.db_sqlite import Database

        db = Database(db_path=tmp_db_path)
        await db.connect()
        try:
            # The pre-seeded searches from the fixture are 4, so check
            # that no empty-query search was added
            stats = await db.get_search_stats(days=30)
            # Fixture logged 4 searches, no new ones from empty search
            assert stats["total_searches"] == 4
        finally:
            await db.disconnect()


# ── Chart Helper Function Tests ──────────────────────────────────


class TestChartHelpers:
    """Tests for SVG chart generation helper functions."""

    def test_svg_line_chart_empty_data(self):
        """Line chart with empty data should return placeholder."""
        from src.web.server import _svg_line_chart

        result = _svg_line_chart([], "count")
        assert "No data" in result

    def test_svg_line_chart_with_data(self):
        """Line chart with data should produce SVG."""
        from src.web.server import _svg_line_chart

        data = [
            {"date": "2025-01-01", "count": 5},
            {"date": "2025-01-02", "count": 10},
            {"date": "2025-01-03", "count": 3},
        ]
        result = _svg_line_chart(data, "count")
        assert "<svg" in result
        assert "polyline" in result

    def test_svg_bar_chart_empty_data(self):
        """Bar chart with empty data should return placeholder."""
        from src.web.server import _svg_bar_chart

        result = _svg_bar_chart([], "tag", "count")
        assert "No data" in result

    def test_svg_bar_chart_with_data(self):
        """Bar chart with data should produce SVG."""
        from src.web.server import _svg_bar_chart

        data = [
            {"tag": "python", "count": 10},
            {"tag": "web", "count": 5},
        ]
        result = _svg_bar_chart(data, "tag", "count")
        assert "<svg" in result
        assert "<rect" in result

    def test_svg_pie_chart_empty_data(self):
        """Pie chart with empty data should return placeholder."""
        from src.web.server import _svg_pie_chart

        result = _svg_pie_chart([])
        assert "No data" in result

    def test_svg_pie_chart_with_data(self):
        """Pie chart with data should produce SVG."""
        from src.web.server import _svg_pie_chart

        data = [("A", 10.0), ("B", 20.0), ("C", 30.0)]
        result = _svg_pie_chart(data)
        assert "<svg" in result
        assert "pie-legend" in result

    def test_svg_pie_chart_all_zero(self):
        """Pie chart with all-zero values should return placeholder."""
        from src.web.server import _svg_pie_chart

        data = [("A", 0.0), ("B", 0.0)]
        result = _svg_pie_chart(data)
        assert "No data" in result

    def test_svg_line_chart_single_point(self):
        """Line chart with a single data point should still render."""
        from src.web.server import _svg_line_chart

        data = [{"date": "2025-01-01", "count": 5}]
        result = _svg_line_chart(data, "count")
        assert "<svg" in result
        assert "circle" in result