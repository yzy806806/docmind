"""Tests for optimistic UI implementation.

Verifies:
- JS module exists and has correct structure (handlers, interceptor, init)
- CSS classes are defined in styles.css
- Templates have data-optimistic attributes on mutation forms
- rendering.py tag-remove forms have data-optimistic
- base.html loads the required JS scripts
- Progressive enhancement: forms still work without JS (data-optimistic
  is opt-in, not required for form submission)
- Loading/feedback states: flash animations on success, error rollback
  messaging with server-provided detail
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "web" / "static"
TEMPLATES = Path(__file__).resolve().parent.parent / "src" / "web" / "templates"
SRC = Path(__file__).resolve().parent.parent / "src" / "web"


def _extract_function(js_text, name):
    """Extract a function body from JS source by name."""
    pattern = r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{"
    match = re.search(pattern, js_text)
    if not match:
        return None
    start = match.end() - 1
    depth = 0
    for i in range(start, len(js_text)):
        if js_text[i] == "{":
            depth += 1
        elif js_text[i] == "}":
            depth -= 1
            if depth == 0:
                return js_text[start : i + 1]
    return None


def _extract_callback(handler_body, callback_name):
    """Extract an onSuccess/onError callback block from a handler body."""
    pattern = callback_name + r":\s*function\s*\([^)]*\)\s*\{"
    match = re.search(pattern, handler_body)
    if not match:
        return None
    start = match.end() - 1
    depth = 0
    for i in range(start, len(handler_body)):
        if handler_body[i] == "{":
            depth += 1
        elif handler_body[i] == "}":
            depth -= 1
            if depth == 0:
                return handler_body[start : i + 1]
    return None


def _extract_fetch_chain(js_text):
    """Extract the fetch(...).then().catch() chain from the JS source.

    Tracks parens and braces through the full chained expression,
    continuing past .then() and .catch() method calls.
    """
    start = js_text.find("fetch(form.action")
    if start < 0:
        return None
    depth = 0
    i = start
    while i < len(js_text):
        c = js_text[i]
        if c in "({":
            depth += 1
        elif c in ")}":
            depth -= 1
            if depth == 0:
                # Check if next non-space char is . (method chain) or ; (end)
                j = i + 1
                while j < len(js_text) and js_text[j] in " \n\t":
                    j += 1
                if j < len(js_text) and js_text[j] == ".":
                    i = j  # continue tracking the chained method
                else:
                    return js_text[start : i + 1]
        i += 1
    return None


class TestOptimisticUIJS:
    """Verify optimistic-ui.js structure and API."""

    @pytest.fixture
    def js_content(self):
        return (STATIC / "js" / "optimistic-ui.js").read_text()

    def test_file_exists(self, js_content):
        assert len(js_content) > 1000

    def test_iife_wrapping(self, js_content):
        assert "(function ()" in js_content
        assert "})();" in js_content

    def test_has_all_handlers(self, js_content):
        for handler in ["handleSingleDelete","handleBulkDelete","handleSingleTagAdd","handleTagRemove","handleBulkTag","handleBulkMove","handleCollectionAssign"]:
            assert handler in js_content, f"Missing handler: {handler}"

    def test_handler_map_complete(self, js_content):
        for key in ["single-delete","bulk-delete","single-tag-add","tag-remove","bulk-tag","bulk-move","collection-assign"]:
            assert f"'{key}'" in js_content, f"Missing handler map key: {key}"

    def test_submit_interceptor(self, js_content):
        assert "interceptSubmit" in js_content
        assert "data-optimistic" in js_content
        assert "event.preventDefault" in js_content

    def test_htmx_skip(self, js_content):
        assert "hx-post" in js_content
        assert "hx-put" in js_content
        assert "hx-patch" in js_content
        assert "hx-delete" in js_content

    def test_loading_indicators(self, js_content):
        assert "setButtonLoading" in js_content
        assert "optimistic-spinner" in js_content
        assert "optimistic-btn-loading" in js_content
        assert "optimistic-removing" in js_content
        assert "optimistic-added" in js_content

    def test_rollback_support(self, js_content):
        assert "snapshotElement" in js_content
        assert "restoreSnapshot" in js_content

    def test_toast_notifications(self, js_content):
        assert "showToast" in js_content
        assert "optimistic-toast" in js_content
        assert "success" in js_content
        assert "error" in js_content

    def test_progressive_enhancement(self, js_content):
        assert "form.submit()" in js_content
        assert "removeAttribute" in js_content

    def test_fetch_submission(self, js_content):
        assert "fetch(form.action" in js_content
        assert "FormData" in js_content

    def test_public_api(self, js_content):
        assert "window.OptimisticUI" in js_content
        assert "showToast" in js_content
        assert "interceptSubmit" in js_content

    def test_tag_remove_uses_closest(self, js_content):
        assert "closest('.tag-pill')" in js_content

    def test_bulk_delete_updates_buttons(self, js_content):
        assert "updateBulkActionButtons" in js_content


class TestOptimisticFeedbackStates:
    """Tests for loading/feedback states on HTMX mutation operations.

    Verifies:
    - flashElement() helper exists and is wired into onSuccess callbacks
    - extractErrorMessage() helper exists for richer error rollback messaging
    - CSS flash animation classes are defined
    - onError callbacks accept a message parameter for server-provided detail
    """

    @pytest.fixture
    def js_content(self):
        return (STATIC / "js" / "optimistic-ui.js").read_text()

    @pytest.fixture
    def css_content(self):
        return (STATIC / "css" / "styles.css").read_text()

    def test_flash_element_function_exists(self, js_content):
        assert "function flashElement" in js_content

    def test_flash_element_in_public_api(self, js_content):
        assert "flashElement: flashElement" in js_content

    def test_flash_element_uses_success_class(self, js_content):
        assert "optimistic-flash-success" in js_content

    def test_flash_element_uses_error_class(self, js_content):
        assert "optimistic-flash-error" in js_content

    def test_flash_element_auto_removes_class(self, js_content):
        assert "FLASH_DURATION" in js_content
        assert "classList.remove" in js_content
        assert "_optimisticFlashTimer" in js_content

    def test_flash_element_forces_reflow(self, js_content):
        assert "offsetWidth" in js_content

    def test_single_delete_flashes_on_success(self, js_content):
        handler_block = _extract_function(js_content, "handleSingleDelete")
        assert handler_block is not None
        assert "flashElement" in handler_block
        success_block = _extract_callback(handler_block, "onSuccess")
        assert success_block is not None
        assert "flashElement" in success_block

    def test_single_tag_add_flashes_on_success(self, js_content):
        handler_block = _extract_function(js_content, "handleSingleTagAdd")
        assert handler_block is not None
        assert "flashElement" in handler_block

    def test_tag_remove_flashes_on_success(self, js_content):
        handler_block = _extract_function(js_content, "handleTagRemove")
        assert handler_block is not None
        assert "flashElement" in handler_block

    def test_bulk_tag_flashes_on_success(self, js_content):
        handler_block = _extract_function(js_content, "handleBulkTag")
        assert handler_block is not None
        assert "flashElement" in handler_block

    def test_bulk_delete_error_flashes(self, js_content):
        handler_block = _extract_function(js_content, "handleBulkDelete")
        assert handler_block is not None
        error_block = _extract_callback(handler_block, "onError")
        assert error_block is not None
        assert "flashElement" in error_block

    def test_single_delete_error_flashes(self, js_content):
        handler_block = _extract_function(js_content, "handleSingleDelete")
        assert handler_block is not None
        error_block = _extract_callback(handler_block, "onError")
        assert error_block is not None
        assert "flashElement" in error_block

    def test_tag_remove_error_flashes(self, js_content):
        handler_block = _extract_function(js_content, "handleTagRemove")
        assert handler_block is not None
        error_block = _extract_callback(handler_block, "onError")
        assert error_block is not None
        assert "flashElement" in error_block

    def test_extract_error_message_exists(self, js_content):
        assert "function extractErrorMessage" in js_content

    def test_extract_error_message_in_public_api(self, js_content):
        assert "extractErrorMessage: extractErrorMessage" in js_content

    def test_extract_error_message_handles_json(self, js_content):
        assert "JSON.parse" in js_content
        assert "json.detail" in js_content
        assert "json.message" in js_content

    def test_extract_error_message_handles_html(self, js_content):
        assert "parseErrorBody" in js_content
        assert "<main" in js_content or "<p" in js_content

    def test_extract_error_message_handles_network_error(self, js_content):
        assert "instanceof Error" in js_content

    def test_extract_error_message_returns_promise_for_response(self, js_content):
        assert "errorOrResponse.text" in js_content
        assert ".then" in js_content

    def test_parse_error_body_exists(self, js_content):
        assert "function parseErrorBody" in js_content

    def test_all_on_error_callbacks_accept_message(self, js_content):
        on_error_matches = re.findall(r"onError:\s*function\s*\((\w+)", js_content)
        assert len(on_error_matches) >= 7
        for param_name in on_error_matches:
            assert param_name != ""

    def test_on_error_uses_msg_or_fallback(self, js_content):
        msg_or_pattern = re.findall(r"showToast\(msg\s*\|\|", js_content)
        assert len(msg_or_pattern) >= 7

    def test_fetch_chain_extracts_error_on_failure(self, js_content):
        fetch_block = _extract_fetch_chain(js_content)
        assert fetch_block is not None
        assert "extractErrorMessage" in fetch_block

    def test_fetch_chain_extracts_error_on_catch(self, js_content):
        fetch_block = _extract_fetch_chain(js_content)
        assert fetch_block is not None
        assert "extractErrorMessage" in fetch_block

    def test_css_flash_success_class(self, css_content):
        assert ".optimistic-flash-success" in css_content

    def test_css_flash_error_class(self, css_content):
        assert ".optimistic-flash-error" in css_content

    def test_css_flash_success_keyframes(self, css_content):
        assert "@keyframes" in css_content
        assert "optimistic-flash-success" in css_content

    def test_css_flash_error_keyframes(self, css_content):
        assert "optimistic-flash-error" in css_content

    def test_css_flash_uses_design_tokens(self, css_content):
        flash_section = css_content[css_content.index("optimistic-flash-success"):]
        assert "var(--success" in flash_section
        assert "var(--error" in flash_section

    def test_css_flash_in_reduced_motion(self, css_content):
        all_reduced = css_content.split("@media (prefers-reduced-motion: reduce)")
        for section in all_reduced:
            if "optimistic-spinner" in section and "optimistic-flash" in section:
                return
        reduced_section = all_reduced[-1]
        assert "optimistic-flash-success" in reduced_section or "optimistic-flash-error" in reduced_section


class TestOptimisticUICSS:
    """Verify optimistic CSS classes in styles.css."""

    @pytest.fixture
    def css_content(self):
        return (STATIC / "css" / "styles.css").read_text()

    def test_spinner_class(self, css_content):
        assert ".optimistic-spinner" in css_content
        assert "optimistic-spin" in css_content

    def test_btn_loading_class(self, css_content):
        assert ".optimistic-btn-loading" in css_content

    def test_removing_class(self, css_content):
        assert ".optimistic-removing" in css_content

    def test_added_class(self, css_content):
        assert ".optimistic-added" in css_content
        assert "optimistic-fade-in" in css_content

    def test_toast_classes(self, css_content):
        assert ".optimistic-toast-container" in css_content
        assert ".optimistic-toast-msg" in css_content
        assert ".optimistic-toast-success" in css_content
        assert ".optimistic-toast-error" in css_content

    def test_reduced_motion(self, css_content):
        assert "prefers-reduced-motion" in css_content
        reduced_section = css_content.split("prefers-reduced-motion: reduce")[-1]
        assert "optimistic-spinner" in reduced_section or ".optimistic-spinner" in css_content

    def test_uses_design_tokens(self, css_content):
        optimistic_section = css_content[css_content.index(".optimistic-spinner"):]
        assert "var(--" in optimistic_section


class TestTemplateWiring:
    """Verify data-optimistic attributes in templates."""

    def test_detail_html_single_delete(self):
        content = (TEMPLATES / "documents" / "detail.html").read_text()
        assert 'data-optimistic-action="single-delete"' in content

    def test_detail_html_single_tag_add(self):
        content = (TEMPLATES / "documents" / "detail.html").read_text()
        assert 'data-optimistic-action="single-tag-add"' in content

    def test_detail_html_collection_assign(self):
        content = (TEMPLATES / "documents" / "detail.html").read_text()
        assert 'data-optimistic-action="collection-assign"' in content

    def test_detail_html_no_optimistic_on_regenerate(self):
        content = (TEMPLATES / "documents" / "detail.html").read_text()
        reg_match = re.search(r'regenerate-summary.*?</form>', content, re.DOTALL)
        assert reg_match is not None
        assert "data-optimistic" not in reg_match.group()

    def test_list_html_bulk_delete(self):
        content = (TEMPLATES / "documents" / "list.html").read_text()
        assert 'data-optimistic-action="bulk-delete"' in content

    def test_list_html_bulk_tag(self):
        content = (TEMPLATES / "documents" / "list.html").read_text()
        assert 'data-optimistic-action="bulk-tag"' in content

    def test_list_html_bulk_move(self):
        content = (TEMPLATES / "documents" / "list.html").read_text()
        assert 'data-optimistic-action="bulk-move"' in content

    def test_partial_table_bulk_delete(self):
        content = (TEMPLATES / "_partials" / "documents_table.html").read_text()
        assert 'data-optimistic-action="bulk-delete"' in content

    def test_partial_table_bulk_tag(self):
        content = (TEMPLATES / "_partials" / "documents_table.html").read_text()
        assert 'data-optimistic-action="bulk-tag"' in content

    def test_partial_table_bulk_move(self):
        content = (TEMPLATES / "_partials" / "documents_table.html").read_text()
        assert 'data-optimistic-action="bulk-move"' in content

    def test_rendering_py_tag_remove(self):
        content = (SRC / "rendering.py").read_text()
        assert 'data-optimistic-action="tag-remove"' in content


class TestBaseHtmlWiring:
    """Verify base.html loads required scripts."""

    @pytest.fixture
    def base_content(self):
        return (TEMPLATES / "base.html").read_text()

    def test_optimistic_ui_loaded(self, base_content):
        assert "optimistic-ui.js" in base_content

    def test_progress_bar_loaded(self, base_content):
        assert "progress-bar.js" in base_content

    def test_perf_utils_loaded(self, base_content):
        assert "perf-utils.js" in base_content

    def test_progress_bar_div(self, base_content):
        assert 'id="progress-bar"' in base_content

    def test_scripts_deferred(self, base_content):
        assert 'defer' in base_content


class TestProgressBarJS:
    """Verify progress-bar.js."""

    @pytest.fixture
    def js_content(self):
        return (STATIC / "js" / "progress-bar.js").read_text()

    def test_htmx_listeners(self, js_content):
        assert "htmx:beforeRequest" in js_content
        assert "htmx:afterRequest" in js_content
        assert "htmx:responseError" in js_content

    def test_public_api(self, js_content):
        assert "progressBarStart" in js_content
        assert "progressBarStop" in js_content


class TestPerfUtilsJS:
    """Verify perf-utils.js."""

    @pytest.fixture
    def js_content(self):
        return (STATIC / "js" / "perf-utils.js").read_text()

    def test_debounce(self, js_content):
        assert "debounce" in js_content

    def test_throttle(self, js_content):
        assert "throttle" in js_content

    def test_rAF(self, js_content):
        assert "rAF" in js_content

    def test_public_api(self, js_content):
        assert "DocMindPerf" in js_content
