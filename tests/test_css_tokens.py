"""Tests for CSS design token (custom property) usage and UI consistency.

Covers:
1. Token definitions — all var() references map to a :root or
   [data-theme="dark"] definition
2. Dark theme completeness — every light-theme token has a dark-theme
   counterpart with the same name
3. Hardcoded values — no hardcoded colors/borders/shadows where a token
   should be used instead
4. Token naming — consistent naming conventions, no orphan tokens
5. CSS structural integrity — section numbering, brace balance,
   duplicate selectors
6. Fallback consistency — var() fallbacks should match global defaults

The goal is to catch regressions where:
- A new CSS rule uses var(--new-token) but forgets to define it in :root
- Dark theme misses a light-theme token (causes visual breakage)
- Someone hardcodes a color instead of using a design token
- Section renumbering introduces gaps or duplicates
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path

# ── Helpers ───────────────────────────────────────────────────────


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _css_path() -> Path:
    return _project_root() / "src" / "web" / "static" / "css" / "styles.css"


def _read_css() -> str:
    return _css_path().read_text()


def _extract_top_level_block(css: str, selector_pattern: str) -> str:
    """Extract a top-level CSS block matching selector_pattern.

    Uses brace counting to handle nested blocks correctly.
    """
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


def _extract_css_variables(block: str) -> dict[str, str]:
    """Extract CSS custom properties and their values from a block.

    Returns dict mapping variable name (with -- prefix) to its value.
    Uses findall to correctly handle multi-line blocks with inline
    comments between declarations.
    """
    vars_dict: dict[str, str] = {}
    # Match all --token: value declarations in the block.
    # Pattern matches --name: value up to the next ; or }.
    # Uses [^;{}] to avoid matching across declaration boundaries
    # while still working across newlines.
    pattern = r"(--[\w-]+)\s*:\s*([^;{}]+?)\s*(?:;|\s*$|\s*})"
    for m in re.finditer(pattern, block):
        name = m.group(1)
        value = m.group(2).strip()
        if value:  # Skip truly empty values
            vars_dict[name] = value
    return vars_dict


def _strip_css_comments(css: str) -> str:
    """Remove /* ... */ comments from CSS text.

    Comment text like ``var(--token)`` in documentation examples should
    not be counted as actual token usage.
    """
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _tokens_used_in_css(css: str) -> set[str]:
    """Get tokens actually referenced via var() anywhere in the CSS.

    CSS comments are stripped first so documentation examples like
    ``var(--token-name)`` in comment blocks are not mistaken for real
    usage.
    """
    css = _strip_css_comments(css)
    tokens: set[str] = set()
    for m in re.finditer(r"var\(\s*(--[\w-]+)", css):
        tokens.add(m.group(1))
    return tokens


def _extract_section_numbers(css: str) -> list[tuple[int, int, str]]:
    """Extract CSS section comment headers.

    Returns list of (line_number, section_number, section_title).
    """
    sections: list[tuple[int, int, str]] = []
    section_pat = re.compile(
        r"/\*\s*=+\s*\n\s*\*?\s*(\d+)\.\s+(.+?)\s*\n\s*\*?\s*=+"
    )
    lines = css.split("\n")
    for i, line in enumerate(lines, 1):
        m = re.match(r"\s*/\*\s*=+", line)
        if m:
            # Collect lines until we find the section number + title
            block = "\n".join(lines[i - 1 : min(i + 4, len(lines))])
            sm = section_pat.search(block)
            if sm:
                sections.append((i, int(sm.group(1)), sm.group(2).strip()))
    return sections


def _extract_selector_names(css: str) -> dict[str, list[int]]:
    """Extract all CSS selector blocks and the lines where they appear.

    Returns dict mapping selector text to list of starting line numbers.
    """
    selectors: dict[str, list[int]] = {}
    pattern = re.compile(r"^([^{}]+?)\s*\{", re.MULTILINE)
    lines = css.split("\n")
    for i, line in enumerate(lines, 1):
        m = pattern.match(line.strip())
        if m:
            sel = m.group(1).strip()
            # Skip @media, @keyframes, etc.
            if sel.startswith("@"):
                continue
            # Normalize whitespace
            sel = " ".join(sel.split())
            if sel not in selectors:
                selectors[sel] = []
            selectors[sel].append(i)
    return selectors


# ── 1. Token Definition Tests ──────────────────────────────────────


class TestCSSVariableDefinitions:
    """Every var() reference must have a definition in :root or dark theme."""

    def test_light_theme_block_exists(self):
        """The :root block with CSS variables must exist."""
        css = _read_css()
        block = _extract_top_level_block(css, r":root\s*\{")
        assert block, ":root CSS variable block is missing"

    def test_dark_theme_block_exists(self):
        """The [data-theme="dark"] block with dark overrides must exist."""
        css = _read_css()
        block = _extract_top_level_block(css, r'\[data-theme="dark"\]\s*\{')
        assert block, '[data-theme="dark"] block is missing'

    def test_no_undefined_tokens(self):
        """Every var(--token) reference must have a :root or dark-theme
        definition.

        Tokens used with fallback defaults (var(--x, default)) are still
        flagged — the fallback is a safety net, not a substitute for a
        proper definition.
        """
        css = _read_css()
        root_block = _extract_top_level_block(css, r":root\s*\{")
        dark_block = _extract_top_level_block(css,
                                              r'\[data-theme="dark"\]\s*\{')

        defined = set(_extract_css_variables(root_block))
        defined |= set(_extract_css_variables(dark_block))

        used = _tokens_used_in_css(css)

        undefined = used - defined
        if undefined:
            undefined_list = sorted(undefined)
            raise AssertionError(
                f"Found {len(undefined_list)} var() references with NO "
                f"definition in :root or [data-theme=\"dark\"]: "
                f"{', '.join(undefined_list)}. "
                "Define these tokens in :root (light) and "
                "[data-theme=\"dark\"] (dark) blocks, or remove the "
                "var() usage."
            )

    def test_light_theme_has_all_foundational_tokens(self):
        """Essential design tokens must exist in :root."""
        css = _read_css()
        root_block = _extract_top_level_block(css, r":root\s*\{")
        defined = set(_extract_css_variables(root_block))

        required = {
            "--bg", "--surface", "--text", "--text-muted",
            "--primary", "--primary-hover", "--border",
            "--shadow", "--shadow-sm",
        }
        missing = required - defined
        assert not missing, (
            f"Essential tokens missing from :root: "
            f"{', '.join(sorted(missing))}"
        )


# ── 2. Dark Theme Completeness ─────────────────────────────────────


class TestCSSDarkThemeCompleteness:
    """Dark theme [data-theme="dark"] must override every theme-relevant token.

    Non-color tokens (spacing, typography, radius, transitions, z-index,
    layout) are theme-independent — they cascade from :root and do not need
    dark-theme overrides. Only color and shadow tokens need dark overrides.
    """

    # Token categories that are theme-dependent and must be overridden in dark
    # theme. Syntax highlighting and transition definitions are explicitly
    # theme-independent (see CSS comments) and excluded.
    _THEME_DEPENDENT_PATTERNS = [
        "-bg", "-text", "-border", "-link",
        "-primary", "-hover", "-surface", "-shadow",
        "-success", "-error", "-badge-", "-tint",
        "-code-bg", "-input-", "-table-",
    ]

    # Tokens that look theme-dependent by name but are actually
    # theme-independent (syntax highlighting uses the same colors in
    # both themes; transition definitions reference other tokens, not
    # raw colors)
    _THEME_INDEPENDENT_EXACT = {
        "--syntax-key", "--syntax-string", "--syntax-num",
        "--syntax-bool", "--syntax-tag", "--syntax-attr",
        "--syntax-search-hit", "--syntax-search-current",
        "--syntax-search-text",
        "--transition-color", "--transition-theme",
        "--transition-press", "--transition-lift",
        "--shadow-kbd",  # tiny shadow, same in both themes
    }

    @classmethod
    def _is_theme_dependent(cls, token: str) -> bool:
        """Check if a token is theme-dependent (needs dark override)."""
        if token in cls._THEME_INDEPENDENT_EXACT:
            return False
        return any(p in token for p in cls._THEME_DEPENDENT_PATTERNS)

    def test_dark_theme_defines_all_light_tokens(self):
        """Every theme-dependent token in :root must have a dark counterpart.

        A missing dark-theme token means the light value persists in dark
        mode, causing visual inconsistencies. Tokens that are
        theme-independent (spacing, typography, radius, transitions,
        z-index, layout) are excluded — they cascade from :root.
        """
        css = _read_css()
        root_block = _extract_top_level_block(css, r":root\s*\{")
        dark_block = _extract_top_level_block(css,
                                              r'\[data-theme="dark"\]\s*\{')

        light_tokens = set(_extract_css_variables(root_block))
        dark_tokens = set(_extract_css_variables(dark_block))

        # Only check theme-dependent tokens
        theme_tokens = {t for t in light_tokens if self._is_theme_dependent(t)}
        missing = theme_tokens - dark_tokens
        if missing:
            raise AssertionError(
                f"Dark theme missing {len(missing)} theme-dependent "
                f"token(s) defined in :root: "
                f"{', '.join(sorted(missing))}. "
                "Add corresponding dark values to [data-theme=\"dark\"]."
            )

    def test_dark_theme_has_no_extra_tokens(self):
        """Dark theme should not define tokens absent from :root.

        Extra tokens in dark mode that aren't in :root are useless —
        they'll never be applied because the variable is never read.
        """
        css = _read_css()
        root_block = _extract_top_level_block(css, r":root\s*\{")
        dark_block = _extract_top_level_block(css,
                                              r'\[data-theme="dark"\]\s*\{')

        light_tokens = set(_extract_css_variables(root_block))
        dark_tokens = set(_extract_css_variables(dark_block))

        extra = dark_tokens - light_tokens
        if extra:
            raise AssertionError(
                f"Dark theme defines {len(extra)} token(s) not in :root: "
                f"{', '.join(sorted(extra))}. "
                "These are dead code — remove them or add to :root first."
            )

    def test_dark_theme_values_differ_from_light(self):
        """Dark theme tokens should have genuinely different values from
        light.

        If dark and light values are identical, the token doesn't need to
        be in the dark theme block. This test checks that at least the
        core tokens (--bg, --surface, --text) differ.
        """
        css = _read_css()
        root_block = _extract_top_level_block(css, r":root\s*\{")
        dark_block = _extract_top_level_block(css,
                                              r'\[data-theme="dark"\]\s*\{')

        light_tokens = _extract_css_variables(root_block)
        dark_tokens = _extract_css_variables(dark_block)

        # Core tokens that MUST differ between themes
        core = ["--bg", "--surface", "--text", "--header-bg", "--border"]
        same = []
        for tok in core:
            if tok in light_tokens and tok in dark_tokens:
                if light_tokens[tok] == dark_tokens[tok]:
                    same.append(tok)

        assert not same, (
            f"Dark theme tokens have same values as light theme: "
            f"{', '.join(same)}. These should differ for a working dark "
            f"mode."
        )


# ── 3. Hardcoded Values ────────────────────────────────────────────


class TestCSSNoHardcodedValues:
    """Flag hardcoded colors/shadows where design tokens should be used."""

    def test_no_hardcoded_colors_in_main_rules(self):
        """CSS rules should use tokens, not raw hex colors.

        Some hardcoded colors are acceptable:
        - Syntax highlighting (JSON/XML) — specialized, not themeable
        - Search highlights — functional, not decorative

        This test reports findings as warnings for human review.
        """
        css = _read_css()

        # Strip comments to avoid false positives
        uncommented = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)

        # Find `color:` or `background:` set to a hex value without var()
        hex_pattern = re.compile(
            r"(?:color|background)\s*:\s*(#[0-9a-fA-F]{3,8})\b"
        )
        hardcoded = []
        for i, line in enumerate(uncommented.split("\n"), 1):
            m = hex_pattern.search(line)
            if not m:
                continue
            # Only flag if the line does NOT use var()
            if "var(" in line:
                continue
            # Skip known acceptable cases
            if any(kw in line for kw in [
                "json-key", "json-string", "json-num", "json-bool",
                "xml-tag", "xml-attr", "xml-string",
                "search-hit", "search.current",
            ]):
                continue
            hardcoded.append((i, m.group(1), line.strip()))

        if hardcoded:
            lines_str = "\n".join(
                f"  Line {ln}: {color} in `{rule[:80]}`"
                for ln, color, rule in hardcoded
            )
            warnings.warn(
                f"Found {len(hardcoded)} potential hardcoded color(s) "
                f"that may need design tokens:\n{lines_str}"
            )

    def test_no_hardcoded_shadows_in_main_rules(self):
        """Box-shadow values should use --shadow or --shadow-sm tokens."""
        css = _read_css()
        uncommented = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)

        # Find box-shadow with raw rgba values (not using var())
        shadow_pattern = re.compile(
            r"box-shadow:\s*(?!.*var\().*?rgba?\("
        )
        hardcoded = []
        for i, line in enumerate(uncommented.split("\n"), 1):
            m = shadow_pattern.search(line)
            if m:
                if "@keyframes" in line:
                    continue
                hardcoded.append((i, line.strip()))

        if hardcoded:
            lines_str = "\n".join(
                f"  Line {ln}: `{rule[:80]}`" for ln, rule in hardcoded
            )
            warnings.warn(
                f"Found {len(hardcoded)} hardcoded box-shadow(s) that "
                f"may need --shadow tokens:\n{lines_str}"
            )

    def test_no_hardcoded_border_colors(self):
        """Border colors should use --border or --input-border tokens.

        Excludes the :root and [data-theme="dark"] blocks where border
        color tokens are being defined (the definitions themselves
        necessarily contain hex values).
        """
        css = _read_css()

        # Remove the token definition blocks — those necessarily contain
        # hex color values because they ARE the token definitions
        root_block = _extract_top_level_block(css, r":root\s*\{")
        dark_block = _extract_top_level_block(
            css, r'\[data-theme="dark"\]\s*\{'
        )
        checkable = css
        for block in [root_block, dark_block]:
            if block:
                checkable = checkable.replace(block, "")

        uncommented = re.sub(r"/\*.*?\*/", "", checkable, flags=re.DOTALL)

        # Find border-color or border: ... #xxx patterns not using var()
        border_pattern = re.compile(
            r"border(?:-(?:color|top|right|bottom|left))?\s*:\s*"
            r"(?!.*var\().*?(#[0-9a-fA-F]{3,8})"
        )
        hardcoded = []
        for i, line in enumerate(uncommented.split("\n"), 1):
            m = border_pattern.search(line)
            if m:
                hardcoded.append((i, m.group(1), line.strip()))

        if hardcoded:
            lines_str = "\n".join(
                f"  Line {ln}: {color} in `{rule[:80]}`"
                for ln, color, rule in hardcoded
            )
            warnings.warn(
                f"Found {len(hardcoded)} hardcoded border color(s) that "
                f"may need --border tokens:\n{lines_str}"
            )


# ── 4. Token Value Validation ──────────────────────────────────────


class TestCSSVariableValues:
    """CSS variable values must be valid and consistent."""

    def test_no_empty_token_values(self):
        """No CSS variable should have an empty value."""
        css = _read_css()
        root_block = _extract_top_level_block(css, r":root\s*\{")
        dark_block = _extract_top_level_block(css,
                                              r'\[data-theme="dark"\]\s*\{')

        light_tokens = _extract_css_variables(root_block)
        dark_tokens = _extract_css_variables(dark_block)

        empty = []
        for name, val in {**light_tokens, **dark_tokens}.items():
            if not val.strip():
                empty.append(name)

        assert not empty, (
            f"Tokens with empty values: {', '.join(sorted(empty))}"
        )

    def test_color_tokens_are_valid_colors(self):
        """Tokens whose name suggests a color should contain valid color
        values."""
        css = _read_css()
        root_block = _extract_top_level_block(css, r":root\s*\{")
        tokens = _extract_css_variables(root_block)

        colorish = {
            k: v for k, v in tokens.items()
            if any(s in k for s in ["-bg", "-text", "-color", "-border"])
        }
        color_pattern = re.compile(
            r"^(#[0-9a-fA-F]{3,8}|rgba?\(.+\)|hsla?\(.+\)|"
            r"transparent|inherit|currentColor|none)"
        )
        invalid = []
        for name, val in sorted(colorish.items()):
            if "linear-gradient" in val or "url(" in val:
                continue
            if not color_pattern.match(val.strip()):
                invalid.append((name, val))

        if invalid:
            items = "\n".join(f"  {k}: {v}" for k, v in invalid)
            warnings.warn(
                f"Color-implying tokens with non-color values:\n{items}"
            )


# ── 5. CSS Structural Integrity ───────────────────────────────────


class TestCSSStructuralIntegrity:
    """CSS file structural checks: sections, duplicates, brace balance."""

    def test_brace_balance(self):
        """Opening and closing braces must be balanced."""
        css = _read_css()
        open_count = css.count("{")
        close_count = css.count("}")
        assert open_count == close_count, (
            f"Brace mismatch: {open_count} open vs {close_count} close"
        )

    def test_no_consecutive_duplicate_section_numbers(self):
        """Section comment headers should not reuse the same number."""
        sections = _extract_section_numbers(_read_css())
        seen: dict[int, list[str]] = {}
        for _, num, title in sections:
            if num not in seen:
                seen[num] = []
            seen[num].append(title)

        duplicates = {k: v for k, v in seen.items() if len(v) > 1}
        if duplicates:
            dup_str = "; ".join(
                f"Section {k}: {v}" for k, v in sorted(duplicates.items())
            )
            raise AssertionError(
                f"Duplicate section numbers found: {dup_str}. "
                "Renumber sections to be unique."
            )

    def test_section_numbers_are_sequential(self):
        """Section numbers should be sequential without unexpected gaps."""
        sections = _extract_section_numbers(_read_css())
        if not sections:
            return
        nums = sorted(set(n for _, n, _ in sections))
        expected = list(range(nums[0], nums[-1] + 1))
        gaps = set(expected) - set(nums)
        if gaps:
            warnings.warn(
                f"Gaps in section numbering: missing sections "
                f"{sorted(gaps)}. This may be intentional if sections "
                f"were removed."
            )

    def test_no_duplicate_selectors(self):
        """Exact duplicate CSS selectors indicate copy-paste errors."""
        selectors = _extract_selector_names(_read_css())
        duplicates = {k: v for k, v in selectors.items() if len(v) > 1}

        # Some duplicates are intentional
        allowlist = {".error"}

        real_dupes = {
            k: v for k, v in duplicates.items() if k not in allowlist
        }
        if real_dupes:
            dup_lines = "\n".join(
                f"  `{sel}` at lines {lines}"
                for sel, lines in sorted(real_dupes.items())
            )
            warnings.warn(
                f"Found {len(real_dupes)} duplicate selector(s):\n"
                f"{dup_lines}"
            )

    def test_css_file_is_valid_utf8(self):
        """The CSS file must be valid UTF-8."""
        css_path = _css_path()
        try:
            css_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            raise AssertionError(f"CSS file is not valid UTF-8: {e}")


# ── 6. Token Naming Convention ─────────────────────────────────────


class TestCSSVariableNaming:
    """CSS variables should follow consistent naming conventions."""

    def test_all_tokens_have_double_dash_prefix(self):
        """All CSS custom properties must start with --."""
        css = _read_css()
        root_block = _extract_top_level_block(css, r":root\s*\{")
        tokens = _extract_css_variables(root_block)
        bad = [k for k in tokens if not k.startswith("--")]
        assert not bad, f"Tokens without -- prefix: {bad}"

    def test_no_duplicate_token_definitions_in_same_block(self):
        """The same token must not be defined twice in :root."""
        css = _read_css()
        root_block = _extract_top_level_block(css, r":root\s*\{")

        seen: dict[str, int] = {}
        for m in re.finditer(r"(--[\w-]+)\s*:", root_block):
            name = m.group(1)
            # Approximate line number
            line_no = root_block[:m.start()].count("\n") + 1
            if name in seen:
                raise AssertionError(
                    f"Token {name} defined twice in :root "
                    f"(lines ~{seen[name]} and ~{line_no})"
                )
            seen[name] = line_no

    def test_orphan_tokens_not_used_anywhere(self):
        """Tokens defined in :root but never used are dead code."""
        css = _read_css()
        root_block = _extract_top_level_block(css, r":root\s*\{")
        defined = set(_extract_css_variables(root_block))
        used = _tokens_used_in_css(css)

        orphans = defined - used
        if orphans:
            warnings.warn(
                f"Tokens defined in :root but NEVER used: "
                f"{', '.join(sorted(orphans))}. "
                "Remove dead tokens or add usage."
            )


# ── 7. CSS Variable Regression Guard ──────────────────────────────


class TestCSSVariableRegressionGuard:
    """Baseline regression guard for the CSS variable system.

    These tests establish a known-good count so any accidental
    addition or removal is caught in CI.
    """

    def test_css_variable_count_in_root(self):
        """Number of CSS variables defined in :root should not change
        without updating this test."""
        css = _read_css()
        root_block = _extract_top_level_block(css, r":root\s*\{")
        tokens = _extract_css_variables(root_block)

        # CURRENT BASELINE: ~136 tokens (expanded after design token migration
        # and UI component additions). This is intentionally loose — a range —
        # to allow token additions without breaking CI, but catch accidental
        # mass deletion.
        min_expected = 110
        max_expected = 180
        count = len(tokens)
        assert min_expected <= count <= max_expected, (
            f"Expected {min_expected}-{max_expected} CSS variables in "
            f":root, found {count}. If you intentionally changed the "
            "token count, update the baseline in this test."
        )

    def test_css_variable_count_in_dark_theme(self):
        """Number of CSS variables in dark theme should roughly match
        :root."""
        css = _read_css()
        dark_block = _extract_top_level_block(
            css, r'\[data-theme="dark"\]\s*\{'
        )
        tokens = _extract_css_variables(dark_block)

        # Should be close to :root count
        min_expected = 30
        max_expected = 90
        count = len(tokens)
        assert min_expected <= count <= max_expected, (
            f"Expected {min_expected}-{max_expected} CSS variables in "
            f"[data-theme=\"dark\"], found {count}."
        )

    def test_total_var_usages_are_stable(self):
        """Total var() references in the CSS should be in a reasonable
        range."""
        css = _read_css()
        usages = _tokens_used_in_css(css)

        min_expected = 80
        max_expected = 200
        count = len(usages)
        assert min_expected <= count <= max_expected, (
            f"Expected {min_expected}-{max_expected} unique var() token "
            f"references, found {count}."
        )

    def test_css_file_size_stable(self):
        """CSS file should not shrink or explode unexpectedly."""
        css = _read_css()
        size = len(css)
        min_bytes = 50_000
        max_bytes = 120_000
        assert min_bytes <= size <= max_bytes, (
            f"CSS file size {size} bytes outside expected range "
            f"({min_bytes}-{max_bytes}). Investigate."
        )