"""Tests for responsive design implementation (Phase 6b, commit 5b7e18c).

Static analysis tests verifying:
1. CSS file exists with expected media query breakpoints (480px, 768px, 1024px)
2. .table-scroll containers in documents list, jobs, and dashboard templates
3. Filter panel stacking at ≤768px (CSS rule exists)
4. Touch target sizes meet accessibility minimums (44px checkbox, 36px pagination)
5. No inline <style> blocks remain in extracted templates
"""

from __future__ import annotations

import re
from pathlib import Path


# ── Helpers ───────────────────────────────────────────────────────


def _project_root() -> Path:
    """Return the project root (parent of tests/)."""
    return Path(__file__).resolve().parent.parent


def _css_path() -> Path:
    return _project_root() / "src" / "web" / "static" / "css" / "styles.css"


def _template_path(name: str) -> Path:
    return _project_root() / "src" / "web" / "templates" / name


def _read_css() -> str:
    return _css_path().read_text()


def _read_template(name: str) -> str:
    return _template_path(name).read_text()


# ── 1. CSS file and media query breakpoints ────────────────────────


class TestCSSFileExists:
    """CSS stylesheet exists and is non-empty."""

    def test_css_file_exists(self):
        """The external stylesheet should exist at the expected path."""
        assert _css_path().exists(), (
            f"Expected CSS file at {_css_path()}, but it does not exist"
        )

    def test_css_file_is_non_empty(self):
        """The external stylesheet should contain actual rules."""
        css = _read_css()
        assert len(css) > 100, (
            f"CSS file at {_css_path()} is too short ({len(css)} chars); "
            "it should contain all extracted styles"
        )

    def test_css_file_has_table_scroll_rule(self):
        """The external stylesheet should define .table-scroll with overflow-x: auto."""
        css = _read_css()
        assert ".table-scroll" in css, (
            "CSS should contain a .table-scroll selector"
        )
        # Verify overflow-x handling exists in or near the .table-scroll block
        assert "overflow-x: auto" in css or "overflow-x:auto" in css, (
            "CSS should use overflow-x: auto for .table-scroll containers"
        )


class TestMediaQueryBreakpoints:
    """Verify the tiered responsive breakpoints exist in CSS."""

    def test_breakpoint_1024px_exists(self):
        """CSS should have a @media (max-width: 1024px) breakpoint."""
        css = _read_css()
        assert "@media (max-width: 1024px)" in css, (
            "Missing 1024px breakpoint for tablet landscape / small laptop"
        )

    def test_breakpoint_768px_exists(self):
        """CSS should have a @media (max-width: 768px) breakpoint."""
        css = _read_css()
        assert "@media (max-width: 768px)" in css, (
            "Missing 768px breakpoint for tablet portrait / large phone"
        )

    def test_breakpoint_480px_exists(self):
        """CSS should have a @media (max-width: 480px) breakpoint."""
        css = _read_css()
        assert "@media (max-width: 480px)" in css, (
            "Missing 480px breakpoint for small phone"
        )

    def test_media_queries_are_not_empty(self):
        """Each breakpoint should contain actual CSS rules, not just braces."""
        css = _read_css()
        # Find the 1024px block
        m1024 = re.search(
            r"@media \(max-width: 1024px\) \{([^}]+)\}", css, re.DOTALL
        )
        assert m1024 is not None, "1024px media query block not found"
        assert m1024.group(1).strip(), "1024px media query block is empty"

        m768 = re.search(
            r"@media \(max-width: 768px\) \{([^}]+)", css, re.DOTALL
        )
        assert m768 is not None, "768px media query block not found"
        assert m768.group(1).strip(), "768px media query block is empty"

        m480 = re.search(
            r"@media \(max-width: 480px\) \{([^}]+)\}", css, re.DOTALL
        )
        assert m480 is not None, "480px media query block not found"
        assert m480.group(1).strip(), "480px media query block is empty"


# ── 2. Table scroll containers ────────────────────────────────────


class TestTableScrollContainers:
    """Verify .table-scroll wrappers exist in table-heavy templates."""

    def test_documents_list_has_table_scroll(self):
        """Documents list template should wrap table in .table-scroll."""
        html = _read_template("documents/list.html")
        assert 'class="table-scroll"' in html, (
            "documents/list.html should contain a .table-scroll wrapper "
            "for horizontal scroll on mobile"
        )

    def test_jobs_list_has_table_scroll(self):
        """Jobs list template should wrap table in .table-scroll."""
        html = _read_template("jobs.html")
        assert 'class="table-scroll"' in html, (
            "jobs.html should contain a .table-scroll wrapper "
            "for horizontal scroll on mobile"
        )

    def test_dashboard_has_table_scroll(self):
        """Dashboard template should wrap recent docs table in .table-scroll."""
        html = _read_template("dashboard.html")
        assert 'class="table-scroll"' in html, (
            "dashboard.html should contain a .table-scroll wrapper "
            "for horizontal scroll on mobile"
        )


