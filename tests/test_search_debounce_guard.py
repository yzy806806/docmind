"""Tests verifying debounced search with loading indicator.

Context: Agora Phase 2, motion-69159b7de5f1, action item 3/7.
Implement 250ms debounced search with a 'Searching…' loading indicator
that appears after first keystroke and disappears on results.

The search forms (search_form.html, search_results.html) now use HTMX
with hx-trigger including 'keyup ... changed delay:250ms' for live
debounced search. These tests verify:
1. Search forms have HTMX attributes with 250ms debounce delay
2. The loading indicator (#search-loading) with 'Searching…' text exists
3. The hx-target points to #search-live-region
4. The hx-indicator points to #search-loading
5. Dashboard search still uses plain form-submit (no live search there)
6. Documents filter form triggers on submit/change, not raw input
7. Viewer in-page find is already debounced and fires no HTTP
8. DocMindPerf.debounce utility exists
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Generator

import pytest


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _read(path: str) -> str:
    return (_project_root() / path).read_text()


# ── 1. Search forms use HTMX with 250ms debounce ──────────────────


class TestSearchFormsHaveDebouncedHTMX:
    """Verify search forms have HTMX with 250ms debounce delay."""

    def test_search_form_page_has_htmx_with_debounce(self):
        """search_form.html: form has hx-get, hx-trigger with delay:250ms."""
        html = _read("src/web/templates/search_form.html")
        assert 'action="/search"' in html
        assert 'method="get"' in html
        form_match = re.search(
            r'<form[^>]*action="/search"[^>]*>', html, re.DOTALL
        )
        assert form_match, "Search form not found"
        form_tag = form_match.group(0)
        assert "hx-get" in form_tag, (
            f"Search form lacks hx-get for live search: {form_tag}"
        )
        assert "hx-target" in form_tag, (
            f"Search form lacks hx-target: {form_tag}"
        )
        assert "hx-trigger" in form_tag, (
            f"Search form lacks hx-trigger: {form_tag}"
        )
        assert "hx-indicator" in form_tag, (
            f"Search form lacks hx-indicator: {form_tag}"
        )

    def test_search_form_page_trigger_has_250ms_delay(self):
        """search_form.html: hx-trigger must contain delay:250ms."""
        html = _read("src/web/templates/search_form.html")
        trigger_match = re.search(r'hx-trigger="([^"]*)"', html)
        assert trigger_match, "No hx-trigger found in search_form.html"
        trigger_value = trigger_match.group(1)
        assert "250ms" in trigger_value, (
            f"hx-trigger must have 250ms debounce delay: {trigger_value}"
        )

    def test_search_form_page_has_loading_indicator(self):
        """search_form.html: must have #search-loading with Searching… text."""
        html = _read("src/web/templates/search_form.html")
        assert 'id="search-loading"' in html, (
            "search_form.html missing #search-loading indicator"
        )
        assert "正在搜索" in html, (
            "search_form.html missing '正在搜索…' loading text"
        )
        assert 'class="htmx-indicator' in html, (
            "#search-loading must use htmx-indicator class"
        )

    def test_search_form_page_has_live_region(self):
        """search_form.html: must have #search-live-region target."""
        html = _read("src/web/templates/search_form.html")
        assert 'id="search-live-region"' in html, (
            "search_form.html missing #search-live-region target div"
        )

    def test_search_results_page_has_htmx_with_debounce(self):
        """search_results.html: form has hx-get, hx-trigger with delay:250ms."""
        html = _read("src/web/templates/search_results.html")
        assert 'action="/search"' in html
        assert 'method="get"' in html
        form_match = re.search(
            r'<form[^>]*action="/search"[^>]*>', html, re.DOTALL
        )
        assert form_match, "Search results form not found"
        form_tag = form_match.group(0)
        assert "hx-get" in form_tag, (
            f"Search results form lacks hx-get: {form_tag}"
        )
        assert "hx-trigger" in form_tag, (
            f"Search results form lacks hx-trigger: {form_tag}"
        )
        assert "hx-indicator" in form_tag, (
            f"Search results form lacks hx-indicator: {form_tag}"
        )

    def test_search_results_page_trigger_has_250ms_delay(self):
        """search_results.html: hx-trigger must contain delay:250ms."""
        html = _read("src/web/templates/search_results.html")
        trigger_match = re.search(r'hx-trigger="([^"]*)"', html)
        assert trigger_match, "No hx-trigger found in search_results.html"
        trigger_value = trigger_match.group(1)
        assert "250ms" in trigger_value, (
            f"hx-trigger must have 250ms debounce delay: {trigger_value}"
        )

    def test_search_results_page_has_loading_indicator(self):
        """search_results.html: must have #search-loading with Searching… text."""
        html = _read("src/web/templates/search_results.html")
        assert 'id="search-loading"' in html, (
            "search_results.html missing #search-loading indicator"
        )
        assert "正在搜索" in html, (
            "search_results.html missing '正在搜索…' loading text"
        )

    def test_search_results_page_has_live_region(self):
        """search_results.html: must have #search-live-region target."""
        html = _read("src/web/templates/search_results.html")
        assert 'id="search-live-region"' in html, (
            "search_results.html missing #search-live-region target div"
        )

    def test_search_results_hx_target_is_live_region(self):
        """search_results.html: hx-target must point to #search-live-region."""
        html = _read("src/web/templates/search_results.html")
        target_match = re.search(r'hx-target="([^"]*)"', html)
        assert target_match, "No hx-target found in search_results.html"
        assert "search-live-region" in target_match.group(1), (
            f"hx-target should point to #search-live-region: {target_match.group(1)}"
        )

    def test_search_results_hx_indicator_is_loading(self):
        """search_results.html: hx-indicator must point to #search-loading."""
        html = _read("src/web/templates/search_results.html")
        indicator_match = re.search(r'hx-indicator="([^"]*)"', html)
        assert indicator_match, "No hx-indicator found in search_results.html"
        assert "search-loading" in indicator_match.group(1), (
            f"hx-indicator should point to #search-loading: {indicator_match.group(1)}"
        )


