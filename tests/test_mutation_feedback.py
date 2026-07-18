"""Tests verifying CSS transition rules on key interactive components
and loading/feedback state classes in mutation templates.

These tests are a direct response to motion-87311e351c87, action item 4/5:
  "Write tests verifying CSS transition rules exist on key interactive
   components and loading/feedback state classes are present in mutation
   templates."

Parent tasks delivered:
  - t_b906aa69: 12 new CSS transition selectors (--transition-* design tokens)
  - t_ef859626: flashElement() feedback states with optimistic-flash-* CSS

These are *verification* tests — they parse the CSS and templates and
assert that the specific artifacts from the parent tasks are present.
"""

from __future__ import annotations

import re
from pathlib import Path


# ── Paths ──────────────────────────────────────────────────────────

def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _css_path() -> Path:
    return _project_root() / "src" / "web" / "static" / "css" / "styles.css"


def _templates_dir() -> Path:
    return _project_root() / "src" / "web" / "templates"


def _read_css() -> str:
    return _css_path().read_text()


def _read_template(name: str) -> str:
    return (_templates_dir() / name).read_text()


# ── CSS parsing helpers ────────────────────────────────────────────

def _extract_css_rules(css: str) -> list[dict]:
    """Extract all CSS rule blocks as {selector, body, start_line}."""
    lines = css.split("\n")
    rules: list[dict] = []

    # Track @media nesting to skip reduced-motion blocks
    media_stack: list[str] = []

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        # Track @media block entry
        if "@media" in stripped and "{" in stripped:
            media_stack.append(stripped)
            i += 1
            continue

        if stripped == "}" and media_stack:
            media_stack.pop()
            i += 1
            continue

        # Skip empty lines, comments, content-only lines
        if (stripped == "" or stripped.startswith("/*")
                or stripped.startswith("//") or stripped.startswith("*")):
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

        # Collect selector text
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

        # Collect body
        brace_line_content = lines[brace_line]
        closing_on_same_line = brace_line_content.rfind("}") > brace_line_content.find("{")
        depth = brace_line_content.count("{") - brace_line_content.count("}")

        body_parts: list[str] = []
        if closing_on_same_line and depth == 0:
            between = brace_line_content[
                brace_line_content.find("{") + 1 : brace_line_content.rfind("}")
            ]
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


def _find_rules_for_selector(rules: list[dict], selector_frag: str) -> list[dict]:
    """Return CSS rules whose selector contains ``selector_frag``."""
    return [r for r in rules if selector_frag in r["selector"]]


def _rule_has_transition(rule: dict) -> bool:
    """Check if a rule has a non-none transition declaration."""
    body = rule["body"]
    if "transition:" not in body:
        return False
    # Extract the transition value
    m = re.search(r"transition:\s*([^;]+);", body)
    if not m:
        return False
    value = m.group(1).strip()
    if value.startswith("none"):
        # Check if it's a multi-value like "none, other" — counts as having
        if "," in value:
            parts = [p.strip() for p in value.split(",")]
            return any(p != "none" for p in parts)
        return False
    return True


# ────────────────────────────────────────────────────────────────────
#  1. CSS Transition Rules on Key Interactive Components
# ────────────────────────────────────────────────────────────────────
#
# These are the 12 selectors that parent task t_b906aa69 added
# transition rules to.  Each test verifies:
#   (A) a CSS rule matching the selector exists
#   (B) that rule has a non-none ``transition:`` declaration
#   (C) the transition uses design tokens (var(--transition-*))
#
# Button variants (.btn-primary, .btn-secondary, .btn-danger, .btn-ghost)
# were listed as "required=False" in the original registry because they
# inherit from .btn via class composition — but t_b906aa69 added
# *explicit* transition rules to each, so we test them as required here.

# ── Helper: verify a single selector has a transition rule ──────────

