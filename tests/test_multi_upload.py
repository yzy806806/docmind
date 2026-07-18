"""Tests for multi-file drag-and-drop upload UI.

Covers:
- GET /upload renders the drag-and-drop form
- POST /upload with single ``file`` field (backward compat) → single success page
- POST /upload with multiple ``files[]`` field → batch results page
- POST /upload with a mix of valid + invalid files → partial-failure batch page
- POST /upload with no files → re-renders the form
- _render_upload_batch rendering helper
- upload_form.html template contains drag-drop zone, multi-file input, JS, progress
- GET /upload?done=1 shows the success banner
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
        yield str(Path(tmpdir) / "test_upload.db")


@pytest.fixture
async def asgi_client(tmp_db_path: str):
    """Create an httpx.AsyncClient backed by the ASGI app.

    Uses httpx.AsyncClient + ASGITransport so the async DB connection
    (created in the same event loop) is accessible from route handlers.
    """
    import httpx
    from src.core.db_sqlite import Database
    from src.web import server

    db = Database(db_path=tmp_db_path)
    await db.connect()

    from unittest.mock import AsyncMock, MagicMock

    mock_queue = MagicMock()
    mock_queue.enqueue = AsyncMock(
        side_effect=lambda **kwargs: MagicMock(id=f"job-{kwargs.get('document_title','x')}")
    )
    mock_queue.complete = AsyncMock(return_value=None)

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


def _make_file_tuple(name: str, content: str = "hello world") -> tuple:
    """Build a (filename, content, content_type) tuple for httpx multipart."""
    return (name, content.encode("utf-8"), "text/plain")


# ── Template / rendering tests ────────────────────────────────────


class TestUploadFormTemplate:
    """Tests for the drag-and-drop upload form template."""

    def test_form_has_drop_zone(self):
        """upload_form.html must contain a drag-and-drop zone element."""
        from src.web.rendering import _render_upload_form

        html = _render_upload_form()
        assert "drop-zone" in html
        assert "Drop files here" in html

    def test_form_has_multi_file_input(self):
        """The file input must have the ``multiple`` attribute."""
        from src.web.rendering import _render_upload_form

        html = _render_upload_form()
        assert "multiple" in html
        # The new multi-file field name is ``files`` (with [])
        assert 'name="files"' in html

    def test_form_has_progress_js(self):
        """The form must load upload.js with per-file progress tracking."""
        from src.web.rendering import _render_upload_form
        from pathlib import Path

        html = _render_upload_form()
        assert "/static/js/upload.js" in html
        upload_js = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "js" / "upload.js"
        assert upload_js.exists(), f"upload.js not found at {upload_js}"
        js_src = upload_js.read_text()
        assert "XMLHttpRequest" in js_src or "fetch(" in js_src or "xhr" in js_src
        assert "progress" in js_src.lower()

    def test_form_has_drag_drop_event_handlers(self):
        """upload.js must wire dragenter / dragover / dragleave / drop events."""
        from src.web.rendering import _render_upload_form
        from pathlib import Path

        html = _render_upload_form()
        assert "/static/js/upload.js" in html
        upload_js = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "js" / "upload.js"
        js_src = upload_js.read_text()
        assert "dragenter" in js_src
        assert "dragover" in js_src
        assert "dragleave" in js_src
        assert "drop" in js_src

    def test_form_has_fallback_no_js(self):
        """A <noscript> or fallback form must exist for no-JS users."""
        from src.web.rendering import _render_upload_form

        html = _render_upload_form()
        # We use a fallback <form> + a no-JS notice link.
        assert "fallback-form" in html


class TestUploadBatchRender:
    """Tests for the _render_upload_batch helper."""

    def test_empty_batch(self):
        from src.web.rendering import _render_upload_batch

        html = _render_upload_batch([], [])
        assert "0 file(s) uploaded" in html

    def test_with_results(self):
        from src.web.rendering import _render_upload_batch

        html = _render_upload_batch(
            [{"title": "doc1.txt", "doc_id": 1, "job_id": "j-1"}],
            [],
        )
        assert "1 file(s) uploaded" in html
        assert "doc1.txt" in html
        assert "/documents/1" in html
        assert "j-1" in html

    def test_with_errors(self):
        from src.web.rendering import _render_upload_batch

        html = _render_upload_batch(
            [],
            [{"filename": "bad.xyz", "error": "Unsupported file type: .xyz"}],
        )
        assert "1 failed" in html
        assert "bad.xyz" in html
        assert "Unsupported file type" in html

    def test_with_mixed(self):
        from src.web.rendering import _render_upload_batch

        html = _render_upload_batch(
            [{"title": "ok.txt", "doc_id": 2, "job_id": "j-2"}],
            [{"filename": "bad.txt", "error": "Extraction failed: boom"}],
        )
        assert "1 file(s) uploaded" in html
        assert "1 failed" in html
        assert "ok.txt" in html
        assert "bad.txt" in html


# ── Route tests (ASGI) ────────────────────────────────────────────


class TestUploadRoutes:
    """Tests for the GET/POST /upload routes."""

    @pytest.mark.asyncio
    async def test_get_upload_form(self, asgi_client):
        """GET /upload should render the drag-drop form (200 OK)."""
        r = await asgi_client.get("/upload")
        assert r.status_code == 200
        assert "drop-zone" in r.text
        assert "Drop files here" in r.text

    @pytest.mark.asyncio
    async def test_get_upload_done_banner(self, asgi_client):
        """GET /upload?done=1 should include the success banner."""
        r = await asgi_client.get("/upload?done=1")
        assert r.status_code == 200
        assert "Batch upload complete" in r.text

    @pytest.mark.asyncio
    async def test_post_no_files_renders_form(self, asgi_client):
        """POST /upload with no file fields re-renders the empty form."""
        r = await asgi_client.post("/upload")
        assert r.status_code == 200
        assert "drop-zone" in r.text

    @pytest.mark.asyncio
    async def test_post_single_file_legacy_field(self, asgi_client):
        """POST /upload with the old ``file`` field (single file) →
        single success page (backward compat)."""
        r = await asgi_client.post(
            "/upload",
            files={"file": _make_file_tuple("legacy.txt", "legacy content")},
        )
        assert r.status_code == 200
        # Single-file success → upload_success.html markers
        assert "Upload Successful" in r.text
        assert "legacy.txt" in r.text
        assert "/documents/" in r.text

    @pytest.mark.asyncio
    async def test_post_multi_files_new_field(self, asgi_client):
        """POST /upload with ``files[]`` (multiple) → batch results page."""
        r = await asgi_client.post(
            "/upload",
            files=[
                ("files", _make_file_tuple("a.txt", "alpha")),
                ("files", _make_file_tuple("b.txt", "beta")),
            ],
        )
        assert r.status_code == 200
        # Batch results page markers
        assert "Upload Results" in r.text
        assert "2 file(s) uploaded" in r.text
        assert "a.txt" in r.text
        assert "b.txt" in r.text

    @pytest.mark.asyncio
    async def test_post_multi_files_partial_failure(self, asgi_client):
        """POST /upload with one valid + one invalid (bad ext) →
        batch page showing 1 success + 1 failure."""
        r = await asgi_client.post(
            "/upload",
            files=[
                ("files", _make_file_tuple("good.txt", "good")),
                ("files", ("bad.xyz", b"garbage", "application/octet-stream")),
            ],
        )
        assert r.status_code == 200
        assert "1 file(s) uploaded" in r.text
        assert "1 failed" in r.text
        assert "good.txt" in r.text
        assert "bad.xyz" in r.text
        assert "Unsupported file type" in r.text

    @pytest.mark.asyncio
    async def test_post_all_invalid_files(self, asgi_client):
        """POST /upload with only invalid files → batch page, 0 ok, N failed."""
        r = await asgi_client.post(
            "/upload",
            files=[
                ("files", ("a.xyz", b"x", "application/octet-stream")),
                ("files", ("b.xyz", b"y", "application/octet-stream")),
            ],
        )
        assert r.status_code == 200
        assert "0 file(s) uploaded" in r.text
        assert "2 failed" in r.text

    @pytest.mark.asyncio
    async def test_post_mixed_legacy_and_new_field(self, asgi_client):
        """POST /upload with both ``file`` and ``files[]`` → all merged
        into the batch (no double-count of the same filename)."""
        r = await asgi_client.post(
            "/upload",
            files=[
                ("file", _make_file_tuple("single.txt", "one")),
                ("files", _make_file_tuple("multi.txt", "two")),
            ],
        )
        assert r.status_code == 200
        assert "Upload Results" in r.text
        assert "2 file(s) uploaded" in r.text
        assert "single.txt" in r.text
        assert "multi.txt" in r.text

    @pytest.mark.asyncio
    async def test_post_duplicate_filename_not_doubled(self, asgi_client):
        """If the same filename appears under both ``file`` and ``files``,
        it should be processed once (dedup by filename)."""
        r = await asgi_client.post(
            "/upload",
            files=[
                ("file", _make_file_tuple("dup.txt", "one")),
                ("files", _make_file_tuple("dup.txt", "one")),
            ],
        )
        assert r.status_code == 200
        # Should be 1 file, not 2 — single success page or batch with 1.
        # Since only one distinct file, and no errors → single success page.
        assert "Upload Successful" in r.text
        assert "dup.txt" in r.text

    @pytest.mark.asyncio
    async def test_post_file_too_large_error(self, asgi_client, tmp_db_path):
        """A file exceeding max_file_size_bytes should show as failed in
        the batch results."""
        # Temporarily lower the max file size via monkeypatching config.
        from src.core.config import config

        original = config.document_limits.max_file_size_bytes
        config.document_limits.max_file_size_bytes = 10  # 10 bytes
        try:
            r = await asgi_client.post(
                "/upload",
                files=[("files", _make_file_tuple("big.txt", "x" * 100))],
            )
            assert r.status_code == 200
            assert "1 failed" in r.text
            assert "File too large" in r.text
        finally:
            config.document_limits.max_file_size_bytes = original


# ── REST API submit endpoint (used by the JS uploader) ────────────


class TestSubmitEndpoint:
    """The JS drag-drop uploader calls /api/v1/documents/submit per file.
    Ensure it still works (smoke test)."""

    @pytest.mark.asyncio
    async def test_submit_single_file(self, asgi_client):
        """POST /api/v1/documents/submit with one file → 202 Accepted."""
        r = await asgi_client.post(
            "/api/v1/documents/submit",
            files={"file": _make_file_tuple("api.txt", "api content")},
        )
        assert r.status_code == 202
        data = r.json()
        assert data["status"] == "completed"
        assert "job_id" in data
