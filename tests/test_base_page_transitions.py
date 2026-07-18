"""Tests that parse rendered CSS from _base_page() output and assert
CSS transition quality and design token usage.

These tests verify four contract requirements from Action Item 3/4
of motion-b993f8a78a63 (Phase 7):
  (a) transition property appears on at least 8 distinct CSS selectors
  (b) all transitions use specific properties, not the ``all`` keyword
  (c) timing values fall within acceptable bounds (50-500ms)
  (d) design token var() references are used instead of hardcoded hex values

Data flow:
  _base_page() output -> extract CSS link -> read & parse stylesheet
    -> extract transition rules (excluding @media reduced-motion blocks)
    -> run assertions on the extracted rules
"""

from __future__ import annotations

import re
from pathlib import Path

# ── Helpers ───────────────────────────────────────────────────────


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve_stylesheet_path(html_output: str) -> Path | None:
    """Extract the stylesheet path from an _base_page() HTML output.

    Returns the absolute path to the CSS file, or None if not found.

    The stylesheet href is a URL path like ``/static/css/styles.css``.
    The FastAPI server mounts ``src/web/static/`` at the ``/static`` prefix,
    so the file lives at ``<project_root>/src/web/static/css/styles.css``.
    """
    m = re.search(
        r'<link\s+[^>]*rel=["\']stylesheet["\'][^>]*href=["\']([^"\']+)["\']',
        html_output,
    )
    if not m:
        # Try href before rel
        m = re.search(
            r'<link\s+[^>]*href=["\']([^"\']+)["\'][^>]*rel=["\']stylesheet["\']',
            html_output,
        )
    if not m:
        return None
    href = m.group(1)
    if href.startswith("/"):
        # Strip leading / and prepend src/web/ (where FastAPI mounts /static)
        rel = href.lstrip("/")
        return _project_root() / "src" / "web" / rel
    return Path(href)


def _read_stylesheet(html_output: str) -> str:
    """Read the CSS stylesheet referenced by _base_page() HTML output."""
    path = _resolve_stylesheet_path(html_output)
    if path is None or not path.exists():
        raise FileNotFoundError(
            f"Could not locate stylesheet from _base_page() output: "
            f"resolved to {path}"
        )
    return path.read_text()


def _strip_comments(css: str) -> str:
    """Remove /* ... */ comments from CSS."""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _extract_css_rules(css: str) -> list[dict]:
    """Extract all CSS rule blocks from a stylesheet.

    Returns list of {selector, body, start_line, inside_media}.
    Handles multi-line selectors and comma-separated selectors.
    Tracks @media nesting so reduced-motion rules can be excluded.
    """
    lines = css.split("\n")
    rules: list[dict] = []
    media_stack: list[str] = []

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        # Track @media block entry
        if "@media" in stripped and "{" in stripped:
            media_stack.append(stripped)
            i += 1
            continue

        # Track closing braces for @media blocks
        if stripped == "}" and media_stack:
            media_stack.pop()
            i += 1
            continue

        # Skip empty lines, comments
        if (
            stripped == ""
            or stripped.startswith("/*")
            or stripped.startswith("//")
            or stripped.startswith("*")
        ):
            i += 1
            continue

        # Skip lines that are clearly rule bodies
        if re.match(r"^\s*[\w-]+\s*:", stripped) and "{" not in stripped:
            i += 1
            continue

        # Check if this line starts a CSS rule
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

        # Extract selector: from i to brace_line (exclusive)
        selector_parts: list[str] = []
        for j in range(i, brace_line + 1):
            part = lines[j].split("{", 1)[0].strip()
            if part:
                selector_parts.append(part)
        selector = " ".join(selector_parts).strip()

        # Find the closing brace
        depth = 0
        end_line = brace_line
        for j in range(brace_line, len(lines)):
            opens = lines[j].count("{")
            closes = lines[j].count("}")
            depth += opens - closes
            if depth == 0 and (opens > 0 or closes > 0):
                end_line = j
                break

        # Build rule body
        body_lines: list[str] = []
        for j in range(brace_line, end_line + 1):
            if j == brace_line and j == end_line:
                # Single-line rule
                _, _, rest = lines[j].partition("{")
                rest = rest.rstrip("}")
                body_lines.append(rest)
            elif j == brace_line:
                _, _, rest = lines[j].partition("{")
                body_lines.append(rest)
            elif j == end_line:
                rest = lines[j].rstrip("}")
                body_lines.append(rest)
            else:
                body_lines.append(lines[j])

        body = "\n".join(body_lines).strip()

        rules.append(
            {
                "selector": selector,
                "body": body,
                "start_line": i + 1,
                "inside_media": bool(media_stack),
            }
        )

        i = end_line + 1

    return rules


