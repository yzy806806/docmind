"""Tests for the remaining frontend smoothness gaps:

1. Custom scrollbar styling — scrollbar-width, scrollbar-color, and
   ::-webkit-scrollbar rules must exist and use design tokens.
2. Focus-visible transitions — :focus-visible must have a transition
   for smooth ring appearance; buttons must have explicit
   :focus-visible rules.
3. Micro-interaction transitions — elements with cursor:pointer must
   also have a transition property (no missing transitions on
   interactive elements).
4. Reduced-motion coverage — new scrollbar and focus transitions
   must have corresponding disables in @media (prefers-reduced-motion).
5. rAFThrottle in perf-utils.js — the function referenced by chat.js
   and vector-weight-slider.js must actually be defined and exported.
"""

from __future__ import annotations

import re
from pathlib import Path


# ── Helpers ───────────────────────────────────────────────────────


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _css_path() -> Path:
    return _project_root() / "src" / "web" / "static" / "css" / "styles.css"


def _read_css() -> str:
    return _css_path().read_text()


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _read_js(name: str) -> str:
    return (_project_root() / "src" / "web" / "static" / "js" / name).read_text()


# ── 1. Custom Scrollbar Styling ──────────────────────────────────


class TestScrollbarStyling:
    """Verify slim, themed scrollbar rules exist and use design tokens."""

    @classmethod
    def setup_class(cls):
        cls.css = _strip_comments(_read_css())

    def test_scrollbar_width_token_exists(self):
        """--scrollbar-width token must be defined in :root."""
        assert "--scrollbar-width:" in self.css, (
            "Missing --scrollbar-width design token in :root"
        )

    def test_scrollbar_color_token_exists(self):
        """--scrollbar-thumb token must be defined in :root."""
        assert "--scrollbar-thumb:" in self.css, (
            "Missing --scrollbar-thumb design token in :root"
        )

    def test_scrollbar_thumb_hover_token_exists(self):
        """--scrollbar-thumb-hover token must be defined in :root."""
        assert "--scrollbar-thumb-hover:" in self.css, (
            "Missing --scrollbar-thumb-hover design token in :root"
        )

    def test_firefox_scrollbar_width_property(self):
        """`scrollbar-width` property must be set (Firefox + standards-track)."""
        assert "scrollbar-width:" in self.css, (
            "No scrollbar-width property found — Firefox/custom scrollbar support missing"
        )

    def test_firefox_scrollbar_color_property(self):
        """`scrollbar-color` property must be set (Firefox + standards-track)."""
        assert "scrollbar-color:" in self.css, (
            "No scrollbar-color property found — Firefox/custom scrollbar support missing"
        )

    def test_webkit_scrollbar_rules_exist(self):
        """::-webkit-scrollbar pseudo-element rules must exist for Chrome/Safari/Edge."""
        assert "::-webkit-scrollbar {" in self.css or "::-webkit-scrollbar{" in self.css, (
            "No ::-webkit-scrollbar rule found — WebKit scrollbar styling missing"
        )

    def test_webkit_scrollbar_thumb_rule_exists(self):
        """::-webkit-scrollbar-thumb must have its own rule with background."""
        assert "::-webkit-scrollbar-thumb" in self.css, (
            "No ::-webkit-scrollbar-thumb rule found"
        )
        # Verify it has a background property
        thumb_section = self.css[self.css.find("::-webkit-scrollbar-thumb"):]
        assert "background:" in thumb_section[:200], (
            "::-webkit-scrollbar-thumb rule missing background property"
        )

    def test_webkit_scrollbar_track_rule_exists(self):
        """::-webkit-scrollbar-track must exist."""
        assert "::-webkit-scrollbar-track" in self.css, (
            "No ::-webkit-scrollbar-track rule found"
        )

    def test_scrollbar_thumb_has_transition(self):
        """Scrollbar thumb should have a transition for smooth hover color change."""
        thumb_section = self.css[self.css.find("::-webkit-scrollbar-thumb"):]
        # Find the rule block
        brace_start = thumb_section.find("{")
        brace_end = thumb_section.find("}", brace_start)
        thumb_body = thumb_section[brace_start:brace_end]
        assert "transition:" in thumb_body, (
            "::-webkit-scrollbar-thumb missing transition for smooth hover"
        )

    def test_scrollbar_thumb_hover_rule_exists(self):
        """::-webkit-scrollbar-thumb:hover must exist for hover feedback."""
        assert "::-webkit-scrollbar-thumb:hover" in self.css or (
            "::-webkit-scrollbar-thumb:hover" in self.css
        ), "No ::-webkit-scrollbar-thumb:hover rule found"

    def test_scrollbar_uses_design_tokens(self):
        """Scrollbar rules must reference design tokens (var(--...)), not hardcoded values."""
        # Find the scrollbar section
        scrollbar_section = ""
        idx = self.css.find("::-webkit-scrollbar {")
        if idx == -1:
            idx = self.css.find("::-webkit-scrollbar{")
        if idx != -1:
            # Grab a generous section covering all scrollbar rules
            scrollbar_section = self.css[idx:idx + 1000]

        assert "var(--scrollbar" in scrollbar_section or "var(--" in scrollbar_section, (
            "Scrollbar rules should use design tokens (var(--...)) "
            "for theme consistency"
        )

    def test_scrollbar_rules_in_reduced_motion(self):
        """Scrollbar thumb transition must be disabled in reduced-motion."""
        # Find reduced-motion blocks
        rpm_text = ""
        for m in re.finditer(
            r"@media\s*\(.*?prefers-reduced-motion.*?\)\s*\{",
            self.css,
        ):
            start = m.end()
            depth = 1
            i = start
            while i < len(self.css) and depth > 0:
                if self.css[i] == "{":
                    depth += 1
                elif self.css[i] == "}":
                    depth -= 1
                i += 1
            rpm_text += self.css[start : i - 1] + "\n"

        assert (
            "scrollbar" in rpm_text.lower() or "::-webkit-scrollbar-thumb" in rpm_text
        ), (
            "Scrollbar thumb transition not disabled in @media (prefers-reduced-motion)"
        )


