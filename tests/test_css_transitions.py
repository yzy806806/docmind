"""Tests verifying CSS ``transition:`` properties on interactive elements.

Part of the frontend beautification Phase 11: CSS transitions on interactive
elements (buttons, links, inputs, cards, navigation, tags, collection items,
drop zones, etc.).

These tests go beyond the 6 hardcoded selectors in test_smoothness_browser.py
and audit ALL known interactive components from the project's component
registry to ensure every interactive element has a CSS transition property.

Approach:
  Extract full CSS rule blocks (selector text + body), then check whether
  transition declarations exist inside blocks whose selectors match each
  known interactive component.
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


def _extract_css_rules(css: str) -> list[dict]:
    """Extract all CSS rule blocks as {selector, body, start_line, inside_rpm}.

    Handles multi-line selectors separated by commas.  Tracks whether
    a rule is inside @media (prefers-reduced-motion: reduce).
    """
    lines = css.split("\n")
    rules: list[dict] = []

    # Track @media nesting
    media_stack: list[str] = []

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        # Track @media block entry
        if "@media" in stripped and "{" in stripped:
            media_stack.append(stripped)
            i += 1
            continue

        # Track closing braces that exit @media blocks
        if stripped == "}" and media_stack:
            # Check depth: is this } closing the @media or something inside it?
            # Simple heuristic: if next non-empty line is not another }, close media
            media_stack.pop()
            i += 1
            continue

        # Skip empty lines, comments, and content-only lines
        if (stripped == "" or stripped.startswith("/*")
                or stripped.startswith("//") or stripped.startswith("*")):
            i += 1
            continue

        # Skip lines that are clearly rule bodies (start with property:)
        if re.match(r"^\s*[\w-]+\s*:", stripped) and "{" not in stripped:
            i += 1
            continue

        # Check if this line starts a CSS rule
        brace_on_this_line = "{" in stripped

        if not brace_on_this_line:
            # Check if next non-empty line is just "{"
            next_i = i + 1
            while next_i < len(lines) and lines[next_i].strip() == "":
                next_i += 1
            if next_i < len(lines) and lines[next_i].strip() == "{":
                brace_on_this_line = True
            else:
                i += 1
                continue

        # Find the brace line
        if "{" in lines[i]:
            brace_line = i
        else:
            brace_line = i + 1
            while brace_line < len(lines) and "{" not in lines[brace_line]:
                brace_line += 1
            if brace_line >= len(lines):
                i += 1
                continue

        # Collect selector text (from i to brace_line, stripping {)
        selector_parts: list[str] = []
        for j in range(i, brace_line + 1):
            part = lines[j].replace("{", "").strip()
            if part and not part.startswith("/*") and not part.startswith("*"):
                selector_parts.append(part)
        selector = " ".join(selector_parts).strip()

        # Skip non-CSS-rule blocks like @keyframes, @font-face, etc.
        if selector.startswith("@"):
            # Still track the body for nesting
            depth = lines[brace_line].count("{") - lines[brace_line].count("}")
            j = brace_line + 1
            while j < len(lines) and depth > 0:
                depth += lines[j].count("{") - lines[j].count("}")
                j += 1
            i = j
            continue

        # Collect body
        brace_line_content = lines[brace_line]
        closing_on_same_line = brace_line_content.rfind("}") > brace_line_content.find("{")
        depth = brace_line_content.count("{") - brace_line_content.count("}")

        body_parts: list[str] = []

        # For single-line rules, extract content between { and }
        if closing_on_same_line and depth == 0:
            between = brace_line_content[brace_line_content.find("{") + 1 : brace_line_content.rfind("}")]
            body_parts.append(between)
            j = brace_line + 1  # no multi-line body to consume
        else:
            j = brace_line + 1
            while j < len(lines) and depth > 0:
                depth += lines[j].count("{") - lines[j].count("}")
                if depth > 0:
                    body_parts.append(lines[j])
                j += 1

        body = "\n".join(body_parts)

        # Determine if inside reduced-motion
        inside_rpm = any("prefers-reduced-motion" in m for m in media_stack)

        rules.append({
            "selector": selector,
            "body": body,
            "start_line": i + 1,
            "inside_rpm": inside_rpm,
        })

        i = j

    return rules


def _find_rules_for_component(
    rules: list[dict], selector_fragment: str
) -> list[dict]:
    """Return CSS rules whose selector contains ``selector_fragment``."""
    return [r for r in rules if selector_fragment in r["selector"]]


def _rule_has_transition(rule: dict) -> bool:
    """Check if a rule's body contains ``transition:`` (excluding ``none``)."""
    body = rule["body"]
    if "transition:" not in body:
        return False
    for val in _get_transition_values(rule):
        if val != "none":
            return True
    return False


