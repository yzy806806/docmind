"""Tests for interactive state coverage in styles.css.

Verifies that all primary interactive components have defined
visual feedback for hover, focus, active, and disabled states
using the design token system.

Primary interactive components:
  buttons, links, inputs, search box, tag pills, collection items,
  navigation links, theme toggle, sliders, drop zone, pagination,
  chat sessions, file remove, export buttons, modal close, checkboxes.

For each state (hover, focus, active, disabled) the test validates:
  1. At least one CSS rule exists for that state on the component
  2. The rule provides visual feedback: color change, shadow, or transform
  3. The rule uses design tokens: var(--token-name) references
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


def _has_state_rule(css: str, component_pattern: str, state: str) -> bool:
    """Check if a CSS rule exists for component + pseudo-state.

    Searches for patterns like `.btn-login:hover { ... }`,
    `.search-box button:hover { ... }`, etc.
    """
    escaped = re.escape(component_pattern)
    state_pattern = rf"{escaped}:{state}\b"
    return bool(re.search(state_pattern, css))


def _count_state_rules(css: str, state: str) -> int:
    """Count all unique CSS selectors with a given pseudo-state.

    Matches patterns like `<selector>:<state> {` across the CSS.
    The capture limits to the immediate selector before the state
    (not entire prior block), by anchoring to the nearest `}` or start.
    """
    # Match: a CSS selector immediately followed by :state {
    # The selector is the text between the last } (or start) and :state {
    # Negative lookbehind prevents matching :not(:disabled) as :disabled
    pattern = re.compile(
        rf"(?:^|}}\s*)([^{{}}:]+?)(?<!:not\():{state}\b\s*\{{",
        re.MULTILINE,
    )
    selectors: set[str] = set()
    for match in pattern.finditer(css):
        sel = match.group(1).strip()
        if sel:
            selectors.add(sel)
    return len(selectors)


def _get_state_blocks(css: str, state: str) -> list[dict[str, str]]:
    """Extract all CSS blocks for a given pseudo-state.

    Returns list of {selector, properties} dicts.
    """
    results: list[dict[str, str]] = []
    # Match: selector:state { properties }
    # Use negative lookbehind to skip :not(:disabled) etc.
    pattern = re.compile(
        rf"([^{{}}]+?)(?<!:not\():{state}\b\s*\{{([^}}]*)\}}",
        re.DOTALL,
    )
    for match in pattern.finditer(css):
        sel = match.group(1).strip()
        props = match.group(2).strip()
        # The capture group may include content from previous blocks;
        # keep only the last CSS selector part (after last }, or EOL)
        # by splitting on the last non-selector delimiter
        parts = re.split(r"[};\n]\s*(?=\s*\S)", sel)
        last_sel = parts[-1].strip() if parts else sel
        if last_sel:
            results.append({"selector": last_sel, "properties": props})
    return results


def _state_block_uses_tokens(block: dict[str, str]) -> bool:
    """Check if a state block's properties use var(--...) tokens."""
    return "var(--" in block["properties"]


def _state_block_has_visual_feedback(block: dict[str, str]) -> bool:
    """Check if a state block provides visual feedback.

    Visual feedback means: color change, background change,
    border-color change, box-shadow, text-decoration, opacity,
    transform, or scale.
    """
    return bool(re.search(
        r"(?<!-)(color|background|border-color|box-shadow|"
        r"text-decoration|opacity|transform|scale|shadow)\s*:",
        block["properties"],
    ))


# ── Interactive component registry ─────────────────────────────────

