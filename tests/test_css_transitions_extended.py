"""Extended CSS transition & animation tests — complement test_css_transitions.py.

Covers what the original file does not:
1. Reduced-motion completeness — every transition/animation rule must have a
   corresponding disable in @media (prefers-reduced-motion: reduce).
2. Animation existence — all @keyframes definitions contributing to smoothness
   must be present and used.
3. Transition timing functions — verify transitions use appropriate easing
   (no accidental `linear` on interactive elements).
4. New component coverage — verify recently added components (optimistic
   feedback, loading states, empty states) have transition/animation rules.
5. Transition count growth — as new interactive elements are added, the
   transition count should not regress below the established baseline.
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


def _strip_comments(css: str) -> str:
    """Remove /* ... */ comments."""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _extract_rule_selectors(css: str, property_name: str) -> list[str]:
    """Extract all selectors that have a given CSS property (transition or animation).

    Returns list of selectors (class-based only, no body/header/*).
    """
    clean = _strip_comments(css)
    clean_lines = clean.split("\n")
    result: list[str] = []
    in_rule = False
    brace_depth = 0
    current_selector = ""
    rule_body: list[str] = []

    for i, line in enumerate(clean_lines):
        stripped = line.strip()
        opens = stripped.count("{")
        closes = stripped.count("}")

        if (
            opens > 0
            and not in_rule
            and not stripped.startswith("@")
            and not stripped.startswith(":root")
            and not stripped.startswith("[data-theme")
        ):
            in_rule = True
            brace_depth = 0
            current_selector = stripped.split("{")[0].strip()
            rule_body = []

        brace_depth += opens - closes

        if in_rule:
            rule_body.append(line)

        if in_rule and brace_depth <= 0:
            body = "\n".join(rule_body)
            if f"{property_name}:" in body:
                if (
                    current_selector.startswith(".")
                    or current_selector.startswith("#")
                    or (
                        " " in current_selector
                        and not current_selector.startswith("@")
                    )
                ):
                    result.append(current_selector)
            in_rule = False
            rule_body = []

    return result


def _get_all_transition_selectors(css: str) -> list[str]:
    return _extract_rule_selectors(css, "transition")


def _get_all_animation_selectors(css: str) -> list[str]:
    return _extract_rule_selectors(css, "animation")


def _extract_keyframes(css: str) -> list[str]:
    """Extract all @keyframes animation names."""
    names: list[str] = []
    for m in re.finditer(r"@keyframes\s+([\w-]+)", css):
        names.append(m.group(1))
    return names


def _get_rpm_disable_selectors(css: str) -> set[str]:
    """Extract all selectors inside @media (prefers-reduced-motion: reduce) blocks."""
    result: set[str] = set()

    for m in re.finditer(
        r"@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)\s*\{",
        css,
    ):
        start = m.end()
        depth = 1
        i = start
        while i < len(css) and depth > 0:
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
            i += 1
        block = css[start : i - 1]
        lines = block.split("\n")

        in_rule = False
        brace_depth = 0
        current_selector = ""
        rule_body: list[str] = []

        for line in lines:
            stripped = line.strip()
            opens = stripped.count("{")
            closes = stripped.count("}")

            if opens > 0 and not in_rule:
                in_rule = True
                brace_depth = 0
                current_selector = stripped.split("{")[0].strip()
                rule_body = []

            brace_depth += opens - closes

            if in_rule:
                rule_body.append(line)

            if in_rule and brace_depth <= 0:
                body = "\n".join(rule_body)
                has_transition_disable = any(
                    kw in body
                    for kw in [
                        "transition:",
                        "animation:",
                        "transition-duration:",
                        "animation-duration:",
                    ]
                )
                if has_transition_disable and current_selector:
                    result.add(current_selector)
                in_rule = False
                rule_body = []

    return result


def _extract_all_css_rules(css: str) -> list[dict]:
    """Extract all CSS rules as {selector, body, inside_rpm}."""
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

        if stripped.startswith("@"):
            depth = lines[i].count("{") - lines[i].count("}")
            j = i + 1
            while j < len(lines) and depth > 0:
                depth += lines[j].count("{") - lines[j].count("}")
                j += 1
            i = j
            continue

        brace_on_line = "{" in stripped
        if not brace_on_line:
            next_i = i + 1
            while next_i < len(lines) and lines[next_i].strip() == "":
                next_i += 1
            if next_i < len(lines) and lines[next_i].strip() == "{":
                brace_on_line = True
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

        depth = lines[brace_line].count("{") - lines[brace_line].count("}")
        j = brace_line + 1
        body_parts: list[str] = []
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


# ── 1. Reduced Motion Completeness ─────────────────────────────────


class TestReducedMotionCompleteness:
    """Verify reduced-motion coverage for transitions and animations.

    The CSS uses two strategies for reduced motion:
    1. A universal catch-all ``*, *::before, *::after`` rule with
       ``animation-duration: 0.01ms !important`` and
       ``transition-duration: 0.01ms !important`` — this blankets ALL
       transitions and animations at once (section 27).
    2. Specific overrides for elements that need extra handling
       (e.g. spinners that should stay visible but not animate,
       progress bars that should jump to final position).

    This test verifies BOTH strategies are present.
    """

    @classmethod
    def setup_class(cls):
        cls.css = _read_css()
        cls.transition_selectors = _get_all_transition_selectors(cls.css)
        cls.animation_selectors = _get_all_animation_selectors(cls.css)
        cls.rpm_disabled = _get_rpm_disable_selectors(cls.css)

    def test_universal_reduced_motion_exists(self):
        """The universal RPM rule covering all transitions/animations must exist.

        Section 27 uses ``*, *::before, *::after`` with forced
        animation-duration and transition-duration to 0.01ms.
        """
        css = _strip_comments(self.css)
        first_rpm = re.search(
            r"@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)\s*\{",
            css,
        )
        assert first_rpm is not None, "No RPM blocks found"

        start = first_rpm.end()
        depth = 1
        i = start
        while i < len(css) and depth > 0:
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
            i += 1
        first_rpm_body = css[start : i - 1]

        has_universal = (
            "*" in first_rpm_body
            and (
                "animation-duration" in first_rpm_body
                or "transition-duration" in first_rpm_body
            )
        )

        assert has_universal, (
            "First RPM block is missing the universal transition/animation "
            "disable rule (*, *::before, *::after with animation-duration: "
            "0.01ms). This is the primary reduced-motion mechanism."
        )

    def test_reduced_motion_blocks_exist(self):
        """At least one @media (prefers-reduced-motion) block must exist."""
        css = _strip_comments(self.css)
        blocks = re.findall(
            r"@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)\s*\{",
            css,
        )
        assert len(blocks) >= 1, (
            "No @media (prefers-reduced-motion: reduce) blocks found"
        )

    def test_specific_elements_have_rpm_overrides(self):
        """Elements with distinct RPM needs must have specific override rules.

        While the universal catch-all handles most cases, some elements
        need per-element RPM rules (e.g. spinners that should show a
        static indicator instead of disappearing).
        """
        require_specific = {
            ".optimistic-spinner": "optimistic spinner",
            ".optimistic-removing": "optimistic removing",
            ".optimistic-added": "optimistic added",
            ".htmx-swapping": "htmx swapping",
            ".htmx-indicator": "htmx indicator",
            ".skeleton": "skeleton",
            ".spinner": "spinner",
            ".progress-bar.active": "progress bar active",
        }

        missing: list[str] = []
        for selector, desc in require_specific.items():
            if selector not in self.rpm_disabled:
                missing.append(f"  {desc} ({selector})")

        assert not missing, (
            "Elements missing specific RPM override rules:\n"
            + "\n".join(missing)
        )

    def test_all_animations_have_reduced_motion(self):
        """Key animation classes must have reduced-motion disable.

        Since the universal rule catches everything, this test verifies
        that critical animation class names appear in an RPM block
        (either universal or specific).
        """
        critical_classes = [
            ".optimistic-spinner",
            ".optimistic-added",
            ".optimistic-removing",
            ".htmx-added",
            ".htmx-swapping",
            ".htmx-indicator",
            ".skeleton",
            ".spinner",
            ".typing-indicator",
        ]

        css = _strip_comments(self.css)
        has_universal_rpm = bool(
            re.search(
                r"@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)\s*\{"
                r"\s*\*,",
                css,
            )
        )

        missing: list[str] = []
        if not has_universal_rpm:
            for cls_name in critical_classes:
                if cls_name not in self.rpm_disabled:
                    missing.append(cls_name)

        assert not missing, (
            f"Animation classes missing RPM coverage and no universal rule: {missing}"
        )


# ── 2. CSS Animation Existence Tests ─────────────────────────────


class TestAnimationExistence:
    """Verify all smoothness-contributing @keyframes animations exist."""

    @classmethod
    def setup_class(cls):
        cls.css = _read_css()
        cls.keyframes = _extract_keyframes(cls.css)

    def test_optimistic_ui_animations_exist(self):
        """Optimistic UI feedback animations must be defined."""
        required = [
            "optimistic-spin",
            "optimistic-fade-in",
            "optimistic-toast-in",
        ]
        missing = [k for k in required if k not in self.keyframes]
        assert not missing, (
            f"Missing optimistic UI keyframe animations: {missing}"
        )

    def test_htmx_animation_exists(self):
        """HTMX row fade-in animation must exist."""
        assert "htmx-row-fade-in" in self.keyframes, (
            "Missing htmx-row-fade-in @keyframes animation"
        )

    def test_lazy_load_animation_exists(self):
        """If a lazy-load class exists, its fade-in animation must be defined."""
        css = _strip_comments(self.css)
        if ".lazy-fade-in" in css or ".lazy-load" in css:
            assert "lazy-fade-in" in self.keyframes, (
                "Missing lazy-fade-in @keyframes animation "
                "but .lazy-fade-in class exists"
            )

    def test_skeleton_animation_exists(self):
        """Skeleton loading shimmer animation must exist."""
        assert "skeleton-shimmer" in self.keyframes, (
            "Missing skeleton-shimmer @keyframes animation"
        )

    def test_spinner_animation_exists(self):
        """Spinner rotate animation must exist."""
        assert "spinner-rotate" in self.keyframes, (
            "Missing spinner-rotate @keyframes animation"
        )

    def test_loading_class_exists(self):
        """.skeleton, .spinner, .htmx-indicator classes must exist."""
        css = _strip_comments(self.css)
        for cls_name in [".skeleton", ".spinner", ".htmx-indicator"]:
            assert re.search(re.escape(cls_name) + r"\s*\{", css), (
                f"Missing CSS class: {cls_name}"
            )

    def test_optimistic_classes_exist(self):
        """.optimistic-* classes must be defined."""
        css = _strip_comments(self.css)
        for cls_name in [
            ".optimistic-spinner",
            ".optimistic-added",
            ".optimistic-removing",
            ".optimistic-toast-msg",
        ]:
            assert re.search(re.escape(cls_name) + r"\s*\{", css), (
                f"Missing CSS class: {cls_name}"
            )

    def test_animations_use_design_tokens(self):
        """Animation durations should use var(--transition-*) tokens."""
        css = _strip_comments(self.css)
        violations: list[tuple[str, str]] = []

        for m in re.finditer(
            r"([\w.#][^{]*?)\s*\{([^}]*?animation[^}]*?)\}",
            css,
            re.DOTALL,
        ):
            selector = m.group(1).strip()
            body = m.group(2)
            dur_match = re.search(r"animation\s*:\s*\S+\s+([\d.]+s)", body)
            if dur_match:
                dur = dur_match.group(1)
                if selector in ("body", "header", "*") or selector.startswith("@"):
                    continue
                if "var(--" not in body:
                    violations.append((selector, dur))

        interactive_violations = [
            (s, d) for s, d in violations
            if any(
                kw in s
                for kw in [".optimistic", ".htmx", ".skeleton", ".spinner", ".lazy"]
            )
        ]

        if interactive_violations:
            warnings.warn(
                "Animations with hardcoded durations (consider tokens): "
                + ", ".join(f"{s}({d})" for s, d in interactive_violations)
            )


# ── 3. Transition Timing Function Quality ─────────────────────


class TestTransitionTimingFunctions:
    """Verify transitions use appropriate timing functions."""

    @classmethod
    def setup_class(cls):
        cls.css = _read_css()
        cls.clean = _strip_comments(cls.css)
        cls.rules = _extract_all_css_rules(cls.css)

    def test_no_linear_transitions_on_interactive_elements(self):
        """Interactive elements should not use linear timing functions.

        Linear timing feels mechanical — ease or ease-out is preferred
        for human interface interactions.
        """
        violations: list[tuple[str, int]] = []
        for rule in self.rules:
            if rule["inside_rpm"]:
                continue
            body = rule["body"]
            selector = rule["selector"]

            if selector in ("body", "header", "*", ":root", "html"):
                continue

            if "transition:" in body and "linear" in body:
                interactive_keywords = [
                    ".btn", ".card", ".input", "button", "a.",
                    ".tag", ".collection", ".chat", ".drop",
                    ".modal", ".htmx", ".optimistic", ".lazy",
                    ".progress",
                ]
                if any(kw in selector for kw in interactive_keywords):
                    violations.append((selector, rule["start_line"]))

        if violations:
            warnings.warn(
                "Interactive elements with 'linear' timing function: "
                + ", ".join(f"{s} (L{ln})" for s, ln in violations)
            )

    def test_transition_timing_tokens_exist(self):
        """Transition timing function tokens (--transition-easing) should exist.

        If not, individual rules must use explicit easing keywords.
        """
        css = self.clean
        root_block = ""
        m = re.search(r":root\s*\{", css)
        if m:
            depth = 1
            i = m.end()
            while i < len(css) and depth > 0:
                if css[i] == "{":
                    depth += 1
                elif css[i] == "}":
                    depth -= 1
                i += 1
            root_block = css[m.start() : i]

        has_easing_token = "transition-easing" in root_block

        if not has_easing_token:
            transitions_without_easing: list[tuple[str, int]] = []
            for rule in self.rules:
                if rule["inside_rpm"]:
                    continue
                body = rule["body"]
                if "transition:" in body:
                    easing_keywords = ["ease", "linear", "step-start", "step-end"]
                    if not any(ekw in body for ekw in easing_keywords):
                        transitions_without_easing.append(
                            (rule["selector"], rule["start_line"])
                        )

            if transitions_without_easing:
                warnings.warn(
                    "Transitions without explicit easing: "
                    + ", ".join(f"{s} (L{ln})" for s, ln in transitions_without_easing[:10])
                )


# ── 4. New Component Coverage ──────────────────────────────────


class TestNewComponentTransitionCoverage:
    """Verify recently added components have transition/animation rules."""

    @classmethod
    def setup_class(cls):
        cls.css = _read_css()
        cls.clean = _strip_comments(cls.css)

    def _rule_exists_for_selector(self, selector: str) -> bool:
        escaped = re.escape(selector)
        return bool(re.search(escaped + r"\s*\{", self.clean))

    def _rule_has_property(self, selector: str, prop: str) -> bool:
        escaped = re.escape(selector)
        pattern = escaped + r"\s*\{([^}]*)\}"
        m = re.search(pattern, self.clean)
        if not m:
            return False
        return f"{prop}:" in m.group(1)

    def test_lazy_fade_in_has_transition(self):
        """.lazy-fade-in class, if present, must use a transition or animation."""
        if self._rule_exists_for_selector(".lazy-fade-in"):
            assert self._rule_has_property(
                ".lazy-fade-in", "animation"
            ) or self._rule_has_property(".lazy-fade-in", "transition"), (
                ".lazy-fade-in missing transition/animation"
            )

    def test_optimistic_flash_classes_have_animation(self):
        """Optimistic flash classes, if present, must have animation."""
        for cls_name in [".optimistic-flash-success", ".optimistic-flash-error"]:
            if self._rule_exists_for_selector(cls_name):
                assert self._rule_has_property(cls_name, "animation"), (
                    f"{cls_name} missing animation property"
                )

    def test_search_loading_indicator_exists(self):
        """A loading indicator class must exist for search."""
        loading_classes = [".skeleton", ".spinner", ".htmx-indicator"]
        found = any(self._rule_exists_for_selector(c) for c in loading_classes)
        assert found, (
            f"No loading indicator class found ({loading_classes}); "
            "search debounce needs a visual loading state"
        )

    def test_empty_state_has_transition(self):
        """Empty state elements — report if found without transitions."""
        empty_patterns = ["empty-state", "no-results", "no-documents"]
        found_any = any(pat in self.clean for pat in empty_patterns)

        if found_any:
            has = any(
                pat in self.clean
                and "transition:"
                in self.clean[self.clean.find(pat) : self.clean.find(pat) + 500]
                for pat in empty_patterns
            )
            if not has:
                warnings.warn(
                    "Empty state classes found but no transitions — consider "
                    "adding fade-in transitions"
                )

    def test_progress_bar_has_transition(self):
        """.progress-bar and .progress-bar.active must have transitions."""
        assert self._rule_exists_for_selector(".progress-bar"), (
            ".progress-bar class missing"
        )
        if self._rule_exists_for_selector(".progress-bar.active"):
            assert self._rule_has_property(
                ".progress-bar.active", "transition"
            ) or self._rule_has_property(".progress-bar.active", "animation"), (
                ".progress-bar.active missing transition/animation"
            )


# ── 5. Transition Count Regression Guard ───────────────────────


class TestTransitionCountRegression:
    """Guard against accidental removal of transition rules."""

    @classmethod
    def setup_class(cls):
        cls.css = _read_css()
        cls.transition_selectors = _get_all_transition_selectors(cls.css)

    def test_minimum_transition_count(self):
        """Established baseline: at least 50 selectors must have transitions."""
        assert len(self.transition_selectors) >= 50, (
            f"Only {len(self.transition_selectors)} selectors have transition rules "
            f"(baseline: 50). Transition rules may have been removed."
        )

    def test_no_transition_regression_below_baseline(self):
        """The transition count should not decrease from known baseline of 55+."""
        assert len(self.transition_selectors) >= 55, (
            f"Transition count {len(self.transition_selectors)} below baseline 55. "
            "Possible regression: transition rules may have been removed."
        )

    def test_key_interactive_categories_still_have_transitions(self):
        """Verify all major categories still have transitions (regression guard)."""
        categories = {
            "buttons": [".btn", ".btn-save", ".btn-delete"],
            "inputs": [".input", ".search-box input"],
            "cards": [".card", ".result", ".stat"],
            "links": [".pagination a"],
            "htmx": [".htmx-indicator", ".htmx-added"],
            "modal": [".kbd-modal-overlay"],
            "tags": [".tag-pill"],
            "dropzone": [".drop-zone"],
            "progress": [".progress-bar"],
        }

        missing_cats: list[str] = []
        for cat_name, check_sels in categories.items():
            found = any(
                any(sel in ts for ts in self.transition_selectors)
                for sel in check_sels
            )
            if not found:
                missing_cats.append(cat_name)

        assert not missing_cats, (
            f"Categories missing transitions (regression): {missing_cats}"
        )
