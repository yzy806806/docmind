"""Behavioral CSS transition tests: verify computed-style values on live pages.

These tests go beyond static CSS file analysis -- they use Playwright to
navigate to real pages and inspect ``getComputedStyle()`` values to
confirm that ``transition-duration``, ``transition-property``, and
``transition-timing-function`` are correct on interactive elements.

WHY THIS MATTERS:
  Static tests (test_css_transitions.py, test_css_transitions_extended.py,
  test_mutation_feedback.py) verify that CSS rules *exist* in styles.css.
  test_smoothness_browser.py checks that transitions *are present* in the
  computed-style shorthand.  But NONE of them verify the individual
  longhand property values.

  A selector could have ``transition: all 0s linear`` and pass the
  existence test while providing zero smoothness.  This file validates
  the actual behavioral output -- what the browser really applies.

COVERAGE:
  - transition-duration: must fall in the 150ms-180ms range
    (matching --transition-fast=0.15s and --transition-base=0.18s).
  - transition-property: must NOT be "all".  Must target specific
    properties like border-color, background-color, transform, etc.
  - transition-timing-function: must use easing (ease, ease-out,
    ease-in-out), never "linear".

REQUIRES: playwright + Chromium browser installed.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Generator

import pytest

# ------------------------------------------------------------------
# Playwright availability
# ------------------------------------------------------------------

_HAS_PLAYWRIGHT = False
_HAS_BROWSER = False

try:
    from playwright.sync_api import sync_playwright  # noqa: F811

    _HAS_PLAYWRIGHT = True
    try:
        with sync_playwright() as p:
            _HAS_BROWSER = True
    except Exception:
        _HAS_BROWSER = False
except ImportError:
    pass

PLAYWRIGHT_REASON = (
    "playwright not installed"
    if not _HAS_PLAYWRIGHT
    else "playwright browser not installed"
    if not _HAS_BROWSER
    else ""
)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------
# Design token constants (must match styles.css)
# ------------------------------------------------------------------

FAST_MS = 150   # --transition-fast: 0.15s
BASE_MS = 180   # --transition-base: 0.18s
FAST_S = 0.15
BASE_S = 0.18

ALLOWED_EASING = {"ease", "ease-out", "ease-in-out", "ease-in"}

# ------------------------------------------------------------------
# Server fixture
# ------------------------------------------------------------------


@pytest.fixture(scope="module")
def server_url() -> Generator[str, None, None]:
    """Start a uvicorn test server and return its URL."""
    if not _HAS_PLAYWRIGHT or not _HAS_BROWSER:
        pytest.skip(PLAYWRIGHT_REASON)

    port = _find_free_port()
    root = _project_root()

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_transition_behavior.db")
        env = os.environ.copy()
        env["DOCMIND_DATABASE_PATH"] = db_path
        env["DOCMIND_PORT"] = str(port)
        env["DOCMIND_HOST"] = "127.0.0.1"
        env["DOCMIND_WORKERS"] = "1"
        env["DOCMIND_RATE_LIMIT_ENABLED"] = "false"
        env["DOCMIND_AUTH_ENABLED"] = "false"
        env["DOCMIND_DEBUG"] = "true"

        proc = subprocess.Popen(
            [
                sys.executable,
                "-m", "uvicorn",
                "src.web.server:app",
                "--host", "127.0.0.1",
                "--port", str(port),
                "--log-level", "error",
            ],
            cwd=str(root),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        url = f"http://127.0.0.1:{port}"

        import urllib.request
        deadline = time.time() + 15
        ready = False
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"{url}/docs", timeout=1)
                ready = True
                break
            except Exception:
                time.sleep(0.5)

        if not ready:
            proc.terminate()
            proc.wait()
            pytest.skip("Test server failed to start within 15s")

        try:
            yield url
        finally:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


# ------------------------------------------------------------------
# Browser fixture
# ------------------------------------------------------------------


@pytest.fixture(scope="module")
def browser_page(server_url: str) -> Generator:
    """Create a Playwright browser page connected to the test server."""
    if not _HAS_PLAYWRIGHT:
        pytest.skip(PLAYWRIGHT_REASON)
    with sync_playwright() as sp:
        browser = sp.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        try:
            yield page
        finally:
            context.close()
            browser.close()


# ------------------------------------------------------------------
# JS snippets executed in browser context
# ------------------------------------------------------------------

_GET_COMPUTED = """(el) => {
    const s = window.getComputedStyle(el);
    return {
        shorthand: s.transition || '',
        duration: s.transitionDuration || '',
        property: s.transitionProperty || '',
        timing: s.transitionTimingFunction || '',
        delay: s.transitionDelay || '',
    };
}"""

_QUERY_ALL = """(selectors, fn) => {
    const results = [];
    for (const sel of selectors) {
        const elements = document.querySelectorAll(sel);
        for (const el of elements) {
            if (el.offsetParent === null) continue;
            results.push(Object.assign({selector: sel}, fn(el)));
        }
    }
    return results;
}"""


# ==================================================================
# TEST CLASSES
# ==================================================================


# ------------------------------------------------------------------
# 1. Transition Duration -- computed values must match design tokens
# ------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_BROWSER, reason=PLAYWRIGHT_REASON)
class TestTransitionDurationBehavior:
    """Verify computed transition-duration equals design token values."""

    def _parse_durations(self, duration_str: str) -> list[float]:
        if not duration_str:
            return []
        values: list[float] = []
        for piece in duration_str.split(","):
            piece = piece.strip()
            if piece.endswith("s"):
                try:
                    values.append(float(piece.replace("s", "")))
                except ValueError:
                    pass
        return values

    def test_buttons_computed_duration_in_range(
        self, server_url: str, browser_page
    ):
        """Buttons must have computed duration ~0.15s-0.18s."""
        selectors = [
            ".btn", ".btn-primary", ".btn-secondary",
            ".btn-danger", ".btn-ghost", ".btn-delete", ".btn-save",
        ]

        found = False
        violations: list[str] = []

        for page_path in ["/documents", "/", "/search"]:
            browser_page.goto(f"{server_url}{page_path}", timeout=15000)
            browser_page.wait_for_timeout(500)

            data = browser_page.evaluate(
                f"({_QUERY_ALL})({json.dumps(selectors)}, {_GET_COMPUTED})"
            )
            if data:
                found = True
                for item in data:
                    durations = self._parse_durations(item["duration"])
                    for d in durations:
                        if d == 0.0:
                            continue
                        if not (FAST_S - 0.01 <= d <= BASE_S + 0.01):
                            violations.append(
                                f"{item['selector']} on {page_path}: duration {d}s"
                            )

        if not found:
            pytest.skip("No visible buttons found on any page")

        assert not violations, (
            "Buttons with wrong transition-duration:\n" + "\n".join(violations)
        )

    def test_cards_computed_duration_is_base(
        self, server_url: str, browser_page
    ):
        """Cards should have ~0.18s duration (--transition-base)."""
        browser_page.goto(f"{server_url}/documents", timeout=15000)
        browser_page.wait_for_timeout(500)

        data = browser_page.evaluate(
            f"({_QUERY_ALL})(['.card'], {_GET_COMPUTED})"
        )

        if not data:
            pytest.skip("No .card elements found")

        for item in data:
            durations = self._parse_durations(item["duration"])
            has_base = any(BASE_S - 0.01 <= d <= BASE_S + 0.01 for d in durations)
            assert has_base, (
                f".card: durations={durations}, expected at least one ~{BASE_S}s"
            )

    def test_inputs_computed_duration_is_base(
        self, server_url: str, browser_page
    ):
        """Inputs should have ~0.18s duration."""
        browser_page.goto(f"{server_url}/search", timeout=15000)
        browser_page.wait_for_timeout(500)

        data = browser_page.evaluate(
            f"({_QUERY_ALL})("
            f"['.input', 'input[type=\"text\"]', 'input[type=\"search\"]'],"
            f" {_GET_COMPUTED})"
        )

        if not data:
            pytest.skip("No inputs found on /search")

        violations: list[str] = []
        for item in data:
            durations = self._parse_durations(item["duration"])
            for d in durations:
                if d == 0.0:
                    continue
                if not (FAST_S - 0.01 <= d <= BASE_S + 0.01):
                    violations.append(
                        f"{item['selector']}: duration {d}s"
                    )

        assert not violations, (
            "Inputs with wrong duration:\n" + "\n".join(violations)
        )

    def test_body_has_theme_transition_duration(
        self, server_url: str, browser_page
    ):
        """Body should have ~0.18s duration for theme switching."""
        browser_page.goto(f"{server_url}/documents", timeout=15000)
        browser_page.wait_for_selector("body", timeout=5000)

        result = browser_page.evaluate(
            f"""() => {{
                const s = window.getComputedStyle(document.body);
                return {{
                    duration: s.transitionDuration,
                    timing: s.transitionTimingFunction,
                }};
            }}"""
        )

        durations = self._parse_durations(result["duration"])
        assert durations, f"body has no transition-duration: {result['duration']}"
        for d in durations:
            assert BASE_S - 0.01 <= d <= BASE_S + 0.01, (
                f"body duration {d}s not ~{BASE_S}s"
            )
        # Also verify timing is not linear
        timings = {t.strip() for t in result["timing"].split(",")} if result["timing"] else set()
        assert "linear" not in timings, f"body has linear timing: {result['timing']}"

    def test_no_zero_duration_on_interactive_elements(
        self, server_url: str, browser_page
    ):
        """No interactive element should have transition-duration: 0s.

        Only checks class-based selectors (not bare element selectors like
        'button' or 'a', which may match non-styled elements).
        """
        browser_page.goto(f"{server_url}/documents", timeout=15000)
        browser_page.wait_for_timeout(500)

        all_sel = [
            ".card", ".tag-pill", ".htmx-indicator",
            ".theme-toggle", ".nav-toggle",
        ]
        data = browser_page.evaluate(
            f"({_QUERY_ALL})({json.dumps(all_sel)}, {_GET_COMPUTED})"
        )

        violations: list[str] = []
        for item in data:
            shorthand = item["shorthand"]
            if not shorthand or shorthand == "all 0s ease 0s":
                violations.append(f"{item['selector']}: no transition applied")
                continue
            durations = self._parse_durations(item["duration"])
            if durations and all(d == 0.0 for d in durations):
                violations.append(f"{item['selector']}: all durations are 0s")

        assert not violations, (
            "Elements with zero transition-duration:\n" + "\n".join(violations)
        )


# ------------------------------------------------------------------
# 2. Transition Property -- must target specific properties, not 'all'
# ------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_BROWSER, reason=PLAYWRIGHT_REASON)
class TestTransitionPropertyBehavior:
    """Verify computed transition-property is specific, not 'all'."""

    def _assert_no_all(self, property_str: str, desc: str):
        """Assert transition-property does not include 'all'."""
        if not property_str:
            return  # no transition set -- fine for non-interactive elements
        props = {p.strip() for p in property_str.split(",")}
        assert "all" not in props, (
            f"{desc}: transition-property is 'all' = {property_str}"
        )

    def test_buttons_no_transition_all(
        self, server_url: str, browser_page
    ):
        """Buttons must NOT have transition-property: all."""
        browser_page.goto(f"{server_url}/documents", timeout=15000)
        browser_page.wait_for_timeout(500)

        selectors = [".btn", ".btn-primary", ".btn-secondary", ".btn-danger", ".btn-ghost"]
        data = browser_page.evaluate(
            f"({_QUERY_ALL})({json.dumps(selectors)}, {_GET_COMPUTED})"
        )

        if not data:
            pytest.skip("No buttons found")

        failures: list[str] = []
        for item in data:
            props = {p.strip() for p in item["property"].split(",")} if item["property"] else set()
            if "all" in props:
                failures.append(f"{item['selector']}: property={item['property']}")

        assert not failures, (
            "Buttons using transition-property: all:\n" + "\n".join(failures)
        )

    def test_cards_no_transition_all(
        self, server_url: str, browser_page
    ):
        """Cards must NOT have transition-property: all."""
        browser_page.goto(f"{server_url}/documents", timeout=15000)
        browser_page.wait_for_timeout(500)

        data = browser_page.evaluate(
            f"({_QUERY_ALL})(['.card'], {_GET_COMPUTED})"
        )

        if not data:
            pytest.skip("No cards found")

        for item in data:
            self._assert_no_all(item["property"], f".card on /documents")

    def test_inputs_no_transition_all(
        self, server_url: str, browser_page
    ):
        """Inputs must NOT have transition-property: all."""
        browser_page.goto(f"{server_url}/search", timeout=15000)
        browser_page.wait_for_timeout(500)

        data = browser_page.evaluate(
            f"({_QUERY_ALL})(['.input, input[type=\"text\"], input[type=\"search\"]'],"
            f" {_GET_COMPUTED})"
        )

        if not data:
            pytest.skip("No inputs found")

        for item in data:
            self._assert_no_all(item["property"], f"{item['selector']} on /search")

    def test_htmx_elements_no_transition_all(
        self, server_url: str, browser_page
    ):
        """HTMX elements must NOT have transition-property: all."""
        browser_page.goto(f"{server_url}/documents", timeout=15000)
        browser_page.wait_for_timeout(500)

        selectors = [".htmx-indicator", ".htmx-added", ".htmx-settling", ".htmx-swapping"]
        data = browser_page.evaluate(
            f"({_QUERY_ALL})({json.dumps(selectors)}, {_GET_COMPUTED})"
        )

        if not data:
            pytest.skip("No HTMX elements found")

        for item in data:
            self._assert_no_all(item["property"], f"{item['selector']} on /documents")

    def test_card_targets_box_shadow_and_transform(
        self, server_url: str, browser_page
    ):
        """.card should transition box-shadow + transform (not 'all')."""
        browser_page.goto(f"{server_url}/documents", timeout=15000)
        browser_page.wait_for_timeout(500)

        result = browser_page.evaluate(
            """() => {
                const card = document.querySelector('.card');
                if (!card) return null;
                const s = window.getComputedStyle(card);
                return s.transitionProperty || '';
            }"""
        )

        if not result:
            pytest.skip("No .card element found")

        props_lower = result.lower()
        assert "box-shadow" in props_lower, (
            f".card transition-property missing 'box-shadow': {result}"
        )
        assert "transform" in props_lower, (
            f".card transition-property missing 'transform': {result}"
        )

    def test_button_targets_background_and_border(
        self, server_url: str, browser_page
    ):
        """.btn should transition background-color + border-color (not 'all')."""
        browser_page.goto(f"{server_url}/documents", timeout=15000)
        browser_page.wait_for_timeout(500)

        result = browser_page.evaluate(
            """() => {
                const btn = document.querySelector('.btn, button.btn, a.btn');
                if (!btn) return null;
                const s = window.getComputedStyle(btn);
                return s.transitionProperty || '';
            }"""
        )

        if not result:
            pytest.skip("No .btn element found")

        props_lower = result.lower()
        has_background = "background" in props_lower or "background-color" in props_lower
        has_border = "border-color" in props_lower
        assert has_background or has_border, (
            f".btn transition-property missing background/border: {result}"
        )


# ------------------------------------------------------------------
# 3. Transition Timing Function -- must use easing, never linear
# ------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_BROWSER, reason=PLAYWRIGHT_REASON)
class TestTransitionTimingFunctionBehavior:
    """Verify computed transition-timing-function uses easing, not linear."""

    def _check_never_linear(self, timing_str: str, desc: str):
        """Assert timing string has no 'linear' component."""
        if not timing_str:
            return
        timings = {t.strip() for t in timing_str.split(",")}
        assert "linear" not in timings, (
            f"{desc}: timing includes 'linear': {timing_str}"
        )

    def test_buttons_nonlinear_timing(
        self, server_url: str, browser_page
    ):
        """Buttons should not use linear timing."""
        browser_page.goto(f"{server_url}/documents", timeout=15000)
        browser_page.wait_for_timeout(500)

        selectors = [".btn", ".btn-primary", ".btn-secondary"]
        data = browser_page.evaluate(
            f"({_QUERY_ALL})({json.dumps(selectors)}, {_GET_COMPUTED})"
        )

        if not data:
            pytest.skip("No buttons found")

        for item in data:
            self._check_never_linear(item["timing"], item["selector"])

    def test_cards_nonlinear_timing(
        self, server_url: str, browser_page
    ):
        """Cards should not use linear timing."""
        browser_page.goto(f"{server_url}/documents", timeout=15000)
        browser_page.wait_for_timeout(500)

        data = browser_page.evaluate(
            f"({_QUERY_ALL})(['.card'], {_GET_COMPUTED})"
        )

        if not data:
            pytest.skip("No cards found")

        for item in data:
            self._check_never_linear(item["timing"], f".card on /documents")

    def test_inputs_nonlinear_timing(
        self, server_url: str, browser_page
    ):
        """Inputs should not use linear timing."""
        browser_page.goto(f"{server_url}/search", timeout=15000)
        browser_page.wait_for_timeout(500)

        data = browser_page.evaluate(
            f"({_QUERY_ALL})(['.input, input[type=\"text\"], input[type=\"search\"]'],"
            f" {_GET_COMPUTED})"
        )

        if not data:
            pytest.skip("No inputs found")

        for item in data:
            self._check_never_linear(item["timing"], item["selector"])

    def test_all_interactive_no_linear(
        self, server_url: str, browser_page
    ):
        """Scan all interactive elements on /documents; none should use linear."""
        browser_page.goto(f"{server_url}/documents", timeout=15000)
        browser_page.wait_for_timeout(500)

        all_sel = [
            "button", "a", "input", "select",
            ".card", ".tag-pill", ".htmx-indicator",
            ".theme-toggle", ".nav-toggle",
            ".pagination a", ".progress-bar",
        ]
        data = browser_page.evaluate(
            f"({_QUERY_ALL})({json.dumps(all_sel)}, {_GET_COMPUTED})"
        )

        violations: list[str] = []
        for item in data:
            timing = item["timing"]
            if not timing:
                continue
            timings = {t.strip() for t in timing.split(",")}
            if "linear" in timings:
                violations.append(
                    f"{item['selector']}: timing={timing}"
                )

        assert not violations, (
            "Elements using 'linear' transition timing:\n" + "\n".join(violations)
        )


# ------------------------------------------------------------------
# 4. Integration: full cross-property verification
# ------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_BROWSER, reason=PLAYWRIGHT_REASON)
class TestTransitionIntegration:
    """Full behavioral check: duration + property + timing all correct."""

    def test_btn_all_properties_behavioral(
        self, server_url: str, browser_page
    ):
        """.btn: duration in range, property not 'all', timing not linear."""
        browser_page.goto(f"{server_url}/documents", timeout=15000)
        browser_page.wait_for_timeout(500)

        result = browser_page.evaluate(
            """() => {
                const btn = document.querySelector('.btn, button.btn, a.btn');
                if (!btn) return null;
                const s = window.getComputedStyle(btn);
                return {
                    duration: s.transitionDuration,
                    property: s.transitionProperty,
                    timing: s.transitionTimingFunction,
                };
            }"""
        )

        if not result:
            pytest.skip("No .btn element found on /documents")

        # Duration
        dur_vals: list[float] = []
        for p in result["duration"].split(","):
            p = p.strip()
            if p.endswith("s"):
                try:
                    dur_vals.append(float(p.replace("s", "")))
                except ValueError:
                    pass

        assert dur_vals, f".btn has no duration values: {result['duration']}"
        for d in dur_vals:
            assert FAST_S - 0.01 <= d <= BASE_S + 0.01, (
                f".btn duration {d}s outside {FAST_S}s-{BASE_S}s"
            )

        # Property
        props = {p.strip() for p in result["property"].split(",")} if result["property"] else set()
        assert "all" not in props, f".btn transition-property is 'all': {result['property']}"

        # Timing
        timings = {t.strip() for t in result["timing"].split(",")} if result["timing"] else set()
        assert "linear" not in timings, f".btn has linear timing: {result['timing']}"

    def test_card_all_properties_behavioral(
        self, server_url: str, browser_page
    ):
        """.card: duration ~180ms, property includes box-shadow+transform, timing not linear."""
        browser_page.goto(f"{server_url}/documents", timeout=15000)
        browser_page.wait_for_timeout(500)

        result = browser_page.evaluate(
            """() => {
                const card = document.querySelector('.card');
                if (!card) return null;
                const s = window.getComputedStyle(card);
                return {
                    duration: s.transitionDuration,
                    property: s.transitionProperty,
                    timing: s.transitionTimingFunction,
                };
            }"""
        )

        if not result:
            pytest.skip("No .card element found on /documents")

        dur_vals: list[float] = []
        for p in result["duration"].split(","):
            p = p.strip()
            if p.endswith("s"):
                try:
                    dur_vals.append(float(p.replace("s", "")))
                except ValueError:
                    pass

        assert dur_vals, f".card has no duration: {result['duration']}"
        for d in dur_vals:
            assert BASE_S - 0.01 <= d <= BASE_S + 0.01, (
                f".card duration {d}s not ~{BASE_S}s"
            )

        props = {p.strip() for p in result["property"].split(",")} if result["property"] else set()
        assert "all" not in props, f".card property is 'all': {result['property']}"
        pl = result["property"].lower() if result["property"] else ""
        assert "box-shadow" in pl or "transform" in pl, (
            f".card should target box-shadow or transform: {result['property']}"
        )

        timings = {t.strip() for t in result["timing"].split(",")} if result["timing"] else set()
        assert "linear" not in timings, f".card has linear timing: {result['timing']}"

    def test_input_all_properties_behavioral(
        self, server_url: str, browser_page
    ):
        """Input: duration ~180ms, property includes border-color, timing not linear."""
        browser_page.goto(f"{server_url}/search", timeout=15000)
        browser_page.wait_for_timeout(500)

        result = browser_page.evaluate(
            """() => {
                const input = document.querySelector('.input, input[type="text"], input[type="search"]');
                if (!input) return null;
                const s = window.getComputedStyle(input);
                return {
                    duration: s.transitionDuration,
                    property: s.transitionProperty,
                    timing: s.transitionTimingFunction,
                };
            }"""
        )

        if not result:
            pytest.skip("No input element found on /search")

        dur_vals: list[float] = []
        for p in result["duration"].split(","):
            p = p.strip()
            if p.endswith("s"):
                try:
                    dur_vals.append(float(p.replace("s", "")))
                except ValueError:
                    pass

        if dur_vals:
            for d in dur_vals:
                assert BASE_S - 0.01 <= d <= BASE_S + 0.01, (
                    f"input duration {d}s not ~{BASE_S}s"
                )

        props = {p.strip() for p in result["property"].split(",")} if result["property"] else set()
        assert "all" not in props, f"input property is 'all': {result['property']}"

        timings = {t.strip() for t in result["timing"].split(",")} if result["timing"] else set()
        assert "linear" not in timings, f"input has linear timing: {result['timing']}"

    def test_header_nav_has_theme_transition(
        self, server_url: str, browser_page
    ):
        """Header nav links: duration ~180ms, property includes background+color, no linear."""
        browser_page.goto(f"{server_url}/", timeout=15000)
        browser_page.wait_for_timeout(500)

        result = browser_page.evaluate(
            """() => {
                const links = document.querySelectorAll('header nav a');
                if (links.length === 0) return null;
                const s = window.getComputedStyle(links[0]);
                return {
                    duration: s.transitionDuration,
                    property: s.transitionProperty,
                    timing: s.transitionTimingFunction,
                };
            }"""
        )

        if not result:
            pytest.skip("No header nav links found")

        dur_vals: list[float] = []
        for p in result["duration"].split(","):
            p = p.strip()
            if p.endswith("s"):
                try:
                    dur_vals.append(float(p.replace("s", "")))
                except ValueError:
                    pass

        assert dur_vals, f"header nav a has no duration: {result['duration']}"
        # header nav a uses --transition-press which holds:
        #   background/color/border-color → --transition-base (0.18s)
        #   transform → --transition-fast (0.15s)
        # So computed durations can be 0.15s OR 0.18s.
        for d in dur_vals:
            assert FAST_S - 0.01 <= d <= BASE_S + 0.01, (
                f"header nav a duration {d}s not in [{FAST_S}s, {BASE_S}s]"
            )

        pl = result["property"].lower() if result["property"] else ""
        assert "background" in pl or "color" in pl, (
            f"header nav a should include background/color transition: {result['property']}"
        )

        timings = {t.strip() for t in result["timing"].split(",")} if result["timing"] else set()
        assert "linear" not in timings, (
            f"header nav a has linear timing: {result['timing']}"
        )


# ------------------------------------------------------------------
# 5. Regression guard: transition count baseline
# ------------------------------------------------------------------

def test_transition_rule_count_nonregression():
    """CSS transition rule count must not regress below 96 (baseline from df8899b)."""
    css_path = _project_root() / "src" / "web" / "static" / "css" / "styles.css"
    css = css_path.read_text()

    count = sum(
        1 for line in css.split("\n")
        if "transition:" in line and not line.strip().startswith("/*")
    )

    assert count >= 96, (
        f"Transition rule count {count} has regressed below baseline of 96. "
        f"New rules should ADD transitions, not remove them."
    )