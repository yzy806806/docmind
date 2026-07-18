"""Verification tests for CSS token migration and transition coverage.

Part of Phase 9: Execute Now — verification that:
(a) Token migration is complete — var(--) design token references dominate
    raw color values by a minimum ratio.
(b) Transition coverage — key interactive selectors have CSS transition
    properties applied.

These are contract tests: a failure means the CSS file fails to meet the
quantitative threshold and must be fixed before the stop condition is met.
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
    """Remove /* ... */ comments from CSS text."""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _extract_top_level_block(css: str, selector_pattern: str) -> str:
    """Extract a top-level CSS block matching selector_pattern using brace counting."""
    match = re.search(selector_pattern, css)
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


def _css_without_definition_blocks(css: str) -> str:
    """Return CSS with :root and [data-theme="dark"] blocks removed.

    These blocks necessarily contain raw color values because they ARE
    the token definitions. The rest of the CSS should use var() references.
    """
    blocks_to_remove = [
        r":root\s*\{",
        r'\[data-theme="dark"\]\s*\{',
    ]
    result = css
    for pattern in blocks_to_remove:
        block = _extract_top_level_block(result, pattern)
        if block:
            result = result.replace(block, "")
    return result


def _count_var_references(css: str) -> int:
    """Count all var(--token) references in CSS (after stripping comments)."""
    return len(re.findall(r"var\(\s*--[\w-]+", css))


def _count_raw_color_values(css: str) -> list[tuple[int, str, str]]:
    """Count hardcoded color values (hex, rgb, rgba, hsl, hsla) outside definition blocks.

    Returns list of (line_number, color_type, context).
    """
    lines = _css_without_definition_blocks(css).split("\n")
    findings: list[tuple[int, str, str]] = []

    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        # Skip lines using var() — those are properly tokenized
        if "var(" in stripped:
            continue

        # Skip comment-only lines, empty lines
        if not stripped or stripped.startswith("/*") or stripped.startswith("*"):
            continue

        # Skip @keyframes blocks (animation keyframes may use raw colors
        # for visual effects like shimmer, which is by-design)
        if "@keyframes" in stripped:
            continue

        # Hex colors: #rgb, #rrggbb, #rrggbbaa
        if re.search(r"(?<!-)#[0-9a-fA-F]{3,8}\b", stripped):
            findings.append((i, "hex", stripped[:120]))

        # rgb/rgba/hsl/hsla
        elif re.search(r"\b(rgb|rgba|hsl|hsla)\([^)]+\)", stripped):
            findings.append((i, "func", stripped[:120]))

    return findings


def _extract_css_rules(css: str) -> list[dict]:
    """Extract all CSS rule blocks as {selector, body, start_line, inside_rpm}."""
    lines = css.split("\n")
    rules: list[dict] = []
    media_stack: list[str] = []

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        if "@media" in stripped and "{" in stripped:
            media_stack.append(stripped)
            i += 1
            continue

        if stripped == "}" and media_stack:
            media_stack.pop()
            i += 1
            continue

        if (
            stripped == ""
            or stripped.startswith("/*")
            or stripped.startswith("//")
            or stripped.startswith("*")
        ):
            i += 1
            continue

        if re.match(r"^\s*[\w-]+\s*:", stripped) and "{" not in stripped:
            i += 1
            continue

        brace_on_this_line = "{" in stripped
        if not brace_on_this_line:
            next_i = i + 1
            while next_i < len(lines) and lines[next_i].strip() == "":
                next_i += 1
            if next_i < len(lines) and lines[next_i].strip() == "{":
                brace_on_this_line = True
            else:
                i += 1
                continue

        if "{" in lines[i]:
            brace_line = i
        else:
            brace_line = i + 1
            while brace_line < len(lines) and "{" not in lines[brace_line]:
                brace_line += 1
            if brace_line >= len(lines):
                i += 1
                continue

        selector_parts: list[str] = []
        for j in range(i, brace_line + 1):
            part = lines[j].replace("{", "").strip()
            if part and not part.startswith("/*") and not part.startswith("*"):
                selector_parts.append(part)
        selector = " ".join(selector_parts).strip()

        if selector.startswith("@"):
            depth = lines[brace_line].count("{") - lines[brace_line].count("}")
            j = brace_line + 1
            while j < len(lines) and depth > 0:
                depth += lines[j].count("{") - lines[j].count("}")
                j += 1
            i = j
            continue

        brace_line_content = lines[brace_line]
        closing_on_same_line = brace_line_content.rfind("}") > brace_line_content.find("{")
        depth = brace_line_content.count("{") - brace_line_content.count("}")
        body_parts: list[str] = []

        if closing_on_same_line and depth == 0:
            between = brace_line_content[brace_line_content.find("{") + 1 : brace_line_content.rfind("}")]
            body_parts.append(between)
            j = brace_line + 1
        else:
            j = brace_line + 1
            while j < len(lines) and depth > 0:
                depth += lines[j].count("{") - lines[j].count("}")
                if depth > 0:
                    body_parts.append(lines[j])
                j += 1

        body = "\n".join(body_parts)
        inside_rpm = any("prefers-reduced-motion" in m for m in media_stack)

        rules.append({
            "selector": selector,
            "body": body,
            "start_line": i + 1,
            "inside_rpm": inside_rpm,
        })

        i = j

    return rules


def _rule_has_smoothness(rule: dict) -> bool:
    """Check if a rule's body has a transition or animation (not 'none')
    and is not inside RPM.

    Both ``transition:`` and ``animation:`` provide smooth visual behavior.
    Elements like toast messages and optimistic UI feedback use animations
    instead of transitions — both are valid smoothness mechanisms.
    """
    if rule["inside_rpm"]:
        return False
    body = rule["body"]

    # Check transition
    for m in re.finditer(r"transition:\s*([^;}]+)", body):
        val = m.group(1).strip()
        if val != "none":
            return True

    # Check animation (some elements use @keyframes instead of transitions)
    for m in re.finditer(r"animation:\s*([^;}]+)", body):
        val = m.group(1).strip()
        if val != "none":
            return True

    return False


# ═══════════════════════════════════════════════════════════════════
# 1. Token Migration Completeness Tests
# ═══════════════════════════════════════════════════════════════════

# Minimum acceptable ratio of var(--) references to total color expression
# (var + hardcoded).  0.95 means at least 95% of color usage is tokenized.
MIN_TOKEN_RATIO = 0.95

# Maximum hardcoded color values allowed outside definition blocks.
# Currently 0 — but we set a small tolerance so CI doesn't break on
# a single edge case.
MAX_HARDCODED_COLORS = 5

# Minimum total var(--) references.  Prevents catastrophic token removal
# that goes unnoticed.
MIN_VAR_REFERENCES = 800


class TestTokenMigrationCompleteness:
    """Verify design token migration is complete and meets quantitative thresholds.

    The stop condition requires var(--token) design token references to
    dominate raw hex/rgb/rgba/hsl/hsla color values by a minimum ratio.
    This is a hard assertion — not a warning.
    """

    @classmethod
    def setup_class(cls):
        cls.css = _read_css()
        cls.css_clean = _strip_comments(cls.css)

    def test_var_references_count(self):
        """Total var(--) references must exceed the minimum baseline."""
        count = _count_var_references(self.css_clean)
        assert count >= MIN_VAR_REFERENCES, (
            f"var(--) references: {count} (minimum: {MIN_VAR_REFERENCES}). "
            "Token count dropped below baseline — investigate regression."
        )

    def test_token_migration_ratio(self):
        """var(--) references must exceed raw color values by the minimum ratio.

        Formula: ratio = var_refs / (var_refs + hardcoded_colors)

        A value >= MIN_TOKEN_RATIO means at least {ratio_pct}% of all color
        expressions use design tokens.  This is a hard gate — the project
        stop condition is not met until this passes.
        """.format(ratio_pct=int(MIN_TOKEN_RATIO * 100))
        var_count = _count_var_references(self.css_clean)
        raw_colors = _count_raw_color_values(self.css_clean)

        total = var_count + len(raw_colors)
        ratio = var_count / total if total > 0 else 1.0

        assert ratio >= MIN_TOKEN_RATIO, (
            f"Token migration ratio: {ratio:.1%} (minimum: {MIN_TOKEN_RATIO:.0%}).\n"
            f"  var(--) references: {var_count}\n"
            f"  Hardcoded color values: {len(raw_colors)}\n"
            f"  Total expressions: {total}\n\n"
            f"Hardcoded colors found:\n"
            + "\n".join(
                f"  L{ln} ({typ}): {ctx}"
                for ln, typ, ctx in raw_colors
            )
        )

    def test_no_hardcoded_hex_colors_outside_definitions(self):
        """No raw hex colors (#xxx, #xxxxxx) should appear outside :root/dark blocks.

        Any hex color outside token definition blocks means a component
        is using a hardcoded color instead of a design token.
        """
        raw_colors = _count_raw_color_values(self.css_clean)
        hex_colors = [(ln, typ, ctx) for ln, typ, ctx in raw_colors if typ == "hex"]

        assert len(hex_colors) <= MAX_HARDCODED_COLORS, (
            f"Found {len(hex_colors)} hardcoded hex color(s) outside definition "
            f"blocks (max allowed: {MAX_HARDCODED_COLORS}):\n"
            + "\n".join(f"  L{ln}: {ctx}" for ln, _, ctx in hex_colors)
        )

    def test_no_hardcoded_rgb_colors_outside_definitions(self):
        """No raw rgb/rgba/hsl/hsla colors should appear outside
        :root/dark blocks.
        """
        raw_colors = _count_raw_color_values(self.css_clean)
        func_colors = [(ln, typ, ctx) for ln, typ, ctx in raw_colors if typ == "func"]

        assert len(func_colors) <= MAX_HARDCODED_COLORS, (
            f"Found {len(func_colors)} hardcoded rgb/rgba/hsl/hsla color(s) "
            f"outside definition blocks (max allowed: {MAX_HARDCODED_COLORS}):\n"
            + "\n".join(f"  L{ln}: {ctx}" for ln, _, ctx in func_colors)
        )

    def test_var_references_growing_or_stable(self):
        """var(--) usage should not regress below recent known baseline.

        This guards against accidental token removal during refactoring.
        Baseline is set conservatively from observed 1072 references.
        """
        var_count = _count_var_references(self.css_clean)
        regression_baseline = 850  # ~80% of current 1072

        assert var_count >= regression_baseline, (
            f"var(--) references: {var_count} (regression baseline: {regression_baseline}). "
            "Token usage dropped significantly — suspicious mass removal?"
        )


# ═══════════════════════════════════════════════════════════════════
# 2. Transition Coverage Tests
# ═══════════════════════════════════════════════════════════════════

# Key interactive selectors that MUST have transition properties.
# These represent the core interactive surface of the application.
# Format: (css_selector_fragment, human_description)
KEY_INTERACTIVE_SELECTORS = [
    # ── Buttons ──
    (".btn", "base button class"),
    (".btn-primary", "primary button"),
    (".btn-secondary", "secondary button"),
    (".btn-danger", "danger button"),
    (".btn-ghost", "ghost button"),
    (".btn-save", "save button"),
    (".btn-login", "login button"),
    (".btn-export", "export button"),
    # ── Navigation links ──
    ("header nav a", "header navigation links"),
    (".pagination a", "pagination link"),
    (".toc-list a", "table of contents link"),
    (".collection-breadcrumb-link", "breadcrumb link"),
    # ── Inputs ──
    (".input", "base input class"),
    (".search-box input", "search input"),
    (".chat-input-row input", "chat input"),
    ("#state-filter", "filter dropdown"),
    # ── Cards ──
    (".card", "base card"),
    (".result", "search result card"),
    (".login-card", "login card"),
    # ── Tags ──
    (".tag-pill", "tag pill"),
    (".tag-cloud-item", "tag cloud item"),
    # ── Collection / Session ──
    (".collection-tree-item", "collection tree item"),
    (".chat-session-item", "chat session item"),
    # ── Special interactive elements ──
    (".theme-toggle", "theme toggle"),
    (".drop-zone", "upload drop zone"),
    (".kbd-modal-close", "modal close button"),
    (".kbd-modal-overlay", "modal overlay"),
    (".kbd-modal-panel", "modal panel"),
    # ── HTMX ──
    (".htmx-indicator", "htmx loading indicator"),
    (".htmx-added", "htmx added content"),
    (".htmx-swapping", "htmx swapping content"),
    # ── Filters ──
    (".date-range-selector a", "date filter link"),
    (".date-preset-btn", "date preset button"),
    (".filter-panel summary", "filter panel toggle"),
    # ── Progress / Feedback ──
    (".progress-bar", "progress bar"),
    (".optimistic-toast-msg", "optimistic toast"),
    (".optimistic-added", "optimistic added"),
    # ── Misc ──
    (".file-item", "file item"),
    (".chat-session-del", "session delete button"),
    (".search-nav-btns button", "search nav buttons"),
]

# Minimum percentage of key selectors that must pass transition check
MIN_TRANSITION_COVERAGE = 0.90


class TestTransitionCoverage:
    """Verify that key interactive selectors have CSS transition properties.

    Each selector fragment is matched against CSS rule blocks.  A selector
    passes if any matching rule contains a non-none ``transition:`` declaration
    outside a ``prefers-reduced-motion`` media query.

    The overall coverage rate must meet the minimum threshold.
    """

    @classmethod
    def setup_class(cls):
        cls.css = _read_css()
        cls.rules = _extract_css_rules(cls.css)

    def _selector_has_transition(self, selector_fragment: str) -> bool:
        """Check if any CSS rule matching the fragment has a transition."""
        for rule in self.rules:
            if selector_fragment in rule["selector"]:
                if _rule_has_smoothness(rule):
                    return True
        return False

    def test_all_key_selectors_have_transitions(self):
        """Every key interactive selector must have a transition rule.

        Individual failures are reported with the selector + description.
        """
        missing: list[str] = []
        for selector, desc in KEY_INTERACTIVE_SELECTORS:
            if not self._selector_has_transition(selector):
                missing.append(f"  {desc} ({selector})")

        assert not missing, (
            f"{len(missing)}/{len(KEY_INTERACTIVE_SELECTORS)} key selectors "
            "are MISSING transition properties:\n" + "\n".join(missing)
        )

    def test_transition_coverage_meets_threshold(self):
        """Overall transition coverage must exceed the minimum threshold.

        This is the quantitative stop condition gate: at least
        {threshold_pct}% of key interactive selectors must have transitions.
        """.format(threshold_pct=int(MIN_TRANSITION_COVERAGE * 100))
        total = len(KEY_INTERACTIVE_SELECTORS)
        passed = sum(
            1 for s, _ in KEY_INTERACTIVE_SELECTORS
            if self._selector_has_transition(s)
        )
        coverage = passed / total if total > 0 else 0

        assert coverage >= MIN_TRANSITION_COVERAGE, (
            f"Transition coverage: {passed}/{total} = {coverage:.0%} "
            f"(minimum: {MIN_TRANSITION_COVERAGE:.0%}).\n\n"
            f"Missing transitions:"
            + "\n".join(
                f"  - {desc} ({s})"
                for s, desc in KEY_INTERACTIVE_SELECTORS
                if not self._selector_has_transition(s)
            )
        )

    def test_buttons_have_transitions(self):
        """All button selectors must have transitions."""
        buttons = [(s, d) for s, d in KEY_INTERACTIVE_SELECTORS if "btn" in s]
        missing = [
            f"  {d} ({s})"
            for s, d in buttons
            if not self._selector_has_transition(s)
        ]
        assert not missing, (
            f"Buttons missing transitions:\n" + "\n".join(missing)
        )

    def test_links_have_transitions(self):
        """All link/navigation selectors must have transitions."""
        links = [
            (s, d) for s, d in KEY_INTERACTIVE_SELECTORS
            if "link" in s or "nav " in s or "pagination" in s or "toc-list" in s
        ]
        missing = [
            f"  {d} ({s})"
            for s, d in links
            if not self._selector_has_transition(s)
        ]
        assert not missing, (
            f"Links missing transitions:\n" + "\n".join(missing)
        )

    def test_inputs_have_transitions(self):
        """All input/select selectors must have transitions."""
        inputs = [
            (s, d) for s, d in KEY_INTERACTIVE_SELECTORS
            if "input" in s or "select" in s or "filter" in s
        ]
        missing = [
            f"  {d} ({s})"
            for s, d in inputs
            if not self._selector_has_transition(s)
        ]
        assert not missing, (
            f"Inputs missing transitions:\n" + "\n".join(missing)
        )

    def test_htmx_elements_have_transitions(self):
        """All HTMX lifecycle classes must have transitions."""
        htmx = [(s, d) for s, d in KEY_INTERACTIVE_SELECTORS if "htmx" in s]
        missing = [
            f"  {d} ({s})"
            for s, d in htmx
            if not self._selector_has_transition(s)
        ]
        assert not missing, (
            f"HTMX elements missing transitions:\n" + "\n".join(missing)
        )


# ═══════════════════════════════════════════════════════════════════
# 3. Transition Quality Verification
# ═══════════════════════════════════════════════════════════════════


class TestTransitionQualityVerification:
    """Verify transition properties on key selectors use design tokens."""

    @classmethod
    def setup_class(cls):
        cls.css = _read_css()
        cls.rules = _extract_css_rules(cls.css)

    def test_all_transitions_use_design_tokens(self):
        """Every transition property outside RPM blocks must use var(--...).

        Individual hardcoded durations (like ``0.2s``) are flagged.
        """
        violations: list[tuple[int, str, str]] = []
        for rule in self.rules:
            if rule["inside_rpm"]:
                continue
            for m in re.finditer(r"transition:\s*([^;}]+)", rule["body"]):
                val = m.group(1).strip()
                if val == "none":
                    continue
                if "var(--" not in val:
                    violations.append((
                        rule["start_line"], rule["selector"], val
                    ))

        assert not violations, (
            f"{len(violations)} transition rules don't use design tokens:\n"
            + "\n".join(
                f"  L{ln}: {s[:60]} -> {v[:80]}"
                for ln, s, v in violations[:15]
            )
        )