# ── 2. Focus-Visible Transitions ────────────────────────────────


class TestFocusVisibleTransitions:
    """Verify :focus-visible has smooth transitions and button coverage."""

    @classmethod
    def setup_class(cls):
        cls.css = _strip_comments(_read_css())

    def test_focus_visible_has_transition(self):
        """The :focus-visible rule must include a transition for smooth ring appearance."""
        # Find the :focus-visible rule (not input:focus-visible, etc.)
        m = re.search(r"^\s*:focus-visible\s*\{([^}]*)\}", self.css, re.MULTILINE)
        assert m, "No standalone :focus-visible rule found"
        body = m.group(1)
        assert "transition:" in body, (
            ":focus-visible rule missing transition property — "
            "focus ring should transition smoothly, not snap"
        )

    def test_button_focus_visible_rules_exist(self):
        """At least one button-related :focus-visible rule must exist beyond
        the generic :focus-visible."""
        # Count :focus-visible rules that mention button-related selectors
        btn_fv_patterns = [
            r"\.btn:focus-visible",
            r"\.btn-[a-z]+:focus-visible",
            r"button:focus-visible",
            r"header nav a:focus-visible",
            r"\.theme-toggle:focus-visible",
        ]
        found = 0
        for pattern in btn_fv_patterns:
            if re.search(pattern, self.css):
                found += 1

        assert found >= 3, (
            f"Only {found} button/link :focus-visible rules found (expected ≥3). "
            "Buttons need explicit focus-visible for keyboard accessibility."
        )

    def test_focus_visible_transition_in_reduced_motion(self):
        """:focus-visible transition must be disabled in reduced-motion."""
        rpm_text = ""
        for m in re.finditer(
            r"@media\s*\(.*?prefers-reduced-motion.*?\)\s*\{",
            self.css,
        ):
            start = m.end()
            depth = 1
            i = start
            while i < len(self.css) and depth > 0:
                if self.css[i] == "{":
                    depth += 1
                elif self.css[i] == "}":
                    depth -= 1
                i += 1
            rpm_text += self.css[start : i - 1] + "\n"

        assert ":focus-visible" in rpm_text, (
            ":focus-visible transition not disabled in @media (prefers-reduced-motion)"
        )

    def test_all_btn_variants_have_focus_visible(self):
        """Every .btn-* variant should be covered by a :focus-visible rule."""
        # Extract all .btn-* class names
        btn_variants = set(re.findall(r"\.btn-([a-z]+)", self.css))
        # Filter to actual button class names (not modifier classes like sm, block)
        real_variants = {
            v for v in btn_variants
            if v not in ("sm", "block", "link", "primary", "secondary", "danger", "ghost", "filter")
        }
        # Check that at least the primary set is covered
        # (the test checks .btn:focus-visible which covers all via inheritance,
        # but we want explicit per-variant rules too)
        key_variants = {"save", "cancel", "delete", "export", "new-collection"}
        for var in key_variants:
            pattern = f".btn-{var}:focus-visible"
            assert pattern in self.css, (
                f"{pattern} not found — button variant missing focus-visible rule"
            )


# ── 3. Micro-Interaction Transition Coverage ─────────────────────