# ── 2. Dashboard search still uses plain form-submit ─────────────


class TestDashboardSearchUsesFormSubmit:
    """Dashboard quick search still uses plain form GET (no live search)."""

    def test_dashboard_search_uses_get_submit(self):
        """dashboard.html: quick search box uses plain form GET, no hx-*."""
        html = _read("src/web/templates/dashboard.html")
        assert 'action="/search"' in html
        assert 'method="get"' in html
        form_match = re.search(
            r'<form[^>]*action="/search"[^>]*>', html
        )
        assert form_match, "Dashboard search form not found"
        form_tag = form_match.group(0)
        assert "hx-" not in form_tag, (
            f"Dashboard search form has HTMX attributes: {form_tag}"
        )


# ── 3. Documents filter form triggers on submit/change, not input ──


class TestDocumentsFilterNoInputTrigger:
    """Verify the /documents faceted filter form does not fire on text input."""

    def test_filter_form_trigger_is_submit_change(self):
        """The facet-filter-form uses hx-trigger='submit, change', not 'input'.

        'change' on <select> elements fires on selection, not per-keystroke.
        'change' on text <input> fires on blur (lose focus), not per-keystroke.
        'input' trigger would fire on every keystroke — that's what we guard
        against.
        """
        html = _read("src/web/templates/documents/list.html")
        # Find the hx-trigger on the filter form
        trigger_match = re.search(r'hx-trigger="([^"]*)"', html)
        assert trigger_match, "No hx-trigger found in documents/list.html"
        trigger_value = trigger_match.group(1)

        # Must NOT contain 'input' as a trigger event
        # 'input' as a standalone word in the trigger means per-keystroke
        trigger_events = [e.strip() for e in trigger_value.split(",")]
        for event in trigger_events:
            # Extract just the event name (before any modifier like 'changed',
            # 'delay:', 'throttle:')
            event_name = re.split(r"[\s(]", event)[0].strip()
            assert event_name != "input", (
                f"hx-trigger contains 'input' event ({trigger_value}): "
                f"per-keystroke HTMX requests would cause re-render stutter. "
                f"Use 'change' or 'submit' instead."
            )

    def test_tag_text_input_has_no_oninput_or_onkeyup(self):
        """The tag text input in the filter form must not have oninput/onkeyup."""
        html = _read("src/web/templates/documents/list.html")
        # Find all input elements in the filter form area
        input_tags = re.findall(r'<input[^>]*>', html)
        for inp in input_tags:
            assert "oninput" not in inp.lower(), (
                f"Input has oninput handler (per-keystroke risk): {inp}"
            )
            assert "onkeyup" not in inp.lower(), (
                f"Input has onkeyup handler (per-keystroke risk): {inp}"
            )


