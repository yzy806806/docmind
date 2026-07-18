"""Tests for keyboard shortcuts: JS module content, CSS styling,
and template inclusion.

Covers:
- keyboard-shortcuts.js module structure and key bindings
- Navigation shortcuts (g + key → URL mapping)
- Quick action shortcuts (/, ?, Escape)
- Document operation shortcuts (e, t, m, Delete)
- CSS styles for the help modal (.kbd-modal-overlay, .kbd-modal-panel, etc.)
- base.html includes the keyboard-shortcuts.js script tag
- Help modal DOM structure (dynamically built by the JS module)
- Editable-element detection (shortcuts suppressed in inputs/textareas)
- g-prefix timeout behavior
"""

from __future__ import annotations

import re
from pathlib import Path


# ── Helpers ──────────────────────────────────────────────────────


def _read_kbd_js() -> str:
    """Read the keyboard-shortcuts.js source file."""
    js_path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "web"
        / "static"
        / "js"
        / "keyboard-shortcuts.js"
    )
    return js_path.read_text()


def _read_css() -> str:
    css_path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "web"
        / "static"
        / "css"
        / "styles.css"
    )
    return css_path.read_text()


def _read_base_html() -> str:
    html_path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "web"
        / "templates"
        / "base.html"
    )
    return html_path.read_text()


# ── JS Module: Structure & IIFE ──────────────────────────────────


class TestKbdJSModuleStructure:
    """Verify the keyboard-shortcuts.js file follows project conventions."""

    def test_file_exists(self):
        """keyboard-shortcuts.js should exist in /static/js/."""
        js = _read_kbd_js()
        assert len(js) > 0

    def test_uses_iife(self):
        """Module should use an IIFE (consistent with theme.js, viewer.js)."""
        js = _read_kbd_js()
        assert "(function ()" in js or "(function()" in js
        assert "use strict" in js

    def test_has_keydown_listener(self):
        """Should register a keydown event listener on document."""
        js = _read_kbd_js()
        assert "keydown" in js
        assert "addEventListener" in js

    def test_exposes_docmind_kbd(self):
        """Should expose a window.DocMindKbd object for testing."""
        js = _read_kbd_js()
        assert "DocMindKbd" in js

    def test_has_g_prefix_timeout(self):
        """Should define a g-prefix timeout for two-key navigation."""
        js = _read_kbd_js()
        assert "G_PREFIX_TIMEOUT" in js
        assert "700" in js  # 700ms timeout


# ── JS Module: Navigation Shortcuts ──────────────────────────────


class TestKbdNavigationShortcuts:
    """Verify the navigation shortcut key→URL mappings."""

    def test_nav_targets_defined(self):
        """NAV_TARGETS should map keys to URLs."""
        js = _read_kbd_js()
        assert "NAV_TARGETS" in js

    def test_dashboard_shortcut(self):
        """g d → / (Dashboard)."""
        js = _read_kbd_js()
        assert '"d":' in js or "'d':" in js
        assert '"/"' in js

    def test_search_shortcut(self):
        """g s → /search."""
        js = _read_kbd_js()
        assert '"s":' in js or "'s':" in js
        assert "/search" in js

    def test_documents_shortcut(self):
        """g D → /documents (capital D)."""
        js = _read_kbd_js()
        assert '"D":' in js or "'D':" in js
        assert "/documents" in js

    def test_upload_shortcut(self):
        """g u → /upload."""
        js = _read_kbd_js()
        assert '"u":' in js or "'u':" in js
        assert "/upload" in js

    def test_email_shortcut(self):
        """g e → /email-accounts."""
        js = _read_kbd_js()
        assert '"e":' in js or "'e':" in js
        assert "/email-accounts" in js

    def test_jobs_shortcut(self):
        """g j → /jobs."""
        js = _read_kbd_js()
        assert '"j":' in js or "'j':" in js
        assert "/jobs" in js

    def test_analytics_shortcut(self):
        """g a → /analytics."""
        js = _read_kbd_js()
        assert '"a":' in js or "'a':" in js
        assert "/analytics" in js

    def test_chat_shortcut(self):
        """g c → /chat."""
        js = _read_kbd_js()
        assert '"c":' in js or "'c':" in js
        assert "/chat" in js

    def test_settings_shortcut(self):
        """g x → /settings."""
        js = _read_kbd_js()
        assert '"x":' in js or "'x':" in js
        assert "/settings" in js