# ── 3. Filter panel stacking at ≤768px ───────────────────────────


class TestFilterPanelStacking:
    """Verify faceted filter panel stacks vertically at ≤768px."""

    def test_filter_form_vertical_at_768px(self):
        """CSS should make #facet-filter-form flex-direction: column at ≤768px."""
        css = _read_css()
        # Find the 768px block content
        m768_start = css.find("@media (max-width: 768px)")
        assert m768_start != -1, "768px media query not found"

        # Find the closing brace of the 768px block
        # The block ends just before the 480px block
        m480_start = css.find("@media (max-width: 480px)")
        assert m480_start != -1, "480px media query not found"

        block_768 = css[m768_start:m480_start]

        assert "#facet-filter-form" in block_768, (
            "768px breakpoint should contain rules for #facet-filter-form"
        )
        assert "flex-direction: column" in block_768, (
            "Filter panel should stack vertically (flex-direction: column) at ≤768px"
        )

    def test_filter_inputs_full_width_at_768px(self):
        """Filter form inputs should go full-width at ≤768px."""
        css = _read_css()
        m768_start = css.find("@media (max-width: 768px)")
        m480_start = css.find("@media (max-width: 480px)")
        block_768 = css[m768_start:m480_start]

        assert (
            "#facet-filter-form input[type=\"text\"]" in block_768
            or "#facet-filter-form .facet-select" in block_768
        ), (
            "768px breakpoint should contain rules for filter form controls"
        )
        assert "width: 100%" in block_768, (
            "Filter form controls should be width: 100% at ≤768px"
        )


# ── 4. Touch target sizes ─────────────────────────────────────────


class TestTouchTargetSizes:
    """Verify touch-friendly minimum sizes for interactive elements."""

    def test_pagination_min_height_36px(self):
        """Pagination links should have min-height: 36px for touch friendliness."""
        css = _read_css()
        assert ".pagination" in css, "Pagination CSS selector should exist"

        # Find the pagination block and verify min-height
        pag_start = css.find(".pagination a, .pagination span")
        assert pag_start != -1, "Pagination link CSS rules not found"

        # Read a chunk after the pagination link rule
        chunk = css[pag_start:pag_start + 500]
        assert "min-height: 36px" in chunk or "min-height:36px" in chunk, (
            "Pagination links should have min-height: 36px for touch targets"
        )

    def test_checkbox_tap_area_44px(self):
        """Table checkboxes should have 44px minimum tap area."""
        css = _read_css()
        # The rule td input[type="checkbox"] should exist with 44px min dimensions
        assert 'td input[type="checkbox"]' in css, (
            "CSS should contain a rule for td input[type=\"checkbox\"]"
        )

        # Check for 44px minimum dimensions
        assert "min-width: 44px" in css or "min-width:44px" in css, (
            "Checkbox tap area should have min-width: 44px"
        )
        assert "min-height: 44px" in css or "min-height:44px" in css, (
            "Checkbox tap area should have min-height: 44px"
        )

    def test_touch_target_comment_exists(self):
        """CSS should document touch target sizing (Task 8.7)."""
        css = _read_css()
        assert "Touch Target" in css, (
            "CSS should have a section comment documenting touch target sizing"
        )


# ── 5. No inline <style> blocks ───────────────────────────────────


class TestNoInlineStyleBlocks:
    """Verify that templates no longer contain inline <style> blocks.
    Per Phase 6b, Task 8.1: all CSS was extracted to styles.css."""

    EXTRACTED_TEMPLATES = [
        "base.html",
        "dashboard.html",
        "analytics.html",
        "viewer.html",
        "upload_form.html",
        "login.html",
    ]

    def test_no_inline_style_tags_in_extracted_templates(self):
        """No extracted template should contain <style> tags."""
        for name in self.EXTRACTED_TEMPLATES:
            path = _template_path(name)
            assert path.exists(), f"Template {name} does not exist at {path}"
            html = path.read_text()
            assert "<style" not in html, (
                f"{name} contains inline <style> block(s) — "
                f"all styles should be in styles.css (Phase 6b, Task 8.1)"
            )

    def test_templates_link_external_stylesheet(self):
        """Extracted templates should reference the external stylesheet."""
        for name in self.EXTRACTED_TEMPLATES:
            path = _template_path(name)
            html = path.read_text()

            # Templates that extend base.html may not directly link CSS
            # (base.html provides the <link>). So check either the template
            # itself or base.html for the stylesheet reference.
            if '{% extends "base.html" %}' in html:
                # The stylesheet link is in base.html
                base_html = _read_template("base.html")
                assert "/static/css/styles.css" in base_html, (
                    f"base.html (parent of {name}) should link to styles.css"
                )
            else:
                assert "/static/css/styles.css" in html, (
                    f"{name} should reference the external stylesheet"
                )


