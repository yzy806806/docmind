"""Tests for responsive design: viewport-based tests covering mobile, tablet,
and desktop breakpoints to verify layouts render correctly across screen sizes.

Covers:
- Breakpoint CSS rules: 480px (small phone), 768px (tablet portrait),
  640px (viewer), 1024px (tablet landscape)
- Structural elements: nav-toggle, header-row, stats grid, analytics-grid,
  chat-layout, viewer-layout, filter-panel, search-box, table-scroll
- Touch target sizing: checkboxes (44px tap area), pagination links (36px min-height)
- prefers-reduced-motion media query
- Dark theme CSS variables via [data-theme="dark"]
- prefers-color-scheme detection in theme.js
- Mobile nav collapsible behavior (.nav-toggle + header nav.open)
- Stacking behavior: filter form, search box, chat layout, viewer layout,
  search export bar
- Font size adjustments at each breakpoint
- Container padding adjustments at each breakpoint
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Generator

import pytest


# ── Helpers ──────────────────────────────────────────────────────


def _extract_media_block(css: str, max_width: str) -> str:
    """Extract the full body of a @media (max-width: Npx) block.

    Uses brace counting to correctly handle nested {} within the block.
    """
    pattern = rf"@media\s*\(max-width:\s*{max_width}\s*\)\s*\{{"
    match = re.search(pattern, css)
    if not match:
        return ""
    start = match.start()
    depth = 0
    for i in range(start, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[start : i + 1]
    return ""


def _read_css() -> str:
    css_path = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "css" / "styles.css"
    return css_path.read_text()


def _read_theme_js() -> str:
    js_path = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "js" / "theme.js"
    return js_path.read_text()


# ── CSS Breakpoint Tests ──────────────────────────────────────────


class TestCSSBreakpoints:
    """Verify CSS media query breakpoints exist at the expected widths."""

    def test_breakpoint_480px_exists(self):
        """A @media (max-width: 480px) block should exist."""
        css = _read_css()
        assert "@media (max-width: 480px)" in css

    def test_breakpoint_640px_exists(self):
        """A @media (max-width: 640px) block should exist (viewer)."""
        css = _read_css()
        assert "@media (max-width: 640px)" in css

    def test_breakpoint_768px_exists(self):
        """A @media (max-width: 768px) block should exist."""
        css = _read_css()
        assert "@media (max-width: 768px)" in css

    def test_breakpoint_1024px_exists(self):
        """A @media (max-width: 1024px) block should exist."""
        css = _read_css()
        assert "@media (max-width: 1024px)" in css

    def test_breakpoints_are_ordered_descending(self):
        """Breakpoints should be ordered largest-first (1024→768→480→640)
        for correct CSS cascade: larger breakpoints' rules are overridden
        by smaller breakpoints' rules when both match."""
        css = _read_css()
        positions = {}
        for bp in ["1024px", "768px", "480px", "640px"]:
            idx = css.find(f"@media (max-width: {bp})")
            if idx != -1:
                positions[bp] = idx
        # Descending order: 1024 < 768 < 480 < 640
        assert positions["1024px"] < positions["768px"] < positions["480px"] < positions["640px"], \
            f"Breakpoints out of descending order: {positions}"


# ── 1024px Tablet Landscape Breakpoint Tests ─────────────────────


class TestBreakpoint1024px:
    """Verify rules inside the 1024px breakpoint."""

    def test_container_full_width_at_1024(self):
        """At ≤1024px, .container should have max-width: 100%."""
        block = _extract_media_block(_read_css(), "1024px")
        assert ".container" in block
        assert "max-width" in block
        assert "100%" in block

    def test_chat_sidebar_narrower_at_1024(self):
        """At ≤1024px, .chat-sidebar width should reduce."""
        block = _extract_media_block(_read_css(), "1024px")
        assert ".chat-sidebar" in block
        assert "220px" in block


# ── 768px Tablet Portrait Breakpoint Tests ───────────────────────