# ── JS Module: Quick Actions ─────────────────────────────────────


class TestKbdQuickActions:
    """Verify quick-action shortcut handlers."""

    def test_slash_focuses_search(self):
        """'/ ' key should trigger focusSearch()."""
        js = _read_kbd_js()
        assert '"/"' in js or "'/" in js
        assert "focusSearch" in js

    def test_question_toggles_help(self):
        """'?' key should toggle the help modal."""
        js = _read_kbd_js()
        assert '"?"' in js or "'?'" in js
        assert "toggleHelpModal" in js

    def test_escape_closes_modal(self):
        """Escape should close the help modal."""
        js = _read_kbd_js()
        assert "Escape" in js
        assert "closeHelpModal" in js

    def test_escape_blurs_editable(self):
        """Escape should blur the active element when in an input."""
        js = _read_kbd_js()
        assert ".blur()" in js

    def test_focus_search_finds_input_q(self):
        """focusSearch() should look for input[name='q']."""
        js = _read_kbd_js()
        assert 'input[name="q"]' in js or "input[name='q']" in js


# ── JS Module: Document Operations ───────────────────────────────


class TestKbdDocumentOperations:
    """Verify document operation shortcuts on the documents list page."""

    def test_export_shortcut(self):
        """'e' key should focus the bulk export select."""
        js = _read_kbd_js()
        assert '"e"' in js or "'e'" in js
        assert "bulk-export-format" in js

    def test_tag_shortcut(self):
        """'t' key should focus the bulk tag input."""
        js = _read_kbd_js()
        assert '"t"' in js or "'t'" in js
        assert "bulk-tag-input" in js

    def test_move_shortcut(self):
        """'m' key should focus the bulk move select."""
        js = _read_kbd_js()
        assert '"m"' in js or "'m'" in js
        assert "bulk-move-select" in js

    def test_delete_shortcut(self):
        """Delete key should trigger bulk delete."""
        js = _read_kbd_js()
        assert "Delete" in js
        assert "triggerBulkDelete" in js
        assert "confirmBulkDelete" in js

    def test_doc_ops_guarded_by_bulk_actions_bar(self):
        """Document operations should only work when bulk-actions-bar exists."""
        js = _read_kbd_js()
        assert "bulk-actions-bar" in js


# ── JS Module: Editable Element Detection ────────────────────────


class TestKbdEditableDetection:
    """Verify the isEditable() function suppresses shortcuts in form fields."""

    def test_checks_input_tag(self):
        """Should detect INPUT elements as editable."""
        js = _read_kbd_js()
        assert "INPUT" in js

    def test_checks_textarea_tag(self):
        """Should detect TEXTAREA elements as editable."""
        js = _read_kbd_js()
        assert "TEXTAREA" in js

    def test_checks_select_tag(self):
        """Should detect SELECT elements as editable."""
        js = _read_kbd_js()
        assert "SELECT" in js

    def test_checks_content_editable(self):
        """Should detect contentEditable elements as editable."""
        js = _read_kbd_js()
        assert "isContentEditable" in js

    def test_clears_g_prefix_when_editable(self):
        """g prefix should be cleared when entering an editable element."""
        js = _read_kbd_js()
        # The isEditable() branch should call clearGPrefix()
        assert "clearGPrefix" in js


# ── JS Module: Help Modal Builder ────────────────────────────────


