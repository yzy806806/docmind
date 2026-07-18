"""Tests for scroll smoothness improvements (Phase 11 action item 3).

Verifies:
1. Design tokens — scroll-behavior, scroll-padding-top, overscroll-behavior
   are defined in :root.
2. html rule — applies scroll-behavior and scroll-padding-top tokens so
   anchor jumps animate smoothly and targets land below the sticky header.
3. Scrollable containers — momentum scrolling (touch + overscroll-contain
   + scroll-behavior) on every element with overflow-y/overflow-x: auto.
4. prefers-reduced-motion — overrides scroll-behavior to auto !important
   so motion-sensitive users get instant jumps.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


# ── Helpers ───────────────────────────────────────────────────────


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _css_path() -> Path:
    return _project_root() / "src" / "web" / "static" / "css" / "styles.css"


def _read_css() -> str:
    return _css_path().read_text()


def _strip_comments(css: str) -> str:
    """Strip CSS comments for cleaner regex matching."""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _extract_rule(css: str, selector: str) -> str:
    """Extract the CSS rule body for a given selector.

    Returns the properties text between { } for the first match.
    Raises AssertionError if the selector is not found.
    """
    escaped = re.escape(selector)
    pattern = re.compile(
        rf"(?:^|\}})\s*{escaped}\s*\{{([^}}]*)\}}",
        re.DOTALL | re.MULTILINE,
    )
    match = pattern.search(css)
    assert match is not None, f"Selector '{selector}' not found in styles.css"
    return match.group(1).strip()


def _has_property(rule_body: str, prop: str, value: str | None = None) -> bool:
    """Check if a rule body contains a property declaration.

    If value is None, matches just the property name. Otherwise matches
    property: value (with optional whitespace and trailing ;/}).
    """
    if value is None:
        pattern = re.compile(rf"\b{re.escape(prop)}\s*:")
        return bool(pattern.search(rule_body))
    pattern = re.compile(
        rf"\b{re.escape(prop)}\s*:\s*{re.escape(value)}(?:\s*[;}}])"
    )
    return bool(pattern.search(rule_body))


def _has_token(rule_body: str, token_name: str) -> bool:
    """Check if a rule body references a specific CSS custom property."""
    return f"var(--{token_name})" in rule_body


# ═══════════════════════════════════════════════════════════════════
# 1. Scroll Design Tokens in :root
# ═══════════════════════════════════════════════════════════════════


class TestScrollTokens:
    """Verify the three scroll tokens are defined in :root."""

    def test_scroll_behavior_token_defined(self) -> None:
        css = _read_css()
        root_block = _extract_rule(css, ":root")
        assert "--scroll-behavior:" in root_block, (
            "--scroll-behavior token missing from :root"
        )

    def test_scroll_behavior_token_value_smooth(self) -> None:
        css = _read_css()
        root_block = _extract_rule(css, ":root")
        match = re.search(r"--scroll-behavior:\s*(\w+)", root_block)
        assert match is not None, "--scroll-behavior value not parseable"
        assert match.group(1) == "smooth", (
            f"--scroll-behavior should be 'smooth' for the default (non-"
            f"reduced-motion) theme, got '{match.group(1)}'. The "
            f"prefers-reduced-motion query overrides this to auto."
        )

    def test_scroll_padding_top_token_defined(self) -> None:
        css = _read_css()
        root_block = _extract_rule(css, ":root")
        assert "--scroll-padding-top:" in root_block, (
            "--scroll-padding-top token missing from :root"
        )

    def test_scroll_padding_top_token_has_value(self) -> None:
        css = _read_css()
        root_block = _extract_rule(css, ":root")
        match = re.search(r"--scroll-padding-top:\s*(\d+)px", root_block)
        assert match is not None, (
            "--scroll-padding-top must be a px value to offset the sticky header"
        )
        value = int(match.group(1))
        # The sticky header is padding 16px + h1 (~32px) + nav (~28px) + gap.
        # Anything under 60px risks hiding the target under the header;
        # anything over 120px wastes too much viewport.
        assert 60 <= value <= 120, (
            f"--scroll-padding-top of {value}px is out of the expected "
            f"60-120px range for offsetting a sticky header"
        )

    def test_overscroll_behavior_token_defined(self) -> None:
        css = _read_css()
        root_block = _extract_rule(css, ":root")
        assert "--overscroll-behavior:" in root_block, (
            "--overscroll-behavior token missing from :root"
        )

    def test_overscroll_behavior_token_value_contain(self) -> None:
        css = _read_css()
        root_block = _extract_rule(css, ":root")
        match = re.search(r"--overscroll-behavior:\s*(\w+)", root_block)
        assert match is not None, "--overscroll-behavior value not parseable"
        assert match.group(1) == "contain", (
            f"--overscroll-behavior should be 'contain' to prevent scroll "
            f"chaining from child regions to the body, got '{match.group(1)}'"
        )


# ═══════════════════════════════════════════════════════════════════
# 2. html Rule — Smooth Scroll + Padding
# ═══════════════════════════════════════════════════════════════════


class TestHtmlScrollRule:
    """Verify the html element gets scroll-behavior and scroll-padding-top."""

    def test_html_rule_exists(self) -> None:
        css = _read_css()
        # Must be a standalone 'html' selector, not 'html.something'
        pattern = re.compile(
            r"(?:^|\})\s*html\s*\{", re.MULTILINE
        )
        assert pattern.search(css), (
            "No standalone 'html { ... }' rule found — needed for "
            "document-level scroll-behavior and scroll-padding-top"
        )

    def test_html_uses_scroll_behavior_token(self) -> None:
        css = _read_css()
        html_rule = _extract_rule(css, "html")
        assert _has_token(html_rule, "scroll-behavior"), (
            "html rule must reference var(--scroll-behavior) so the "
            "prefers-reduced-motion override can flip it to auto"
        )

    def test_html_uses_scroll_padding_top_token(self) -> None:
        css = _read_css()
        html_rule = _extract_rule(css, "html")
        assert _has_token(html_rule, "scroll-padding-top"), (
            "html rule must reference var(--scroll-padding-top) so anchor "
            "targets land below the sticky header"
        )

    def test_html_has_overscroll_behavior_y(self) -> None:
        css = _read_css()
        html_rule = _extract_rule(css, "html")
        assert _has_property(html_rule, "overscroll-behavior-y"), (
            "html rule must set overscroll-behavior-y to prevent the iOS "
            "rubber-band/scroll-chaining glitch on the body"
        )


# ═══════════════════════════════════════════════════════════════════
# 3. Momentum Scrolling on Scrollable Containers
# ═══════════════════════════════════════════════════════════════════


# Every scrollable container that should have momentum scrolling.
# Each entry: (selector, expected_overflow_axis)
# axis 'y' → overscroll-behavior (full), 'x' → overscroll-behavior-x
SCROLLABLE_CONTAINERS: list[tuple[str, str]] = [
    (".table-scroll", "x"),
    (".doc-excerpt", "y"),
    (".chat-messages", "y"),
    (".chat-sidebar", "y"),
    (".doc-toc", "y"),
    (".doc-reader pre", "x"),
    (".kbd-modal-panel", "y"),
]


class TestMomentumScrolling:
    """Verify every scrollable container has momentum-scroll properties."""

    @pytest.mark.parametrize(
        "selector,axis",
        SCROLLABLE_CONTAINERS,
        ids=[s for s, _ in SCROLLABLE_CONTAINERS],
    )
    def test_has_webkit_overflow_scrolling(self, selector: str, axis: str) -> None:
        css = _read_css()
        rule = _extract_rule(css, selector)
        assert "-webkit-overflow-scrolling: touch" in rule, (
            f"{selector} must have '-webkit-overflow-scrolling: touch' for "
            f"iOS momentum scrolling on older Safari"
        )

    @pytest.mark.parametrize(
        "selector,axis",
        SCROLLABLE_CONTAINERS,
        ids=[s for s, _ in SCROLLABLE_CONTAINERS],
    )
    def test_has_overscroll_behavior_contain(
        self, selector: str, axis: str
    ) -> None:
        css = _read_css()
        rule = _extract_rule(css, selector)
        # The property name differs by axis (overscroll-behavior vs
        # overscroll-behavior-x), but both reference the same token:
        # var(--overscroll-behavior).
        assert _has_token(rule, "overscroll-behavior"), (
            f"{selector} must reference var(--overscroll-behavior) to "
            f"prevent scroll chaining to the body"
        )

    @pytest.mark.parametrize(
        "selector,axis",
        SCROLLABLE_CONTAINERS,
        ids=[s for s, _ in SCROLLABLE_CONTAINERS],
    )
    def test_has_scroll_behavior_token(self, selector: str, axis: str) -> None:
        css = _read_css()
        rule = _extract_rule(css, selector)
        assert _has_token(rule, "scroll-behavior"), (
            f"{selector} must reference var(--scroll-behavior) so programmatic "
            f"scrolls within the region animate smoothly (and reduce-motion "
            f"override applies)"
        )


# ═══════════════════════════════════════════════════════════════════
# 4. prefers-reduced-motion Overrides
# ═══════════════════════════════════════════════════════════════════


class TestReducedMotionOverride:
    """Verify prefers-reduced-motion disables smooth scrolling."""

    def test_reduced_motion_query_exists(self) -> None:
        css = _read_css()
        assert "prefers-reduced-motion: reduce" in css, (
            "No @media (prefers-reduced-motion: reduce) query found"
        )

    def test_reduced_motion_overrides_scroll_behavior(self) -> None:
        css = _read_css()
        # Find the reduced-motion block and check it forces scroll-behavior: auto
        # The universal selector * ensures the override applies to html and
        # every scrollable container.
        match = re.search(
            r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{",
            css,
        )
        assert match is not None, "Reduced-motion media query not found"
        # Search within the block (find the closing brace at the same level)
        start = match.end()
        depth = 1
        i = start
        while i < len(css) and depth > 0:
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
            i += 1
        block = css[start:i]
        assert "scroll-behavior: auto !important" in block, (
            "prefers-reduced-motion block must contain "
            "'scroll-behavior: auto !important' to disable smooth scrolling "
            "for motion-sensitive users"
        )

    def test_reduced_motion_uses_universal_selector(self) -> None:
        """The override should use * (universal) so it covers html AND all
        scrollable containers, not just one specific element."""
        css = _read_css()
        match = re.search(
            r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{",
            css,
        )
        assert match is not None
        start = match.end()
        depth = 1
        i = start
        while i < len(css) and depth > 0:
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
            i += 1
        block = css[start:i]
        # Look for the universal selector pattern containing scroll-behavior: auto
        universal_pattern = re.compile(
            r"\*\s*,\s*\*::before\s*,\s*\*::after\s*\{[^}]*scroll-behavior:\s*auto",
            re.DOTALL,
        )
        assert universal_pattern.search(block), (
            "Reduced-motion override should use the universal selector "
            "(*, *::before, *::after) so the scroll-behavior: auto override "
            "applies to html and every scrollable container"
        )


# ═══════════════════════════════════════════════════════════════════
# 5. Regression Guard — No Container Missing Momentum Scroll
# ═══════════════════════════════════════════════════════════════════


class TestScrollRegressionGuard:
    """Catch any overflow: auto rule that lacks momentum-scroll properties."""

    def test_no_bare_overflow_auto_without_momentum(self) -> None:
        """Every CSS rule with 'overflow-y: auto' or 'overflow-x: auto'
        should also have the momentum-scroll trio. This catches future
        additions that forget to add smooth scrolling.

        Excludes rules inside @media (prefers-reduced-motion) and
        one-liner rules that set overflow as part of a shorthand."""
        css = _strip_comments(_read_css())
        # Find all top-level rules (not inside @media) with overflow: auto
        # Simple heuristic: find rule bodies containing overflow.*auto
        # and check they also contain the momentum properties.
        rule_pattern = re.compile(
            r"([.#]?[a-zA-Z][\w-]*(?:\s+[.#]?[a-zA-Z][\w-]*)*)\s*\{([^}]*)\}",
            re.DOTALL,
        )
        missing: list[str] = []
        for match in rule_pattern.finditer(css):
            selector = match.group(1).strip()
            body = match.group(2)
            # Skip if no overflow: auto
            if not re.search(r"overflow[xy]?\s*:\s*auto", body):
                continue
            # Skip rules inside @media (heuristic: they tend to be indented
            # or follow a media query; we only flag standalone rules here)
            if "scroll-behavior" in body or "-webkit-overflow-scrolling" in body:
                continue
            # Flag rules that have overflow: auto but no momentum properties
            missing.append(selector)
        # Known false positives: rules where overflow:auto is incidental
        # (e.g. a flex utility). We only fail if we find genuinely missing
        # scroll containers.
        known_incidental = {
            # Add selectors here if a rule legitimately uses overflow: auto
            # for a non-scrolling purpose (e.g. clearfix variant)
        }
        genuinely_missing = [s for s in missing if s not in known_incidental]
        assert not genuinely_missing, (
            f"Found CSS rules with 'overflow: auto' but no momentum-scroll "
            f"properties (scroll-behavior, -webkit-overflow-scrolling, "
            f"overscroll-behavior): {genuinely_missing}"
        )
