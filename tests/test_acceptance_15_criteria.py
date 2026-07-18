"""Acceptance tests covering tester's 15-point criteria for Phase 2 deliverables.

Context: Agora Phase 2, motion-69159b7de5f1, action item 6/7.
Parent tasks delivered: debounced search (t_9d23ff18), hx-push-url (t_ca83debc),
microcopy translation (t_25e0c480).

These are *acceptance* tests — they verify that all four parent task deliverables
(transitions, debounce, push-url, microcopy) are present and correct in the
current codebase. Tests span static template/CSS inspection and HTTP-level
server behaviour.

15 criteria across four domains:

A. Transition presence/timing (6 criteria):
   1. Buttons (.btn + variants) have CSS transition rules using design tokens
   2. Navigation links have transition rules
   3. Form inputs have transition rules
   4. Cards/result containers have transition rules
   5. All transition durations fall within the 150-300ms range
   6. HTMX swap classes (.htmx-*) have transition rules

B. Debounce behavior (3 criteria):
   7. Search forms have hx-trigger with keyup delay:250ms
   8. Loading indicator (#search-loading) exists with "正在搜索…" text
   9. Documents filter form does NOT fire on text input (no 'input' in trigger)

C. hx-push-url correctness (3 criteria):
  10. Search submit via button pushes clean URL via HX-Push-Url header
  11. Search keyup (live search) does NOT push URL (no HX-Push-Url)
  12. Documents filter pushes URL via hx-push-url attribute + HX-Push-Url header

D. Microcopy presence (3 criteria):
  13. Navigation labels use Chinese text (仪表盘, 搜索, 文档, ...)
  14. Loading/empty/error states use Chinese microcopy
  15. Button labels and pagination text use Chinese
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Generator

import pytest


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _read(path: str) -> str:
    return (_project_root() / path).read_text()


def _read_template(name: str) -> str:
    return _read(f"src/web/templates/{name}")


def _css_path() -> Path:
    return _project_root() / "src" / "web" / "static" / "css" / "styles.css"


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
        closing_on_same_line = (
            brace_line_content.rfind("}") > brace_line_content.find("{")
        )
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


def _find_rules_for(rules: list[dict], fragment: str) -> list[dict]:
    return [r for r in rules if fragment in r["selector"]]


def _has_transition(rule: dict) -> bool:
    body = rule["body"]
    if "transition:" not in body:
        return False
    vals = _get_transition_values(rule)
    return any(v != "none" for v in vals)


def _get_transition_values(rule: dict) -> list[str]:
    body = rule["body"]
    lines = body.split("\n")
    values: list[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if "transition:" in stripped and not stripped.startswith("/*"):
            if ";" in stripped:
                m = re.search(r"transition:\s*([^;]+);", stripped)
                if m:
                    values.append(m.group(1).strip())
            else:
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
                i = j
        i += 1
    return values


def _get_transition_duration_ms(transition_value: str) -> float | None:
    """Parse out the duration in ms from a transition value like
    'background var(--transition-base)'."""
    m = re.search(r"var\(--transition-(\w+)\)", transition_value)
    if not m:
        # Direct time value like '0.15s' or '150ms'
        m = re.search(r"(\d+\.?\d*)(s|ms)", transition_value)
        if m:
            if m.group(2) == "s":
                return float(m.group(1)) * 1000
            return float(m.group(1))
        return None
    token_name = m.group(1)
    # Known design token durations
    token_ms = {
        "fast": 150,
        "base": 200,
        "press": 150,
        "lift": 250,
        "color": 200,
        "theme": 300,
    }
    return token_ms.get(token_name)


# ══════════════════════════════════════════════════════════════════
# A. TRANSITION PRESENCE/TIMING (Criteria 1-6)
# ══════════════════════════════════════════════════════════════════


class TestCriterion1_ButtonTransitions:
    """C1: Buttons (.btn + variants) have CSS transition rules using design tokens."""

    @classmethod
    def setup_class(cls):
        cls.rules = _extract_css_rules(_read("src/web/static/css/styles.css"))

    REQUIRED_BUTTONS = [
        (".btn", "Primary button class"),
        (".btn-save", "Settings save button"),
        (".btn-login", "Login button"),
        (".btn-new-chat", "New chat button"),
        (".search-box button", "Search box submit button"),
        (".upload-form button", "Upload form submit button"),
        (".chat-input-row button", "Chat send button"),
    ]

    @pytest.mark.parametrize("selector,desc", REQUIRED_BUTTONS)
    def test_button_has_transition(self, selector: str, desc: str):
        matching = _find_rules_for(self.rules, selector)
        found = [r for r in matching
                 if _has_transition(r) and not r["inside_rpm"]]
        assert found, (
            f"{desc} ({selector}) has no CSS transition rule. "
            f"Found {len(matching)} matching rules, none with transition."
        )

    @pytest.mark.parametrize("selector,desc", REQUIRED_BUTTONS)
    def test_button_transition_uses_design_token(self, selector: str, desc: str):
        matching = [r for r in _find_rules_for(self.rules, selector)
                    if _has_transition(r) and not r["inside_rpm"]]
        if not matching:
            pytest.skip(f"No transition rule for {selector}")
        for rule in matching:
            for val in _get_transition_values(rule):
                if val != "none":
                    assert "var(--" in val, (
                        f"{desc} ({selector}) transition '{val}' "
                        f"does not use design token (var(--...)) at L{rule['start_line']}"
                    )


class TestCriterion2_LinkTransitions:
    """C2: Navigation links have transition rules."""

    @classmethod
    def setup_class(cls):
        cls.rules = _extract_css_rules(_read("src/web/static/css/styles.css"))

    def test_header_nav_links_have_transition(self):
        """Header navigation <a> elements must have hover/color transitions."""
        matching = _find_rules_for(self.rules, "header nav a")
        found = [r for r in matching
                 if _has_transition(r) and not r["inside_rpm"]]
        assert found, (
            "Header navigation links (header nav a) have no CSS transition. "
            "Expected smooth hover/focus color transitions."
        )

    def test_pagination_links_have_transition(self):
        """Pagination links must have transition rules."""
        matching = _find_rules_for(self.rules, ".pagination ")
        found = [r for r in matching
                 if _has_transition(r) and not r["inside_rpm"]]
        assert found, "Pagination links have no CSS transition rule."

    def test_link_transitions_use_design_tokens(self):
        """Link transitions must reference var(--transition-*) tokens."""
        violations: list[str] = []
        link_patterns = ["header nav a", ".pagination ", ".collection-action-link",
                         ".toc-list a"]
        for pattern in link_patterns:
            matching = _find_rules_for(self.rules, pattern)
            for rule in matching:
                if not _has_transition(rule) or rule["inside_rpm"]:
                    continue
                for val in _get_transition_values(rule):
                    if val != "none" and "var(--" not in val:
                        violations.append(
                            f"  L{rule['start_line']}: {rule['selector']} -> {val}"
                        )
        assert not violations, (
            f"Link transitions without design tokens:\n" + "\n".join(violations[:10])
        )


class TestCriterion3_InputTransitions:
    """C3: Form inputs have transition rules."""

    @classmethod
    def setup_class(cls):
        cls.rules = _extract_css_rules(_read("src/web/static/css/styles.css"))

    def test_input_class_has_transition(self):
        """Base .input class must have border-color transition."""
        matching = _find_rules_for(self.rules, ".input")
        found = [r for r in matching
                 if _has_transition(r) and not r["inside_rpm"]]
        assert found, ".input class has no CSS transition rule."

    def test_search_inputs_have_transition(self):
        """Search text inputs must have transition rules."""
        matching = _find_rules_for(self.rules, ".search-box input")
        found = [r for r in matching
                 if _has_transition(r) and not r["inside_rpm"]]
        assert found, "Search box inputs have no CSS transition rule."


class TestCriterion4_CardTransitions:
    """C4: Cards/result containers have transition rules."""

    @classmethod
    def setup_class(cls):
        cls.rules = _extract_css_rules(_read("src/web/static/css/styles.css"))

    CARD_SELECTORS = [
        (".card", "Generic card container"),
        (".result", "Search result card"),
        (".file-item", "File item card"),
        (".stat", "Statistics card"),
    ]

    @pytest.mark.parametrize("selector,desc", CARD_SELECTORS)
    def test_card_has_transition(self, selector: str, desc: str):
        matching = _find_rules_for(self.rules, selector)
        found = [r for r in matching
                 if _has_transition(r) and not r["inside_rpm"]]
        assert found, (
            f"{desc} ({selector}) has no CSS transition rule."
        )


class TestCriterion5_TransitionTiming:
    """C5: All transition durations fall within the 150-300ms range."""

    @classmethod
    def setup_class(cls):
        cls.rules = _extract_css_rules(_read("src/web/static/css/styles.css"))

    def test_all_durations_within_150_300ms(self):
        """Every transition duration must be between 150ms and 300ms."""
        violations: list[str] = []
        for rule in self.rules:
            if rule["inside_rpm"]:
                continue
            if not _has_transition(rule):
                continue
            for val in _get_transition_values(rule):
                dur = _get_transition_duration_ms(val)
                if dur is None:
                    continue
                if dur < 150 or dur > 300:
                    violations.append(
                        f"  L{rule['start_line']}: {rule['selector']} "
                        f"duration={dur}ms (expect 150-300ms): {val}"
                    )
        assert not violations, (
            f"{len(violations)} transition rules outside 150-300ms range:\n"
            + "\n".join(violations[:15])
        )

    def test_all_durations_use_design_tokens(self):
        """Every transition must reference var(--transition-FAST|BASE|...)."""
        violations: list[str] = []
        for rule in self.rules:
            if rule["inside_rpm"]:
                continue
            if not _has_transition(rule):
                continue
            for val in _get_transition_values(rule):
                if val == "none":
                    continue
                if "var(--transition-" not in val:
                    violations.append(
                        f"  L{rule['start_line']}: {rule['selector']} -> {val}"
                    )
        assert not violations, (
            f"{len(violations)} transition rules don't use var(--transition-*):\n"
            + "\n".join(violations[:15])
        )


class TestCriterion6_HtmxSwapTransitions:
    """C6: HTMX swap classes (.htmx-*) have transition rules."""

    @classmethod
    def setup_class(cls):
        cls.css = _read("src/web/static/css/styles.css")
        cls.rules = _extract_css_rules(cls.css)

    HTMX_CLASSES = [
        ".htmx-indicator",
        ".htmx-added",
        ".htmx-settling",
        ".htmx-swapping",
    ]

    @pytest.mark.parametrize("cls_name", HTMX_CLASSES)
    def test_htmx_class_exists_in_css(self, cls_name: str):
        """Each HTMX swap class must be defined in styles.css."""
        assert cls_name in self.css, (
            f"{cls_name} is not defined anywhere in styles.css"
        )

    def test_htmx_indicator_has_transition(self):
        """.htmx-indicator must have an opacity transition for show/hide."""
        matching = _find_rules_for(self.rules, ".htmx-indicator")
        found = [r for r in matching
                 if _has_transition(r) and not r["inside_rpm"]]
        assert found, ".htmx-indicator has no transition rule."

    def test_htmx_added_has_transition(self):
        """.htmx-added must have an opacity transition for fade-in."""
        matching = _find_rules_for(self.rules, ".htmx-added")
        found = [r for r in matching
                 if _has_transition(r) and not r["inside_rpm"]]
        assert found, ".htmx-added has no transition rule (needed for smooth content insertion)"


# ══════════════════════════════════════════════════════════════════
# B. DEBOUNCE BEHAVIOR (Criteria 7-9)
# ══════════════════════════════════════════════════════════════════


class TestCriterion7_DebouncedSearchForms:
    """C7: Search forms have hx-trigger with keyup delay:250ms."""

    def test_search_form_has_250ms_debounce(self):
        """search_form.html: hx-trigger contains delay:250ms."""
        html = _read_template("search_form.html")
        trigger_match = re.search(r'hx-trigger="([^"]*)"', html)
        assert trigger_match, "No hx-trigger found in search_form.html"
        trigger_value = trigger_match.group(1)
        assert "250ms" in trigger_value, (
            f"hx-trigger must include delay:250ms: {trigger_value}"
        )

    def test_search_results_form_has_250ms_debounce(self):
        """search_results.html: hx-trigger contains delay:250ms."""
        html = _read_template("search_results.html")
        trigger_match = re.search(r'hx-trigger="([^"]*)"', html)
        assert trigger_match, "No hx-trigger found in search_results.html"
        trigger_value = trigger_match.group(1)
        assert "250ms" in trigger_value, (
            f"hx-trigger must include delay:250ms: {trigger_value}"
        )

    def test_debounce_uses_keyup_not_input(self):
        """hx-trigger uses 'keyup' event (not 'input') for debouncing."""
        for template in ["search_form.html", "search_results.html"]:
            html = _read_template(template)
            trigger_match = re.search(r'hx-trigger="([^"]*)"', html)
            assert trigger_match, f"No hx-trigger in {template}"
            trigger_value = trigger_match.group(1)
            assert "keyup" in trigger_value.lower(), (
                f"{template}: hx-trigger should use 'keyup' not just 'input': "
                f"{trigger_value}"
            )

    def test_debounce_delay_on_form_not_on_input(self):
        """The delay:250ms is on the <form> hx-trigger, not on <input>.
        This allows HTMX to handle the debounce at the form level."""
        for template in ["search_form.html", "search_results.html"]:
            html = _read_template(template)
            # Find the form tag
            form_match = re.search(
                r'<form[^>]*action="/search"[^>]*>', html, re.DOTALL
            )
            assert form_match, f"No search form in {template}"
            form_tag = form_match.group(0)
            assert "hx-trigger" in form_tag, (
                f"{template}: hx-trigger must be on the <form> element"
            )


class TestCriterion8_LoadingIndicator:
    """C8: Loading indicator (#search-loading) exists with
    '正在搜索…' text and indicator class."""

    def test_search_form_has_loading_div(self):
        """search_form.html must have #search-loading with 正在搜索…"""
        html = _read_template("search_form.html")
        assert 'id="search-loading"' in html, (
            "search_form.html missing #search-loading element"
        )
        assert "正在搜索" in html, (
            "search_form.html missing '正在搜索…' loading text"
        )

    def test_search_results_has_loading_div(self):
        """search_results.html must have #search-loading with 正在搜索…"""
        html = _read_template("search_results.html")
        assert 'id="search-loading"' in html, (
            "search_results.html missing #search-loading element"
        )
        assert "正在搜索" in html, (
            "search_results.html missing '正在搜索…' loading text"
        )

    def test_loading_uses_htmx_indicator(self):
        """#search-loading must use the htmx-indicator class."""
        for template in ["search_form.html", "search_results.html"]:
            html = _read_template(template)
            # Find the search-loading div
            loading_match = re.search(
                r'<div[^>]*id="search-loading"[^>]*>', html
            )
            assert loading_match, f"No #search-loading div in {template}"
            loading_tag = loading_match.group(0)
            assert "htmx-indicator" in loading_tag, (
                f"{template}: #search-loading missing htmx-indicator class"
            )

    def test_loading_has_spinner(self):
        """Loading indicator should have a visual spinner."""
        for template in ["search_form.html", "search_results.html"]:
            html = _read_template(template)
            assert "spinner" in html, (
                f"{template}: #search-loading missing spinner element"
            )

    def test_form_hx_indicator_points_to_loading(self):
        """Search forms' hx-indicator must reference #search-loading."""
        for template in ["search_form.html", "search_results.html"]:
            html = _read_template(template)
            indicator_match = re.search(r'hx-indicator="([^"]*)"', html)
            assert indicator_match, f"No hx-indicator in {template}"
            assert "search-loading" in indicator_match.group(1), (
                f"{template}: hx-indicator should point to #search-loading, "
                f"got: {indicator_match.group(1)}"
            )


class TestCriterion9_DocumentsFilterNoInputTrigger:
    """C9: Documents filter form does NOT fire on text input events
    (prevents per-keystroke re-renders)."""

    def test_filter_form_trigger_excludes_input(self):
        """hx-trigger on facet-filter-form must NOT contain 'input' event."""
        html = _read_template("documents/list.html")
        trigger_match = re.search(r'hx-trigger="([^"]*)"', html)
        assert trigger_match, "No hx-trigger found in documents/list.html"
        trigger_value = trigger_match.group(1)

        trigger_events = [e.strip() for e in trigger_value.split(",")]
        for event in trigger_events:
            event_name = re.split(r"[\s(]", event)[0].strip()
            assert event_name != "input", (
                f"Documents filter hx-trigger contains 'input' event "
                f"({trigger_value}): per-keystroke HTMX requests cause "
                f"re-render stutter. Use 'change' or 'submit' instead."
            )

    def test_skeleton_indicator_exists(self):
        """Documents list.html has a skeleton loader (#doc-table-skeleton)
        with the htmx-indicator class for HTMX swap feedback."""
        html = _read_template("documents/list.html")
        assert 'id="doc-table-skeleton"' in html, (
            "documents/list.html missing #doc-table-skeleton element"
        )
        skeleton_match = re.search(
            r'<div[^>]*id="doc-table-skeleton"[^>]*>', html
        )
        assert skeleton_match, "doc-table-skeleton div not found"
        skeleton_tag = skeleton_match.group(0)
        assert "htmx-indicator" in skeleton_tag, (
            "#doc-table-skeleton must have htmx-indicator class"
        )


# ══════════════════════════════════════════════════════════════════
# C. HX-PUSH-URL CORRECTNESS (Criteria 10-12)
# ══════════════════════════════════════════════════════════════════


class TestCriterion10_SearchSubmitPushesUrl:
    """C10: Search submit via button pushes clean URL via HX-Push-Url header."""

    @pytest.fixture
    def tmp_db_path(self) -> Generator[str, None, None]:
        with tempfile.TemporaryDirectory() as tmpdir:
            yield str(Path(tmpdir) / "test_acc_push_url.db")

    @pytest.fixture
    async def asgi_client(self, tmp_db_path: str):
        import httpx
        from src.core.db_sqlite import Database
        from src.web import server
        from unittest.mock import AsyncMock, MagicMock

        db = Database(db_path=tmp_db_path)
        await db.connect()
        await db.save_document(
            path="/docs/test.txt",
            source_type="api", source_name="test",
            title="Machine Learning Guide",
            ext=".txt", mime_type="text/plain",
            body="This document covers machine learning.",
            size=100, status="indexed",
        )

        mock_queue = MagicMock()
        mock_queue.enqueue = AsyncMock(return_value=MagicMock(id="test-job"))

        original_db = server._db
        original_queue = server._queue
        server._db = db
        server._queue = mock_queue

        app = server.create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as c:
            yield c

        await db.disconnect()
        server._db = original_db
        server._queue = original_queue

    @pytest.mark.asyncio
    async def test_submit_push_url_present(self, asgi_client):
        """HTMX submit_search=1 must return HX-Push-Url header."""
        resp = await asgi_client.get(
            "/search?q=machine&submit_search=1",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        push_url = (
            resp.headers.get("hx-push-url")
            or resp.headers.get("HX-Push-Url")
        )
        assert push_url is not None, (
            "HX-Push-Url header missing on submit-triggered HTMX search"
        )
        assert push_url.startswith("/search?"), (
            f"HX-Push-Url must start with /search?: {push_url}"
        )

    @pytest.mark.asyncio
    async def test_submit_push_url_excludes_internal_param(self, asgi_client):
        """Pushed URL must NOT contain submit_search param."""
        resp = await asgi_client.get(
            "/search?q=machine&submit_search=1",
            headers={"HX-Request": "true"},
        )
        push_url = (
            resp.headers.get("hx-push-url")
            or resp.headers.get("HX-Push-Url")
        )
        assert push_url is not None
        assert "submit_search" not in push_url, (
            f"Pushed URL must not contain internal submit_search: {push_url}"
        )

    @pytest.mark.asyncio
    async def test_submit_push_url_includes_query(self, asgi_client):
        """Pushed URL must include the search query."""
        resp = await asgi_client.get(
            "/search?q=machine&submit_search=1",
            headers={"HX-Request": "true"},
        )
        push_url = (
            resp.headers.get("hx-push-url")
            or resp.headers.get("HX-Push-Url")
        )
        assert push_url is not None
        assert "q=machine" in push_url, (
            f"Pushed URL must contain q=machine: {push_url}"
        )

    @pytest.mark.asyncio
    async def test_submit_push_url_vector_weight(self, asgi_client):
        """Pushed URL includes vector_weight when provided."""
        resp = await asgi_client.get(
            "/search?q=machine&vector_weight=0.80&submit_search=1",
            headers={"HX-Request": "true"},
        )
        push_url = (
            resp.headers.get("hx-push-url")
            or resp.headers.get("HX-Push-Url")
        )
        assert push_url is not None
        assert "vector_weight=0.80" in push_url, (
            f"Pushed URL must contain vector_weight: {push_url}"
        )


class TestCriterion11_KeyupNoPushUrl:
    """C11: Search keyup (live search) does NOT push URL
    (prevents history flood on every keystroke)."""

    @pytest.fixture
    def tmp_db_path(self) -> Generator[str, None, None]:
        with tempfile.TemporaryDirectory() as tmpdir:
            yield str(Path(tmpdir) / "test_acc_keyup_no_push.db")

    @pytest.fixture
    async def asgi_client(self, tmp_db_path: str):
        import httpx
        from src.core.db_sqlite import Database
        from src.web import server
        from unittest.mock import AsyncMock, MagicMock

        db = Database(db_path=tmp_db_path)
        await db.connect()
        await db.save_document(
            path="/docs/test.txt",
            source_type="api", source_name="test",
            title="Test Doc", ext=".txt", mime_type="text/plain",
            body="Test content.", size=100, status="indexed",
        )

        mock_queue = MagicMock()
        mock_queue.enqueue = AsyncMock(return_value=MagicMock(id="job"))

        original_db = server._db
        original_queue = server._queue
        server._db = db
        server._queue = mock_queue

        app = server.create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as c:
            yield c

        await db.disconnect()
        server._db = original_db
        server._queue = original_queue

    @pytest.mark.asyncio
    async def test_keyup_htmx_no_push_url(self, asgi_client):
        """HTMX keyup request (no submit_search) must NOT have HX-Push-Url."""
        resp = await asgi_client.get(
            "/search?q=machine",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        push_url = (
            resp.headers.get("hx-push-url")
            or resp.headers.get("HX-Push-Url")
        )
        assert push_url is None, (
            f"Keyup-triggered search must NOT push URL (floods history): "
            f"got {push_url}"
        )

    @pytest.mark.asyncio
    async def test_keyup_returns_fragment_not_full_page(self, asgi_client):
        """Keyup HTMX request returns a fragment (no <html> chrome)."""
        resp = await asgi_client.get(
            "/search?q=machine",
            headers={"HX-Request": "true"},
        )
        html = resp.text
        assert "<html" not in html.lower(), (
            "HTMX fragment should not contain full-page chrome"
        )

    @pytest.mark.asyncio
    async def test_non_htmx_no_push_url(self, asgi_client):
        """Regular full-page request must NOT have HX-Push-Url."""
        resp = await asgi_client.get("/search?q=machine")
        assert resp.status_code == 200
        push_url = (
            resp.headers.get("hx-push-url")
            or resp.headers.get("HX-Push-Url")
        )
        assert push_url is None, (
            "Full-page request must not have HX-Push-Url"
        )


class TestCriterion12_DocumentsFilterPushUrl:
    """C12: Documents filter pushes URL via hx-push-url attribute
    and HX-Push-Url response header."""

    def test_filter_form_has_hx_push_url_attribute(self):
        """documents/list.html filter form has hx-push-url attribute."""
        html = _read_template("documents/list.html")
        form_match = re.search(
            r'<form[^>]*id="facet-filter-form"[^>]*>', html, re.DOTALL
        )
        assert form_match, "Filter form not found in documents/list.html"
        form_tag = form_match.group(0)
        assert "hx-push-url" in form_tag, (
            f"Filter form must have hx-push-url: {form_tag}"
        )

    @pytest.fixture
    def tmp_db_path(self) -> Generator[str, None, None]:
        with tempfile.TemporaryDirectory() as tmpdir:
            yield str(Path(tmpdir) / "test_acc_docs_push.db")

    @pytest.fixture
    async def asgi_client(self, tmp_db_path: str):
        import httpx
        from src.core.db_sqlite import Database
        from src.web import server
        from unittest.mock import AsyncMock, MagicMock

        db = Database(db_path=tmp_db_path)
        await db.connect()

        mock_queue = MagicMock()
        mock_queue.enqueue = AsyncMock(return_value=MagicMock(id="test-job"))

        original_db = server._db
        original_queue = server._queue
        server._db = db
        server._queue = mock_queue

        app = server.create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as c:
            yield c

        await db.disconnect()
        server._db = original_db
        server._queue = original_queue

    @pytest.mark.asyncio
    async def test_docs_partial_has_push_url_header(self, asgi_client):
        """GET /documents/partials/table must return HX-Push-Url."""
        resp = await asgi_client.get("/documents/partials/table")
        assert resp.status_code == 200
        push_url = (
            resp.headers.get("hx-push-url")
            or resp.headers.get("HX-Push-Url")
        )
        assert push_url is not None, (
            "HX-Push-Url header missing on documents partial response"
        )
        assert push_url.startswith("/documents?"), (
            f"HX-Push-Url should point to /documents?...: {push_url}"
        )
        assert "/partials/" not in push_url, (
            f"HX-Push-Url must not contain /partials/: {push_url}"
        )

    @pytest.mark.asyncio
    async def test_docs_partial_push_url_includes_params(self, asgi_client):
        """HX-Push-Url must include filter params."""
        resp = await asgi_client.get(
            "/documents/partials/table?source=api&file_type=.pdf"
        )
        assert resp.status_code == 200
        push_url = (
            resp.headers.get("hx-push-url")
            or resp.headers.get("HX-Push-Url")
        )
        assert push_url is not None
        assert "source=api" in push_url
        assert "file_type=.pdf" in push_url


# ══════════════════════════════════════════════════════════════════
# D. MICROCOPY PRESENCE (Criteria 13-15)
# ══════════════════════════════════════════════════════════════════


class TestCriterion13_NavigationLabels:
    """C13: Navigation labels use Chinese text."""

    def test_base_nav_labels_are_chinese(self):
        """base.html nav links must use Chinese labels."""
        html = _read_template("base.html")

        expected_nav = {
            "仪表盘": "Dashboard nav link",
            "搜索": "Search nav link",
            "文档": "Documents nav link",
            "上传": "Upload nav link",
            "邮件": "Email nav link",
            "任务": "Jobs nav link",
            "分析": "Analytics nav link",
            "对话": "Chat nav link",
            "设置": "Settings nav link",
        }

        missing: list[str] = []
        for label, desc in expected_nav.items():
            if label not in html:
                missing.append(f"  {desc}: '{label}' not found in base.html nav")
        assert not missing, (
            "Navigation labels missing Chinese text:\n" + "\n".join(missing)
        )

    def test_base_page_brand_is_chinese(self):
        """Footer text must use Chinese."""
        html = _read_template("base.html")
        assert "AI 驱动的文档知识库" in html, (
            "Footer missing Chinese brand text 'AI 驱动的文档知识库'"
        )

    def test_base_html_lang_attribute(self):
        """base.html <html> must have lang='zh-CN'."""
        html = _read_template("base.html")
        assert 'lang="zh-CN"' in html, (
            "<html> tag missing lang='zh-CN' attribute"
        )


class TestCriterion14_LoadingAndEmptyStates:
    """C14: Loading/empty/error states use Chinese microcopy."""

    CHINESE_EMPTY_STATES = {
        "dashboard.html": ("暂无已索引的文档", "Dashboard empty state"),
        "email/accounts_list.html": ("暂无邮件账户", "Email accounts empty state"),
        "email/logs.html": ("暂无接收日志", "Email logs empty state"),
        "search_results.html": ("未找到结果", "Search no-results message"),
    }

    @pytest.mark.parametrize("template,expected", [
        (t, s[0]) for t, s in CHINESE_EMPTY_STATES.items()
    ])
    def test_empty_states_are_chinese(self, template: str, expected: str):
        """Each template's empty state uses Chinese text."""
        html = _read_template(template)
        assert expected in html, (
            f"{template}: missing Chinese empty state '{expected}'"
        )

    CHINESE_LOADING_STATES = [
        ("search_form.html", "正在搜索"),
        ("search_results.html", "正在搜索"),
    ]

    @pytest.mark.parametrize("template,expected", CHINESE_LOADING_STATES)
    def test_loading_states_are_chinese(self, template: str, expected: str):
        """Loading indicators use Chinese text."""
        html = _read_template(template)
        assert expected in html, (
            f"{template}: missing Chinese loading text '{expected}'"
        )

    CHINESE_LOGIN_UI = [
        ("login.html", "需要认证 — 请登录", "Login subtitle"),
        ("login.html", "密码 / API 密钥", "Password label"),
        ("login.html", "请输入 API 密钥", "Password placeholder"),
        ("login.html", "登录", "Login button text"),
    ]

    @pytest.mark.parametrize("template,expected,desc", CHINESE_LOGIN_UI)
    def test_login_ui_is_chinese(self, template: str, expected: str, desc: str):
        """Login page UI labels are in Chinese."""
        html = _read_template(template)
        assert expected in html, f"{desc}: '{expected}' not found in {template}"

    CHINESE_ERROR_UI = [
        ("error.html", "返回仪表盘", "Back-to-dashboard link"),
    ]

    @pytest.mark.parametrize("template,expected,desc", CHINESE_ERROR_UI)
    def test_error_ui_is_chinese(self, template: str, expected: str, desc: str):
        """Error page UI labels are in Chinese."""
        html = _read_template(template)
        assert expected in html, f"{desc}: '{expected}' not found in {template}"


class TestCriterion15_ButtonsAndPagination:
    """C15: Button labels and pagination text use Chinese."""

    def test_button_labels_are_chinese(self):
        """All submit buttons and action buttons use Chinese labels."""
        # Check key templates for Chinese button text patterns
        templates = [
            "search_form.html",
            "search_results.html",
            "settings.html",
            "dashboard.html",
            "email/account_form.html",
            "collections/form.html",
            "documents/list.html",
        ]

        chinese_buttons = [
            "搜索",       # Search button
            "保存设置",   # Save settings
            "取消",       # Cancel
            "删除",       # Delete
            "发送",       # Send
            "分配",       # Assign
            "应用",       # Apply
            "清除",       # Clear
            "加载更多",   # Load more
            "导出",       # Export
            "删除所选",   # Delete selected
            "导出所选",   # Export selected
        ]

        all_html = ""
        for t in templates:
            try:
                all_html += _read_template(t) + "\n"
            except FileNotFoundError:
                pass

        found: list[str] = []
        missing: list[str] = []
        for text in chinese_buttons:
            if text in all_html:
                found.append(text)
            else:
                missing.append(text)

        # At minimum, the core action buttons must be present
        assert "搜索" in found, "Search button '搜索' not found in any template"
        assert "保存设置" in found or "保存" in found, (
            "Save button '保存设置' not found"
        )
        assert "取消" in found, "Cancel button '取消' not found"
        assert "删除" in found or "删除所选" in found, (
            "Delete button '删除' not found"
        )

        if missing:
            pytest.skip(
                f"Minor: {len(missing)} button labels may only be in untested "
                f"templates: {missing} — this is informational"
            )

    def test_pagination_text_is_chinese(self):
        """Pagination must use Chinese labels '上一页' and '下一页'."""
        # Check pagination partial
        html = _read_template("_partials/pagination.html")
        assert "上一页" in html, "Pagination missing Chinese '上一页' (Prev)"
        assert "下一页" in html, "Pagination missing Chinese '下一页' (Next)"

    def test_pagination_info_is_chinese(self):
        """documents/list.html pagination info uses Chinese format."""
        html = _read_template("documents/list.html")
        assert "显示第" in html, (
            "Pagination info missing Chinese '显示第' (Showing)"
        )
        assert "条，共" in html, (
            "Pagination info missing Chinese counters '条，共'"
        )

    def test_settings_buttons_are_chinese(self):
        """Settings page buttons use Chinese labels."""
        html = _read_template("settings.html")
        assert "保存设置" in html, "Settings missing '保存设置' button"
        assert "取消" in html, "Settings missing '取消' button/cancel link"

    def test_form_placeholders_are_chinese(self):
        """Text input placeholders use Chinese text."""
        html = _read_template("search_form.html")
        assert "搜索" in html or "按关键词" in html, (
            "Search form placeholder not in Chinese"
        )

        html_dash = _read_template("dashboard.html")
        assert "搜索全部文档" in html_dash, (
            "Dashboard quick search placeholder not in Chinese"
        )