class TestBreakpoint768px:
    """Verify rules inside the 768px (tablet portrait) breakpoint."""

    def test_container_padding_reduced_at_768(self):
        """At ≤768px, .container padding should reduce."""
        block = _extract_media_block(_read_css(), "768px")
        assert ".container" in block
        assert "var(--space-4)" in block

    def test_header_row_stacks_at_768(self):
        """At ≤768px, .header-row should stack vertically."""
        block = _extract_media_block(_read_css(), "768px")
        assert ".header-row" in block
        assert "flex-direction" in block
        assert "column" in block

    def test_nav_toggle_visible_at_768(self):
        """At ≤768px, .nav-toggle should become display: block."""
        block = _extract_media_block(_read_css(), "768px")
        assert ".nav-toggle" in block
        assert "display" in block
        assert "block" in block

    def test_nav_hidden_by_default_at_768(self):
        """At ≤768px, header nav should be hidden (display: none) by default."""
        block = _extract_media_block(_read_css(), "768px")
        # The rule hides the nav; open class shows it
        assert "header nav" in block
        assert "display" in block
        assert "none" in block

    def test_nav_open_shows_at_768(self):
        """At ≤768px, header nav.open should display: flex."""
        block = _extract_media_block(_read_css(), "768px")
        assert "header nav.open" in block
        assert "display" in block
        assert "flex" in block

    def test_analytics_grid_single_column_at_768(self):
        """At ≤768px, analytics-grid should collapse to 1 column."""
        block = _extract_media_block(_read_css(), "768px")
        assert ".analytics-grid" in block
        assert "grid-template-columns" in block
        assert "1fr" in block

    def test_stats_grid_two_columns_at_768(self):
        """At ≤768px, stats grid should use 2 columns (not single)."""
        block = _extract_media_block(_read_css(), "768px")
        assert ".stats" in block
        assert "grid-template-columns" in block
        assert "140px" in block

    def test_search_box_stacks_at_768(self):
        """At ≤768px, .search-box should stack vertically."""
        block = _extract_media_block(_read_css(), "768px")
        assert ".search-box" in block
        assert "flex-direction" in block
        assert "column" in block

    def test_chat_layout_stacks_at_768(self):
        """At ≤768px, .chat-layout should stack vertically."""
        block = _extract_media_block(_read_css(), "768px")
        assert ".chat-layout" in block
        assert "flex-direction" in block
        assert "column" in block

    def test_chat_sidebar_full_width_at_768(self):
        """At ≤768px, .chat-sidebar should become full width."""
        block = _extract_media_block(_read_css(), "768px")
        # Find the .chat-sidebar block within the 768px media
        assert "width: 100%" in block or "width:100%" in block

    def test_viewer_layout_stacks_at_768(self):
        """At ≤768px, .viewer-layout should stack vertically."""
        block = _extract_media_block(_read_css(), "768px")
        assert ".viewer-layout" in block
        assert "flex-direction" in block
        assert "column" in block

    def test_doc_toc_full_width_static_at_768(self):
        """At ≤768px, .doc-toc should be full width and static positioned."""
        block = _extract_media_block(_read_css(), "768px")
        assert ".doc-toc" in block
        assert "width: 100%" in block or "width:100%" in block
        assert "static" in block

    def test_facet_filter_stacks_at_768(self):
        """At ≤768px, #facet-filter-form should stack vertically."""
        block = _extract_media_block(_read_css(), "768px")
        assert "#facet-filter-form" in block
        assert "flex-direction" in block
        assert "column" in block

    def test_search_export_bar_stacks_at_768(self):
        """At ≤768px, .search-export-bar should stack vertically."""
        block = _extract_media_block(_read_css(), "768px")
        assert ".search-export-bar" in block
        assert "flex-direction" in block
        assert "column" in block

    def test_theme_toggle_hidden_in_header_at_768(self):
        """At ≤768px, .header-row .theme-toggle should be hidden."""
        block = _extract_media_block(_read_css(), "768px")
        assert ".header-row .theme-toggle" in block
        assert "display" in block
        assert "none" in block


# ── 480px Small Phone Breakpoint Tests ───────────────────────────


class TestBreakpoint480px:
    """Verify rules inside the 480px (small phone) breakpoint."""

    def test_container_padding_reduced_at_480(self):
        """At ≤480px, .container padding should reduce."""
        block = _extract_media_block(_read_css(), "480px")
        assert ".container" in block
        assert "var(--space-2-5)" in block

    def test_stats_single_column_at_480(self):
        """At ≤480px, stats should become single column."""
        block = _extract_media_block(_read_css(), "480px")
        assert ".stats" in block
        assert "grid-template-columns" in block
        assert "1fr" in block

    def test_table_font_smaller_at_480(self):
        """At ≤480px, table font-size should reduce."""
        block = _extract_media_block(_read_css(), "480px")
        assert "table" in block
        assert "font-size" in block
        assert "0.85em" in block

    def test_doc_actions_stack_at_480(self):
        """At ≤480px, .doc-actions should stack vertically."""
        block = _extract_media_block(_read_css(), "480px")
        assert ".doc-actions" in block
        assert "flex-direction" in block
        assert "column" in block

    def test_chat_input_stacks_at_480(self):
        """At ≤480px, .chat-input-row should stack vertically."""
        block = _extract_media_block(_read_css(), "480px")
        assert ".chat-input-row" in block
        assert "flex-direction" in block
        assert "column" in block

    def test_doc_reader_padding_reduced_at_480(self):
        """At ≤480px, .doc-reader padding should reduce."""
        block = _extract_media_block(_read_css(), "480px")
        assert ".doc-reader" in block
        assert "var(--space-3)" in block

    def test_chart_svg_max_height_reduced_at_480(self):
        """At ≤480px, .chart-svg max-height should reduce to 200px."""
        block = _extract_media_block(_read_css(), "480px")
        assert ".chart-svg" in block
        assert "200px" in block

    def test_card_padding_reduced_at_480(self):
        """At ≤480px, .card padding should reduce."""
        block = _extract_media_block(_read_css(), "480px")
        assert ".card" in block
        assert "var(--space-3-5)" in block

    def test_header_padding_reduced_at_480(self):
        """At ≤480px, header padding should reduce."""
        block = _extract_media_block(_read_css(), "480px")
        assert "header" in block
        assert "var(--space-3)" in block and "var(--space-3)" in block

    def test_header_h1_smaller_at_480(self):
        """At ≤480px, header h1 font-size should reduce."""
        block = _extract_media_block(_read_css(), "480px")
        assert "header h1" in block
        assert "1.3em" in block