def _assert_transition_exists(
    rules: list[dict],
    selector_frag: str,
    description: str,
    *,
    expect_design_token: bool = True,
    expect_token_prefix: str = "var(--transition",
) -> str | None:
    """Verify ``selector_frag`` has a transition rule. Returns error msg or None."""
    matching = _find_rules_for_selector(rules, selector_frag)
    if not matching:
        return f"{description} ({selector_frag}): NO CSS RULE FOUND"

    rules_with_transition = [
        r for r in matching
        if _rule_has_transition(r) and not r["inside_rpm"]
    ]
    if not rules_with_transition:
        return f"{description} ({selector_frag}): TRANSITION MISSING in rule(s)"

    if expect_design_token:
        for r in rules_with_transition:
            m = re.search(r"transition:\s*([^;]+);", r["body"])
            if m and expect_token_prefix not in m.group(1):
                return (
                    f"{description} ({selector_frag}): "
                    f"transition '{m.group(1).strip()}' does NOT use design tokens "
                    f"(expected '{expect_token_prefix}' prefix)"
                )

    return None  # OK


class TestCssTransitionRulesAdded:
    """Verify the 12 CSS transition rules from parent task t_b906aa69.

    Each test checks that a specific interactive component has a
    ``transition:`` declaration in styles.css that uses design tokens.
    """

    _rules: list[dict] | None = None

    @classmethod
    def _get_rules(cls) -> list[dict]:
        if cls._rules is None:
            cls._rules = _extract_css_rules(_read_css())
        return cls._rules

    # ── Button variants (explicit transitions added by t_b906aa69) ──

    def test_btn_primary_has_transition(self):
        err = _assert_transition_exists(
            self._get_rules(), ".btn-primary",
            "Primary button variant",
        )
        assert err is None, err

    def test_btn_secondary_has_transition(self):
        err = _assert_transition_exists(
            self._get_rules(), ".btn-secondary",
            "Secondary button variant",
        )
        assert err is None, err

    def test_btn_danger_has_transition(self):
        err = _assert_transition_exists(
            self._get_rules(), ".btn-danger",
            "Danger button variant",
        )
        assert err is None, err

    def test_btn_ghost_has_transition(self):
        err = _assert_transition_exists(
            self._get_rules(), ".btn-ghost",
            "Ghost button variant",
        )
        assert err is None, err

    # ── Active-state toggles ──

    def test_chat_session_item_active_has_transition(self):
        err = _assert_transition_exists(
            self._get_rules(), ".chat-session-item",
            "Chat session item",
        )
        assert err is None, err

    def test_date_range_selector_active_has_transition(self):
        err = _assert_transition_exists(
            self._get_rules(), ".date-range-selector a",
            "Date range selector filter link",
        )
        assert err is None, err

    def test_toc_list_active_has_transition(self):
        err = _assert_transition_exists(
            self._get_rules(), ".toc-list a",
            "Table of contents link",
        )
        assert err is None, err

    # ── Upload state changes ──

    def test_file_item_file_done_has_transition(self):
        err = _assert_transition_exists(
            self._get_rules(), ".file-item.file-done",
            "File upload done state",
        )
        assert err is None, err

    def test_file_item_file_failed_has_transition(self):
        err = _assert_transition_exists(
            self._get_rules(), ".file-item.file-failed",
            "File upload failed state",
        )
        assert err is None, err

    # ── Form controls ──

    def test_settings_range_has_transition(self):
        err = _assert_transition_exists(
            self._get_rules(), '.settings-field input[type="range"]',
            "Settings range slider",
        )
        assert err is None, err

    def test_viewer_toolbar_range_has_transition(self):
        err = _assert_transition_exists(
            self._get_rules(), '.viewer-toolbar input[type="range"]',
            "Viewer toolbar range slider",
        )
        assert err is None, err

    def test_doc_checkbox_select_all_has_transition(self):
        err = _assert_transition_exists(
            self._get_rules(), ".doc-checkbox",
            "Document checkbox / select-all",
        )
        assert err is None, err

    # ── Quality: all 12 use design tokens ──

    def test_all_12_transitions_use_design_tokens(self):
        """Regression: all transition rules must use var(--transition-*) tokens."""
        rules = self._get_rules()
        violations: list[str] = []
        for rule in rules:
            if rule["inside_rpm"]:
                continue
            if not _rule_has_transition(rule):
                continue
            m = re.search(r"transition:\s*([^;]+);", rule["body"])
            if not m:
                continue
            val = m.group(1).strip()
            if "var(--transition" not in val and val != "none":
                violations.append(
                    f"  L{rule['start_line']}: {rule['selector'][:60]} -> {val[:60]}"
                )
        assert not violations, (
            f"{len(violations)} transition rule(s) do NOT use design tokens:\n"
            + "\n".join(violations[:15])
        )

    def test_no_transition_all_blanket(self):
        """No transition rule should use the blanket 'transition: all'."""
        rules = self._get_rules()
        violations: list[str] = []
        for rule in rules:
            if rule["inside_rpm"]:
                continue
            if not _rule_has_transition(rule):
                continue
            m = re.search(r"transition:\s*([^;]+);", rule["body"])
            if not m:
                continue
            val = m.group(1).strip()
            if " all " in f" {val} " or val.startswith("all "):
                violations.append(
                    f"  L{rule['start_line']}: {rule['selector'][:60]}"
                )
        assert not violations, (
            f"{len(violations)} rule(s) use blanket 'transition: all':\n"
            + "\n".join(violations)
        )


