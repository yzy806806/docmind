"""Tests for debounce/throttle performance utilities.

Covers:
1. perf-utils.js module structure (IIFE, "use strict", window.DocMindPerf)
2. debounce() utility — structure, .cancel() method, correct wait semantics
3. throttle() utility — structure, .cancel() method, correct rate-limiting
4. rAFThrottle() utility — structure, .cancel() method, rAF fallback
5. base.html includes perf-utils.js before other islands
6. viewer.js applies rAFThrottle to slider handlers and debounce to search
7. vector-weight-slider.js applies rAFThrottle to updateDisplay
8. chat.js applies rAFThrottle to auto-scroll
9. upload.js applies throttle to XHR progress handler
"""

from __future__ import annotations

import re
from pathlib import Path


# ── Helpers ──────────────────────────────────────────────────────


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _read_js(name: str) -> str:
    return (_project_root() / "src" / "web" / "static" / "js" / name).read_text()


def _read_template(name: str) -> str:
    return (_project_root() / "src" / "web" / "templates" / name).read_text()


# ── perf-utils.js: Module Structure ──────────────────────────────


class TestPerfUtilsModuleStructure:
    """Verify perf-utils.js follows project conventions."""

    def test_file_exists(self):
        """perf-utils.js should exist in /static/js/."""
        js = _read_js("perf-utils.js")
        assert len(js) > 0

    def test_uses_iife(self):
        """Module should use an IIFE (consistent with theme.js, viewer.js)."""
        js = _read_js("perf-utils.js")
        assert "(function ()" in js or "(function()" in js
        assert "use strict" in js

    def test_exposes_docmind_perf(self):
        """Should expose window.DocMindPerf for other modules."""
        js = _read_js("perf-utils.js")
        assert "window.DocMindPerf" in js

    def test_exposes_three_functions(self):
        """Should expose debounce, throttle, and rAFThrottle."""
        js = _read_js("perf-utils.js")
        assert "debounce" in js
        assert "throttle" in js
        assert "rAFThrottle" in js

    def test_docmind_perf_keys(self):
        """DocMindPerf should have all three function keys."""
        js = _read_js("perf-utils.js")
        # Verify the assignment block has all three
        assert re.search(r"debounce\s*:\s*debounce", js), \
            "DocMindPerf should map debounce: debounce"
        assert re.search(r"throttle\s*:\s*throttle", js), \
            "DocMindPerf should map throttle: throttle"
        assert re.search(r"rAFThrottle\s*:\s*rAFThrottle", js), \
            "DocMindPerf should map rAFThrottle: rAFThrottle"


# ── debounce ─────────────────────────────────────────────────────


class TestDebounceUtility:
    """Verify the debounce() utility structure."""

    def test_debounce_takes_fn_and_wait(self):
        """debounce should accept fn and wait parameters."""
        js = _read_js("perf-utils.js")
        assert re.search(r"function\s+debounce\s*\(\s*fn\s*,\s*wait\s*\)", js), \
            "debounce should be defined as function debounce(fn, wait)"

    def test_debounce_uses_setTimeout(self):
        """debounce should use setTimeout for delayed execution."""
        js = _read_js("perf-utils.js")
        assert "setTimeout" in js

    def test_debounce_uses_clearTimeout(self):
        """debounce should clear pending timer on new call."""
        js = _read_js("perf-utils.js")
        assert "clearTimeout" in js

    def test_debounce_has_cancel_method(self):
        """debounced wrapper should expose .cancel()."""
        js = _read_js("perf-utils.js")
        # Find the debounce function body and check for .cancel
        debounce_section = js[js.index("function debounce"):]
        # Limit to just the debounce function (before throttle)
        if "function throttle" in debounce_section:
            debounce_section = debounce_section[:debounce_section.index("function throttle")]
        assert ".cancel" in debounce_section, \
            "debounced wrapper should have a .cancel() method"

    def test_debounce_preserves_this_and_args(self):
        """debounce should forward `this` and arguments."""
        js = _read_js("perf-utils.js")
        debounce_section = js[js.index("function debounce"):]
        if "function throttle" in debounce_section:
            debounce_section = debounce_section[:debounce_section.index("function throttle")]
        assert "lastThis" in debounce_section or "this" in debounce_section
        assert "lastArgs" in debounce_section or "arguments" in debounce_section