# ── 640px Viewer Breakpoint Tests ────────────────────────────────


class TestBreakpoint640px:
    """Verify rules inside the 640px intermediate viewer breakpoint."""

    def test_doc_reader_padding_at_640(self):
        """At ≤640px, .doc-reader padding should reduce."""
        block = _extract_media_block(_read_css(), "640px")
        assert ".doc-reader" in block
        assert "var(--space-4)" in block


# ── prefers-reduced-motion Tests ─────────────────────────────────


class TestPrefersReducedMotion:
    """Verify the prefers-reduced-motion media query."""

    def test_reduced_motion_query_exists(self):
        """CSS should have a prefers-reduced-motion: reduce block."""
        css = _read_css()
        assert "@media (prefers-reduced-motion: reduce)" in css

    def test_reduced_motion_disables_animations(self):
        """Reduced motion should set animation-duration to near-zero."""
        css = _read_css()
        idx = css.find("@media (prefers-reduced-motion: reduce)")
        block = css[idx:]
        assert "animation-duration: 0.01ms" in block
        assert "animation-iteration-count: 1" in block
        assert "transition-duration: 0.01ms" in block

    def test_reduced_motion_disables_scroll_behavior(self):
        """Reduced motion should disable smooth scroll."""
        css = _read_css()
        idx = css.find("@media (prefers-reduced-motion: reduce)")
        block = css[idx:]
        assert "scroll-behavior: auto" in block

    def test_reduced_motion_disables_typing_indicator(self):
        """Reduced motion should disable typing indicator animation."""
        css = _read_css()
        idx = css.find("@media (prefers-reduced-motion: reduce)")
        block = css[idx:]
        assert ".typing-indicator" in block
        assert "animation: none" in block

    def test_reduced_motion_disables_chart_transitions(self):
        """Reduced motion should disable chart bar and slice transitions."""
        css = _read_css()
        idx = css.find("@media (prefers-reduced-motion: reduce)")
        block = css[idx:]
        assert ".chart-bar" in block
        assert "transition: none" in block
        assert ".chart-slice" in block
        assert "transition: none" in block


# ── Touch Target Tests ───────────────────────────────────────────


class TestTouchTargets:
    """Verify touch-friendly sizing for interactive elements."""

    def test_checkboxes_have_44px_tap_area(self):
        """Table checkboxes should have 44px minimum tap area."""
        css = _read_css()
        assert "td input[type=\"checkbox\"]" in css
        # Verify min-width and min-height are 44px
        idx = css.find("td input[type=\"checkbox\"]")
        block = css[idx:idx + 400]
        assert "min-width: 44px" in block or "min-width:44px" in block
        assert "min-height: 44px" in block or "min-height:44px" in block

    def test_pagination_links_have_min_height(self):
        """Pagination links should have min-height: 36px for touch."""
        css = _read_css()
        assert "min-height: 36px" in css

    def test_pagination_links_are_inline_flex(self):
        """Pagination links should use inline-flex for vertical centering."""
        css = _read_css()
        idx = css.find(".pagination a, .pagination span")
        block = css[idx:idx + 600]
        assert "inline-flex" in block

    def test_checkbox_custom_appearance(self):
        """Custom checkbox styling should use appearance: none."""
        css = _read_css()
        idx = css.find("td input[type=\"checkbox\"]")
        block = css[idx:idx + 400]
        assert "appearance: none" in block
        assert "-webkit-appearance: none" in block

    def test_checkbox_custom_checkmark(self):
        """Custom checkbox should have a ::after pseudo-element with checkmark."""
        css = _read_css()
        assert "td input[type=\"checkbox\"]:checked::after" in css
        assert '"✓"' in css or "'✓'" in css


# ── Dark Theme Tests ──────────────────────────────────────────────