# ── 4. Viewer in-page find is already debounced ────────────────────


class TestViewerFindAlreadyDebounced:
    """Verify the viewer.js in-page find uses debounce and fires no HTTP."""

    def test_viewer_find_input_has_debounce(self):
        """viewer.js search input listener must use a debounce timer."""
        js = _read("src/web/static/js/viewer.js")
        # The input event listener should use setTimeout/clearTimeout
        # (debounce pattern)
        assert "addEventListener('input'" in js or \
               'addEventListener("input"' in js, (
            "No input event listener found in viewer.js"
        )
        # Check for debounce pattern: clearTimeout + setTimeout
        assert "clearTimeout" in js, (
            "viewer.js input listener lacks clearTimeout (no debounce)"
        )
        assert "setTimeout" in js, (
            "viewer.js input listener lacks setTimeout (no debounce)"
        )

    def test_viewer_find_does_not_trigger_htmx_or_fetch(self):
        """The viewer find function should not make HTTP requests."""
        js = _read("src/web/static/js/viewer.js")
        # Find the highlightTerm function and the input listener scope
        # The input listener calls highlightTerm, which does DOM manipulation
        # only — no fetch/XMLHttpRequest/htmx.ajax calls
        # Check that the input listener callback doesn't contain fetch/XHR
        input_listener_match = re.search(
            r"addEventListener\(['\"]input['\"].*?function.*?\{(.*?)\}",
            js,
            re.DOTALL
        )
        if input_listener_match:
            callback_body = input_listener_match.group(1)
            assert "fetch(" not in callback_body, (
                "Viewer find input listener contains fetch() — should be "
                "DOM-only (no HTTP requests)"
            )
            assert "XMLHttpRequest" not in callback_body, (
                "Viewer find input listener contains XMLHttpRequest"
            )
            assert "htmx.ajax" not in callback_body, (
                "Viewer find input listener contains htmx.ajax()"
            )


# ── 5. Debounce utility exists for future use ──────────────────────


class TestDebounceUtilityAvailable:
    """Verify DocMindPerf.debounce exists if search-as-you-type is ever added."""

    def test_perf_utils_provides_debounce(self):
        """perf-utils.js exposes DocMindPerf.debounce for future use."""
        js = _read("src/web/static/js/perf-utils.js")
        assert "debounce" in js, "perf-utils.js missing debounce function"
        assert "DocMindPerf" in js, "perf-utils.js missing DocMindPerf global"
        assert "debounce:" in js, "DocMindPerf doesn't export debounce"

    def test_perf_utils_loaded_in_base_html(self):
        """base.html loads perf-utils.js before other islands."""
        html = _read("src/web/templates/base.html")
        assert "perf-utils.js" in html, (
            "base.html doesn't load perf-utils.js — debounce utility unavailable"
        )


# ── 6. Server returns live fragment for HTMX requests ─────────────