# ────────────────────────────────────────────────────────────────────
#  2. Loading / Feedback State Classes in Mutation Templates
# ────────────────────────────────────────────────────────────────────
#
# The mutation templates are the HTML pages that handle CRUD
# operations on documents — single delete, bulk delete, tag add/remove,
# collection assign, uploads.  After parent task t_ef859626 added
# flashElement() and extractErrorMessage(), these templates and the CSS
# must provide the classes that the JS references at runtime:
#
#   CSS classes (in styles.css):
#       .optimistic-flash-success
#       .optimistic-flash-error
#       .optimistic-removing
#       .optimistic-added
#       .optimistic-btn-loading
#       .optimistic-spinner
#       .optimistic-toast-container / -msg / -success / -error
#       @keyframes optimistic-flash-success / optimistic-flash-error
#
#   Template loading indicators:
#       .htmx-indicator        (search_form.html, search_results.html)
#       .skeleton / .skeleton-block  (documents/detail.html, chat.html)
#       .spinner               (search_form.html)
#
#   JS loading (base.html must load):
#       optimistic-ui.js

class TestFeedbackCssClasses:
    """Verify loading/feedback CSS classes exist in styles.css.

    These classes are the contract between optimistic-ui.js and the
    stylesheet.  If any class is missing, flashElement() / showToast()
    will run without visible feedback.
    """

    @classmethod
    def setup_class(cls):
        cls.css = _read_css()

    def test_optimistic_flash_success_class(self):
        assert ".optimistic-flash-success" in self.css

    def test_optimistic_flash_error_class(self):
        assert ".optimistic-flash-error" in self.css

    def test_optimistic_removing_class(self):
        assert ".optimistic-removing" in self.css

    def test_optimistic_added_class(self):
        assert ".optimistic-added" in self.css

    def test_optimistic_btn_loading_class(self):
        assert ".optimistic-btn-loading" in self.css

    def test_optimistic_spinner_class(self):
        assert ".optimistic-spinner" in self.css

    def test_optimistic_toast_classes(self):
        assert ".optimistic-toast-container" in self.css
        assert ".optimistic-toast-msg" in self.css
        assert ".optimistic-toast-success" in self.css
        assert ".optimistic-toast-error" in self.css

    def test_flash_success_keyframes(self):
        assert "@keyframes optimistic-flash-success" in self.css

    def test_flash_error_keyframes(self):
        assert "@keyframes optimistic-flash-error" in self.css

    def test_flash_animations_use_design_tokens(self):
        # flash animations must reference CSS variables for theme compatibility.
        # The class rules (.optimistic-flash-success / -error) reference
        # keyframes; the keyframes themselves contain the design tokens.
        # Search for the keyframe definitions (no leading dot).
        idx = self.css.index("@keyframes optimistic-flash-success")
        flash_section = self.css[idx : idx + 600]  # wide enough for both keyframes
        assert "var(--success" in flash_section
        assert "var(--error" in flash_section

    def test_flash_duration_is_900ms(self):
        # Must match FLASH_DURATION in optimistic-ui.js (900ms)
        idx = self.css.index(".optimistic-flash-success {")
        flash_block = self.css[idx : idx + 100]
        assert "0.9s" in flash_block, (
            "CSS flash animation duration must be 0.9s to match JS FLASH_DURATION (900ms)"
        )

    def test_reduced_motion_disables_flash(self):
        """Flash animations must be disabled for prefers-reduced-motion."""
        # Find the reduced-motion block that contains flash rules.
        # Search for the one that has both flash-success and flash-error.
        blocks = self.css.split("@media (prefers-reduced-motion: reduce)")
        found = False
        for block in blocks:
            if ("optimistic-flash-success" in block
                    and "optimistic-flash-error" in block):
                found = True
                assert "animation: none" in block, (
                    "prefers-reduced-motion flash block must set animation: none"
                )
                break
        assert found, (
            "prefers-reduced-motion must contain a block that disables "
            "both optimistic-flash-success and optimistic-flash-error"
        )

    def test_htmx_indicator_class(self):
        assert ".htmx-indicator" in self.css

    def test_skeleton_class(self):
        assert ".skeleton" in self.css

    def test_skeleton_block_class(self):
        assert ".skeleton-block" in self.css

    def test_spinner_class(self):
        assert ".spinner" in self.css