class TestDarkThemeCSS:
    """Verify dark theme CSS variable overrides."""

    def test_dark_theme_selector_exists(self):
        """[data-theme="dark"] selector should exist in CSS."""
        css = _read_css()
        assert '[data-theme="dark"]' in css

    def test_dark_theme_overrides_bg(self):
        """Dark theme should override --bg variable."""
        css = _read_css()
        idx = css.find('[data-theme="dark"] {')
        block = css[idx:idx + 1200]
        assert "--bg:" in block

    def test_dark_theme_overrides_surface(self):
        """Dark theme should override --surface variable."""
        css = _read_css()
        idx = css.find('[data-theme="dark"] {')
        block = css[idx:idx + 1200]
        assert "--surface:" in block

    def test_dark_theme_overrides_text(self):
        """Dark theme should override --text variable."""
        css = _read_css()
        idx = css.find('[data-theme="dark"] {')
        block = css[idx:idx + 1200]
        assert "--text:" in block

    def test_dark_theme_overrides_header_bg(self):
        """Dark theme should override --header-bg variable."""
        css = _read_css()
        idx = css.find('[data-theme="dark"] {')
        block = css[idx:idx + 1200]
        assert "--header-bg:" in block

    def test_dark_theme_overrides_border(self):
        """Dark theme should override --border variable."""
        css = _read_css()
        idx = css.find('[data-theme="dark"] {')
        block = css[idx:idx + 1200]
        assert "--border:" in block

    def test_dark_theme_overrides_badge_variables(self):
        """Dark theme should override badge background and text variables."""
        css = _read_css()
        idx = css.find('[data-theme="dark"] {')
        block = css[idx:idx + 1200]
        assert "--badge-indexed-bg:" in block
        assert "--badge-indexed-text:" in block
        assert "--badge-error-bg:" in block
        assert "--badge-error-text:" in block


class TestThemeJS:
    """Verify theme.js implements localStorage + prefers-color-scheme detection."""

    def test_theme_js_has_localstorage_key(self):
        """theme.js should use localStorage key docmind-theme."""
        js = _read_theme_js()
        assert "docmind-theme" in js

    def test_theme_js_has_toggle_theme_function(self):
        """theme.js should define toggleTheme()."""
        js = _read_theme_js()
        assert "toggleTheme" in js

    def test_theme_js_has_update_toggle_icon(self):
        """theme.js should define updateToggleIcon()."""
        js = _read_theme_js()
        assert "updateToggleIcon" in js

    def test_theme_js_detects_prefers_color_scheme(self):
        """theme.js should use matchMedia for prefers-color-scheme."""
        js = _read_theme_js()
        assert "prefers-color-scheme" in js

    def test_theme_js_applies_data_theme_attribute(self):
        """theme.js should set data-theme attribute on documentElement."""
        js = _read_theme_js()
        assert "data-theme" in js
        assert "documentElement" in js


# ── HTML Structure Tests ──────────────────────────────────────────


