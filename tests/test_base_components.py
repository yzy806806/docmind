"""Tests for base UI component classes (buttons, inputs, cards).

Verifies that the reusable component classes exist in styles.css and
that they use design tokens (CSS custom properties) for all visual
properties — colors, spacing, typography, radius, shadows, transitions.

Base components tested:
  Buttons: .btn, .btn-primary, .btn-secondary, .btn-danger, .btn-ghost
  Inputs:  .input
  Cards:   .card (already existed; verify token usage)
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


def _extract_rule(css: str, selector: str) -> str:
    """Extract the CSS rule body for a given selector.

    Returns the properties text between { } for the first match.
    Raises AssertionError if the selector is not found.
    """
    # Escape dots in selector for regex
    escaped = re.escape(selector)
    pattern = re.compile(
        rf"(?:^|\}})\s*{escaped}\s*\{{([^}}]*)\}}",
        re.DOTALL | re.MULTILINE,
    )
    match = pattern.search(css)
    assert match is not None, f"Selector '{selector}' not found in styles.css"
    return match.group(1).strip()


def _has_token(rule_body: str, token_name: str) -> bool:
    """Check if a rule body references a specific CSS custom property."""
    return f"var(--{token_name})" in rule_body


def _has_any_token(rule_body: str) -> bool:
    """Check if a rule body references any CSS custom property."""
    return bool(re.search(r"var\(--[\w-]+\)", rule_body))


def _extract_all_rules(css: str, selector_pattern: str) -> list[str]:
    """Extract all CSS rule bodies matching a selector regex pattern.

    Returns list of property strings (between { }).
    """
    pattern = re.compile(
        rf"(?:^|\}})\s*({selector_pattern})\s*\{{([^}}]*)\}}",
        re.DOTALL | re.MULTILINE,
    )
    results = []
    for match in pattern.finditer(css):
        results.append(match.group(2).strip())
    return results


# ═══════════════════════════════════════════════════════════════════
# 1. Button Base Class (.btn)
# ═══════════════════════════════════════════════════════════════════


class TestButtonBase:
    """Tests for .btn — the shared button base class."""

    def test_btn_class_exists(self):
        """The .btn class must be defined in styles.css."""
        css = _read_css()
        assert ".btn " in css or ".btn\n" in css or ".btn {" in css, (
            ".btn base class not found in styles.css"
        )

    def test_btn_base_uses_padding_token(self):
        css = _read_css()
        rule = _extract_rule(css, ".btn")
        assert _has_token(rule, "space-2-5") or _has_token(rule, "space-6"), (
            ".btn should use spacing tokens for padding"
        )

    def test_btn_base_uses_radius_token(self):
        css = _read_css()
        rule = _extract_rule(css, ".btn")
        assert _has_token(rule, "radius-lg"), (
            ".btn should use --radius-lg token for border-radius"
        )

    def test_btn_base_uses_font_token(self):
        css = _read_css()
        rule = _extract_rule(css, ".btn")
        assert _has_token(rule, "font-size-base") or _has_token(rule, "font-family"), (
            ".btn should use typography tokens for font"
        )

    def test_btn_base_uses_transition_token(self):
        css = _read_css()
        rule = _extract_rule(css, ".btn")
        assert _has_token(rule, "transition-press"), (
            ".btn should use --transition-press token for interactive transition"
        )

    def test_btn_active_has_scale(self):
        """The .btn:active state should provide tactile scale feedback."""
        css = _read_css()
        rule = _extract_rule(css, ".btn:active")
        assert "transform" in rule, (
            ".btn:active should use transform for press feedback"
        )

    def test_btn_disabled_has_opacity(self):
        """The .btn:disabled state should visually indicate disabled."""
        css = _read_css()
        rule = _extract_rule(css, ".btn:disabled")
        assert "opacity" in rule, (
            ".btn:disabled should use opacity to indicate disabled state"
        )


# ═══════════════════════════════════════════════════════════════════
# 2. Button Variants
# ═══════════════════════════════════════════════════════════════════


class TestButtonVariants:
    """Tests for .btn-primary, .btn-secondary, .btn-danger, .btn-ghost."""

    @pytest.mark.parametrize("variant", [
        ".btn-primary",
        ".btn-secondary",
        ".btn-danger",
        ".btn-ghost",
    ])
    def test_variant_exists(self, variant):
        css = _read_css()
        assert variant in css, f"{variant} not found in styles.css"

    @pytest.mark.parametrize("variant,token", [
        (".btn-primary", "primary"),
        (".btn-primary", "header-text"),
        (".btn-secondary", "surface"),
        (".btn-secondary", "input-border"),
        (".btn-danger", "badge-error-bg"),
        (".btn-danger", "badge-error-text"),
    ])
    def test_variant_uses_color_token(self, variant, token):
        css = _read_css()
        rule = _extract_rule(css, variant)
        assert _has_token(rule, token), (
            f"{variant} should use --{token} token"
        )

    @pytest.mark.parametrize("variant", [
        ".btn-primary",
        ".btn-secondary",
        ".btn-danger",
        ".btn-ghost",
    ])
    def test_variant_has_hover_state(self, variant):
        """Each button variant should have a :hover state rule."""
        css = _read_css()
        hover_selector = f"{variant}:hover"
        assert hover_selector in css, (
            f"{hover_selector} not found — variants need hover states"
        )

    @pytest.mark.parametrize("variant", [
        ".btn-primary",
        ".btn-secondary",
        ".btn-danger",
        ".btn-ghost",
    ])
    def test_variant_hover_uses_tokens(self, variant):
        """Hover states should use design tokens for visual feedback."""
        css = _read_css()
        hover_rule = _extract_rule(css, f"{variant}:hover")
        assert _has_any_token(hover_rule), (
            f"{variant}:hover should use design tokens"
        )


# ═══════════════════════════════════════════════════════════════════
# 3. Input Base Class (.input)
# ═══════════════════════════════════════════════════════════════════


class TestInputBase:
    """Tests for .input — the shared text input base class."""

    def test_input_class_exists(self):
        css = _read_css()
        assert ".input " in css or ".input\n" in css or ".input {" in css, (
            ".input base class not found in styles.css"
        )

    def test_input_uses_padding_token(self):
        css = _read_css()
        rule = _extract_rule(css, ".input")
        assert _has_token(rule, "space-2-5") or _has_token(rule, "space-3-5"), (
            ".input should use spacing tokens for padding"
        )

    def test_input_uses_border_token(self):
        css = _read_css()
        rule = _extract_rule(css, ".input")
        assert _has_token(rule, "input-border"), (
            ".input should use --input-border token"
        )

    def test_input_uses_radius_token(self):
        css = _read_css()
        rule = _extract_rule(css, ".input")
        assert _has_token(rule, "radius-lg"), (
            ".input should use --radius-lg token"
        )

    def test_input_uses_color_tokens(self):
        css = _read_css()
        rule = _extract_rule(css, ".input")
        assert _has_token(rule, "text"), (
            ".input should use --text token for text color"
        )
        assert _has_token(rule, "input-bg") or _has_token(rule, "surface"), (
            ".input should use background token"
        )

    def test_input_uses_transition_token(self):
        css = _read_css()
        rule = _extract_rule(css, ".input")
        assert _has_token(rule, "transition-base") or _has_token(rule, "transition-color"), (
            ".input should use --transition-base or --transition-color token"
        )

    def test_input_focus_uses_nav_link_token(self):
        css = _read_css()
        rule = _extract_rule(css, ".input:focus")
        assert _has_token(rule, "nav-link"), (
            ".input:focus should use --nav-link token for border color"
        )

    def test_input_focus_removes_outline(self):
        css = _read_css()
        rule = _extract_rule(css, ".input:focus")
        assert "outline: none" in rule, (
            ".input:focus should set outline: none"
        )


# ═══════════════════════════════════════════════════════════════════
# 4. Card (.card) — canonical surface card
# ═══════════════════════════════════════════════════════════════════


class TestCardBase:
    """Tests for .card — the canonical surface card."""

    def test_card_class_exists(self):
        css = _read_css()
        rule = _extract_rule(css, ".card")
        assert rule, ".card class not found in styles.css"

    def test_card_uses_surface_bg(self):
        css = _read_css()
        rule = _extract_rule(css, ".card")
        assert _has_token(rule, "surface"), (
            ".card should use --surface token for background"
        )

    def test_card_uses_radius_token(self):
        css = _read_css()
        rule = _extract_rule(css, ".card")
        assert _has_token(rule, "radius-xl"), (
            ".card should use --radius-xl token for border-radius"
        )

    def test_card_uses_shadow_token(self):
        css = _read_css()
        rule = _extract_rule(css, ".card")
        assert _has_token(rule, "shadow"), (
            ".card should use a shadow token for box-shadow"
        )

    def test_card_uses_spacing_tokens(self):
        css = _read_css()
        rule = _extract_rule(css, ".card")
        assert _has_token(rule, "space-5"), (
            ".card should use spacing token for padding"
        )
        assert _has_token(rule, "space-4"), (
            ".card should use spacing token for margin"
        )

    def test_card_has_hover_lift(self):
        """The .card should have a hover lift effect using tokens."""
        css = _read_css()
        # Find .card:hover rule
        hover_rules = _extract_all_rules(css, r"\.card:hover")
        assert len(hover_rules) > 0, ".card:hover not found"
        hover = hover_rules[0]
        assert _has_token(hover, "shadow-lg"), (
            ".card:hover should use --shadow-lg token"
        )


# ═══════════════════════════════════════════════════════════════════
# 5. Template Usage — verify templates can use the base classes
# ═══════════════════════════════════════════════════════════════════


class TestTemplateUsage:
    """Verify that templates referencing .btn-primary and .btn-secondary
    have corresponding CSS rules."""

    def test_upload_form_btn_primary_has_css(self):
        """upload_form.html uses .btn-primary — CSS must define it."""
        css = _read_css()
        rule = _extract_rule(css, ".btn-primary")
        assert rule, (
            ".btn-primary is used in upload_form.html but not defined in CSS"
        )

    def test_upload_form_btn_secondary_has_css(self):
        """upload_form.html uses .btn-secondary — CSS must define it."""
        css = _read_css()
        rule = _extract_rule(css, ".btn-secondary")
        assert rule, (
            ".btn-secondary is used in upload_form.html but not defined in CSS"
        )

    def test_card_used_in_multiple_templates(self):
        """Verify .card is used in multiple templates (dashboard, analytics, etc.)."""
        templates_dir = _project_root() / "src" / "web" / "templates"
        card_files = []
        for tpl in templates_dir.rglob("*.html"):
            if 'class="card' in tpl.read_text():
                card_files.append(tpl.name)
        assert len(card_files) >= 5, (
            f".card should be used in at least 5 templates, found {len(card_files)}"
        )