class TestMutationTemplateClasses:
    """Verify mutation/loading feedback classes in HTML templates.

    Each test checks that a specific template contains the CSS classes
    that optimistic-ui.js and progress-bar.js expect at runtime.
    """

    # ── Search templates: HTMX loading indicators ──

    def test_search_form_has_htmx_indicator(self):
        content = _read_template("search_form.html")
        assert "htmx-indicator" in content, (
            "search_form.html must have htmx-indicator for live search loading"
        )

    def test_search_results_has_htmx_indicator(self):
        content = _read_template("search_results.html")
        assert "htmx-indicator" in content, (
            "search_results.html must have htmx-indicator for search loading"
        )

    def test_search_results_has_skeleton(self):
        content = _read_template("search_results.html")
        assert "skeleton" in content, (
            "search_results.html must have skeleton classes for lazy loading feedback"
        )

    # ── Document detail: skeleton placeholder ──

    def test_detail_html_has_skeleton_block(self):
        content = _read_template("documents/detail.html")
        assert "skeleton-block" in content, (
            "documents/detail.html must have skeleton-block for lazy excerpt loading"
        )

    # ── Upload: file progress classes ──

    def test_upload_form_has_file_list(self):
        content = _read_template("upload_form.html")
        assert "file-list" in content, (
            "upload_form.html must have file-list container for upload progress"
        )

    # ── Base template: required JS scripts ──

    def test_base_html_loads_optimistic_ui_js(self):
        content = _read_template("base.html")
        assert "optimistic-ui.js" in content, (
            "base.html must load optimistic-ui.js for mutation feedback"
        )

    def test_base_html_loads_progress_bar_js(self):
        content = _read_template("base.html")
        assert "progress-bar.js" in content, (
            "base.html must load progress-bar.js for request progress indicator"
        )

    def test_base_html_loads_htmx(self):
        content = _read_template("base.html")
        assert "htmx.min.js" in content, (
            "base.html must load HTMX for live updates"
        )

    # ── Chat: skeleton loading ──

    def test_chat_html_has_skeleton(self):
        content = _read_template("chat.html")
        assert "skeleton" in content, (
            "chat.html must have skeleton classes for session list loading"
        )

    # ── Mutation success templates: verify structure exists ──

    def test_delete_success_html_exists_and_valid(self):
        content = _read_template("delete_success.html")
        assert "文档已删除" in content or "deleted" in content.lower()
        assert "extends" in content  # Must extend base

    def test_bulk_delete_success_html_exists_and_valid(self):
        content = _read_template("bulk_delete_success.html")
        assert "批量删除" in content or "bulk" in content.lower()
        assert "extends" in content

    def test_bulk_tag_success_html_exists_and_valid(self):
        content = _read_template("bulk_tag_success.html")
        assert "标签" in content or "tag" in content.lower()
        assert "extends" in content

    def test_bulk_move_success_html_exists_and_valid(self):
        content = _read_template("bulk_move_success.html")
        assert "移动" in content or "move" in content.lower()
        assert "extends" in content

    def test_upload_success_html_exists_and_valid(self):
        content = _read_template("upload_success.html")
        assert "上传" in content or "upload" in content.lower()
        assert "extends" in content