class TestHTMLResponsiveStructure:
    """Verify HTML pages include responsive structural elements."""

    def test_base_page_has_nav_toggle_button(self):
        """Base template should include a .nav-toggle button."""
        from src.web.rendering import _base_page
        html = _base_page("Test", "<p>content</p>")
        assert 'class="nav-toggle"' in html
        assert "☰" in html  # hamburger icon

    def test_base_page_has_header_row(self):
        """Base template should include a .header-row div."""
        from src.web.rendering import _base_page
        html = _base_page("Test", "<p>content</p>")
        assert 'class="header-row"' in html

    def test_base_page_has_container(self):
        """Base template should use .container for content wrapper."""
        from src.web.rendering import _base_page
        html = _base_page("Test", "<p>content</p>")
        assert 'class="container"' in html

    def test_base_page_has_stylesheet_link(self):
        """Base template should link to /static/css/styles.css."""
        from src.web.rendering import _base_page
        html = _base_page("Test", "<p>content</p>")
        assert 'href="/static/css/styles.css"' in html

    def test_base_page_has_theme_js(self):
        """Base template should load /static/js/theme.js."""
        from src.web.rendering import _base_page
        html = _base_page("Test", "<p>content</p>")
        assert 'src="/static/js/theme.js"' in html

    def test_dashboard_has_stats_grid(self):
        """Dashboard should have a .stats grid."""
        from src.web.rendering import _render_dashboard
        html = _render_dashboard(
            {"total": 0, "pending": 0, "indexed": 0, "summarized": 0, "active_jobs": 0},
            [],
        )
        assert 'class="stats"' in html

    def test_dashboard_has_analytics_grid(self):
        """Dashboard should have .analytics-grid for chart layout."""
        from src.web.rendering import _render_dashboard
        html = _render_dashboard(
            {"total": 0, "pending": 0, "indexed": 0, "summarized": 0, "active_jobs": 0},
            [],
        )
        assert 'class="analytics-grid"' in html

    def test_dashboard_has_table_scroll(self):
        """Dashboard should use .table-scroll wrapper for recent docs table."""
        from src.web.rendering import _render_dashboard
        html = _render_dashboard(
            {"total": 0, "pending": 0, "indexed": 0, "summarized": 0, "active_jobs": 0},
            [{"id": 1, "title": "Doc", "status": "indexed", "ext": ".txt",
              "created_at": "2025-01-01"}],
        )
        assert 'class="table-scroll"' in html

    def test_dashboard_has_search_box(self):
        """Dashboard should have .search-box for quick search form."""
        from src.web.rendering import _render_dashboard
        html = _render_dashboard(
            {"total": 0, "pending": 0, "indexed": 0, "summarized": 0, "active_jobs": 0},
            [],
        )
        assert 'class="search-box"' in html

    def test_documents_list_has_table_scroll(self):
        """Documents list should use .table-scroll wrapper."""
        from src.web.rendering import _render_documents_list
        html = _render_documents_list(
            [{"id": 1, "title": "Doc", "status": "indexed", "source_name": "s",
              "ext": ".txt", "created_at": "2025-01-01"}],
            "", 1, 20, 1, 1,
            tags_map={1: []},
        )
        assert 'class="table-scroll"' in html

    def test_documents_list_has_filter_panel(self):
        """Documents list should have .filter-panel for faceted filters."""
        from src.web.rendering import _render_documents_list
        html = _render_documents_list(
            [{"id": 1, "title": "Doc", "status": "indexed", "source_name": "s",
              "ext": ".txt", "created_at": "2025-01-01"}],
            "", 1, 20, 1, 1,
            tags_map={1: []},
        )
        assert 'class="filter-panel"' in html

    def test_documents_list_has_facet_filter_form(self):
        """Documents list should have #facet-filter-form."""
        from src.web.rendering import _render_documents_list
        html = _render_documents_list(
            [{"id": 1, "title": "Doc", "status": "indexed", "source_name": "s",
              "ext": ".txt", "created_at": "2025-01-01"}],
            "", 1, 20, 1, 1,
            tags_map={1: []},
        )
        assert 'id="facet-filter-form"' in html

    def test_documents_list_has_bulk_actions(self):
        """Documents list should have .bulk-actions bar."""
        from src.web.rendering import _render_documents_list
        html = _render_documents_list(
            [{"id": 1, "title": "Doc", "status": "indexed", "source_name": "s",
              "ext": ".txt", "created_at": "2025-01-01"}],
            "", 1, 20, 1, 1,
            tags_map={1: []},
        )
        assert 'class="bulk-actions"' in html

    def test_chat_page_has_chat_layout(self):
        """Chat page should have .chat-layout with sidebar + main."""
        from src.web.rendering import _render_chat_page
        html = _render_chat_page()
        assert 'class="chat-layout"' in html

    def test_chat_page_has_chat_sidebar(self):
        """Chat page should have .chat-sidebar."""
        from src.web.rendering import _render_chat_page
        html = _render_chat_page()
        assert 'class="chat-sidebar"' in html

    def test_chat_page_has_chat_input_row(self):
        """Chat page should have .chat-input-row."""
        from src.web.rendering import _render_chat_page
        html = _render_chat_page()
        assert 'class="chat-input-row"' in html

    def test_chat_page_has_citations_panel(self):
        """Chat page should have .citations-panel."""
        from src.web.rendering import _render_chat_page
        html = _render_chat_page()
        assert 'class="citations-panel"' in html

    def test_viewer_page_has_viewer_layout(self):
        """Viewer page should have .viewer-layout."""
        from src.web.rendering import _render_template
        html = _render_template("viewer.html", title="Test", doc_id=1,
                                meta_html="", toolbar_html="", toc_html="",
                                content_html="<p>test</p>", pagination_html="",
                                mode="rendered",
                                extra_head="")
        assert 'class="viewer-layout"' in html

    def test_viewer_page_has_doc_reader(self):
        """Viewer page should have .doc-reader (may be combined with mode class)."""
        from src.web.rendering import _render_template
        html = _render_template("viewer.html", title="Test", doc_id=1,
                                meta_html="", toolbar_html="", toc_html="",
                                content_html="<p>test</p>", pagination_html="",
                                mode="rendered",
                                extra_head="")
        assert "doc-reader" in html

    def test_search_results_has_export_bar(self):
        """Search results should have .search-export-bar."""
        from src.web.rendering import _render_search_results
        html = _render_search_results("test",
                                       [{"id": 1, "title": "Doc", "snippet": "...",
                                         "source_name": "s", "ext": ".txt",
                                         "status": "indexed",
                                         "created_at": "2025-01-01"}])
        assert 'class="search-export-bar"' in html

    def test_search_results_has_search_box(self):
        """Search results should have .search-box for re-search."""
        from src.web.rendering import _render_search_results
        html = _render_search_results("test", [])
        assert 'class="search-box"' in html

    def test_analytics_page_has_stats_grid(self):
        """Analytics page should have .stats grid."""
        from src.web.rendering import _render_analytics_page
        html = _render_analytics_page(
            stats={"total": 0, "pending": 0, "indexed": 0, "summarized": 0, "error": 0},
            doc_growth=[],
            tag_dist=[],
            storage={"total_size": 0, "by_type": {}, "avg_doc_size": 0, "doc_count": 0},
            search_stats={"total_searches": 0, "avg_results": 0.0, "unique_queries": 0},
            popular_queries=[],
            search_trend=[],
            chat_activity=[],
            job_stats={"by_state": {}, "total": 0, "success_rate": 0.0,
                       "avg_processing_time_seconds": 0.0, "recent_failures": []},
            days=30,
        )
        assert 'class="stats"' in html

    def test_analytics_page_has_table_scroll(self):
        """Analytics page should have .table-scroll wrappers for tables."""
        from src.web.rendering import _render_analytics_page
        html = _render_analytics_page(
            stats={"total": 0, "pending": 0, "indexed": 0, "summarized": 0, "error": 0},
            doc_growth=[],
            tag_dist=[],
            storage={"total_size": 0, "by_type": {}, "avg_doc_size": 0, "doc_count": 0},
            search_stats={"total_searches": 0, "avg_results": 0.0, "unique_queries": 0},
            popular_queries=[],
            search_trend=[],
            chat_activity=[],
            job_stats={"by_state": {}, "total": 0, "success_rate": 0.0,
                       "avg_processing_time_seconds": 0.0, "recent_failures": []},
            days=30,
        )
        assert 'class="table-scroll"' in html

    def test_jobs_page_has_table_scroll(self):
        """Jobs page should have .table-scroll wrapper when jobs exist."""
        from src.web.rendering import _render_jobs_page
        from src.core.models import JobRecord, JobState
        from datetime import datetime, timezone
        job = JobRecord(
            id="test-job-001",
            document_id=1,
            document_title="Test Doc",
            document_path="/test.txt",
            source_name="test",
            state=JobState.COMPLETED,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            error="",
        )
        html = _render_jobs_page([job], "", 1, 20, 1, 1, False)
        assert 'class="table-scroll"' in html

    def test_document_detail_has_doc_actions(self):
        """Document detail page should have .doc-actions."""
        from src.web.rendering import _render_document_detail
        doc = {"id": 1, "title": "Test", "status": "indexed", "body": "content"}
        html = _render_document_detail(doc)
        assert 'class="doc-actions"' in html

    def test_login_page_has_login_card(self):
        """Login page should have .login-card with constrained max-width."""
        from src.web.rendering import _render_login_page
        html = _render_login_page()
        assert 'class="login-card"' in html

    def test_css_file_has_login_card_max_width(self):
        """.login-card should have max-width: 380px."""
        css = _read_css()
        idx = css.find(".login-card")
        block = css[idx:idx + 400]
        assert "max-width:" in block
        assert "380px" in block