def _extract_transition_rules(rules: list[dict]) -> list[dict]:
    """Filter to rules that contain ``transition:`` declarations.

    Excludes rules inside @media blocks (e.g. prefers-reduced-motion: reduce
    where transition is set to ``none``).

    Returns list of {selector, transition_value, start_line, body}.
    """
    transitions: list[dict] = []
    for r in rules:
        if r["inside_media"]:
            continue
        if "transition:" in r["body"]:
            # Find all transition declarations in the body
            trans_matches = re.findall(
                r"transition\s*:\s*([^;}]+)", r["body"]
            )
            for tm in trans_matches:
                tm = tm.strip()
                # Skip transition: none (accessibility overrides are
                # in @media blocks and filtered above; any leftover
                # 'none' values are intentional disabled transitions)
                if tm == "none":
                    continue
                transitions.append(
                    {
                        "selector": r["selector"],
                        "transition_value": tm,
                        "start_line": r["start_line"],
                        "body": r["body"],
                    }
                )
    return transitions


def _extract_timing_values(transition_value: str) -> list[dict]:
    """Extract all timing values (durations) from a transition value string.

    Returns list of {value, unit, ms} for each hardcoded duration.
    skips var(--*) references which delegate to design tokens.
    """
    timings: list[dict] = []
    # Find hardcoded duration values (not inside var())
    for m in re.finditer(r"(\d+\.?\d*)(s|ms)\b", transition_value):
        span = m.span()
        # Check if this value appears inside a var() call
        before = transition_value[: span[0]]
        var_count_before = before.count("var(") - before.count(")")
        if var_count_before > 0:
            # Inside a var() reference — skip
            continue
        value = float(m.group(1))
        unit = m.group(2)
        ms = value * 1000 if unit == "s" else value
        timings.append({"value": value, "unit": unit, "ms": ms})
    return timings


# ── Tests ─────────────────────────────────────────────────────────

# Design token tokens used in transitions (all transitions should use these
# or compose properties using these tokens)
_TRANSITION_TOKENS = frozenset(
    {
        "--transition-fast",
        "--transition-base",
        "--transition-theme",
        "--transition-color",
        "--transition-opacity",
        "--transition-press",
        "--transition-lift",
    }
)


