"""
Responsive design validation for DocMind.
Tests all 4 breakpoints (1024/768/640/480px), hamburger nav, and prefers-reduced-motion.

These are integration tests that require:
  - Playwright (pip install playwright && playwright install chromium)
  - A running DocMind server at localhost:8080

When Playwright is not installed, the entire module is skipped via
pytest.importorskip — this prevents the collection-time ImportError that
would otherwise block the full test suite.
"""
import json
import socket
import sys

import pytest

# Guard the playwright import — if playwright is not installed, skip the
# entire module instead of raising ImportError at collection time.
playwright = pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.slow]

BASE_URL = "http://localhost:8080"
PAGES = ["/", "/search", "/documents", "/analytics", "/upload"]
BREAKPOINTS = [1024, 768, 640, 480]


def _server_available(host="localhost", port=8080, timeout=1.0):
    """Return True if a TCP connection to host:port succeeds within timeout."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, OSError):
        return False


def check_page_at_width(page, url, width, reduced_motion=False):
    """Navigate to a page at a given viewport width and check for layout issues."""
    page.set_viewport_size({"width": width, "height": 800})
    if reduced_motion:
        page.emulate_media(reduced_motion="reduce")
    else:
        page.emulate_media(reduced_motion=None)
    page.goto(f"{BASE_URL}{url}")
    page.wait_for_load_state("networkidle")

    results = {
        "url": url,
        "width": width,
        "reduced_motion": reduced_motion,
        "issues": []
    }

    # 1. Check for page-level horizontal overflow (the real check)
    scroll_width = page.evaluate("document.documentElement.scrollWidth")
    client_width = page.evaluate("document.documentElement.clientWidth")
    if scroll_width > client_width + 2:  # 2px tolerance
        results["issues"].append(
            f"Page-level horizontal overflow: scrollWidth={scroll_width} > clientWidth={client_width} (overflow={scroll_width - client_width}px)"
        )

    # 2. Check for elements OUTSIDE scroll containers that overflow viewport
    overflowing_elements = page.evaluate("""() => {
        const vw = document.documentElement.clientWidth;
        const overflow = [];
        for (const el of document.querySelectorAll('div, section, header, footer, nav, main, form')) {
            let parent = el.parentElement;
            let inScrollContainer = false;
            while (parent && parent !== document.body) {
                const style = window.getComputedStyle(parent);
                if ((style.overflowX === 'auto' || style.overflowX === 'scroll') && parent.classList.contains('table-scroll')) {
                    inScrollContainer = true;
                    break;
                }
                parent = parent.parentElement;
            }
            if (inScrollContainer) continue;

            const rect = el.getBoundingClientRect();
            if (rect.right > vw + 2 && rect.width > 10 && rect.width < vw * 2) {
                overflow.push({
                    tag: el.tagName,
                    class: el.className.toString().slice(0, 50),
                    rect_right: Math.round(rect.right),
                    width: Math.round(rect.width)
                });
            }
        }
        return overflow.slice(0, 5);
    }""")
    if overflowing_elements:
        results["issues"].append(f"Elements overflowing viewport (outside scroll containers): {json.dumps(overflowing_elements)}")

    # 3. Check for zero-height or negative-width elements (collapsed layouts)
    collapsed = page.evaluate("""() => {
        const issues = [];
        document.querySelectorAll('.container, .card, .stats, .analytics-grid, .search-box, .chat-layout, .viewer-layout').forEach(el => {
            const rect = el.getBoundingClientRect();
            if (rect.height < 1) {
                issues.push({tag: el.tagName, class: el.className.slice(0, 50), height: rect.height});
            }
        });
        return issues;
    }""")
    if collapsed:
        results["issues"].append(f"Collapsed elements: {json.dumps(collapsed)}")

    # 4. Check hamburger nav at <= 768px
    if width <= 768:
        nav_toggle = page.query_selector(".nav-toggle")
        if nav_toggle:
            toggle_display = page.evaluate("""() => {
                const el = document.querySelector('.nav-toggle');
                return window.getComputedStyle(el).display;
            }""")
            if toggle_display != "block":
                results["issues"].append(f"Nav toggle not visible (display={toggle_display}) at {width}px")

            # Check nav is hidden by default
            nav_display = page.evaluate("""() => {
                const el = document.querySelector('header nav');
                return window.getComputedStyle(el).display;
            }""")
            if nav_display != "none":
                results["issues"].append(f"Nav should be hidden by default at {width}px (display={nav_display})")

            # Click hamburger and check nav opens
            nav_toggle.click()
            page.wait_for_timeout(300)
            nav_display_after = page.evaluate("""() => {
                const el = document.querySelector('header nav');
                return window.getComputedStyle(el).display;
            }""")
            if nav_display_after != "flex":
                results["issues"].append(f"Nav should be visible after toggle click at {width}px (display={nav_display_after})")

            # Click again to close
            nav_toggle.click()
            page.wait_for_timeout(300)
            nav_display_closed = page.evaluate("""() => {
                const el = document.querySelector('header nav');
                return window.getComputedStyle(el).display;
            }""")
            if nav_display_closed != "none":
                results["issues"].append(f"Nav should hide after second toggle click at {width}px (display={nav_display_closed})")
        else:
            results["issues"].append(f"No .nav-toggle button found at {width}px")

    # 5. At > 768px, nav should be visible and hamburger hidden
    if width > 768:
        nav_toggle = page.query_selector(".nav-toggle")
        if nav_toggle:
            toggle_display = page.evaluate("""() => {
                const el = document.querySelector('.nav-toggle');
                return window.getComputedStyle(el).display;
            }""")
            if toggle_display != "none":
                results["issues"].append(f"Nav toggle should be hidden at {width}px (display={toggle_display})")

        nav_display = page.evaluate("""() => {
            const el = document.querySelector('header nav');
            return window.getComputedStyle(el).display;
        }""")
        if nav_display == "none":
            results["issues"].append(f"Nav should be visible at {width}px (display={nav_display})")

    # 6. Check prefers-reduced-motion
    if reduced_motion:
        transition_duration = page.evaluate("""() => {
            const el = document.querySelector('.card') || document.querySelector('a') || document.body;
            return window.getComputedStyle(el).transitionDuration;
        }""")
        try:
            val_str = transition_duration.strip()
            if val_str.endswith('ms'):
                ms = float(val_str[:-2])
            elif val_str.endswith('s'):
                ms = float(val_str[:-1]) * 1000
            else:
                ms = float(val_str)
            if ms > 0.1:  # Allow up to 0.1ms for rounding
                results["issues"].append(
                    f"Reduced motion not applied: transitionDuration={transition_duration} ({ms}ms, expected <=0.1ms)"
                )
        except (ValueError, TypeError):
            results["issues"].append(
                f"Could not parse transitionDuration={transition_duration} for reduced-motion check"
            )

    return results


# ---------------------------------------------------------------------------
# Pytest test functions — parametrized over (page, breakpoint)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _skip_if_no_server():
    """Skip all tests in this module if no DocMind server is running at localhost:8080."""
    if not _server_available():
        pytest.skip("DocMind server not running at localhost:8080 — run 'python -m src.main' first")


@pytest.fixture(scope="module")
def browser_context():
    """Launch a headless Chromium browser for the test module."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        yield page
        context.close()
        browser.close()