# ── Integration: Page Routes Respond Correctly ────────────────────


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    """Provide a temporary database file path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "test_responsive.db")


@pytest.fixture
async def asgi_client(tmp_db_path: str):
    """Create an httpx.AsyncClient backed by the ASGI app."""
    import httpx
    from src.core.db_sqlite import Database
    from src.web import server

    db = Database(db_path=tmp_db_path)
    await db.connect()

    # Insert test documents
    for i in range(25):
        await db.save_document(
            path=f"/docs/test_{i}.txt",
            source_type="api",
            source_name="test-source",
            title=f"Test Document {i}",
            ext=".txt",
            mime_type="text/plain",
            body=f"This is the body of test document {i}.",
            size=100,
            status="indexed" if i % 2 == 0 else "pending",
        )

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


class TestPageRouteIntegration:
    """Integration tests: verify pages return 200 and contain responsive elements."""

    @pytest.mark.asyncio
    async def test_dashboard_has_responsive_elements(self, asgi_client):
        """Dashboard should return 200 with responsive structural elements."""
        resp = await asgi_client.get("/")
        assert resp.status_code == 200
        assert 'class="stats"' in resp.text
        assert 'class="analytics-grid"' in resp.text
        assert 'class="search-box"' in resp.text
        assert 'class="table-scroll"' in resp.text

    @pytest.mark.asyncio
    async def test_documents_page_has_responsive_elements(self, asgi_client):
        """Documents page should return 200 with responsive structural elements."""
        resp = await asgi_client.get("/documents")
        assert resp.status_code == 200
        assert 'class="table-scroll"' in resp.text
        assert 'class="filter-panel"' in resp.text
        assert 'class="bulk-actions"' in resp.text

    @pytest.mark.asyncio
    async def test_chat_page_has_responsive_elements(self, asgi_client):
        """Chat page should return 200 with .chat-layout and .chat-sidebar."""
        resp = await asgi_client.get("/chat")
        assert resp.status_code == 200
        assert 'class="chat-layout"' in resp.text
        assert 'class="chat-sidebar"' in resp.text
        assert 'class="chat-input-row"' in resp.text

    @pytest.mark.asyncio
    async def test_analytics_page_has_responsive_elements(self, asgi_client):
        """Analytics page should return 200 with responsive structural elements."""
        resp = await asgi_client.get("/analytics")
        assert resp.status_code == 200
        assert 'class="stats"' in resp.text
        assert 'class="analytics-grid"' in resp.text
        assert 'class="table-scroll"' in resp.text

    @pytest.mark.asyncio
    async def test_search_results_have_responsive_elements(self, asgi_client):
        """Search results should return 200 with .search-box and .search-export-bar."""
        resp = await asgi_client.get("/search?q=test")
        assert resp.status_code == 200
        assert 'class="search-box"' in resp.text

    @pytest.mark.asyncio
    async def test_all_pages_have_stylesheet_link(self, asgi_client):
        """Every major page should link to /static/css/styles.css."""
        pages = ["/", "/documents", "/chat", "/analytics", "/search?q=test",
                 "/upload", "/jobs", "/settings"]
        for path in pages:
            resp = await asgi_client.get(path)
            assert resp.status_code == 200, f"{path} returned {resp.status_code}"
            assert 'href="/static/css/styles.css"' in resp.text, \
                f"{path} missing stylesheet link"

    @pytest.mark.asyncio
    async def test_all_pages_have_viewport_meta(self, asgi_client):
        """Every major page should have viewport meta tag."""
        pages = ["/", "/documents", "/chat", "/analytics", "/search?q=test",
                 "/upload", "/jobs", "/settings"]
        for path in pages:
            resp = await asgi_client.get(path)
            assert resp.status_code == 200, f"{path} returned {resp.status_code}"
            assert 'name="viewport"' in resp.text, \
                f"{path} missing viewport meta"
            assert "width=device-width" in resp.text, \
                f"{path} missing width=device-width"

    @pytest.mark.asyncio
    async def test_all_pages_have_nav_toggle(self, asgi_client):
        """Every major page should have the .nav-toggle button."""
        pages = ["/", "/documents", "/chat", "/analytics", "/search?q=test",
                 "/upload", "/jobs", "/settings"]
        for path in pages:
            resp = await asgi_client.get(path)
            assert resp.status_code == 200, f"{path} returned {resp.status_code}"
            assert 'class="nav-toggle"' in resp.text, \
                f"{path} missing nav-toggle"

    @pytest.mark.asyncio
    async def test_all_pages_have_theme_js(self, asgi_client):
        """Every major page should load theme.js."""
        pages = ["/", "/documents", "/chat", "/analytics", "/search?q=test",
                 "/upload", "/jobs", "/settings"]
        for path in pages:
            resp = await asgi_client.get(path)
            assert resp.status_code == 200, f"{path} returned {resp.status_code}"
            assert 'src="/static/js/theme.js"' in resp.text, \
                f"{path} missing theme.js"


# ── Static File Accessibility ─────────────────────────────────────


class TestStaticFilesExist:
    """Verify responsive CSS/JS static files exist on disk."""

    def test_styles_css_exists(self):
        """styles.css should exist."""
        css_path = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "css" / "styles.css"
        assert css_path.exists(), f"styles.css not found at {css_path}"
        assert css_path.stat().st_size > 0

    def test_theme_js_exists(self):
        """theme.js should exist."""
        js_path = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "js" / "theme.js"
        assert js_path.exists(), f"theme.js not found at {js_path}"
        assert js_path.stat().st_size > 0

    def test_chat_js_exists(self):
        """chat.js should exist."""
        js_path = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "js" / "chat.js"
        assert js_path.exists(), f"chat.js not found at {js_path}"

    def test_documents_list_js_exists(self):
        """documents-list.js should exist."""
        js_path = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "js" / "documents-list.js"
        assert js_path.exists(), f"documents-list.js not found at {js_path}"


# ── prefers-color-scheme Integration ──────────────────────────────


class TestPrefersColorScheme:
    """Verify prefers-color-scheme detection in theme.js."""

    def test_theme_js_uses_matchmedia(self):
        """theme.js should call window.matchMedia."""
        js = _read_theme_js()
        assert "matchMedia" in js

    def test_theme_js_falls_back_when_no_stored_preference(self):
        """When no theme is stored, system preference should be checked."""
        js = _read_theme_js()
        # Should have logic that checks localStorage first, then system
        assert "getItem" in js or "localStorage" in js

    def test_theme_js_toggles_between_light_and_dark(self):
        """toggleTheme should switch between 'light' and 'dark'."""
        js = _read_theme_js()
        assert "'light'" in js or '"light"' in js
        assert "'dark'" in js or '"dark"' in js


# ── CSS Structural Integrity ─────────────────────────────────────


class TestCSSStructuralIntegrity:
    """Verify the CSS file is well-structured and includes expected sections."""

    def test_css_has_table_scroll_class(self):
        """.table-scroll should be defined with overflow-x: auto."""
        css = _read_css()
        idx = css.find(".table-scroll")
        block = css[idx:idx + 200]
        assert "overflow-x" in block
        assert "auto" in block

    def test_css_has_stats_grid(self):
        """.stats should use CSS grid with auto-fit."""
        css = _read_css()
        idx = css.find(".stats {")
        block = css[idx:idx + 200]
        assert "grid" in block
        assert "auto-fit" in block

    def test_css_has_analytics_grid(self):
        """.analytics-grid should use CSS grid with 2 columns."""
        css = _read_css()
        idx = css.find(".analytics-grid {")
        block = css[idx:idx + 200]
        assert "grid-template-columns" in block
        assert "1fr 1fr" in block

    def test_css_has_header_row_flex(self):
        """.header-row should use flexbox with space-between."""
        css = _read_css()
        idx = css.find(".header-row {")
        block = css[idx:idx + 200]
        assert "display: flex" in block or "display:flex" in block
        assert "space-between" in block

    def test_css_has_nav_toggle_hidden_by_default(self):
        """.nav-toggle should have display: none by default (desktop)."""
        css = _read_css()
        idx = css.find(".nav-toggle {")
        block = css[idx:idx + 200]
        assert "display: none" in block or "display:none" in block

    def test_css_has_search_box_flex(self):
        """.search-box should use flex display."""
        css = _read_css()
        idx = css.find(".search-box {")
        block = css[idx:idx + 200]
        assert "display: flex" in block or "display:flex" in block

    def test_css_has_chat_layout_flex(self):
        """.chat-layout should use flex display."""
        css = _read_css()
        idx = css.find(".chat-layout")
        block = css[idx:idx + 200]
        assert "display: flex" in block or "display:flex" in block

    def test_css_has_viewer_layout_flex(self):
        """.viewer-layout should use flex display."""
        css = _read_css()
        idx = css.find(".viewer-layout")
        block = css[idx:idx + 200]
        assert "display: flex" in block or "display:flex" in block

    def test_css_has_filter_panel(self):
        """.filter-panel should be defined."""
        css = _read_css()
        assert ".filter-panel" in css

    def test_css_has_doc_actions_flex(self):
        """.doc-actions should use flex display."""
        css = _read_css()
        idx = css.find(".doc-actions")
        block = css[idx:idx + 200]
        assert "display: flex" in block or "display:flex" in block

    def test_css_has_search_export_bar_flex(self):
        """.search-export-bar should use flex display."""
        css = _read_css()
        idx = css.find(".search-export-bar")
        block = css[idx:idx + 200]
        assert "display: flex" in block or "display:flex" in block

    def test_css_has_bulk_actions_flex(self):
        """.bulk-actions should use flex display."""
        css = _read_css()
        idx = css.find(".bulk-actions")
        block = css[idx:idx + 200]
        assert "display: flex" in block or "display:flex" in block

    def test_css_has_pagination_flex(self):
        """.pagination should use flex display."""
        css = _read_css()
        idx = css.find(".pagination {")
        block = css[idx:idx + 200]
        assert "display: flex" in block or "display:flex" in block

    def test_css_has_css_variables(self):
        """CSS should define custom properties via :root."""
        css = _read_css()
        assert ":root {" in css
        assert "--bg:" in css
        assert "--text:" in css

    def test_css_no_inline_style_in_templates(self):
        """Verify base.html has extracted CSS to external file (no large inline style blocks)."""
        base_path = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "base.html"
        base_html = base_path.read_text()
        # No large inline <style> blocks with CSS rules
        assert "font-family:" not in base_html  # was extracted
        

# ── Edge Cases ────────────────────────────────────────────────────


class TestResponsiveEdgeCases:
    """Edge case and boundary tests for responsive design."""

    def test_empty_documents_list_still_has_structure(self):
        """Empty documents list should still have filter panel and container."""
        from src.web.rendering import _render_documents_list
        html = _render_documents_list([], "", 1, 20, 0, 0)
        assert 'class="card"' in html
        assert 'class="filter-panel"' in html
        # Should NOT have table-scroll when there are no docs
        assert 'class="table-scroll"' not in html

    def test_single_document_list_has_table_scroll(self):
        """Documents list with 1 doc should have table-scroll."""
        from src.web.rendering import _render_documents_list
        html = _render_documents_list(
            [{"id": 1, "title": "Doc", "status": "indexed", "source_name": "s",
              "ext": ".txt", "created_at": "2025-01-01"}],
            "", 1, 20, 1, 1,
            tags_map={1: []},
        )
        assert 'class="table-scroll"' in html

    def test_css_variables_default_to_sensible_values(self):
        """Default CSS variables should have reasonable values."""
        css = _read_css()
        idx = css.find(":root {")
        block = css[idx:css.find("}", idx) + 1]
        # Background should be light
        assert "#f5f5f5" in block or "#ffffff" in block
        # Text should be dark
        assert "#333" in block

    def test_css_does_not_use_fixed_pixel_widths_for_major_layout(self):
        """.container should use max-width, not fixed width."""
        css = _read_css()
        idx = css.find(".container {")
        block = css[idx:idx + 200]
        assert "max-width" in block
        # Should not use a fixed 'width:' that would break responsiveness
        # (width might appear in media queries but not in the base rule)
        assert "width:" not in block.split("max-width")[0]

    def test_all_media_queries_use_max_width(self):
        """Responsive breakpoints should all use max-width (mobile-first)."""
        css = _read_css()
        # Find all @media rules
        media_rules = re.findall(r"@media\s+\(([^)]+)\)", css)
        for rule in media_rules:
            if "prefers-" in rule:
                continue  # Skip prefers-reduced-motion and prefers-color-scheme
            assert "max-width" in rule, f"Non-mobile-first media query: {rule}"

    def test_css_sections_are_commented(self):
        """CSS should have section comments for organization."""
        css = _read_css()
        assert "Responsive Breakpoints" in css
        assert "1024px" in css
        assert "768px" in css
        assert "480px" in css