def _get_transition_values(rule: dict) -> list[str]:
    """Extract transition property values from a rule's body.

    Handles multi-line transition values where the property spans
    lines (common when multiple transitions are comma-separated).
    """
    body = rule["body"]
    lines = body.split("\n")
    values: list[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if "transition:" in stripped and not stripped.startswith("/*"):
            # Check if ; is on this line (complete value)
            if ";" in stripped:
                m = re.search(r"transition:\s*([^;]+);", stripped)
                if m:
                    values.append(m.group(1).strip())
            else:
                # Multi-line value: collect until ;
                combined = stripped
                j = i + 1
                while j < len(lines):
                    combined += " " + lines[j].strip()
                    if ";" in lines[j]:
                        break
                    j += 1
                m = re.search(r"transition:\s*([^;]+);", combined)
                if m:
                    values.append(m.group(1).strip())
                i = j  # skip consumed lines
        i += 1
    return values


# ── Interactive component registry ─────────────────────────────────
#
# Each entry: (selector_fragment, description, transition_required)
# The selector_fragment is matched against the full selector text of
# each CSS rule.  Components that inherit transitions via CSS class
# composition (e.g. .btn-primary inherits from .btn) are marked
# required=False.

INTERACTIVE_COMPONENTS = [
    # ── Buttons — base .btn covers transition; variants use class="btn btn-*" ──
    (".btn", "base button class", True),
    (".btn-delete", "delete document button", True),
    (".btn-save", "settings save button", True),
    (".btn-login", "login button", True),
    (".btn-new-chat", "new chat button", True),
    (".btn-new-collection", "new collection button", True),
    (".btn-export", "export button", True),
    (".btn-read-full", "read full document button", True),
    (".btn-viewer-back", "viewer back link button", True),
    (".btn-cancel", "cancel link button", True),
    (".btn-view-link", "view link button", True),
    (".search-box button", "search box submit button", True),
    (".upload-form button", "upload form submit button", True),
    (".tag-input-row button", "tag input submit button", True),
    (".chat-input-row button", "chat send button", True),
    (".search-nav-btns button", "search navigation buttons", True),
    (".date-preset-btn", "date preset button", True),
    (".file-remove", "file remove button", True),
    (".kbd-modal-close", "keyboard modal close button", True),
    # ── Button variants (inherit .btn transition via class composition) ──
    (".btn-primary", "primary button variant", False),
    # uses class="btn btn-primary"
    (".btn-secondary", "secondary button variant", False),
    (".btn-danger", "danger button variant", False),
    (".btn-ghost", "ghost button variant", False),

    # ── Links ──
    ("header nav a", "header navigation links", True),
    (".pagination a", "pagination link", True),
    (".toc-list a", "table of contents link", True),
    (".collection-breadcrumb-link", "collection breadcrumb link", True),
    (".collection-action-link", "collection action link", True),

    # ── Inputs — base .input covers border-color transition ──
    (".input", "base input class", True),
    (".search-box input", "search box text input", True),
    (".tag-input-row input", "tag input text field", True),
    (".chat-input-row input", "chat text input", True),
    (".viewer-toolbar input[type=\"search\"]", "viewer search input", True),
    (".login-field input[type=\"password\"]", "login password input", True),
    ("#state-filter", "jobs state filter dropdown", True),
    (".viewer-pagination .page-jump", "viewer page jump input", True),
    # ── Input variants (covered by .input base rule + comma-separated selector) ──
    (".settings-field input[type=\"text\"]", "settings text input", False),
    (".settings-field input[type=\"password\"]", "settings password input", False),
    (".settings-field select", "settings select dropdown", False),
    (".settings-field input[type=\"range\"]", "settings range slider", False),

    # ── Cards / Containers ──
    (".card", "generic card container", True),
    (".result", "search result card", True),
    (".file-item", "file item card", True),
    (".stat", "statistics card", True),
    (".login-card", "login page card", True),

    # ── Tag Pills ──
    (".tag-pill", "tag pill", True),
    (".tag-cloud-item", "tag cloud item", True),

    # ── Collection / Tree / Session Items ──
    (".collection-tree-item", "collection tree item", True),
    (".chat-session-item", "chat session item", True),
    (".chat-session-del", "chat session delete button", True),

    # ── Theme Toggle ──
    (".theme-toggle", "theme toggle button", True),

    # ── Sliders ──
    (".vw-slider::-webkit-slider-thumb", "vector weight slider thumb (webkit)", True),
    (".vw-slider::-moz-range-thumb", "vector weight slider thumb (moz)", True),

    # ── Drop Zone ──
    (".drop-zone", "upload drop zone", True),

    # ── Modal ──
    (".kbd-modal-overlay", "keyboard shortcuts modal overlay", True),
    (".kbd-modal-panel", "keyboard shortcuts modal panel", True),

    # ── Progress Bar ──
    (".progress-bar", "progress bar", True),

    # ── HTMX ──
    (".htmx-indicator", "htmx loading indicator", True),
    (".htmx-added", "htmx added content", True),
    (".htmx-settling", "htmx settling content", True),
    (".htmx-swapping", "htmx swapping content", True),

    # ── Filters / Navigation ──
    (".date-range-selector a", "date range selector filter", True),
    (".filter-panel summary", "filter panel summary toggle", True),

    # ── Upload ──
    (".upload-form", "upload form container", True),

    # ── Exempt ──
    ("td input[type=\"checkbox\"]", "document checkbox row", False),
]


# ── 1. Transition existence tests ──────────────────────────────────

class TestTransitionExistence:
    """Verify every interactive component has a CSS transition rule."""

    @classmethod
    def setup_class(cls):
        cls.css = _read_css()
        cls.rules = _extract_css_rules(cls.css)

    def test_all_components_have_transitions(self):
        """Every component marked required=True must have a transition rule."""
        missing: list[str] = []

        for selector, desc, required in INTERACTIVE_COMPONENTS:
            if not required:
                continue
            matching = _find_rules_for_component(self.rules, selector)
            if not any(
                _rule_has_transition(r) and not r["inside_rpm"]
                for r in matching
            ):
                missing.append(f"  {desc} ({selector})")

        assert not missing, (
            "The following interactive components are MISSING CSS transition rules:\n"
            + "\n".join(missing)
        )

    def test_buttons_have_transitions(self):
        """Every required button selector must have a transition rule."""
        button_selectors = [
            s for s, d, r in INTERACTIVE_COMPONENTS
            if (("button" in s or "btn" in s) and r)
        ]
        missing: list[str] = []
        for s in button_selectors:
            matching = _find_rules_for_component(self.rules, s)
            if not any(
                _rule_has_transition(r) and not r["inside_rpm"]
                for r in matching
            ):
                missing.append(s)
        assert not missing, f"Buttons missing transitions: {missing}"

    def test_links_have_transitions(self):
        """Navigation and action links must have transition rules."""
        link_selectors = [
            s for s, d, r in INTERACTIVE_COMPONENTS
            if ("link" in s or "nav " in s or ".pagination" in s
                or ".toc-list" in s) and r
        ]
        missing: list[str] = []
        for s in link_selectors:
            matching = _find_rules_for_component(self.rules, s)
            if not any(
                _rule_has_transition(r) and not r["inside_rpm"]
                for r in matching
            ):
                missing.append(s)
        assert not missing, f"Links missing transitions: {missing}"

    def test_inputs_have_transitions(self):
        """All required input elements must have transition rules."""
        input_selectors = [
            s for s, d, r in INTERACTIVE_COMPONENTS
            if ("input" in s or "select" in s or "filter" in s)
            and "button" not in s and r
        ]
        missing: list[str] = []
        for s in input_selectors:
            matching = _find_rules_for_component(self.rules, s)
            if not any(
                _rule_has_transition(r) and not r["inside_rpm"]
                for r in matching
            ):
                missing.append(s)
        assert not missing, f"Inputs missing transitions: {missing}"

    def test_cards_have_transitions(self):
        """Card/container elements must have elevation/lift transitions."""
        card_selectors = [
            s for s, d, r in INTERACTIVE_COMPONENTS
            if ("card" in s or "result" in s or "file-item" in s or "stat" in s)
            and r
        ]
        missing: list[str] = []
        for s in card_selectors:
            matching = _find_rules_for_component(self.rules, s)
            if not any(
                _rule_has_transition(r) and not r["inside_rpm"]
                for r in matching
            ):
                missing.append(s)
        assert not missing, f"Cards missing transitions: {missing}"

    def test_special_elements_have_transitions(self):
        """Modal, progress bar, and HTMX elements must have transitions."""
        special = [
            ".kbd-modal-overlay",
            ".kbd-modal-panel",
            ".progress-bar",
            ".htmx-indicator",
            ".htmx-added",
            ".htmx-settling",
            ".htmx-swapping",
        ]
        missing: list[str] = []
        for s in special:
            matching = _find_rules_for_component(self.rules, s)
            if not any(
                _rule_has_transition(r) and not r["inside_rpm"]
                for r in matching
            ):
                missing.append(s)
        assert not missing, f"Special elements missing transitions: {missing}"


# ── 2. Transition property quality tests ───────────────────────────

class TestTransitionPropertyQuality:
    """Verify transitions use design tokens and target specific properties."""

    @classmethod
    def setup_class(cls):
        cls.css = _read_css()
        cls.rules = _extract_css_rules(cls.css)

    def test_all_transitions_use_design_tokens(self):
        """Every non-reduced-motion transition must use var(--...) design tokens."""
        violations: list[tuple[int, str, str]] = []
        for rule in self.rules:
            if rule["inside_rpm"]:
                continue
            for val in _get_transition_values(rule):
                if val == "none":
                    continue
                if "var(--" not in val:
                    violations.append((
                        rule["start_line"], rule["selector"], val
                    ))

        assert not violations, (
            f"{len(violations)} transition rules don't use design tokens:\n"
            + "\n".join(
                f"  L{ln}: {s} -> {v}" for ln, s, v in violations[:10]
            )
        )

    def test_no_blanket_transition_all(self):
        """No transition rule should use blanket 'all'."""
        violations: list[tuple[int, str, str]] = []
        for rule in self.rules:
            if rule["inside_rpm"]:
                continue
            for val in _get_transition_values(rule):
                if "all" in val and val != "none":
                    violations.append((
                        rule["start_line"], rule["selector"], val
                    ))

        assert not violations, (
            f"Found {len(violations)} rules using blanket 'transition: all':\n"
            + "\n".join(f"  L{ln}: {s}" for ln, s, _ in violations)
        )

    def test_button_transitions_use_press_or_color(self):
        """Buttons should use --transition-press or specific property tokens."""
        button_sels = [
            ".btn", ".btn-delete", ".btn-save", ".btn-login",
            ".btn-new-chat", ".btn-new-collection",
        ]
        violations: list[tuple[str, str]] = []
        for sel in button_sels:
            matching = _find_rules_for_component(self.rules, sel)
            for rule in matching:
                if _rule_has_transition(rule) and not rule["inside_rpm"]:
                    for val in _get_transition_values(rule):
                        if "var(--" not in val:
                            violations.append((sel, val))

        assert not violations, (
            f"Buttons with non-token transitions: {violations}"
        )

    def test_card_transitions_use_lift(self):
        """Base card classes (.card, .login-card) should use --transition-lift."""
        for s in [".card", ".login-card"]:
            matching = _find_rules_for_component(self.rules, s)
            assert matching, f"No CSS rule found for {s}"
            found = False
            for rule in matching:
                if _rule_has_transition(rule) and not rule["inside_rpm"]:
                    for val in _get_transition_values(rule):
                        if "var(--transition-lift)" in val:
                            found = True
            assert found, (
                f"Card '{s}' transition should use --transition-lift; "
                f"got: {[_get_transition_values(r) for r in matching if _rule_has_transition(r)]}"
            )

    def test_input_transitions_use_border_color(self):
        """Base input class should transition border-color."""
        matching = _find_rules_for_component(self.rules, ".input")
        assert matching, ".input base rule not found"
        found = False
        for rule in matching:
            for val in _get_transition_values(rule):
                if "border-color" in val:
                    found = True
        assert found, (
            ".input transition should include border-color; "
            f"got: {[_get_transition_values(r) for r in matching]}"
        )


# ── 3. Transition duration range tests ─────────────────────────────

class TestTransitionDurations:
    """Verify all transition durations stay within 150-300ms."""

    @classmethod
    def setup_class(cls):
        cls.css = _read_css()
        cls.rules = _extract_css_rules(cls.css)

    def test_all_durations_in_range(self):
        """No hardcoded duration should be below 150ms or above 300ms."""
        violations: list[tuple[int, str, str]] = []
        for rule in self.rules:
            if rule["inside_rpm"]:
                continue
            for val in _get_transition_values(rule):
                if val == "none" or "var(--" in val:
                    continue
                dur_match = re.search(r"([\d.]+)\s*s\b", val)
                if dur_match:
                    dur_s = float(dur_match.group(1))
                    dur_ms = dur_s * 1000
                    if dur_ms < 150 or dur_ms > 300:
                        violations.append((
                            rule["start_line"], rule["selector"], val
                        ))

        assert not violations, (
            f"{len(violations)} transition rules have duration outside 150-300ms:\n"
            + "\n".join(
                f"  L{ln}: {s} -> {v}" for ln, s, v in violations[:10]
            )
        )


# ── 4. Coverage completeness test ──────────────────────────────────

class TestTransitionCoverageCompleteness:
    """Verify transition rules cover all major interactive categories."""

    @classmethod
    def setup_class(cls):
        cls.css = _read_css()
        cls.rules = _extract_css_rules(cls.css)

    def test_all_categories_covered(self):
        """Each major category must have at least one element with a transition."""
        categories = {
            "buttons": [".btn", ".btn-save"],
            "links": ["header nav a", ".pagination a"],
            "inputs": [".input", ".search-box input"],
            "cards": [".card", ".result"],
            "tags": [".tag-pill", ".tag-cloud-item"],
            "collections": [".collection-tree-item", ".chat-session-item"],
            "htmx": [".htmx-indicator", ".htmx-added"],
            "dropzone": [".drop-zone"],
            "modal": [".kbd-modal-overlay"],
        }

        for cat_name, check_selectors in categories.items():
            found = False
            for s in check_selectors:
                matching = _find_rules_for_component(self.rules, s)
                if any(
                    _rule_has_transition(r) and not r["inside_rpm"]
                    for r in matching
                ):
                    found = True
                    break
            assert found, (
                f"Category '{cat_name}' has NO elements with transition rules. "
                f"Checked: {check_selectors}"
            )

    def test_transition_count_reasonable(self):
        """At least 50 interactive transition rules should exist."""
        meaningful = [
            r for r in self.rules
            if _rule_has_transition(r) and not r["inside_rpm"]
        ]
        assert len(meaningful) >= 50, (
            f"Only {len(meaningful)} meaningful transition rules found (expected >= 50)"
        )

    def test_critical_elements_have_transitions(self):
        """Curated list of most important interactive elements must have transitions."""
        critical = [
            (".btn", "primary button class"),
            (".input", "base input class"),
            (".card", "base card class"),
            (".result", "search result card"),
            (".tag-pill", "tag pill"),
            (".collection-tree-item", "collection tree item"),
            (".chat-session-item", "chat session item"),
            (".drop-zone", "upload drop zone"),
            (".search-box input", "search input"),
            (".htmx-indicator", "htmx loading indicator"),
        ]
        missing = []
        for sel, desc in critical:
            matching = _find_rules_for_component(self.rules, sel)
            if not any(
                _rule_has_transition(r) and not r["inside_rpm"]
                for r in matching
            ):
                missing.append(f"  {desc} ({sel})")
        assert not missing, (
            "Critical interactive elements missing CSS transitions:\n"
            + "\n".join(missing)
        )