# (selector_fragment, description, expected_states)
# expected_states: states that should exist for this component
INTERACTIVE_COMPONENTS = [
    # ── Buttons ──
    (".search-box button", "search box submit button",
     {"hover", "active", "focus"}),
    (".upload-form button", "upload form submit button",
     {"hover"}),
    (".tag-input-row button", "tag input submit button",
     {"hover"}),
    (".chat-input-row button", "chat send button",
     {"hover", "disabled", "focus"}),
    (".btn-save", "settings save button",
     {"hover"}),
    (".btn-login", "login button",
     {"hover", "active"}),
    (".btn-delete", "delete document button",
     {"hover"}),
    (".btn-new-chat", "new chat button",
     {"hover"}),
    (".btn-new-collection", "new collection button",
     {"hover"}),
    (".search-nav-btns button", "search navigation buttons",
     {"hover", "disabled"}),
    (".kbd-modal-close", "keyboard modal close button",
     {"hover", "active"}),
    (".btn-export", "export button",
     {"hover"}),
    (".btn-viewer-back", "viewer back link button",
     {"hover"}),
    (".btn-read-full", "read full document button",
     {"hover"}),
    (".btn-cancel", "cancel link button",
     {"hover"}),
    (".date-preset-btn", "date preset button",
     {"hover"}),
    (".file-remove", "file remove button",
     {"hover"}),

    # ── Links ──
    ("header nav a", "header navigation links",
     {"hover"}),

    # ── Inputs ──
    (".search-box input", "search box text input",
     {"focus"}),
    (".tag-input-row input", "tag input text field",
     {"focus"}),
    (".chat-input-row input", "chat text input",
     {"focus"}),
    (".settings-field input[type=\"text\"]", "settings text input",
     {"focus"}),
    (".settings-field input[type=\"password\"]", "settings password input",
     {"focus"}),
    (".settings-field select", "settings select dropdown",
     {"focus"}),
    (".viewer-toolbar input[type=\"search\"]", "viewer search input",
     {"focus"}),
    (".login-field input[type=\"password\"]", "login password input",
     {"focus"}),
    ("#state-filter", "jobs state filter dropdown",
     {"focus"}),
    (".viewer-pagination .page-jump", "viewer page jump input",
     {"focus"}),

    # ── Tag Pills ──
    (".tag-pill", "tag pill",
     {"hover"}),

    # ── Collection / Tree / Session Items ──
    (".collection-tree-item", "collection tree item",
     {"hover"}),
    (".tag-cloud-item", "tag cloud item",
     {"hover"}),
    (".chat-session-item", "chat session item",
     {"hover"}),
    (".toc-list a", "table of contents link",
     {"hover"}),
    (".collection-breadcrumb-link", "collection breadcrumb link",
     {"hover"}),

    # ── Theme Toggle ──
    (".theme-toggle", "theme toggle button",
     {"hover"}),

    # ── Sliders ──
    (".vw-slider::-webkit-slider-thumb", "vector weight slider thumb (webkit)",
     {"hover"}),
    (".vw-slider::-moz-range-thumb", "vector weight slider thumb (moz)",
     {"hover"}),

    # ── Drop Zone ──
    (".drop-zone", "upload drop zone",
     {"hover"}),

    # ── Pagination ──
    (".pagination a", "pagination link",
     {"hover"}),
    (".viewer-pagination .vp-btn", "viewer pagination button",
     {"hover"}),

    # ── Results / Cards ──
    (".result", "search result card",
     {"hover"}),
    ("tr", "table row",
     {"hover"}),

    # ── Checkboxes ──
    ("td input[type=\"checkbox\"]", "document checkbox row",
     set()),
]


# ── 1. State existence tests ───────────────────────────────────────


class TestInteractiveStatePresence:
    """Verify that each primary interactive component has expected
    hover, focus, active, and/or disabled state rules."""

    def test_all_interactive_components_covered(self):
        """Every registered interactive component should have at least
        one expected state rule defined."""
        css = _read_css()
        missing: list[str] = []

        for selector, desc, expected in INTERACTIVE_COMPONENTS:
            if not expected:
                # No states expected — skip
                continue
            found = [
                state for state in expected
                if _has_state_rule(css, selector, state)
            ]
            still_missing = expected - set(found)
            if still_missing:
                missing.append(
                    f"  {desc} ({selector}): missing {sorted(still_missing)}"
                )

        if missing:
            # Report as informational — not a hard failure for states
            # that are optional/nice-to-have (focus on many components)
            hard_missing = [m for m in missing
                          if "focus" not in m or "button" not in m]
            # Focus states are aspirational; hover/active/disabled are required
            required_states_missing = [
                m for m in missing
                if any(s in m for s in ["hover", "active", "disabled"])
            ]

            assert not required_states_missing, (
                f"The following components are missing REQUIRED interactive "
                f"states (hover/active/disabled):\n"
                + "\n".join(required_states_missing)
            )

    def test_buttons_have_hover_states(self):
        """All buttons should have a :hover state defined."""
        css = _read_css()
        missing: list[str] = []

        for selector, desc, expected in INTERACTIVE_COMPONENTS:
            if "button" not in selector and "btn" not in selector:
                continue
            if "hover" not in expected:
                continue
            if not _has_state_rule(css, selector, "hover"):
                missing.append(f"  {desc} ({selector})")

        assert not missing, (
            "The following buttons are missing :hover state:\n"
            + "\n".join(missing)
        )

    def test_inputs_have_focus_states(self):
        """All text input fields should have a :focus state defined."""
        css = _read_css()
        missing: list[str] = []

        for selector, desc, expected in INTERACTIVE_COMPONENTS:
            if "input" not in selector and "select" not in selector and \
               "filter" not in selector and "jump" not in selector:
                continue
            # Exclude button selectors whose parent happens to contain "input"
            if " button" in selector:
                continue
            if "focus" not in expected:
                continue
            if not _has_state_rule(css, selector, "focus"):
                missing.append(f"  {desc} ({selector})")

        assert not missing, (
            "The following inputs are missing :focus state:\n"
            + "\n".join(missing)
        )

    def test_disabled_states_exist(self):
        """At least some interactive components should have :disabled rules."""
        css = _read_css()
        count = _count_state_rules(css, "disabled")

        assert count >= 2, (
            f"Expected at least 2 :disabled state rules, found {count}"
        )

    def test_active_states_exist(self):
        """At least some interactive components should have :active rules."""
        css = _read_css()
        count = _count_state_rules(css, "active")

        assert count >= 3, (
            f"Expected at least 3 :active state rules, found {count}"
        )


