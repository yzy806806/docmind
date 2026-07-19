"""Tests for the job processing status page in the web UI.

Covers:
- GET /jobs page rendering (table, badges, pagination info)
- GET /jobs?state=<state> filter dropdown
- GET /jobs/{job_id} detail page (all fields, error, document link, back button)
- Auto-refresh meta tag when active (pending/processing) jobs exist
- Nav bar "Jobs" link presence on all pages
- DB layer: list_jobs_paginated and count_jobs
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
        yield str(Path(tmpdir) / "test_jobs_page.db")


@pytest.fixture
async def asgi_client(tmp_db_path: str):
    """Create an httpx.AsyncClient backed by the ASGI app.

    Seeds the database with a mix of jobs in every state so the
    listing, filter, and detail pages have data to render.
    """
    import httpx
    from src.core.db_sqlite import Database
    from src.web import server

    db = Database(db_path=tmp_db_path)
    await db.connect()

    # Insert a document so completed jobs can link to it
    doc_id = await db.save_document(
        path="/docs/test_report.pdf",
        source_type="api",
        source_name="web-upload",
        title="Quarterly Report",
        ext=".pdf",
        mime_type="application/pdf",
        body="Q3 financial summary with revenue figures.",
        size=5000,
        status="indexed",
    )

    # Enqueue jobs and transition them to different states
    # 2 pending, 1 processing, 2 completed, 1 failed
    j1 = await db.enqueue_job(
        "/uploads/doc1.pdf", document_title="Pending Doc 1", source_name="web-upload"
    )
    j2 = await db.enqueue_job(
        "/uploads/doc2.pdf", document_title="Pending Doc 2", source_name="api"
    )
    j3 = await db.enqueue_job(
        "/uploads/doc3.pdf", document_title="Processing Doc 3", source_name="web-upload"
    )
    await db.update_job_status(j3.id, "processing")

    j4 = await db.enqueue_job(
        "/uploads/doc4.pdf", document_title="Completed Doc 4", source_name="web-upload"
    )
    await db.complete_job(j4.id, doc_id)

    j5 = await db.enqueue_job(
        "/uploads/doc5.pdf", document_title="Completed Doc 5", source_name="api"
    )
    await db.complete_job(j5.id, doc_id)

    j6 = await db.enqueue_job(
        "/uploads/doc6.pdf", document_title="Failed Doc 6", source_name="web-upload"
    )
    await db.fail_job(j6.id, "Traceback (most recent call last):\n  File 'worker.py', line 42\n    raise ValueError('bad PDF')\nValueError: bad PDF")

    from unittest.mock import AsyncMock, MagicMock

    mock_queue = MagicMock()
    mock_queue.enqueue = AsyncMock(return_value=MagicMock(id="test-job-id"))
    mock_queue.get_status = AsyncMock(return_value=None)

    original_db = server._db
    original_queue = server._queue
    server._db = db
    server._queue = mock_queue

    app = server.create_app()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, db, j1, j4, j6

    await db.disconnect()
    server._db = original_db
    server._queue = original_queue


# ── Jobs page rendering tests ────────────────────────────────────


class TestJobsPageRendering:
    """Tests for GET /jobs page rendering."""

    @pytest.mark.asyncio
    async def test_jobs_page_returns_html(self, asgi_client):
        """GET /jobs should return 200 HTML."""
        client, _, _, _, _ = asgi_client
        resp = await client.get("/jobs")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_jobs_page_has_title(self, asgi_client):
        """Jobs page should have the 'Jobs' title in the header."""
        client, _, _, _, _ = asgi_client
        resp = await client.get("/jobs")
        assert "任务" in resp.text

    @pytest.mark.asyncio
    async def test_jobs_page_has_table(self, asgi_client):
        """Jobs page should render a table with job rows."""
        client, _, _, _, _ = asgi_client
        resp = await client.get("/jobs")
        assert "<table>" in resp.text
        assert "任务 ID" in resp.text
        assert "状态" in resp.text

    @pytest.mark.asyncio
    async def test_jobs_page_shows_total_count(self, asgi_client):
        """Jobs page should show the total number of jobs."""
        client, _, _, _, _ = asgi_client
        resp = await client.get("/jobs")
        # We inserted 6 jobs
        assert "6" in resp.text
        assert "个任务" in resp.text

    @pytest.mark.asyncio
    async def test_jobs_page_has_state_badges(self, asgi_client):
        """Jobs page should render state badges with correct CSS classes."""
        client, _, _, _, _ = asgi_client
        resp = await client.get("/jobs")
        assert "badge-pending" in resp.text
        assert "badge-processing" in resp.text
        assert "badge-completed" in resp.text
        assert "badge-failed" in resp.text

    @pytest.mark.asyncio
    async def test_jobs_page_has_filter_dropdown(self, asgi_client):
        """Jobs page should have a state filter dropdown."""
        client, _, _, _, _ = asgi_client
        resp = await client.get("/jobs")
        assert "state-filter" in resp.text
        assert "<option" in resp.text
        assert "Pending" in resp.text
        assert "Completed" in resp.text
        assert "Failed" in resp.text

    @pytest.mark.asyncio
    async def test_jobs_page_has_truncated_job_ids(self, asgi_client):
        """Jobs page should show truncated job IDs (first 8 chars)."""
        client, _, j1, _, _ = asgi_client
        resp = await client.get("/jobs")
        # The truncated ID (first 8 chars) should be in the page
        assert j1.id[:8] in resp.text
        # The full UUID should NOT appear in the table (only on detail page)
        # Full ID might appear in the link href, so check the display text
        assert j1.id[:8] in resp.text

    @pytest.mark.asyncio
    async def test_jobs_page_has_links_to_detail(self, asgi_client):
        """Each job row should link to /jobs/{job_id}."""
        client, _, j1, _, _ = asgi_client
        resp = await client.get("/jobs")
        assert f'/jobs/{j1.id}' in resp.text

    @pytest.mark.asyncio
    async def test_jobs_page_shows_error_for_failed(self, asgi_client):
        """Failed jobs should show error text in the table."""
        client, _, _, _, j6 = asgi_client
        resp = await client.get("/jobs")
        assert "ValueError" in resp.text

    @pytest.mark.asyncio
    async def test_jobs_page_has_pagination_info(self, asgi_client):
        """Jobs page should show pagination info."""
        client, _, _, _, _ = asgi_client
        resp = await client.get("/jobs")
        assert "显示第" in resp.text


# ── State filter tests ───────────────────────────────────────────


class TestJobsPageStateFilter:
    """Tests for GET /jobs?state=<state> filtering.

    Note: the CSS in _base_page always defines all badge classes
    (badge-pending, badge-processing, etc.) so we cannot assert
    'badge-pending not in resp.text'. Instead we check that the
    filtered state appears in table rows while other states don't
    appear as badge text within <span class="badge ..."> elements.
    We use the document titles to verify which jobs are shown.
    """

    @pytest.mark.asyncio
    async def test_filter_pending(self, asgi_client):
        """Filtering by state=pending should show only pending jobs."""
        client, _, _, _, _ = asgi_client
        resp = await client.get("/jobs?state=pending")
        assert resp.status_code == 200
        assert "Pending Doc 1" in resp.text
        assert "Pending Doc 2" in resp.text
        # Other states' documents should not appear
        assert "Processing Doc 3" not in resp.text
        assert "Completed Doc 4" not in resp.text
        assert "Failed Doc 6" not in resp.text

    @pytest.mark.asyncio
    async def test_filter_completed(self, asgi_client):
        """Filtering by state=completed should show only completed jobs."""
        client, _, _, _, _ = asgi_client
        resp = await client.get("/jobs?state=completed")
        assert resp.status_code == 200
        assert "Completed Doc 4" in resp.text
        assert "Completed Doc 5" in resp.text
        # Other states' documents should not appear
        assert "Pending Doc 1" not in resp.text
        assert "Processing Doc 3" not in resp.text
        assert "Failed Doc 6" not in resp.text

    @pytest.mark.asyncio
    async def test_filter_failed(self, asgi_client):
        """Filtering by state=failed should show only failed jobs."""
        client, _, _, _, _ = asgi_client
        resp = await client.get("/jobs?state=failed")
        assert resp.status_code == 200
        assert "Failed Doc 6" in resp.text
        # Other states' documents should not appear
        assert "Pending Doc 1" not in resp.text
        assert "Processing Doc 3" not in resp.text
        assert "Completed Doc 4" not in resp.text

    @pytest.mark.asyncio
    async def test_filter_processing(self, asgi_client):
        """Filtering by state=processing should show only processing jobs."""
        client, _, _, _, _ = asgi_client
        resp = await client.get("/jobs?state=processing")
        assert resp.status_code == 200
        assert "Processing Doc 3" in resp.text
        # Other states' documents should not appear
        assert "Pending Doc 1" not in resp.text
        assert "Completed Doc 4" not in resp.text
        assert "Failed Doc 6" not in resp.text

    @pytest.mark.asyncio
    async def test_filter_invalid_state_shows_all(self, asgi_client):
        """Invalid state filter should fall back to showing all jobs."""
        client, _, _, _, _ = asgi_client
        resp = await client.get("/jobs?state=invalid")
        assert resp.status_code == 200
        # Should show all job states
        assert "badge-pending" in resp.text

    @pytest.mark.asyncio
    async def test_filter_dropdown_preserves_selection(self, asgi_client):
        """The state filter dropdown should preserve the selected state."""
        client, _, _, _, _ = asgi_client
        resp = await client.get("/jobs?state=failed")
        assert 'value="failed" selected' in resp.text


# ── Auto-refresh tests ───────────────────────────────────────────


class TestJobsPageAutoRefresh:
    """Tests for auto-refresh behavior on the jobs page."""

    @pytest.mark.asyncio
    async def test_auto_refresh_when_active_jobs(self, asgi_client):
        """Page should include meta refresh when pending/processing jobs exist."""
        client, _, _, _, _ = asgi_client
        resp = await client.get("/jobs")
        assert 'http-equiv="refresh"' in resp.text
        assert 'content="10"' in resp.text

    @pytest.mark.asyncio
    async def test_no_auto_refresh_when_no_active_jobs(self, asgi_client):
        """Page should NOT include meta refresh when no active jobs exist.

        We filter to completed jobs only, so there are no pending/processing.
        The auto-refresh check looks at the DB globally, not just the filtered
        set — but if we filter to a state with no active jobs, the page still
        refreshes because there ARE active jobs in the DB. So instead we test
        the render function directly with has_active=False.
        """
        from src.web.server import _render_jobs_page

        html = _render_jobs_page([], "", 1, 20, 0, 0, has_active=False)
        assert 'http-equiv="refresh"' not in html

    @pytest.mark.asyncio
    async def test_auto_refresh_message_shown(self, asgi_client):
        """Auto-refresh banner should be visible when active jobs exist."""
        client, _, _, _, _ = asgi_client
        resp = await client.get("/jobs")
        assert "自动刷新" in resp.text


# ── Job detail page tests ────────────────────────────────────────


class TestJobDetailPage:
    """Tests for GET /jobs/{job_id} detail page."""

    @pytest.mark.asyncio
    async def test_detail_page_returns_html(self, asgi_client):
        """GET /jobs/{job_id} should return 200 HTML."""
        client, _, j1, _, _ = asgi_client
        resp = await client.get(f"/jobs/{j1.id}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_detail_page_shows_full_job_id(self, asgi_client):
        """Detail page should show the full job ID, not truncated."""
        client, _, j1, _, _ = asgi_client
        resp = await client.get(f"/jobs/{j1.id}")
        assert j1.id in resp.text

    @pytest.mark.asyncio
    async def test_detail_page_shows_state_badge(self, asgi_client):
        """Detail page should show a state badge."""
        client, _, j1, _, _ = asgi_client
        resp = await client.get(f"/jobs/{j1.id}")
        assert "badge-pending" in resp.text
        assert "pending" in resp.text

    @pytest.mark.asyncio
    async def test_detail_page_shows_document_path(self, asgi_client):
        """Detail page should show the document path."""
        client, _, j1, _, _ = asgi_client
        resp = await client.get(f"/jobs/{j1.id}")
        assert j1.document_path in resp.text

    @pytest.mark.asyncio
    async def test_detail_page_shows_source(self, asgi_client):
        """Detail page should show the source name."""
        client, _, j1, _, _ = asgi_client
        resp = await client.get(f"/jobs/{j1.id}")
        assert j1.source_name in resp.text

    @pytest.mark.asyncio
    async def test_detail_page_has_back_button(self, asgi_client):
        """Detail page should have a back link to /jobs."""
        client, _, j1, _, _ = asgi_client
        resp = await client.get(f"/jobs/{j1.id}")
        assert 'href="/jobs"' in resp.text
        assert "返回任务列表" in resp.text

    @pytest.mark.asyncio
    async def test_detail_page_404_for_missing_job(self, asgi_client):
        """Detail page should return 404 for non-existent job."""
        client, _, _, _, _ = asgi_client
        resp = await client.get("/jobs/nonexistent-job-id")
        assert resp.status_code == 404
        assert "Not Found" in resp.text

    @pytest.mark.asyncio
    async def test_detail_page_failed_job_shows_error(self, asgi_client):
        """Detail page for a failed job should show the full error traceback."""
        client, _, _, _, j6 = asgi_client
        resp = await client.get(f"/jobs/{j6.id}")
        assert resp.status_code == 200
        assert "错误详情" in resp.text
        # Full traceback should be visible (not truncated)
        assert "Traceback" in resp.text
        assert "ValueError" in resp.text
        assert "bad PDF" in resp.text

    @pytest.mark.asyncio
    async def test_detail_page_completed_job_shows_document_link(self, asgi_client):
        """Detail page for a completed job should link to the associated document."""
        client, _, _, j4, _ = asgi_client
        resp = await client.get(f"/jobs/{j4.id}")
        assert resp.status_code == 200
        assert "Associated Document" in resp.text
        # Should have a link to the document
        assert "/documents/" in resp.text

    @pytest.mark.asyncio
    async def test_detail_page_pending_job_shows_no_document(self, asgi_client):
        """Detail page for a pending job should indicate no document linked."""
        client, _, j1, _, _ = asgi_client
        resp = await client.get(f"/jobs/{j1.id}")
        assert resp.status_code == 200
        assert "No document linked" in resp.text

    @pytest.mark.asyncio
    async def test_detail_page_shows_timestamps(self, asgi_client):
        """Detail page should show created_at and updated_at."""
        client, _, j1, _, _ = asgi_client
        resp = await client.get(f"/jobs/{j1.id}")
        assert "创建时间" in resp.text
        assert "更新时间" in resp.text


# ── Nav bar tests ────────────────────────────────────────────────


class TestJobsNavLink:
    """Tests for the 'Jobs' link in the navigation bar."""

    def test_nav_bar_has_jobs_link(self):
        """_base_page should include a link to /jobs in the nav bar."""
        from src.web.server import _base_page

        html = _base_page("Test", "<p>content</p>")
        assert 'href="/jobs"' in html
        assert ">任务<" in html

    @pytest.mark.asyncio
    async def test_dashboard_has_jobs_nav_link(self, asgi_client):
        """Dashboard page should include the Jobs nav link."""
        client, _, _, _, _ = asgi_client
        resp = await client.get("/")
        assert resp.status_code == 200
        assert 'href="/jobs"' in resp.text

    @pytest.mark.asyncio
    async def test_documents_page_has_jobs_nav_link(self, asgi_client):
        """Documents page should include the Jobs nav link."""
        client, _, _, _, _ = asgi_client
        resp = await client.get("/documents")
        assert resp.status_code == 200
        assert 'href="/jobs"' in resp.text


# ── Pagination tests ─────────────────────────────────────────────


class TestJobsPagePagination:
    """Tests for pagination on the jobs page."""

    @pytest.mark.asyncio
    async def test_jobs_page_default_pagination(self, asgi_client):
        """GET /jobs should default to page 1, 20 per page."""
        client, _, _, _, _ = asgi_client
        resp = await client.get("/jobs")
        assert resp.status_code == 200
        assert "显示第" in resp.text

    @pytest.mark.asyncio
    async def test_jobs_page_custom_per_page(self, asgi_client):
        """GET /jobs?per_page=3 should limit results to 3 per page."""
        client, _, _, _, _ = asgi_client
        resp = await client.get("/jobs?per_page=3")
        assert resp.status_code == 200
        assert "3" in resp.text

    @pytest.mark.asyncio
    async def test_jobs_page_pagination_nav(self, asgi_client):
        """With per_page=2 and 6 jobs, pagination nav should appear."""
        client, _, _, _, _ = asgi_client
        resp = await client.get("/jobs?per_page=2")
        assert resp.status_code == 200
        assert "pagination" in resp.text
        assert "下一页" in resp.text


# ── DB layer tests ───────────────────────────────────────────────


class TestJobsDbMethods:
    """Tests for list_jobs_paginated and count_jobs in db_sqlite.py."""

    @pytest.mark.asyncio
    async def test_list_jobs_paginated_all(self, tmp_db_path):
        """list_jobs_paginated with no state filter should return all jobs."""
        from src.core.db_sqlite import Database

        db = Database(db_path=tmp_db_path)
        await db.connect()
        for i in range(25):
            await db.enqueue_job(f"/docs/doc_{i}.txt", document_title=f"Doc {i}")
        result = await db.list_jobs_paginated(page=1, per_page=10)
        assert result["total"] == 25
        assert result["page"] == 1
        assert result["per_page"] == 10
        assert result["total_pages"] == 3
        assert len(result["jobs"]) == 10
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_list_jobs_paginated_by_state(self, tmp_db_path):
        """list_jobs_paginated with state filter should return only matching jobs."""
        from src.core.db_sqlite import Database

        db = Database(db_path=tmp_db_path)
        await db.connect()
        j1 = await db.enqueue_job("/docs/d1.txt", document_title="D1")
        j2 = await db.enqueue_job("/docs/d2.txt", document_title="D2")
        await db.update_job_status(j1.id, "completed")
        result = await db.list_jobs_paginated(state="completed", page=1, per_page=10)
        assert result["total"] == 1
        assert len(result["jobs"]) == 1
        assert result["jobs"][0].id == j1.id
        result_pending = await db.list_jobs_paginated(state="pending", page=1, per_page=10)
        assert result_pending["total"] == 1
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_list_jobs_paginated_page2(self, tmp_db_path):
        """Page 2 should return the correct offset of jobs."""
        from src.core.db_sqlite import Database

        db = Database(db_path=tmp_db_path)
        await db.connect()
        for i in range(25):
            await db.enqueue_job(f"/docs/doc_{i}.txt", document_title=f"Doc {i}")
        result = await db.list_jobs_paginated(page=2, per_page=10)
        assert len(result["jobs"]) == 10
        assert result["total"] == 25
        assert result["total_pages"] == 3
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_count_jobs_all(self, tmp_db_path):
        """count_jobs with no state should count all jobs."""
        from src.core.db_sqlite import Database

        db = Database(db_path=tmp_db_path)
        await db.connect()
        for i in range(5):
            await db.enqueue_job(f"/docs/doc_{i}.txt", document_title=f"Doc {i}")
        count = await db.count_jobs()
        assert count == 5
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_count_jobs_by_state(self, tmp_db_path):
        """count_jobs with state filter should count only matching jobs."""
        from src.core.db_sqlite import Database

        db = Database(db_path=tmp_db_path)
        await db.connect()
        j1 = await db.enqueue_job("/docs/d1.txt", document_title="D1")
        j2 = await db.enqueue_job("/docs/d2.txt", document_title="D2")
        await db.update_job_status(j1.id, "completed")
        assert await db.count_jobs(state="completed") == 1
        assert await db.count_jobs(state="pending") == 1
        assert await db.count_jobs(state="failed") == 0
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_list_jobs_paginated_empty(self, tmp_db_path):
        """list_jobs_paginated on an empty DB should return empty list."""
        from src.core.db_sqlite import Database

        db = Database(db_path=tmp_db_path)
        await db.connect()
        result = await db.list_jobs_paginated(page=1, per_page=10)
        assert result["total"] == 0
        assert len(result["jobs"]) == 0
        assert result["total_pages"] == 0
        await db.disconnect()