class TestSearchHTMXLiveFragment:
    """Verify the /search endpoint returns a fragment (not a full page)
    when the HX-Request header is present.

    When the user types in the search box, HTMX fires a debounced (250ms)
    GET /search with the HX-Request: true header.  The server must respond
    with only the inner content of #search-live-region — the export bar +
    results list — so HTMX can swap it in via hx-swap="innerHTML" without
    reloading the page chrome, form, or slider.
    """

    @pytest.fixture
    def tmp_db_path(self) -> Generator[str, None, None]:
        """Provide a temporary database file path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield str(Path(tmpdir) / "test_htmx_live.db")

    @pytest.fixture
    async def asgi_client(self, tmp_db_path: str):
        """Build an ASGI test client with a temp DB containing one doc."""
        import httpx
        from src.core.db_sqlite import Database
        from src.web import server
        from unittest.mock import AsyncMock, MagicMock

        db = Database(db_path=tmp_db_path)
        await db.connect()
        await db.save_document(
            path="/docs/htmx_test.txt",
            source_type="api",
            source_name="test",
            title="HTMX Live Search Doc",
            ext=".txt",
            mime_type="text/plain",
            body="This doc is about machine learning for live search.",
            size=100,
            status="indexed",
        )

        mock_queue = MagicMock()
        mock_queue.enqueue = AsyncMock(return_value=MagicMock(id="job"))
        original_db = server._db
        original_queue = server._queue
        server._db = db
        server._queue = mock_queue

        app = server.create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as c:
            yield c

        await db.disconnect()
        server._db = original_db
        server._queue = original_queue

    @pytest.mark.asyncio
    async def test_htmx_request_returns_fragment_not_full_page(self, asgi_client):
        """GET /search with HX-Request header returns a fragment (no <html>)."""
        resp = await asgi_client.get(
            "/search?q=machine",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        html = resp.text
        # Fragment must NOT contain full-page chrome
        assert "<html" not in html.lower(), (
            "HTMX response should be a fragment, not a full page"
        )
        assert "<head" not in html.lower(), (
            "HTMX response should not contain <head>"
        )
        assert "<!DOCTYPE" not in html, "HTMX response should not be a full document"
        # Fragment SHOULD contain results
        assert "HTMX Live Search Doc" in html, (
            "HTMX response should contain search results"
        )

    @pytest.mark.asyncio
    async def test_non_htmx_request_returns_full_page(self, asgi_client):
        """GET /search WITHOUT HX-Request header returns a full HTML page."""
        resp = await asgi_client.get("/search?q=machine")
        assert resp.status_code == 200
        html = resp.text
        # Full page MUST contain the page chrome
        assert "<html" in html.lower() or "<!DOCTYPE" in html, (
            "Non-HTMX response should be a full HTML page"
        )
        assert "HTMX Live Search Doc" in html

    @pytest.mark.asyncio
    async def test_htmx_fragment_does_not_contain_form(self, asgi_client):
        """The live fragment must not include the <form> (it's already on the page)."""
        resp = await asgi_client.get(
            "/search?q=machine",
            headers={"HX-Request": "true"},
        )
        html = resp.text
        assert '<form' not in html.lower(), (
            "HTMX fragment should not contain a <form> element"
        )

    @pytest.mark.asyncio
    async def test_htmx_fragment_has_export_bar(self, asgi_client):
        """The live fragment should include the search-export-bar with results."""
        resp = await asgi_client.get(
            "/search?q=machine",
            headers={"HX-Request": "true"},
        )
        html = resp.text
        assert "search-export-bar" in html, (
            "HTMX fragment should contain the export bar"
        )
        assert "Export CSV" in html or "export=csv" in html

    @pytest.mark.asyncio
    async def test_htmx_fragment_no_results_message(self, asgi_client):
        """When HTMX search finds nothing, fragment shows no-results message."""
        resp = await asgi_client.get(
            "/search?q=zzz_nonexistent_zzz",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        html = resp.text
        assert "No results found" in html, (
            "HTMX fragment should show no-results message"
        )
        # Still no full-page chrome
        assert "<html" not in html.lower()
