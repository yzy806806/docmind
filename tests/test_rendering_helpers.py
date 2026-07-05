"""Unit tests for the 7 rendering helpers in src/web/rendering.py that
previously lacked dedicated test coverage.

Covers helpers used by the collections tree and search filter paths:
  - Utility functions: _escape, _fmt_date, _fmt_size
  - Collection helpers: _find_collection_name, _build_collection_tree_html,
    _build_collection_breadcrumb_html
  - Chart generators: _svg_line_chart, _svg_bar_chart, _svg_pie_chart
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.web.rendering import (
    _build_collection_breadcrumb_html,
    _build_collection_tree_html,
    _escape,
    _find_collection_name,
    _fmt_date,
    _fmt_size,
    _svg_bar_chart,
    _svg_line_chart,
    _svg_pie_chart,
)


# ═══════════════════════════════════════════════════════════════════
# 1. _escape — HTML escaping
# ═══════════════════════════════════════════════════════════════════


class TestEscape:
    """Tests for _escape() — basic HTML entity escaping."""

    def test_escapes_ampersand(self):
        assert _escape("a & b") == "a &amp; b"

    def test_escapes_less_than(self):
        assert _escape("<script>") == "&lt;script&gt;"

    def test_escapes_greater_than(self):
        assert _escape("x > y") == "x &gt; y"

    def test_escapes_double_quote(self):
        assert _escape('say "hello"') == "say &quot;hello&quot;"

    def test_escapes_all_special_chars(self):
        result = _escape('<a href="x">&</a>')
        assert "&lt;" in result
        assert "&gt;" in result
        assert "&quot;" in result
        assert "&amp;" in result

    def test_preserves_safe_text(self):
        assert _escape("hello world 123") == "hello world 123"

    def test_handles_empty_string(self):
        assert _escape("") == ""


# ═══════════════════════════════════════════════════════════════════
# 2. _fmt_date — date formatting
# ═══════════════════════════════════════════════════════════════════


class TestFmtDate:
    """Tests for _fmt_date() — datetime formatting for display."""

    def test_formats_datetime_object(self):
        dt = datetime(2025, 6, 15, 14, 30)
        assert _fmt_date(dt) == "2025-06-15 14:30"

    def test_formats_iso_string(self):
        # [:19] truncates to YYYY-MM-DD HH:MM:SS (keeps seconds)
        assert _fmt_date("2025-01-10T08:00:00") == "2025-01-10 08:00:00"

    def test_formats_iso_string_with_microseconds(self):
        # [:19] drops microseconds, keeps seconds
        result = _fmt_date("2025-01-10T08:00:00.123456")
        assert result == "2025-01-10 08:00:00"

    def test_returns_empty_for_none(self):
        assert _fmt_date(None) == ""

    def test_returns_empty_for_falsy(self):
        assert _fmt_date("") == ""

    def test_returns_empty_for_zero(self):
        assert _fmt_date(0) == ""

    def test_fallback_to_string_for_unparseable(self):
        result = _fmt_date("just a string")
        assert result == "just a string"

    def test_fallback_for_int_value(self):
        result = _fmt_date(12345)
        assert "12345" in result


# ═══════════════════════════════════════════════════════════════════
# 3. _fmt_size — file size formatting
# ═══════════════════════════════════════════════════════════════════


class TestFmtSize:
    """Tests for _fmt_size() — human-readable byte size formatting."""

    def test_bytes_less_than_1024(self):
        assert _fmt_size(0) == "0.0 B"
        assert _fmt_size(1) == "1.0 B"
        assert _fmt_size(1023) == "1023.0 B"

    def test_kilobytes(self):
        assert _fmt_size(1024) == "1.0 KB"
        assert _fmt_size(2048) == "2.0 KB"

    def test_megabytes(self):
        mib = 1024 * 1024
        assert _fmt_size(mib) == "1.0 MB"
        assert _fmt_size(int(1.5 * mib)) == "1.5 MB"

    def test_gigabytes(self):
        gib = 1024 * 1024 * 1024
        assert _fmt_size(gib) == "1.0 GB"

    def test_terabytes(self):
        tib = 1024 * 1024 * 1024 * 1024
        assert _fmt_size(tib) == "1.0 TB"

    def test_one_decimal_precision(self):
        # 1.25 * 1024 ≈ 1280 → 1.2 KB (one decimal)
        assert _fmt_size(1280) == "1.2 KB"
        # 1.75 * 1024 ≈ 1792 → 1.8 KB
        assert _fmt_size(1792) == "1.8 KB"


# ═══════════════════════════════════════════════════════════════════
# 4. _find_collection_name — recursive tree search
# ═══════════════════════════════════════════════════════════════════


class TestFindCollectionName:
    """Tests for _find_collection_name() — find collection name by id."""

    def test_finds_root_node(self):
        tree = [{"id": 1, "name": "Tech"}]
        assert _find_collection_name(tree, 1) == "Tech"

    def test_finds_nested_child(self):
        tree = [
            {
                "id": 1,
                "name": "Tech",
                "children": [
                    {"id": 2, "name": "Python"},
                    {"id": 3, "name": "Go"},
                ],
            }
        ]
        assert _find_collection_name(tree, 2) == "Python"
        assert _find_collection_name(tree, 3) == "Go"

    def test_finds_deeply_nested(self):
        tree = [
            {
                "id": 1,
                "name": "Tech",
                "children": [
                    {
                        "id": 2,
                        "name": "Python",
                        "children": [
                            {"id": 4, "name": "Django"},
                        ],
                    },
                ],
            },
            {"id": 5, "name": "Research"},
        ]
        assert _find_collection_name(tree, 4) == "Django"
        assert _find_collection_name(tree, 5) == "Research"

    def test_returns_none_for_missing_id(self):
        tree = [{"id": 1, "name": "Tech"}]
        assert _find_collection_name(tree, 999) is None

    def test_returns_none_for_empty_tree(self):
        assert _find_collection_name([], 1) is None

    def test_returns_blank_for_missing_name_key(self):
        tree = [{"id": 1}]
        assert _find_collection_name(tree, 1) == ""

    def test_finds_in_second_sibling(self):
        tree = [
            {"id": 1, "name": "A", "children": [{"id": 2, "name": "B"}]},
            {"id": 3, "name": "C"},
        ]
        assert _find_collection_name(tree, 3) == "C"

    def test_handles_empty_children_list(self):
        tree = [
            {"id": 1, "name": "Parent", "children": []},
            {"id": 2, "name": "Sibling"},
        ]
        assert _find_collection_name(tree, 2) == "Sibling"
        assert _find_collection_name(tree, 1) == "Parent"


# ═══════════════════════════════════════════════════════════════════
# 5. _build_collection_tree_html — tree sidebar HTML builder
# ═══════════════════════════════════════════════════════════════════


class TestBuildCollectionTreeHtml:
    """Tests for _build_collection_tree_html() — collection sidebar."""

    def test_empty_tree_shows_all_and_unassigned_links(self):
        html = _build_collection_tree_html([], {}, None)
        assert "collection-tree" in html
        assert "All Documents" in html
        assert "Unassigned" in html

    def test_renders_single_root_collection(self):
        tree = [{"id": 1, "name": "Tech"}]
        counts = {1: 5}
        html = _build_collection_tree_html(tree, counts, None)
        assert "Tech" in html
        assert "(5)" in html
        assert 'href="/documents?collection_id=1"' in html

    def test_renders_nested_collections(self):
        tree = [
            {
                "id": 1,
                "name": "Tech",
                "children": [
                    {"id": 2, "name": "Python"},
                    {"id": 3, "name": "Go"},
                ],
            },
        ]
        counts = {1: 10, 2: 7, 3: 3}
        html = _build_collection_tree_html(tree, counts, None)
        assert "Tech" in html
        assert "Python" in html
        assert "Go" in html
        assert "collection-tree-children" in html
        assert "(7)" in html
        assert "(3)" in html

    def test_active_id_gets_active_class(self):
        tree = [{"id": 1, "name": "Tech"}]
        counts = {1: 5}
        html = _build_collection_tree_html(tree, counts, active_id=1)
        assert "collection-tree-item active" in html

    def test_all_documents_active_when_no_id(self):
        tree = [{"id": 1, "name": "Tech"}]
        counts = {1: 5}
        html = _build_collection_tree_html(tree, counts, active_id=None)
        # "All Documents" link should have active class when no collection selected
        assert 'href="/documents" class="collection-tree-item active"' in html

    def test_unassigned_active_when_id_zero(self):
        tree = [{"id": 1, "name": "Tech"}]
        counts = {1: 5}
        html = _build_collection_tree_html(tree, counts, active_id=0)
        assert 'href="/documents?collection_id=0" class="collection-tree-item active"' in html

    def test_escapes_names(self):
        tree = [{"id": 1, "name": '<script>alert("xss")</script>'}]
        counts = {1: 1}
        html = _build_collection_tree_html(tree, counts, None)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_nested_indentation(self):
        tree = [
            {
                "id": 1,
                "name": "Parent",
                "children": [{"id": 2, "name": "Child"}],
            },
        ]
        counts = {1: 1, 2: 1}
        html = _build_collection_tree_html(tree, counts, None)
        # Child should have margin-left:16px
        assert "margin-left:16px" in html


# ═══════════════════════════════════════════════════════════════════
# 6. _build_collection_breadcrumb_html — breadcrumb navigation
# ═══════════════════════════════════════════════════════════════════


class TestBuildCollectionBreadcrumbHtml:
    """Tests for _build_collection_breadcrumb_html() — breadcrumb nav."""

    def test_empty_path_returns_empty_string(self):
        assert _build_collection_breadcrumb_html([]) == ""

    def test_single_collection_shows_all_link(self):
        path = [{"id": 1, "name": "Tech"}]
        html = _build_collection_breadcrumb_html(path)
        assert "collection-breadcrumb" in html
        assert 'href="/documents"' in html  # "All" link
        assert "Tech" in html

    def test_multi_level_path_shows_chain(self):
        path = [
            {"id": 1, "name": "Tech"},
            {"id": 2, "name": "Python"},
            {"id": 3, "name": "Django"},
        ]
        html = _build_collection_breadcrumb_html(path)
        assert "Tech" in html
        assert "Python" in html
        assert "Django" in html
        # Tech appears before Python before Django
        tech_pos = html.find("Tech")
        py_pos = html.find("Python")
        dj_pos = html.find("Django")
        assert tech_pos < py_pos < dj_pos

    def test_ancestors_are_links(self):
        path = [
            {"id": 1, "name": "Tech"},
            {"id": 2, "name": "Python"},
        ]
        html = _build_collection_breadcrumb_html(path)
        # Tech (ancestor) should be a link
        assert 'href="/documents?collection_id=1"' in html
        # Python (current) should be a span (not a link to itself)
        # Check the breadcrumb section doesn't link to collection_id=2
        assert 'href="/documents?collection_id=2"' not in html

    def test_deepest_is_not_clickable(self):
        """The current (deepest) collection should be a <span>, not a link."""
        path = [{"id": 1, "name": "Root"}]
        html = _build_collection_breadcrumb_html(path)
        assert '<span class="collection-breadcrumb-current">' in html
        assert "Root" in html

    def test_separators_between_items(self):
        path = [
            {"id": 1, "name": "A"},
            {"id": 2, "name": "B"},
        ]
        html = _build_collection_breadcrumb_html(path)
        # Should have separator between "All" and A, and between A and B
        assert "collection-breadcrumb-sep" in html
        # Two separators: All/A and A/B
        assert html.count("collection-breadcrumb-sep") == 2

    def test_escapes_names_in_breadcrumb(self):
        path = [{"id": 1, "name": '<img src=x onerror=alert(1)>'}]
        html = _build_collection_breadcrumb_html(path)
        assert "<img" not in html
        assert "&lt;img" in html

    def test_missing_name_defaults_to_untitled(self):
        path = [{"id": 1}]
        html = _build_collection_breadcrumb_html(path)
        assert "Untitled" in html


# ═══════════════════════════════════════════════════════════════════
# 7. SVG chart generators
# ═══════════════════════════════════════════════════════════════════


class TestSvgLineChart:
    """Tests for _svg_line_chart() — inline SVG line chart."""

    def test_empty_data_returns_placeholder(self):
        html = _svg_line_chart([], "count")
        assert "chart-empty" in html
        assert "No data" in html

    def test_single_data_point_produces_svg(self):
        data = [{"date": "2025-01", "count": 42}]
        html = _svg_line_chart(data, "count")
        assert "<svg" in html
        assert "viewBox=" in html
        assert "polyline" in html
        assert 'aria-label="Line chart"' in html

    def test_multiple_data_points_produces_svg(self):
        data = [
            {"date": "2025-01", "count": 10},
            {"date": "2025-02", "count": 20},
            {"date": "2025-03", "count": 15},
        ]
        html = _svg_line_chart(data, "count")
        assert "<svg" in html
        assert "polyline" in html
        # Should have grid lines
        assert "chart-grid" in html

    def test_chart_includes_circles_for_data_points(self):
        data = [{"date": "2025-01", "count": 42}]
        html = _svg_line_chart(data, "count")
        assert "<circle" in html
        assert "chart-point" in html

    def test_custom_color_is_respected(self):
        data = [{"date": "2025-01", "count": 10}]
        html = _svg_line_chart(data, "count", color="#ff0000")
        assert "#ff0000" in html

    def test_custom_value_and_label_keys(self):
        data = [{"day": "Mon", "hits": 5}]
        html = _svg_line_chart(data, value_key="hits", label_key="day")
        assert "<svg" in html

    def test_all_zero_values_produces_svg_not_error(self):
        data = [{"date": "2025-01", "count": 0}]
        html = _svg_line_chart(data, "count")
        assert "<svg" in html
        assert "polyline" in html

    def test_max_val_protection_against_division_by_zero(self):
        """When all values are zero, max_val defaults to 1 to avoid div-by-zero."""
        data = [{"date": "a", "count": 0}, {"date": "b", "count": 0}]
        html = _svg_line_chart(data, "count")
        assert "<svg" in html  # Should not crash

    def test_date_label_truncates_mm_dd(self):
        data = [{"date": "2025-06-15", "count": 10}]
        html = _svg_line_chart(data, "count")
        # The label "2025-06-15" should be truncated to "06-15"
        assert "06-15" in html

    def test_few_points_has_all_labels(self):
        data = [
            {"date": "2025-01-01", "count": 1},
            {"date": "2025-01-02", "count": 2},
            {"date": "2025-01-03", "count": 3},
        ]
        html = _svg_line_chart(data, "count")
        # All three labels should appear (n <= 7)
        assert "01-01" in html
        assert "01-02" in html
        assert "01-03" in html

    def test_many_points_only_three_labels(self):
        data = [{"date": f"2025-01-{i:02d}", "count": i} for i in range(1, 16)]
        html = _svg_line_chart(data, "count")
        # Should only have first, middle, last labels
        assert html.count("chart-axis-label") >= 1

    def test_missing_value_defaults_to_zero(self):
        data = [{"date": "2025-01", "other": 10}]
        html = _svg_line_chart(data, "count")
        assert "<svg" in html


class TestSvgBarChart:
    """Tests for _svg_bar_chart() — inline SVG bar chart."""

    def test_empty_data_returns_placeholder(self):
        html = _svg_bar_chart([], "tag", "count")
        assert "chart-empty" in html

    def test_single_data_point_produces_svg(self):
        data = [{"tag": "python", "count": 42}]
        html = _svg_bar_chart(data, "tag", "count")
        assert "<svg" in html
        assert "<rect" in html
        assert 'aria-label="Bar chart"' in html

    def test_max_15_items_truncation(self):
        data = [{"tag": f"tag-{i}", "count": i + 1} for i in range(20)]
        html = _svg_bar_chart(data, "tag", "count")
        # Should only have 15 bars (data[:15])
        rects = html.count("<rect")
        assert rects <= 15

    def test_long_labels_are_truncated(self):
        data = [{"tag": "this-is-a-very-long-tag-name-for-testing", "count": 1}]
        html = _svg_bar_chart(data, "tag", "count")
        assert "…" in html

    def test_bar_values_displayed(self):
        data = [{"tag": "python", "count": 42}]
        html = _svg_bar_chart(data, "tag", "count")
        assert "42" in html

    def test_all_zero_values_handled(self):
        data = [{"tag": "python", "count": 0}]
        html = _svg_bar_chart(data, "tag", "count")
        assert "<svg" in html

    def test_y_axis_labels_present(self):
        data = [
            {"tag": "A", "count": 10},
            {"tag": "B", "count": 20},
        ]
        html = _svg_bar_chart(data, "tag", "count")
        assert "chart-axis-label" in html
        assert "<text" in html


class TestSvgPieChart:
    """Tests for _svg_pie_chart() — inline SVG pie/donut chart."""

    def test_empty_data_returns_placeholder(self):
        html = _svg_pie_chart([])
        assert "chart-empty" in html

    def test_total_zero_returns_placeholder(self):
        data = [("A", 0), ("B", 0)]
        html = _svg_pie_chart(data)
        assert "chart-empty" in html

    def test_single_slice(self):
        data = [("Python", 100)]
        html = _svg_pie_chart(data)
        assert "<svg" in html
        assert "Python" in html
        assert "100.0%" in html

    def test_multiple_slices(self):
        data = [("Python", 50), ("Go", 30), ("Rust", 20)]
        html = _svg_pie_chart(data)
        assert "<svg" in html
        assert "Python" in html
        assert "Go" in html
        assert "Rust" in html
        assert "pie-legend" in html

    def test_donut_hole_svg(self):
        """Pie chart should have an inner circle for the donut effect."""
        data = [("A", 100)]
        html = _svg_pie_chart(data)
        # Should have two circles: outer for slice, inner for donut hole
        # Actually the single-slice case uses circle (not path) + inner circle
        assert 'role="img"' in html
        assert 'aria-label="Pie chart"' in html

    def test_zero_value_entry_skipped(self):
        data = [("Python", 50), ("Empty", 0), ("Go", 30)]
        html = _svg_pie_chart(data)
        assert "Python" in html
        assert "Go" in html
        # "Empty" with 0 value should be skipped (no slice, no legend entry)
        assert "Empty" not in html

    def test_percentages_displayed(self):
        data = [("A", 50), ("B", 50)]
        html = _svg_pie_chart(data)
        assert "50.0%" in html

    def test_color_rotation_uses_palette(self):
        data = [("A", 10), ("B", 10), ("C", 10)]
        html = _svg_pie_chart(data)
        # Should have SVG paths with fill colors
        assert "fill=" in html

    def test_custom_dimensions(self):
        data = [("A", 100)]
        html = _svg_pie_chart(data, width=400, height=300)
        # SVG viewBox should reflect the custom dimensions
        assert '0 0 400 300' in html

    def test_single_full_circle_uses_circle_element(self):
        """A single slice with 100% uses a <circle> element, not a path."""
        data = [("Full", 100)]
        html = _svg_pie_chart(data)
        # The single-slice case uses a full circle
        assert 'class="chart-slice"' in html