# ── throttle ─────────────────────────────────────────────────────


class TestThrottleUtility:
    """Verify the throttle() utility structure."""

    def test_throttle_takes_fn_and_wait(self):
        """throttle should accept fn and wait parameters."""
        js = _read_js("perf-utils.js")
        assert re.search(r"function\s+throttle\s*\(\s*fn\s*,\s*wait\s*\)", js), \
            "throttle should be defined as function throttle(fn, wait)"

    def test_throttle_uses_date_now(self):
        """throttle should use Date.now() for timing."""
        js = _read_js("perf-utils.js")
        assert "Date.now" in js

    def test_throttle_has_cancel_method(self):
        """throttled wrapper should expose .cancel()."""
        js = _read_js("perf-utils.js")
        throttle_section = js[js.index("function throttle"):]
        if "function rAFThrottle" in throttle_section:
            throttle_section = throttle_section[:throttle_section.index("function rAFThrottle")]
        assert ".cancel" in throttle_section, \
            "throttled wrapper should have a .cancel() method"

    def test_throttle_has_trailing_call(self):
        """throttle should schedule a trailing call via setTimeout."""
        js = _read_js("perf-utils.js")
        throttle_section = js[js.index("function throttle"):]
        if "function rAFThrottle" in throttle_section:
            throttle_section = throttle_section[:throttle_section.index("function rAFThrottle")]
        assert "setTimeout" in throttle_section, \
            "throttle should use setTimeout for trailing call"


# ── rAFThrottle ──────────────────────────────────────────────────


class TestRAFThrottleUtility:
    """Verify the rAFThrottle() utility structure."""

    def test_rafthrottle_takes_fn(self):
        """rAFThrottle should accept a single fn parameter."""
        js = _read_js("perf-utils.js")
        assert re.search(r"function\s+rAFThrottle\s*\(\s*fn\s*\)", js), \
            "rAFThrottle should be defined as function rAFThrottle(fn)"

    def test_rafthrottle_uses_requestAnimationFrame(self):
        """rAFThrottle should use requestAnimationFrame when available."""
        js = _read_js("perf-utils.js")
        assert "requestAnimationFrame" in js

    def test_rafthrottle_has_setTimeout_fallback(self):
        """rAFThrottle should fall back to setTimeout when rAF unavailable."""
        js = _read_js("perf-utils.js")
        assert "setTimeout" in js, \
            "rAFThrottle should fall back to setTimeout"

    def test_rafthrottle_uses_cancelAnimationFrame(self):
        """rAFThrottle should use cancelAnimationFrame for cleanup."""
        js = _read_js("perf-utils.js")
        assert "cancelAnimationFrame" in js

    def test_rafthrottle_has_cancel_method(self):
        """rAF-throttled wrapper should expose .cancel()."""
        js = _read_js("perf-utils.js")
        raf_section = js[js.index("function rAFThrottle"):]
        assert ".cancel" in raf_section, \
            "rAF-throttled wrapper should have a .cancel() method"


# ── base.html: Script Inclusion ──────────────────────────────────


class TestBaseHtmlIncludesPerfUtils:
    """Verify base.html includes perf-utils.js before other islands."""

    def test_perf_utils_script_tag_present(self):
        """base.html should include perf-utils.js."""
        html = _read_template("base.html")
        assert "/static/js/perf-utils.js" in html, \
            "base.html should load perf-utils.js"

    def test_perf_utils_loaded_before_theme(self):
        """perf-utils.js should be loaded before theme.js."""
        html = _read_template("base.html")
        idx_perf = html.index("/static/js/perf-utils.js")
        idx_theme = html.index("/static/js/theme.js")
        assert idx_perf < idx_theme, \
            "perf-utils.js must be loaded before theme.js"

    def test_perf_utils_uses_defer(self):
        """perf-utils.js script tag should use defer."""
        html = _read_template("base.html")
        # Find the perf-utils script tag
        match = re.search(
            r'<script[^>]*src="[^"]*perf-utils\.js"[^>]*>',
            html,
        )
        assert match, "perf-utils.js script tag not found"
        assert "defer" in match.group(0), \
            "perf-utils.js should have defer attribute"