class TestKbdHelpModalBuilder:
    """Verify the help modal is dynamically built with correct content."""

    def test_builds_modal_with_id(self):
        """Modal should be created with id 'kbd-shortcuts-modal'."""
        js = _read_kbd_js()
        assert "kbd-shortcuts-modal" in js
        assert "buildHelpModal" in js

    def test_modal_has_overlay_class(self):
        """Modal overlay should use class 'kbd-modal-overlay'."""
        js = _read_kbd_js()
        assert "kbd-modal-overlay" in js

    def test_modal_has_panel_class(self):
        """Modal panel should use class 'kbd-modal-panel'."""
        js = _read_kbd_js()
        assert "kbd-modal-panel" in js

    def test_modal_has_aria_attributes(self):
        """Modal should have role=dialog and aria-modal=true."""
        js = _read_kbd_js()
        assert "dialog" in js
        assert "aria-modal" in js

    def test_modal_lists_all_nav_shortcuts(self):
        """Modal content should list all navigation shortcuts."""
        js = _read_kbd_js()
        for label in ["Dashboard", "Search", "Documents", "Upload",
                       "Email", "Jobs", "Analytics", "Chat", "Settings"]:
            assert label in js, f"Modal should list '{label}' shortcut"

    def test_modal_lists_quick_actions(self):
        """Modal should list quick action shortcuts."""
        js = _read_kbd_js()
        assert "Focus search" in js
        assert "Show/hide" in js

    def test_modal_lists_doc_operations(self):
        """Modal should list document operation shortcuts."""
        js = _read_kbd_js()
        assert "Export selected" in js
        assert "Tag selected" in js
        assert "Move selected" in js
        assert "Delete selected" in js

    def test_modal_click_outside_closes(self):
        """Clicking outside the modal should close it."""
        js = _read_kbd_js()
        assert "click" in js
        # The click listener should check e.target === modal
        assert "e.target" in js or "event.target" in js


# ── CSS Tests ────────────────────────────────────────────────────


class TestKbdCSS:
    """Verify CSS styles for the keyboard shortcuts modal."""

    def test_overlay_hidden_by_default(self):
        """".kbd-modal-overlay should be hidden by default (visibility: hidden
        or display: none — both are valid, visibility allows transitions)."""
        css = _read_css()
        idx = css.find(".kbd-modal-overlay")
        assert idx != -1, "Missing .kbd-modal-overlay selector"
        block = css[idx:idx + 500]
        assert ("display: none" in block or "display:none" in block
                or "visibility: hidden" in block or "visibility:hidden" in block)

    def test_overlay_shown_when_open(self):
        """".kbd-modal-overlay.open should be visible (visibility: visible
        or display: flex — the overlay uses visibility for transitions)."""
        css = _read_css()
        idx = css.find(".kbd-modal-overlay.open")
        assert idx != -1, "Missing .kbd-modal-overlay.open selector"
        block = css[idx:idx + 200]
        assert ("display: flex" in block or "display:flex" in block
                or "visibility: visible" in block or "visibility:visible" in block)

    def test_overlay_is_fixed_position(self):
        """Overlay should be position: fixed covering the viewport."""
        css = _read_css()
        idx = css.find(".kbd-modal-overlay")
        block = css[idx:idx + 300]
        assert "position: fixed" in block or "position:fixed" in block

    def test_overlay_has_z_index(self):
        """Overlay should have a high z-index to sit above content."""
        css = _read_css()
        idx = css.find(".kbd-modal-overlay")
        block = css[idx:idx + 300]
        assert "z-index" in css[idx:idx + 300]

    def test_panel_has_max_width(self):
        """.kbd-modal-panel should have a max-width constraint."""
        css = _read_css()
        idx = css.find(".kbd-modal-panel")
        block = css[idx:idx + 300]
        assert "max-width" in block

    def test_panel_has_border_radius(self):
        """.kbd-modal-panel should have rounded corners."""
        css = _read_css()
        idx = css.find(".kbd-modal-panel")
        block = css[idx:idx + 300]
        assert "border-radius" in block

    def test_kbd_element_styled(self):
        """kbd elements should have inline-block styling."""
        css = _read_css()
        assert "kbd {" in css or "kbd{" in css
        idx = css.find("kbd {") if "kbd {" in css else css.find("kbd{")
        block = css[idx:idx + 200]
        assert "inline-block" in block or "inline block" in block

    def test_modal_animation_exists(self):
        """An animation or transition for modal entry should exist.
        Accepts either a named @keyframes animation (kbd-modal-in)
        or a CSS transition on .kbd-modal-panel (transform/opacity)."""
        css = _read_css()
        # Check for named animation
        if "kbd-modal-in" in css:
            return
        # Otherwise check for transition-based animation
        idx = css.find(".kbd-modal-panel")
        assert idx != -1, "Missing .kbd-modal-panel selector"
        block = css[idx:idx + 700]
        assert ("transition" in block and
                ("transform" in block or "opacity" in block)), \
            "Modal panel should have either kbd-modal-in animation or transition"

    def test_responsive_breakpoint_for_modal(self):
        """Modal should have responsive rules at 480px breakpoint."""
        css = _read_css()
        # There are two @media (max-width: 480px) blocks — the original
        # responsive one and the one we added for the modal. Find the
        # LAST one, which should contain kbd-modal-panel rules.
        all_480 = [m.start() for m in re.finditer(r"@media.*480px", css)]
        assert len(all_480) >= 2, "Expected at least two 480px media blocks"
        last_idx = all_480[-1]
        block = css[last_idx:last_idx + 500]
        assert "kbd-modal-panel" in block