class TestMicroInteractionTransitions:
    """Elements with cursor:pointer must also have a transition."""

    @classmethod
    def setup_class(cls):
        css = _read_css()
        cls.css = _strip_comments(css)

    def _get_rules_without_transition(self) -> list[str]:
        """Find all rules with cursor:pointer but no transition property."""
        lines = self.css.split("\n")
        violations: list[str] = []
        current_selector = ""
        rule_body: list[str] = []
        in_rule = False
        brace_depth = 0

        for line in lines:
            stripped = line.strip()
            opens = stripped.count("{")
            closes = stripped.count("}")

            if opens > 0 and not in_rule and not stripped.startswith("@"):
                in_rule = True
                brace_depth = 0
                current_selector = stripped.split("{")[0].strip()
                rule_body = []

            brace_depth += opens - closes

            if in_rule:
                rule_body.append(line)

            if in_rule and brace_depth <= 0:
                body = "\n".join(rule_body)
                has_cursor_pointer = "cursor: pointer" in body or "cursor:pointer" in body
                has_transition = "transition:" in body

                if has_cursor_pointer and not has_transition:
                    # Skip pseudo-elements like ::-webkit-scrollbar-thumb (those are
                    # handled separately) and @media blocks
                    if not current_selector.startswith("@"):
                        violations.append(current_selector)
                in_rule = False
                rule_body = []

        return violations

    def test_no_interactive_element_without_transition(self):
        """No element with cursor:pointer should lack a transition property.

        Interactive elements need transitions for smooth state changes.
        The only exceptions are elements whose interactivity comes purely
        from pseudo-elements (slider thumbs already have their own transitions).
        """
        violations = self._get_rules_without_transition()
        # Filter out acceptable cases:
        # - .vw-slider already has transition on the pseudo-element thumbs
        # - ::-webkit-scrollbar-* are handled separately
        acceptable = {".vw-slider"}  # transition is on ::-webkit-slider-thumb
        real_violations = [
            v for v in violations
            if v not in acceptable and "::-webkit-scrollbar" not in v
        ]
        assert not real_violations, (
            "Elements with cursor:pointer but no transition:\n"
            + "\n".join(f"  {v}" for v in real_violations)
        )

    def test_btn_filter_has_transition(self):
        """.btn-filter must have a transition property."""
        m = re.search(r"\.btn-filter\s*\{([^}]*)\}", self.css)
        assert m, ".btn-filter rule not found"
        assert "transition:" in m.group(1), (
            ".btn-filter missing transition property"
        )

    def test_vw_slider_has_transition(self):
        """.vw-slider must have a transition property."""
        m = re.search(r"\.vw-slider\s*\{([^}]*)\}", self.css)
        assert m, ".vw-slider rule not found"
        assert "transition:" in m.group(1), (
            ".vw-slider missing transition property"
        )


# ── 4. rAFThrottle in perf-utils.js ──────────────────────────────


class TestRAFThrottleExport:
    """Verify rAFThrottle is defined and exported from perf-utils.js."""

    def test_rafthrottle_function_defined(self):
        """perf-utils.js must define the rAFThrottle function."""
        js = _read_js("perf-utils.js")
        assert "function rAFThrottle" in js, (
            "rAFThrottle function not defined in perf-utils.js"
        )

    def test_rafthrottle_exported_on_docmindperf(self):
        """rAFThrottle must be exported on window.DocMindPerf."""
        js = _read_js("perf-utils.js")
        assert "rAFThrottle" in js, "rAFThrottle not referenced in perf-utils.js"
        # Check it's in the export object
        export_match = re.search(
            r"window\.DocMindPerf\s*=\s*\{([^}]*)\}",
            js,
            re.DOTALL,
        )
        assert export_match, "window.DocMindPerf export not found"
        assert "rAFThrottle" in export_match.group(1), (
            "rAFThrottle not in window.DocMindPerf export object"
        )

    def test_rafthrottle_has_cancel_method(self):
        """rAFThrottle should expose a .cancel() method for cleanup."""
        js = _read_js("perf-utils.js")
        # Find the rAFThrottle function body
        idx = js.find("function rAFThrottle")
        assert idx != -1
        # Get a generous section of the function
        section = js[idx:idx + 2000]
        assert "cancel" in section, (
            "rAFThrottle function missing .cancel() method"
        )

    def test_chat_js_references_rafthrottle(self):
        """chat.js must reference DocMindPerf.rAFThrottle for scroll coalescing."""
        js = _read_js("chat.js")
        assert "rAFThrottle" in js, (
            "chat.js does not reference rAFThrottle — scroll coalescing missing"
        )

    def test_vector_weight_slider_references_rafthrottle(self):
        """vector-weight-slider.js must reference rAFThrottle for input coalescing."""
        js = _read_js("vector-weight-slider.js")
        assert "rAFThrottle" in js, (
            "vector-weight-slider.js does not reference rAFThrottle"
        )

    def test_rafthrottle_uses_requestanimationframe(self):
        """rAFThrottle must use requestAnimationFrame for frame-synced updates."""
        js = _read_js("perf-utils.js")
        idx = js.find("function rAFThrottle")
        section = js[idx:idx + 2000]
        assert "requestAnimationFrame" in section, (
            "rAFThrottle does not use requestAnimationFrame"
        )