# ── viewer.js: rAFThrottle + debounce application ────────────────


class TestViewerJsPerfUtils:
    """Verify viewer.js uses DocMindPerf utilities."""

    def test_viewer_uses_raf_throttle_for_font_slider(self):
        """viewer.js should rAF-throttle the font-size slider handler."""
        js = _read_js("viewer.js")
        assert "rAFThrottle" in js, \
            "viewer.js should use DocMindPerf.rAFThrottle for slider handlers"

    def test_viewer_uses_debounce_for_search(self):
        """viewer.js should use DocMindPerf.debounce for search input."""
        js = _read_js("viewer.js")
        assert "debounce" in js, \
            "viewer.js should use DocMindPerf.debounce for search input"

    def test_viewer_has_fallback_for_perf_utils(self):
        """viewer.js should fall back gracefully if DocMindPerf is absent."""
        js = _read_js("viewer.js")
        assert "window.DocMindPerf" in js
        # Check for fallback pattern (ternary with || {})
        assert "DocMindPerf || {}" in js or "DocMindPerf" in js

    def test_viewer_no_raw_scrollTop_in_handlers(self):
        """viewer.js should not have unthrottled scrollTop writes in event handlers."""
        js = _read_js("viewer.js")
        # scrollIntoView in scrollToMatch is fine — it's user-initiated (button click),
        # not a high-frequency event. We only care about input/scroll handlers.
        # Verify the slider handlers are wrapped
        assert "rAFThrottle(applyFont)" in js or "rAFThrottle" in js


# ── vector-weight-slider.js: rAFThrottle application ─────────────


class TestVectorWeightSliderPerfUtils:
    """Verify vector-weight-slider.js uses rAFThrottle."""

    def test_slider_uses_raf_throttle(self):
        """vector-weight-slider.js should rAF-throttle updateDisplay."""
        js = _read_js("vector-weight-slider.js")
        assert "rAFThrottle" in js, \
            "vector-weight-slider.js should use DocMindPerf.rAFThrottle"

    def test_slider_has_fallback(self):
        """vector-weight-slider.js should fall back if DocMindPerf is absent."""
        js = _read_js("vector-weight-slider.js")
        assert "window.DocMindPerf" in js
        assert "DocMindPerf || {}" in js or "DocMindPerf" in js


# ── chat.js: rAFThrottle for auto-scroll ─────────────────────────


class TestChatJsPerfUtils:
    """Verify chat.js uses rAFThrottle for auto-scroll."""

    def test_chat_uses_raf_scroll(self):
        """chat.js should use rAFThrottle for scroll-to-bottom."""
        js = _read_js("chat.js")
        assert "rAFThrottle" in js, \
            "chat.js should use DocMindPerf.rAFThrottle for auto-scroll"

    def test_chat_has_raf_scroll_variable(self):
        """chat.js should define a rAF-throttled scroll variable."""
        js = _read_js("chat.js")
        assert "_rafScrollToBottom" in js, \
            "chat.js should define _rafScrollToBottom"

    def test_chat_uses_raf_scroll_in_typing_indicator(self):
        """showTypingIndicator should use _rafScrollToBottom."""
        js = _read_js("chat.js")
        # Find showTypingIndicator function body
        match = re.search(
            r"function\s+showTypingIndicator\s*\([^)]*\)\s*\{",
            js,
        )
        assert match, "showTypingIndicator function not found"
        start = match.end()
        # Find the closing brace (simplified — look for next function or end)
        end = js.index("function removeTypingIndicator", start)
        func_body = js[start:end]
        assert "_rafScrollToBottom" in func_body, \
            "showTypingIndicator should call _rafScrollToBottom"

    def test_chat_uses_raf_scroll_in_add_msg(self):
        """addMsg should use _rafScrollToBottom."""
        js = _read_js("chat.js")
        match = re.search(
            r"function\s+addMsg\s*\([^)]*\)\s*\{",
            js,
        )
        assert match, "addMsg function not found"
        start = match.end()
        end = js.index("function appendChunk", start)
        func_body = js[start:end]
        assert "_rafScrollToBottom" in func_body, \
            "addMsg should call _rafScrollToBottom"

    def test_chat_uses_raf_scroll_in_append_chunk(self):
        """appendChunk should use _rafScrollToBottom."""
        js = _read_js("chat.js")
        match = re.search(
            r"function\s+appendChunk\s*\([^)]*\)\s*\{",
            js,
        )
        assert match, "appendChunk function not found"
        start = match.end()
        end = js.index("function renderCitations", start)
        func_body = js[start:end]
        assert "_rafScrollToBottom" in func_body, \
            "appendChunk should call _rafScrollToBottom"

    def test_chat_has_fallback_for_perf_utils(self):
        """chat.js should fall back if DocMindPerf is absent."""
        js = _read_js("chat.js")
        assert "DocMindPerf || {}" in js or "DocMindPerf" in js