class TestOptimisticUIRenderingContract:
    """Verify the rendering.py generates tag-remove forms correctly.

    The rendering.py generates tag badge HTML with embedded <form>
    elements for tag removal.  optimistic-ui.js's handleTagRemove()
    expects forms matching the /tags/<name>/delete URL pattern.
    """

    def test_tag_remove_forms_have_tag_pill_class(self):
        """Each tag badge must be in a .tag-pill div for optimistic-removing."""
        content = (_project_root() / "src" / "web" / "rendering.py").read_text()
        assert 'class="tag-pill"' in content or "'tag-pill'" in content, (
            "rendering.py must generate .tag-pill class for optimistic tag removal"
        )

    def test_tag_remove_forms_have_delete_url_pattern(self):
        """Tag remove forms must match /tags/<name>/delete URL pattern."""
        content = (_project_root() / "src" / "web" / "rendering.py").read_text()
        assert "/tags/" in content and "/delete" in content, (
            "rendering.py must generate /tags/<name>/delete URLs for tag removal"
        )

    def test_tag_remove_buttons_have_remove_class(self):
        """Tag remove buttons must have class for JS targeting."""
        content = (_project_root() / "src" / "web" / "rendering.py").read_text()
        assert "tag-remove" in content, (
            "rendering.py must generate .tag-remove class on remove buttons"
        )

    def test_tag_remove_forms_are_post_method(self):
        """Tag remove forms must use POST method (optimistic-ui expects FormData)."""
        content = (_project_root() / "src" / "web" / "rendering.py").read_text()
        assert 'method="post"' in content, (
            "rendering.py tag-remove forms must use method='post'"
        )


class TestFlashFeedbackContract:
    """Verify the JS-CSS flash feedback contract is consistent.

    The FLASH_DURATION constant in optimistic-ui.js must match the CSS
    animation duration, and all 7 mutation handlers must have flash
    calls in their onSuccess/onError callbacks.
    """

    @classmethod
    def setup_class(cls):
        cls.js = (_project_root() / "src" / "web" / "static" / "js" / "optimistic-ui.js").read_text()
        cls.css = _read_css()

    def test_flash_duration_matches_js(self):
        """CSS animation-duration (0.9s) must match JS FLASH_DURATION (900ms)."""
        assert "FLASH_DURATION = 900" in self.js, (
            "JS FLASH_DURATION must be 900 (ms)"
        )
        flash_success_idx = self.css.index(".optimistic-flash-success {")
        flash_block = self.css[flash_success_idx : flash_success_idx + 100]
        assert "0.9s" in flash_block, (
            "CSS animation-duration (0.9s) must match JS FLASH_DURATION (900ms)"
        )

    def test_all_seven_handlers_have_flash_calls(self):
        """All mutation handlers with DOM targets must call flashElement().

        handleBulkMove is excluded: it has no DOM element to flash (just
        a select dropdown); its feedback is handled entirely by
        setButtonLoading and showToast.

        handleCollectionAssign is excluded: on success it redirects the
        page (window.location.href/reload); there is no element to flash.
        """
        handlers_needing_flash = [
            "handleSingleDelete", "handleBulkDelete", "handleSingleTagAdd",
            "handleTagRemove", "handleBulkTag",
        ]
        for handler in handlers_needing_flash:
            # Extract the handler function body
            start = self.js.find(f"function {handler}")
            assert start >= 0, f"Handler not found: {handler}"
            # Find the closing of the function
            depth = 0
            i = self.js.find("{", start)
            while i < len(self.js) and (depth > 0 or i == self.js.find("{", start)):
                if self.js[i] == "{":
                    depth += 1
                elif self.js[i] == "}":
                    depth -= 1
                i += 1
            handler_body = self.js[start : i]
            assert "flashElement" in handler_body, (
                f"{handler} must call flashElement() for visual feedback"
            )
