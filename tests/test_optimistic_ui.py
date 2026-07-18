"""Tests for optimistic UI implementation.

Verifies:
- JS module exists and has correct structure (handlers, interceptor, init)
- CSS classes are defined in styles.css
- Templates have data-optimistic attributes on mutation forms
- rendering.py tag-remove forms have data-optimistic
- base.html loads the required JS scripts
- Progressive enhancement: forms still work without JS (data-optimistic
  is opt-in, not required for form submission)
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "web" / "static"
TEMPLATES = Path(__file__).resolve().parent.parent / "src" / "web" / "templates"
SRC = Path(__file__).resolve().parent.parent / "src" / "web"


class TestOptimisticUIJS:
    """Verify optimistic-ui.js structure and API."""

    @pytest.fixture
    def js_content(self):
        return (STATIC / "js" / "optimistic-ui.js").read_text()

    def test_file_exists(self, js_content):
        assert len(js_content) > 1000, "optimistic-ui.js must be substantial"

    def test_iife_wrapping(self, js_content):
        assert "(function ()" in js_content
        assert "})();" in js_content

    def test_has_all_handlers(self, js_content):
        """All 7 mutation handlers must be present."""
        for handler in [
            "handleSingleDelete",
            "handleBulkDelete",
            "handleSingleTagAdd",
            "handleTagRemove",
            "handleBulkTag",
            "handleBulkMove",
            "handleCollectionAssign",
        ]:
            assert handler in js_content, f"Missing handler: {handler}"

    def test_handler_map_complete(self, js_content):
        for key in [
            "single-delete",
            "bulk-delete",
            "single-tag-add",
            "tag-remove",
            "bulk-tag",
            "bulk-move",
            "collection-assign",
        ]:
            assert f"'{key}'" in js_content, f"Missing handler map key: {key}"

    def test_submit_interceptor(self, js_content):
        assert "interceptSubmit" in js_content
        assert "data-optimistic" in js_content
        assert "event.preventDefault" in js_content

    def test_htmx_skip(self, js_content):
        """Forms with hx-post etc. should NOT be intercepted."""
        assert "hx-post" in js_content
        assert "hx-put" in js_content
        assert "hx-patch" in js_content
        assert "hx-delete" in js_content

    def test_loading_indicators(self, js_content):
        """Button loading + spinner + element removing must be present."""
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
        """If handler fails, form should fall back to normal submit."""
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
        """Tag remove should find badge via form.closest('.tag-pill')."""
        assert "closest('.tag-pill')" in js_content

    def test_bulk_delete_updates_buttons(self, js_content):
        assert "updateBulkActionButtons" in js_content


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
        """prefers-reduced-motion must disable animations."""
        assert "prefers-reduced-motion" in css_content
        # Check it's in a reduced-motion block for optimistic classes
        reduced_section = css_content.split("prefers-reduced-motion: reduce")[-1]
        assert "optimistic-spinner" in reduced_section or ".optimistic-spinner" in css_content

    def test_uses_design_tokens(self, css_content):
        """CSS should use var() references, not hardcoded values."""
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
        """Regenerate-summary form should NOT have data-optimistic."""
        content = (TEMPLATES / "documents" / "detail.html").read_text()
        # Find the regenerate-summary form
        reg_match = re.search(
            r'regenerate-summary.*?</form>',
            content, re.DOTALL
        )
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