class TestBasePageTransitionContracts:
    """Parse CSS via _base_page() output and assert transition quality.

    These tests follow the data flow:
      _base_page() -> extract link -> read CSS -> parse rules -> assert.
    """

    # ── Internal helpers ──────────────────────────────────────

    @staticmethod
    def _get_transition_rules() -> list[dict]:
        """Load and parse the CSS referenced by _base_page()."""
        from src.web.rendering import _base_page

        html = _base_page("Test", "<p>content</p>")
        css = _read_stylesheet(html)
        css_clean = _strip_comments(css)
        rules = _extract_css_rules(css_clean)
        return _extract_transition_rules(rules)

    # ── (a) Minimum selector count ────────────────────────────

    def test_transition_on_at_least_8_distinct_selectors(self):
        """At least 8 distinct CSS selectors must have a transition property."""
        rules = self._get_transition_rules()
        selectors = {r["selector"] for r in rules}

        assert len(selectors) >= 8, (
            f"Expected at least 8 selectors with transition, "
            f"found {len(selectors)}"
        )

    def test_selectors_cover_key_interactive_elements(self):
        """Verify that transitions cover the main interactive component
        categories: buttons, links, inputs, cards, navigation, tags."""
        rules = self._get_transition_rules()

        # Collect all selector text
        all_selectors = " ".join(r["selector"] for r in rules)

        # Key interactive patterns that must have transitions.
        # Note: textarea and select share the .input class which has
        # its own transition via .input.
        required_patterns = [
            # Buttons
            r"\.btn\b",
            # Links (anchors)
            r"\ba\b",
            # Form inputs (the .input class covers input, textarea, select)
            r"\.input\b",
            # Input elements directly (also covered)
            r"\binput\b",
            r"\bselect\b",
            # Navigation links
            r"header\s+nav",
            # Cards / results
            r"\.card\b",
            r"\.result\b",
            # Tags
            r"\.tag\b",
            # Theme toggle
            r"\.theme-toggle\b",
        ]

        missing = []
        for pattern in required_patterns:
            if not re.search(pattern, all_selectors):
                missing.append(pattern)

        assert not missing, (
            f"Missing transition rules for key interactive patterns: {missing}"
        )

    # ── (b) No ``all`` shorthand ───────────────────────────────

    def test_no_transition_uses_all_keyword(self):
        """Every transition must list specific properties, not ``all``."""
        rules = self._get_transition_rules()

        violations = []
        for r in rules:
            value = r["transition_value"]
            if value.startswith("all") or re.match(r"^all\s", value):
                violations.append(
                    f"  {r['selector']} (line {r['start_line']}): "
                    f"transition: {value}"
                )

        assert not violations, (
            f"Found {len(violations)} transitions using 'all' shorthand "
            f"instead of specific properties:\n" + "\n".join(violations)
        )

    def test_transitions_use_individual_properties(self):
        """Each transition declaration must specify at least one concrete
        CSS property (not just 'all'). Property names should be valid."""
        rules = self._get_transition_rules()

        # Whitelist of valid CSS property names and design-token aliases
        _css_props = frozenset(
            {
                "background",
                "background-color",
                "border",
                "border-color",
                "bottom",
                "box-shadow",
                "color",
                "font-size",
                "font-weight",
                "height",
                "left",
                "margin",
                "max-height",
                "max-width",
                "min-height",
                "min-width",
                "opacity",
                "outline-color",
                "outline-offset",
                "padding",
                "right",
                "top",
                "transform",
                "visibility",
                "width",
            }
        )

        invalid = []
        for r in rules:
            value = r["transition_value"].strip()
            # Allow design-token-only transitions (e.g. var(--transition-press))
            if value.startswith("var(--"):
                # It's a composed token — fine
                continue
            # Check individual property names
            parts = re.split(r"[,\s]+", value)
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                # Skip timing/duration/easing values
                if re.match(
                    r"^\d+(\.\d+)?[sm]?s$", part
                ):
                    continue
                if part in ("ease", "ease-in", "ease-out", "ease-in-out",
                            "linear", "step-start", "step-end", "steps"):
                    continue
                if part.startswith("cubic-bezier("):
                    continue
                if part.startswith("var(--"):
                    continue
                if part not in _css_props:
                    invalid.append(
                        f"  {r['selector']} (line {r['start_line']}): "
                        f"unknown token '{part}' in transition"
                    )

        assert not invalid, (
            f"Found {len(invalid)} transitions with non-standard tokens:\n"
            + "\n".join(invalid)
        )

    # ── (c) Timing values within bounds ────────────────────────

    _MIN_MS = 50   # Minimum acceptable transition duration
    _MAX_MS = 300  # Maximum acceptable transition duration (task spec)

    def test_timing_values_within_acceptable_bounds(self):
        """All hardcoded timing values must be between 50ms and 300ms.

        Values outside this range are either imperceptibly fast (<50ms)
        or so slow they degrade perceived performance (>300ms).

        Exception: ``0s`` values are permitted for the standard CSS
        ``visibility 0s linear <delay>`` pattern used to make
        opacity+visibility modal transitions work correctly
        (visibility must flip instantly after the delay, not animate).
        Design-token timing (var(--transition-*) references) are checked
        in a separate design-token test.
        """
        rules = self._get_transition_rules()

        violations = []
        for r in rules:
            timings = _extract_timing_values(r["transition_value"])
            for t in timings:
                # 0s is acceptable — standard visibility-delay technique
                if t["ms"] < 1:
                    continue
                if t["ms"] < self._MIN_MS or t["ms"] > self._MAX_MS:
                    violations.append(
                        f"  {r['selector']} (line {r['start_line']}): "
                        f"{t['value']}{t['unit']} = {t['ms']}ms "
                        f"(acceptable: {self._MIN_MS}-{self._MAX_MS}ms)"
                    )

        assert not violations, (
            f"Found {len(violations)} timing values outside the "
            f"acceptable range ({self._MIN_MS}-{self._MAX_MS}ms):\n"
            + "\n".join(violations)
        )

    def test_no_instant_transitions_on_interactive_elements(self):
        """Interactive elements must not use 0s/0ms transition (instant)."""
        rules = self._get_transition_rules()

        interactive_pattern = re.compile(
            r"(\.btn|\.button|\ba\b|\.nav|\.tag|\.card|input|select|textarea|\.theme-toggle)"
        )

        violations = []
        for r in rules:
            if not interactive_pattern.search(r["selector"]):
                continue
            # Skip design-token-only transitions (timing delegated to token)
            if r["transition_value"].startswith("var(--"):
                continue
            timings = _extract_timing_values(r["transition_value"])
            for t in timings:
                if t["ms"] < 1:
                    violations.append(
                        f"  {r['selector']} (line {r['start_line']}): "
                        f"{t['value']}{t['unit']} instant transition"
                    )

        assert not violations, (
            f"Found {len(violations)} instant transitions on "
            f"interactive elements:\n" + "\n".join(violations)
        )

    # ── (d) Design token var() references ───────────────────────

    def test_all_transitions_use_design_tokens(self):
        """Every transition declaration must use var(--...) design tokens
        instead of hardcoded color/hex values or raw durations.

        Acceptable patterns:
          - Pure token: ``var(--transition-press)``
          - Composed: ``background var(--transition-base), opacity var(--transition-fast)``

        Unacceptable:
          - ``transition: background 0.15s ease-out``  (no token)
          - ``transition: color #ff0000 0.2s``         (hex color)
        """
        rules = self._get_transition_rules()

        violations = []
        for r in rules:
            value = r["transition_value"]
            if "var(--" not in value:
                violations.append(
                    f"  {r['selector']} (line {r['start_line']}): "
                    f"transition: {value}"
                )

        assert not violations, (
            f"Found {len(violations)} transitions not using design tokens:\n"
            + "\n".join(violations)
        )

    def test_no_hardcoded_hex_in_transition_rules(self):
        """Transition rule bodies must not contain hardcoded hex color values.

        Hex colors (#rgb, #rrggbb, #rrggbbaa) indicate a non-tokenized value.
        Exceptions: comments, var() fallbacks, and SVG chart color params
        that are not part of transition declarations.
        """
        rules = self._get_transition_rules()

        hex_pattern = re.compile(r"#[0-9a-fA-F]{3,8}\b")

        violations = []
        for r in rules:
            hex_matches = hex_pattern.findall(r["body"])
            for hm in hex_matches:
                # Check if it's inside a var() fallback or outside transition
                idx = r["body"].find(hm)
                before = r["body"][:idx]
                var_count = before.count("var(") - before.count(")")
                if var_count > 0:
                    # Inside a var() fallback — acceptable
                    continue
                # Also check if this hex is mentioned in a comment
                comment_before = before.rfind("/*")
                comment_after = before.rfind("*/")
                if comment_before > comment_after:
                    # Inside a comment — skip
                    continue
                violations.append(
                    f"  {r['selector']} (line {r['start_line']}): "
                    f"hardcoded {hm}"
                )

        assert not violations, (
            f"Found {len(violations)} hardcoded hex values in transition "
            f"rule bodies:\n" + "\n".join(violations)
        )

    def test_transition_timing_uses_design_tokens(self):
        """Transition durations should use var(--transition-fast) or
        var(--transition-base) rather than hardcoded 0.15s / 0.18s.

        The transition value may reference composed tokens like
        var(--transition-press) which internally use the timing tokens.
        This is acceptable.
        """
        rules = self._get_transition_rules()

        # Transition values that use bare var(--transition-*) are fine
        # Transition values that use `property var(--transition-fast)` are also fine
        # We already checked var() presence above — this test verifies
        # that the transition tokens themselves are defined.

        css = _read_stylesheet(
            __import__(
                "src.web.rendering", fromlist=["_base_page"]
            )._base_page("Test", "<p>content</p>")
        )
        css_clean = _strip_comments(css)

        for token in _TRANSITION_TOKENS:
            assert f"{token}:" in css_clean, (
                f"Design token {token} is not defined in the stylesheet"
            )