# ── Template Inclusion Tests ─────────────────────────────────────


class TestKbdTemplateInclusion:
    """Verify base.html includes the keyboard-shortcuts.js script."""

    def test_base_html_loads_keyboard_shortcuts_js(self):
        """base.html should include /static/js/keyboard-shortcuts.js."""
        html = _read_base_html()
        assert "/static/js/keyboard-shortcuts.js" in html

    def test_script_has_defer_attribute(self):
        """The script tag should use defer (consistent with other JS)."""
        html = _read_base_html()
        assert 'src="/static/js/keyboard-shortcuts.js" defer' in html

    def test_script_loaded_after_htmx(self):
        """keyboard-shortcuts.js should load after htmx.min.js."""
        html = _read_base_html()
        htmx_idx = html.find("htmx.min.js")
        kbd_idx = html.find("keyboard-shortcuts.js")
        assert htmx_idx != -1, "htmx.min.js not found in base.html"
        assert kbd_idx != -1, "keyboard-shortcuts.js not found in base.html"
        assert htmx_idx < kbd_idx, "keyboard-shortcuts.js should load after htmx"

    def test_script_before_extra_js_block(self):
        """keyboard-shortcuts.js should load before the extra_js block."""
        html = _read_base_html()
        kbd_idx = html.find("keyboard-shortcuts.js")
        block_idx = html.find("extra_js")
        assert kbd_idx < block_idx, "keyboard-shortcuts.js should load before extra_js"


# ── Integration: _base_page rendering ────────────────────────────


class TestKbdBasePageRendering:
    """Verify the rendered base page includes the keyboard shortcuts script."""

    def test_base_page_includes_kbd_script(self):
        """_base_page() output should include the keyboard-shortcuts.js script."""
        from src.web.rendering import _base_page
        html = _base_page("Test", "<p>content</p>")
        assert "/static/js/keyboard-shortcuts.js" in html

    def test_base_page_has_kbd_script_with_defer(self):
        """The rendered page should have the script with defer attribute."""
        from src.web.rendering import _base_page
        html = _base_page("Test", "<p>content</p>")
        assert 'src="/static/js/keyboard-shortcuts.js" defer' in html