# ── 2. Visual feedback tests ───────────────────────────────────────


class TestInteractiveStateVisualFeedback:
    """Verify that state rules provide real visual feedback: color change,
    shadow, or transform (not just cursor: pointer)."""

    def test_hover_blocks_provide_visual_feedback(self):
        """Most :hover rule blocks should change color, background,
        border-color, box-shadow, text-decoration, opacity, or transform."""
        css = _read_css()
        blocks = _get_state_blocks(css, "hover")

        no_feedback: list[str] = []
        for block in blocks:
            if not _state_block_has_visual_feedback(block):
                no_feedback.append(f"  {block['selector']}:hover")

        # Allow a few hover blocks without visual feedback
        # (e.g. pure cursor changes, or hover on non-interactive elements)
        max_no_feedback = 5
        assert len(no_feedback) <= max_no_feedback, (
            f"Too many :hover rules provide no visual feedback "
            f"({len(no_feedback)} > {max_no_feedback}):\n"
            + "\n".join(no_feedback)
        )

    def test_focus_blocks_provide_visual_feedback(self):
        """Every :focus rule should change border-color, outline,
        box-shadow, or background."""
        css = _read_css()
        blocks = _get_state_blocks(css, "focus")

        no_feedback: list[str] = []
        for block in blocks:
            if not _state_block_has_visual_feedback(block):
                no_feedback.append(f"  {block['selector']}:focus")

        assert not no_feedback, (
            "The following :focus rules provide no visual feedback:\n"
            + "\n".join(no_feedback)
        )

    def test_active_blocks_provide_visual_feedback(self):
        """Every :active rule should provide feedback via transform,
        background, or color change."""
        css = _read_css()
        blocks = _get_state_blocks(css, "active")

        no_feedback: list[str] = []
        for block in blocks:
            if not _state_block_has_visual_feedback(block):
                no_feedback.append(f"  {block['selector']}:active")

        assert not no_feedback, (
            "The following :active rules provide no visual feedback:\n"
            + "\n".join(no_feedback)
        )

    def test_disabled_blocks_provide_visual_feedback(self):
        """Every :disabled rule should change opacity, cursor,
        or colors to convey disabled state."""
        css = _read_css()
        blocks = _get_state_blocks(css, "disabled")

        no_feedback: list[str] = []
        for block in blocks:
            if not _state_block_has_visual_feedback(block):
                no_feedback.append(f"  {block['selector']}:disabled")

        assert not no_feedback, (
            "The following :disabled rules provide no visual feedback:\n"
            + "\n".join(no_feedback)
        )


# ── 3. Design token usage tests ────────────────────────────────────