# ── 6. Additional responsive design verifications ─────────────────


class TestResponsiveDesignExtras:
    """Additional verifications for responsive design implementation."""

    def test_viewport_meta_in_base(self):
        """Base template should include viewport meta tag for mobile."""
        html = _read_template("base.html")
        assert 'name="viewport"' in html, (
            "base.html should include a viewport meta tag"
        )
        assert "width=device-width" in html, (
            "viewport meta should use width=device-width"
        )

    def test_nav_toggle_in_base(self):
        """Base template should have a navigation toggle for mobile."""
        html = _read_template("base.html")
        assert "nav-toggle" in html, (
            "base.html should have a nav-toggle element for mobile navigation"
        )

    def test_chat_sidebar_350px_at_768px(self):
        """Chat sidebar max-height should be 350px at ≤768px (Task 8.5)."""
        css = _read_css()
        m768_start = css.find("@media (max-width: 768px)")
        m480_start = css.find("@media (max-width: 480px)")
        block_768 = css[m768_start:m480_start]

        assert ".chat-sidebar" in block_768, (
            "768px breakpoint should contain .chat-sidebar rules"
        )
        assert "350px" in block_768, (
            "Chat sidebar max-height should be increased to 350px at ≤768px (Task 8.5)"
        )

    def test_prefers_reduced_motion_support(self):
        """CSS should include prefers-reduced-motion media query (Task 8.9)."""
        css = _read_css()
        assert "@media (prefers-reduced-motion: reduce)" in css, (
            "CSS should include prefers-reduced-motion support"
        )
        # Verify it disables animations/transitions
        m_motion_start = css.find("@media (prefers-reduced-motion: reduce)")
        # Look ahead a reasonable chunk
        chunk = css[m_motion_start:m_motion_start + 400]
        assert "animation-duration: 0.01ms" in chunk or "animation-duration:0.01ms" in chunk, (
            "prefers-reduced-motion should disable animation durations"
        )
        assert "transition-duration: 0.01ms" in chunk or "transition-duration:0.01ms" in chunk, (
            "prefers-reduced-motion should disable transition durations"
        )

    def test_search_export_bar_stacks_at_768px(self):
        """Search export bar should stack vertically at ≤768px (Task 8.8)."""
        css = _read_css()
        m768_start = css.find("@media (max-width: 768px)")
        m480_start = css.find("@media (max-width: 480px)")
        block_768 = css[m768_start:m480_start]

        assert ".search-export-bar" in block_768, (
            "768px breakpoint should contain .search-export-bar rules"
        )
        assert "flex-direction: column" in block_768, (
            "Search export bar should stack vertically at ≤768px"
        )

    def test_viewer_padding_16px_at_640px(self):
        """Document viewer should have 16px padding at ≤640px (Task 8.6)."""
        css = _read_css()
        # The 640px breakpoint should exist
        assert "@media (max-width: 640px)" in css, (
            "CSS should include a 640px breakpoint for viewer padding"
        )

        m640_start = css.find("@media (max-width: 640px)")
        # Look ahead ~200 chars for the rule
        chunk = css[m640_start:m640_start + 200]
        assert ".doc-reader" in chunk, (
            "640px breakpoint should contain .doc-reader rules"
        )
        assert "var(--space-4)" in chunk, (
            "Document viewer should have padding: 16px at ≤640px"
        )

    def test_viewer_padding_12px_at_480px(self):
        """Document viewer should have 12px padding at ≤480px (Task 8.6)."""
        css = _read_css()
        m480_start = css.find("@media (max-width: 480px)")
        m_end = css.find("@media (prefers-reduced-motion: reduce)", m480_start)
        if m_end == -1:
            chunk = css[m480_start:]
        else:
            chunk = css[m480_start:m_end]

        assert ".doc-reader" in chunk, (
            "480px breakpoint should contain .doc-reader rules"
        )
        assert "var(--space-3)" in chunk, (
            "Document viewer should have padding: 12px at ≤480px"
        )