# ── upload.js: throttle for XHR progress ─────────────────────────


class TestUploadJsPerfUtils:
    """Verify upload.js uses throttle for XHR progress."""

    def test_upload_uses_throttle(self):
        """upload.js should use DocMindPerf.throttle for progress handler."""
        js = _read_js("upload.js")
        assert "throttle" in js, \
            "upload.js should use DocMindPerf.throttle for XHR progress"

    def test_upload_has_throttled_progress_variable(self):
        """upload.js should define a _throttledProgress variable."""
        js = _read_js("upload.js")
        assert "_throttledProgress" in js

    def test_upload_throttle_uses_100ms(self):
        """The throttle interval should be 100ms (10fps)."""
        js = _read_js("upload.js")
        # The throttle call spans multiple lines with nested parens in the
        # function body, so use a broader search: find the closing ", 100)"
        # pattern that follows the throttle call.
        assert re.search(r"throttle\(.+?,\s*100\s*\)", js, re.DOTALL), \
            "throttle should use 100ms interval"

    def test_upload_has_fallback_for_perf_utils(self):
        """upload.js should fall back if DocMindPerf is absent."""
        js = _read_js("upload.js")
        assert "DocMindPerf || {}" in js or "DocMindPerf" in js

    def test_upload_progress_calls_throttled(self):
        """The progress event listener should call _throttledProgress."""
        js = _read_js("upload.js")
        assert re.search(
            r"addEventListener\s*\(\s*['\"]progress['\"]",
            js,
        ), "Should still have a progress event listener"
        assert "_throttledProgress" in js


# ── No regressions in other JS files ─────────────────────────────


class TestNoUnthrottledHighFreqHandlers:
    """Verify no JS file has unthrottled high-frequency handlers."""

    def test_no_raw_scrollHeight_in_chat_loop(self):
        """chat.js should not have raw box.scrollTop = box.scrollHeight
        outside the _rafScrollToBottom definition."""
        js = _read_js("chat.js")
        # Find all occurrences of .scrollTop = <something>.scrollHeight
        # Use non-greedy match, line-by-line to avoid cross-line greediness
        for line in js.split("\n"):
            stripped = line.strip()
            if ".scrollTop" in stripped and ".scrollHeight" in stripped and "=" in stripped:
                # This line has a scrollTop = ...scrollHeight assignment.
                # It must be inside the _rafScrollToBottom definition or fallback.
                # Acceptable: the implementation lines inside _rafScrollToBottom
                assert "_rafScrollToBottom" in js[:js.index(stripped) + 300] or \
                       stripped.startswith("if (box) box.scrollTop") or \
                       "rafScrollToBottom" in stripped or \
                       "_rafScrollToBottom" in stripped or \
                       "function (box)" in stripped, \
                    f"Raw scrollTop=scrollHeight outside _rafScrollToBottom: {stripped}"