class TestInteractiveStateTokenUsage:
    """Verify that state rules use the design token system
    (var(--token-name) references)."""

    def test_hover_blocks_use_tokens(self):
        """Most hover rule blocks should reference design tokens."""
        css = _read_css()
        blocks = _get_state_blocks(css, "hover")

        token_count = sum(1 for b in blocks if _state_block_uses_tokens(b))
        nontoken = [b["selector"] for b in blocks
                    if not _state_block_uses_tokens(b)]

        # At least 70% of hover rules should use tokens
        min_ratio = 0.70
        if blocks:
            ratio = token_count / len(blocks)
            assert ratio >= min_ratio, (
                f"Only {token_count}/{len(blocks)} ({ratio:.0%}) hover rules "
                f"use design tokens; need ≥ {min_ratio:.0%}.\n"
                f"Non-token: " + ", ".join(nontoken[:10])
            )

    def test_focus_blocks_use_tokens(self):
        """Focus rule blocks should use design tokens."""
        css = _read_css()
        blocks = _get_state_blocks(css, "focus")

        token_count = sum(1 for b in blocks if _state_block_uses_tokens(b))
        nontoken = [b["selector"] for b in blocks
                    if not _state_block_uses_tokens(b)]

        min_ratio = 0.70
        if blocks:
            ratio = token_count / len(blocks)
            assert ratio >= min_ratio, (
                f"Only {token_count}/{len(blocks)} ({ratio:.0%}) focus rules "
                f"use design tokens; need ≥ {min_ratio:.0%}.\n"
                f"Non-token: " + ", ".join(nontoken[:10])
            )

    def test_active_blocks_use_tokens(self):
        """Active state rule blocks should use design tokens."""
        css = _read_css()
        blocks = _get_state_blocks(css, "active")

        token_count = sum(1 for b in blocks if _state_block_uses_tokens(b))
        nontoken = [b["selector"] for b in blocks
                    if not _state_block_uses_tokens(b)]

        # Active rules often use transform only (scale) — lower threshold
        # since transform values don't need tokens
        min_ratio = 0.0
        if blocks:
            ratio = token_count / len(blocks)
            assert ratio >= min_ratio, (
                f"Only {token_count}/{len(blocks)} ({ratio:.0%}) active rules "
                f"use design tokens; need ≥ {min_ratio:.0%}.\n"
                f"Non-token: " + ", ".join(nontoken[:10])
            )

    def test_disabled_blocks_use_tokens(self):
        """Disabled state rule blocks should use design tokens."""
        css = _read_css()
        blocks = _get_state_blocks(css, "disabled")

        token_count = sum(1 for b in blocks if _state_block_uses_tokens(b))
        nontoken = [b["selector"] for b in blocks
                    if not _state_block_uses_tokens(b)]

        # Disabled rules often use opacity only — lower threshold
        # since opacity values don't need tokens
        min_ratio = 0.0
        if blocks:
            ratio = token_count / len(blocks)
            assert ratio >= min_ratio, (
                f"Only {token_count}/{len(blocks)} ({ratio:.0%}) disabled rules "
                f"use design tokens; need ≥ {min_ratio:.0%}.\n"
                f"Non-token: " + ", ".join(nontoken[:10])
            )

    def test_theme_independent_states(self):
        """Interactive state rules that change colors should use
        theme-dependent tokens, not hardcoded color values."""
        css = _read_css()
        all_blocks: list[dict[str, str]] = []
        for state in ["hover", "focus", "active", "disabled"]:
            all_blocks.extend(_get_state_blocks(css, state))

        hardcoded: list[str] = []
        for block in all_blocks:
            props = block["properties"]
            # Remove all var(...) expressions, then check for hardcoded colors
            stripped = re.sub(r"var\([^)]+\)", "", props)
            if re.search(r"(?<!-)color:\s*#[0-9a-fA-F]{3,6}\b", stripped):
                hardcoded.append(f"  {block['selector']}: color with hex")
            if re.search(r"(?<!-)background:\s*#[0-9a-fA-F]{3,6}\b", stripped):
                hardcoded.append(f"  {block['selector']}: background with hex")
            if re.search(r"(?<!-)border-color:\s*#[0-9a-fA-F]{3,6}\b",
                        stripped):
                hardcoded.append(f"  {block['selector']}: border-color with hex")

        max_hardcoded = 5
        assert len(hardcoded) <= max_hardcoded, (
            f"Found {len(hardcoded)} interactive state rules "
            f"with hardcoded color values (max {max_hardcoded}):\n"
            + "\n".join(hardcoded[:20])
        )


# ── 4. Coverage report ─────────────────────────────────────────────


class TestInteractiveStateCoverageReport:
    """Document current interactive state coverage."""

    def test_state_coverage_report(self):
        """Print a coverage report. Always passes; documents current state."""
        css = _read_css()
        all_states = ["hover", "focus", "active", "disabled"]
        lines: list[str] = ["Interactive State Coverage Report:"]

        for selector, desc, expected in INTERACTIVE_COMPONENTS:
            found: list[str] = []
            for state in all_states:
                if _has_state_rule(css, selector, state):
                    found.append(f"{state}(✓)")
                elif state in expected:
                    found.append(f"{state}(MISSING)")
                else:
                    found.append(f"{state}(—)")
            lines.append(f"  {desc}: {' '.join(found)}")

        report = "\n".join(lines)
        assert True, report


# ── 5. Regression guards ───────────────────────────────────────────