@pytest.mark.parametrize("url", PAGES)
@pytest.mark.parametrize("width", BREAKPOINTS)
def test_no_layout_issues_at_breakpoint(browser_context, url, width):
    """No horizontal overflow, collapsed elements, or overflowing elements at any breakpoint."""
    result = check_page_at_width(browser_context, url, width, reduced_motion=False)
    assert not result["issues"], (
        f"Layout issues at {url} @ {width}px:\n  - " + "\n  - ".join(result["issues"])
    )


@pytest.mark.parametrize("url", PAGES)
def test_hamburger_nav_at_mobile_width(browser_context, url):
    """Hamburger nav toggles correctly at 768px."""
    result = check_page_at_width(browser_context, url, 768, reduced_motion=False)
    # Filter for hamburger-related issues only
    nav_issues = [i for i in result["issues"] if "Nav" in i or "nav-toggle" in i]
    assert not nav_issues, (
        f"Hamburger nav issues at {url} @ 768px:\n  - " + "\n  - ".join(nav_issues)
    )


@pytest.mark.parametrize("url", PAGES)
def test_prefers_reduced_motion(browser_context, url):
    """prefers-reduced-motion disables transitions."""
    result = check_page_at_width(browser_context, url, 768, reduced_motion=True)
    motion_issues = [i for i in result["issues"] if "Reduced motion" in i or "transitionDuration" in i]
    assert not motion_issues, (
        f"Reduced-motion issues at {url} @ 768px:\n  - " + "\n  - ".join(motion_issues)
    )


# ---------------------------------------------------------------------------
# Standalone script entry point (python -m tests.test_responsive_layout)
# ---------------------------------------------------------------------------

def main():
    """Run all responsive checks as a standalone script (no pytest needed)."""
    all_results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        for url in PAGES:
            for width in BREAKPOINTS:
                try:
                    result = check_page_at_width(page, url, width, reduced_motion=False)
                    all_results.append(result)
                    if result["issues"]:
                        print(f"FAIL: {url} @ {width}px")
                        for issue in result["issues"]:
                            print(f"  - {issue}")
                    else:
                        print(f"PASS: {url} @ {width}px")
                except Exception as e:
                    print(f"ERROR: {url} @ {width}px — {e}")
                    all_results.append({"url": url, "width": width, "issues": [str(e)]})

            # Also test with reduced motion at 768px
            try:
                result = check_page_at_width(page, url, 768, reduced_motion=True)
                all_results.append(result)
                if result["issues"]:
                    print(f"FAIL: {url} @ 768px (reduced-motion)")
                    for issue in result["issues"]:
                        print(f"  - {issue}")
                else:
                    print(f"PASS: {url} @ 768px (reduced-motion)")
            except Exception as e:
                print(f"ERROR: {url} @ 768px (reduced-motion) — {e}")

        browser.close()

    # Summary
    total = len(all_results)
    passed = sum(1 for r in all_results if not r["issues"])
    failed = total - passed
    print(f"\n{'='*60}")
    print(f"Results: {passed}/{total} passed, {failed} failed")

    if failed > 0:
        print(f"\nFailed tests:")
        for r in all_results:
            if r["issues"]:
                rm = " (reduced-motion)" if r.get("reduced_motion") else ""
                print(f"  {r['url']} @ {r['width']}px{rm}:")
                for issue in r["issues"]:
                    print(f"    - {issue}")
        sys.exit(1)
    else:
        print("All responsive design checks passed!")


if __name__ == "__main__":
    main()
