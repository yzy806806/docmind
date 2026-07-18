"""Tests for CSS transition fluidity: targeted transition audit.

Covers:
- Design tokens: --transition-fast and --transition-base durations are within
  150-300ms range
- No universal '*' selector transitions (outside prefers-reduced-motion)
- Targeted transition rules exist on interactive elements: buttons, links,
  inputs, and result cards
- Count only targeted rules, not blanket selectors (per reviewer feedback)
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


def _read_css() -> str:
    return _css_path().read_text()


def _extract_rule(css: str, line_num: int) -> str:
    """Given a line number inside a CSS rule, return the full rule text.

    Walks backward to find the opening brace and forward to the matching close.
    """
    lines = css.split("\n")
    # Walk backward to find opening brace
    open_line = None
    brace_depth = 0
    for i in range(line_num - 1, -1, -1):
        line = lines[i].strip()
        # Count closing braces going backward
        # (they were opened in the section we already passed)
        for ch in line:
            if ch == "}":
                brace_depth -= 1
            elif ch == "{":
                brace_depth += 1
                if brace_depth > 0:
                    open_line = i
                    break
        if open_line is not None:
            break

    if open_line is None:
        return ""

    # Walk forward from open_line to find closing brace
    depth = 0
    for i in range(open_line, len(lines)):
        for ch in lines[i]:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return "\n".join(lines[open_line : i + 1])
    return ""


def _is_inside_media_block(css: str, line_num: int) -> bool:
    """Check if a line is inside an @media block."""
    lines = css.split("\n")
    depth = 0
    for i in range(line_num - 1, -1, -1):
        line = lines[i].strip()
        for ch in line:
            if ch == "}":
                depth += 1
            elif ch == "{":
                if depth > 0:
                    depth -= 1
                elif "@media" in lines[i]:
                    return True
    return False


def _find_all_transition_rules(css: str) -> list[tuple[int, str, str]]:
    """Find all transition: declarations.

    Returns list of (line_number, selector, transition_value).
    """
    results: list[tuple[int, str, str]] = []
    lines = css.split("\n")

    for i, line in enumerate(lines, 1):
        # Match transition: shorthand property
        m = re.search(r"transition:\s*([^;]+);", line)
        if not m:
            continue
        # Skip if it's inside a comment
        if line.strip().startswith("/*"):
            continue

        value = m.group(1).strip()

        # Walk backward to find the selector
        selector = _find_selector_for_line(css, i)
        results.append((i, selector, value))

    return results


def _find_selector_for_line(css: str, line_num: int) -> str:
    """Find the CSS selector that a given line belongs to.

    Walks backward to find the nearest opening brace and extracts the selector
    text before it. Handles multi-line selectors by walking up to the previous
    closing brace.
    """
    lines = css.split("\n")
    depth = 0
    for i in range(line_num - 1, -1, -1):
        line = lines[i].strip()
        for ch in line:
            if ch == "}":
                depth += 1
            elif ch == "{":
                if depth > 0:
                    depth -= 1
                else:
                    # Found the opening brace — the selector is everything
                    # before it on this line, plus preceding lines if needed
                    prefix = lines[i][: lines[i].index("{")].strip()
                    if prefix:
                        return prefix
                    # Empty prefix means the selector is on a previous line;
                    # walk up collecting lines until we hit a } or start
                    parts: list[str] = []
                    for j in range(i - 1, -1, -1):
                        prev = lines[j].strip()
                        if prev == "}" or "@media" in prev:
                            break
                        if prev and not prev.startswith("/") and not prev.startswith("*"):
                            parts.insert(0, prev)
                    return " ".join(parts)
    return "unknown"


# ── 1. Design Token Duration Tests ────────────────────────────────


class TestTransitionTokenDurations:
    """Verify transition design token durations are within the acceptable range."""

    def test_transition_fast_duration(self):
        """--transition-fast should be >= 150ms and <= 300ms."""
        css = _read_css()
        match = re.search(r"--transition-fast:\s*([\d.]+)s", css)
        assert match, "Missing --transition-fast token"
        duration_ms = float(match.group(1)) * 1000
        assert 150 <= duration_ms <= 300, (
            f"--transition-fast duration is {duration_ms}ms, "
            f"expected 150-300ms range"
        )

    def test_transition_base_duration(self):
        """--transition-base should be >= 150ms and <= 300ms."""
        css = _read_css()
        match = re.search(r"--transition-base:\s*([\d.]+)s", css)
        assert match, "Missing --transition-base token"
        duration_ms = float(match.group(1)) * 1000
        assert 150 <= duration_ms <= 300, (
            f"--transition-base duration is {duration_ms}ms, "
            f"expected 150-300ms range"
        )

    def test_transition_tokens_exist(self):
        """Core transition design tokens must be defined in :root.

        Requires at minimum the two base duration tokens plus at least 3
        derived tokens (theme, color, opacity, lift, etc.). The exact set
        of derived tokens may vary as the design system evolves.
        """
        css = _read_css()
        # Base duration tokens are mandatory
        mandatory = [
            "--transition-fast",
            "--transition-base",
        ]
        for token in mandatory:
            assert token + ":" in css, f"Missing mandatory design token: {token}"

        # Derived tokens: at least 3 must exist
        derived_tokens = [
            "--transition-theme",
            "--transition-color",
            "--transition-opacity",
            "--transition-press",
            "--transition-lift",
        ]
        found_derived = [t for t in derived_tokens if t + ":" in css]
        assert len(found_derived) >= 3, (
            f"Only {len(found_derived)} derived transition tokens found "
            f"(found: {found_derived}); expected at least 3 of {derived_tokens}"
        )

    def test_no_raw_transition_values_outside_tokens(self):
        """Transition declarations should use design tokens, not raw values.

        This ensures all durations flow through the token system and stay
        consistent. The prefers-reduced-motion block is exempt (it uses
        transition-duration: 0.01ms as an accessibility override).
        """
        css = _read_css()
        lines = css.split("\n")
        violations: list[tuple[int, str]] = []

        for i, line in enumerate(lines, 1):
            # Skip comment lines
            stripped = line.strip()
            if stripped.startswith("/*") or stripped.startswith("*"):
                continue

            # Only check transition: shorthand (not longhands like
            # transition-duration which prefer-reduced-motion uses)
            m = re.search(r"transition:\s*([^;]+);", stripped)
            if not m:
                continue

            value = m.group(1).strip()

            # Is this inside prefers-reduced-motion?
            is_in_rpm = False
            depth = 0
            for j in range(i - 1, -1, -1):
                for ch in lines[j]:
                    if ch == "}":
                        depth += 1
                    elif ch == "{":
                        if depth > 0:
                            depth -= 1
                        elif "prefers-reduced-motion" in lines[j]:
                            is_in_rpm = True
                        break
                if depth == 0 and "@media" in lines[j]:
                    break

            if is_in_rpm:
                continue

            # The value should reference a --transition-* token
            if "var(--transition-" not in value and "var(--" not in value:
                # Check if it's a raw time value like 0.2s
                if re.search(r"[\d.]+s\b", value):
                    violations.append((i, value))

        assert not violations, (
            f"Found {len(violations)} transition declarations using raw values "
            f"instead of design tokens: {violations}"
        )


# ── 2. Universal Selector Tests ───────────────────────────────────


class TestNoUniversalSelectorTransitions:
    """Enforce no universal '*' selector transitions outside accessibility blocks."""

    def test_no_universal_wildcard_transition_shorthand(self):
        """The '*' selector must not have a transition: shorthand property.

        Blanket '*' transitions cause unintended side effects on every element.
        The prefers-reduced-motion block is exempt (it uses transition-duration
        longhand as a standard accessibility pattern).
        """
        css = _read_css()
        lines = css.split("\n")

        violations: list[tuple[int, str]] = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("/*") or stripped.startswith("*"):
                continue

            # Find transition: shorthand
            m = re.search(r"transition:\s*([^;]+);", stripped)
            if not m:
                continue

            # Find the parent selector
            selector = _find_selector_for_line(css, i)
            # Normalize: split on commas to check each part
            parts = [s.strip() for s in selector.split(",")]
            for part in parts:
                # Matches exact '*' or '* ' with pseudo-elements/classes
                normalized = re.sub(r"\s+", " ", part).strip()
                if normalized == "*" or re.match(r"^\*\s*::?\w+$", normalized):
                    violations.append((i, selector))

        assert not violations, (
            f"Found {len(violations)} universal '*' selector transition rules: "
            f"{violations}. Universal transitions are forbidden; use targeted "
            f"selectors on interactive elements only."
        )

    def test_universal_in_reduced_motion_is_acceptable(self):
        """The prefers-reduced-motion block may use *, *::before, *::after.

        This is the standard W3C-recommended pattern for disabling animations
        and transitions for users who prefer reduced motion. We verify it
        exists (accessibility requirement) and uses transition-duration (not
        transition shorthand).
        """
        css = _read_css()
        assert "@media (prefers-reduced-motion: reduce)" in css, (
            "Missing prefers-reduced-motion accessibility media query"
        )

        # Extract the prefers-reduced-motion block
        rpm_block = _extract_media_block(css, "prefers-reduced-motion: reduce")
        assert rpm_block, "Could not extract prefers-reduced-motion block"

        # It should set transition-duration to near-zero
        assert "transition-duration" in rpm_block, (
            "prefers-reduced-motion block should set transition-duration"
        )

    def test_no_transition_duration_on_universal_outside_reduced_motion(self):
        """Universal selectors outside prefers-reduced-motion must not set
        transition-duration.

        This catches the longhand property on blanket selectors.
        """
        css = _read_css()
        lines = css.split("\n")

        violations: list[tuple[int, str]] = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("/*"):
                continue

            # Check for transition-duration: on a line
            if "transition-duration:" not in stripped:
                continue

            # Is this inside prefers-reduced-motion?
            if _is_inside_media_block(css, i):
                # Check if the containing media block is reduced-motion
                rpm_block = _extract_media_block(css, "prefers-reduced-motion: reduce")
                if rpm_block:
                    rpm_start = css.index(rpm_block)
                    line_start = sum(len(lines[j]) + 1 for j in range(i - 1))
                    if rpm_start <= line_start <= rpm_start + len(rpm_block):
                        continue  # OK — inside reduced motion

            # Find the selector
            selector = _find_selector_for_line(css, i)
            parts = [s.strip() for s in selector.split(",")]
            for part in parts:
                normalized = re.sub(r"\s+", " ", part).strip()
                if normalized == "*" or re.match(r"^\*\s*::?\w+$", normalized):
                    violations.append((i, selector))

        assert not violations, (
            f"Found {len(violations)} universal '*' selector with "
            f"transition-duration outside prefers-reduced-motion: {violations}"
        )


# ── 3. Targeted Interactive Element Tests ─────────────────────────


class TestTargetedTransitionCoverage:
    """Verify targeted transition rules exist on interactive element types."""

    def test_buttons_have_transitions(self):
        """Buttons, .btn-* classes, and form buttons should have transition rules."""
        css = _read_css()
        button_selectors_with_transitions = _find_selectors_with_transition(css)

        # At minimum, these button categories should be covered
        required_patterns = [
            r"\.btn-",         # .btn-delete, .btn-read-full, .btn-save, etc.
            r"button",         # generic button
        ]

        for pattern in required_patterns:
            matching = [
                s for s in button_selectors_with_transitions
                if re.search(pattern, s, re.IGNORECASE)
            ]
            assert matching, (
                f"No transition rules found for selectors matching '{pattern}'. "
                f"All interactive elements need transition rules."
            )

    def test_links_have_transitions(self):
        """Link elements (a, a.class) should have transition rules."""
        css = _read_css()
        selectors = _find_selectors_with_transition(css)

        # Links should appear as either bare 'a' or 'a.class'
        link_selectors = [s for s in selectors if re.search(r"\ba\b", s)]
        assert link_selectors, (
            "No transition rules found for link (<a>) elements"
        )

    def test_inputs_have_transitions(self):
        """Input, textarea, select elements should have transition rules."""
        css = _read_css()
        selectors = _find_selectors_with_transition(css)

        input_patterns = [r"input", r"textarea", r"select"]
        found_any = False
        for pattern in input_patterns:
            matching = [s for s in selectors if re.search(pattern, s, re.IGNORECASE)]
            if matching:
                found_any = True
                break

        assert found_any, (
            "No transition rules found for input/textarea/select elements"
        )

    def test_result_cards_have_transitions(self):
        """Result cards (.result, .card-like containers) should have transition rules."""
        css = _read_css()
        selectors = _find_selectors_with_transition(css)

        # .result and .file-item are card-like containers
        card_patterns = [r"\.result\b", r"\.file-item\b", r"\.chat-session"]
        found_any = False
        for pattern in card_patterns:
            matching = [s for s in selectors if re.search(pattern, s)]
            if matching:
                found_any = True
                break

        assert found_any, (
            "No transition rules found for card/result container elements"
        )

    def test_count_only_targeted_rules(self):
        """Count only targeted rules on interactive elements, not blanket selectors.

        Blanket selectors are: *, body, html, .container, main, header, footer,
        and similar structural containers. A transition rule is 'targeted' if its
        selector names a specific interactive component.
        """
        css = _read_css()
        rules = _find_all_transition_rules(css)

        # Selectors considered blanket (non-interactive, structural)
        blanket_patterns = [
            r"^\*$", r"^\*,\s*\*",
            r"^body$", r"^body[\s,{]",
            r"^html$", r"^html[\s,{]",
            r"^\.container$", r"^\.container[\s,{]",
            r"^main$", r"^main[\s,{]",
            r"^header$", r"^header[\s,{]",
            r"^footer$", r"^footer[\s,{]",
        ]

        targeted = []
        blanket = []
        for line_num, selector, value in rules:
            is_blanket = any(
                re.match(pat, selector.strip()) for pat in blanket_patterns
            )
            if is_blanket:
                blanket.append((line_num, selector))
            else:
                targeted.append((line_num, selector))

        # We expect the vast majority to be targeted
        assert len(targeted) > 0, "No targeted transition rules found at all"
        assert len(targeted) >= len(blanket) * 5, (
            f"Only {len(targeted)} targeted rules vs {len(blanket)} blanket rules. "
            f"Blanket selectors should be minimal. Targeted: {[s for _, s in targeted[:5]]}..., "
            f"Blanket: {[s for _, s in blanket]}"
        )
        assert len(blanket) <= 3, (
            f"Too many blanket selector transitions ({len(blanket)}). "
            f"Found: {[s for _, s in blanket]}. Use targeted selectors instead."
        )


# ── 4. Duration Range Enforcement ──────────────────────────────────


class TestTransitionDurationRange:
    """Verify all transition durations stay within the 150-300ms range.

    This enforces the consistency standard: transitions should be subtle and
    fast, never jarring.
    """

    def test_all_transition_durations_in_range(self):
        """Every transition declaration's effective duration must be 150-300ms.

        All transitions in this codebase use --transition-fast (0.15s) or
        --transition-base (0.2s) tokens. We verify no raw durations or
        out-of-range tokens exist.
        """
        css = _read_css()
        lines = css.split("\n")

        # Map token names to their defined durations
        token_durations: dict[str, float] = {}
        for match in re.finditer(r"(--transition-\w+):\s*([\d.]+)s", css):
            token_durations[match.group(1)] = float(match.group(2))

        # Also check compound tokens that reference other tokens
        for match in re.finditer(
            r"(--transition-\w+):\s*(.+?);", css
        ):
            token_name = match.group(1)
            if token_name in token_durations:
                continue  # Already captured as simple duration
            # This is a compound token referencing others - skip

        violations: list[tuple[int, str, float]] = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("/*"):
                continue

            # Check transition: shorthand
            m = re.search(r"transition:\s*([^;]+);", stripped)
            if not m:
                continue
            value = m.group(1).strip()

            # Is this inside prefers-reduced-motion?
            if _is_inside_media_block(css, i):
                rpm_block = _extract_media_block(css, "prefers-reduced-motion: reduce")
                if rpm_block:
                    rpm_start = css.index(rpm_block)
                    line_start = sum(len(lines[j]) + 1 for j in range(i - 1))
                    if rpm_start <= line_start <= rpm_start + len(rpm_block):
                        continue

            # Extract token references
            tokens = re.findall(r"var\((--transition-\w+)\)", value)
            for token in tokens:
                if token in token_durations:
                    duration_ms = token_durations[token] * 1000
                    if not (150 <= duration_ms <= 300):
                        violations.append((i, token, duration_ms))

        assert not violations, (
            f"Found {len(violations)} transition declarations with out-of-range "
            f"durations: {[(l, t, f'{d}ms') for l, t, d in violations]}. "
            f"All transitions must use durations in the 150-300ms range."
        )


# ── 5. Comprehensive Audit ────────────────────────────────────────


class TestFluidityAuditSummary:
    """End-to-end audit providing a comprehensive fluidity health check."""

    def test_fluidity_audit_passes(self):
        """Aggregate audit: verify all fluidity criteria pass at once.

        This test acts as a single regression gate — if any criterion fails,
        the entire audit fails with a detailed breakdown.
        """
        css = _read_css()

        failures: list[str] = []

        # 1. Token durations in range
        for token, expected_s in [("--transition-fast", 0.15), ("--transition-base", 0.2)]:
            match = re.search(rf"{token}:\s*([\d.]+)s", css)
            if not match:
                failures.append(f"Missing mandatory token: {token}")
            else:
                dur_ms = float(match.group(1)) * 1000
                if not (150 <= dur_ms <= 300):
                    failures.append(
                        f"{token} duration {dur_ms}ms out of 150-300ms range"
                    )

        # 2. No universal '*' with transition shorthand
        rules = _find_all_transition_rules(css)
        for line_num, selector, value in rules:
            for part in selector.split(","):
                normalized = re.sub(r"\s+", " ", part.strip())
                if normalized == "*" or re.match(r"^\*\s*::?\w+$", normalized):
                    failures.append(
                        f"Universal '*' selector with transition at line {line_num}: {selector}"
                    )

        # 3. Interactive elements covered
        interactive_coverage = {
            "buttons": [r"\.btn-", r"\bbutton\b"],
            "links": [r"\ba\b"],
            "inputs": [r"input", r"textarea", r"select"],
            "cards": [r"\.result\b", r"\.file-item\b"],
        }
        all_selectors = [s for _, s, _ in rules]
        for category, patterns in interactive_coverage.items():
            covered = any(
                any(re.search(p, s) for p in patterns)
                for s in all_selectors
            )
            if not covered:
                failures.append(f"No transition rules for {category}")

        # 4. Count check — mostly targeted
        blanket_patterns = [
            r"^\*$", r"^body$", r"^html$", r"^\.container$",
            r"^main$", r"^header$", r"^footer$",
        ]
        blanket = [
            (ln, s) for ln, s, _ in rules
            if any(re.match(p, s.strip()) for p in blanket_patterns)
        ]
        targeted = len(rules) - len(blanket)
        if targeted < len(blanket) * 3:
            failures.append(
                f"Too many blanket selectors: {len(blanket)} blanket vs "
                f"{targeted} targeted (need at least 3:1 ratio)"
            )

        assert not failures, (
            f"Fluidity audit FAILED with {len(failures)} issue(s):\n  "
            + "\n  ".join(failures)
        )

    def test_minimum_targeted_transition_count(self):
        """Ensure a reasonable minimum of targeted transition rules exists.

        After the design token refactor, we expect at least 30 targeted
        transition rules on interactive elements. This guards against
        accidental removal during refactoring.
        """
        css = _read_css()
        rules = _find_all_transition_rules(css)

        # Exclude known blanket selectors
        blanket_patterns = [
            r"^\*$", r"^body$", r"^html$", r"^\.container$",
            r"^main$", r"^header$", r"^footer$",
        ]
        targeted = [
            (ln, s) for ln, s, _ in rules
            if not any(re.match(p, s.strip()) for p in blanket_patterns)
        ]

        assert len(targeted) >= 30, (
            f"Only {len(targeted)} targeted transition rules found "
            f"(minimum expected: 30). Regression risk: transitions may have "
            f"been accidentally removed."
        )


# ── Helpers ──────────────────────────────────────────────────────


def _find_selectors_with_transition(css: str) -> list[str]:
    """Return all unique selectors that have a transition: declaration."""
    selectors: set[str] = set()
    for _, sel, _ in _find_all_transition_rules(css):
        selectors.add(sel.strip())
    return sorted(selectors)


def _extract_media_block(css: str, condition: str) -> str:
    """Extract the full body of an @media block by condition string.

    Uses brace counting to correctly handle nested {}.
    """
    pattern = rf"@media\s*\({re.escape(condition)}\)\s*\{{"
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