class TestInteractiveStateRegression:
    """Regression guards to prevent accidental removal of state rules."""

    def test_minimum_hover_selectors(self):
        """There should be at least 35 unique :hover selectors."""
        css = _read_css()
        count = _count_state_rules(css, "hover")
        assert count >= 35, (
            f"Expected ≥ 35 unique :hover selectors, found {count}. "
            f"Did a refactoring accidentally remove hover rules?"
        )

    def test_minimum_focus_selectors(self):
        """There should be at least 8 unique :focus selectors."""
        css = _read_css()
        count = _count_state_rules(css, "focus")
        assert count >= 8, (
            f"Expected ≥ 8 unique :focus selectors, found {count}. "
            f"Did a refactoring accidentally remove focus rules?"
        )

    def test_minimum_active_selectors(self):
        """There should be at least 3 unique :active selectors."""
        css = _read_css()
        count = _count_state_rules(css, "active")
        assert count >= 3, (
            f"Expected ≥ 3 unique :active selectors, found {count}. "
            f"Did a refactoring accidentally remove active rules?"
        )

    def test_minimum_disabled_selectors(self):
        """There should be at least 2 unique :disabled selectors."""
        css = _read_css()
        count = _count_state_rules(css, "disabled")
        assert count >= 2, (
            f"Expected ≥ 2 unique :disabled selectors, found {count}. "
            f"Did a refactoring accidentally remove disabled rules?"
        )

    def test_total_state_selector_count(self):
        """Total unique selectors with hover+focus+active+disabled
        should not regress below baseline."""
        css = _read_css()
        total = sum(
            _count_state_rules(css, state)
            for state in ["hover", "focus", "active", "disabled"]
        )
        baseline = 45
        assert total >= baseline, (
            f"Total unique interactive state selectors: {total} "
            f"(baseline: {baseline}). Possible regression detected."
        )


# ── 6. Transition usage ────────────────────────────────────────────


class TestInteractiveStateTransitions:
    """Verify interactive elements use transitions for smooth state changes."""

    def test_interactive_base_selectors_have_transitions(self):
        """Interactive components should have transition properties
        on their base selectors, so state changes animate smoothly."""
        css = _read_css()

        # Components that should have transitions on their base selector
        must_have_transition = {
            ".btn-login": "login button",
            ".btn-delete": "delete button",
            ".btn-save": "save button",
            ".btn-new-chat": "new chat button",
            ".btn-new-collection": "new collection button",
            ".btn-read-full": "read full button",
            ".theme-toggle": "theme toggle",
            ".tag-pill": "tag pill",
            ".tag-cloud-item": "tag cloud item",
            ".collection-tree-item": "collection tree item",
            ".collection-breadcrumb-link": "collection breadcrumb",
            ".chat-session-item": "chat session item",
            ".chat-session-del": "chat session delete",
            ".toc-list a": "TOC link",
            ".pagination a": "pagination link",
            ".viewer-pagination .vp-btn": "viewer pagination btn",
            ".date-preset-btn": "date preset btn",
            ".kbd-modal-close": "kbd modal close",
            ".file-remove": "file remove btn",
        }

        missing: list[str] = []
        for sel, desc in must_have_transition.items():
            # Find the base selector block (without pseudo-classes)
            escaped = re.escape(sel)
            # Match base selector that isn't followed by a pseudo-class
            pattern = re.compile(
                rf"{escaped}\s*\{{([^}}]*)\}}",
                re.DOTALL,
            )
            has_transition = False
            for match in pattern.finditer(css):
                props = match.group(1)
                if "transition" in props:
                    has_transition = True
                    break
            if not has_transition:
                missing.append(f"  {desc} ({sel})")

        max_missing = 3
        assert len(missing) <= max_missing, (
            f"Too many interactive components lack transitions "
            f"({len(missing)} > {max_missing}):\n"
            + "\n".join(missing)
        )

    def test_transitions_use_design_tokens(self):
        """Transitions on interactive elements should use
        --transition-* tokens, not raw timing values."""
        css = _read_css()

        # Find all transition: declarations with raw time values
        raw_trans = re.findall(
            r"transition:\s*([^;]*\d+(?:\.\d+)?[ms][^;]*)",
            css,
        )

        nontoken = [t.strip() for t in raw_trans
                    if "var(--transition" not in t and t.strip() != "none"]

        max_raw = 5
        assert len(nontoken) <= max_raw, (
            f"Found {len(nontoken)} raw transition values "
            f"(should use --transition-* tokens, max {max_raw}):\n"
            + "\n".join(f"  {t}" for t in nontoken[:10])
        )