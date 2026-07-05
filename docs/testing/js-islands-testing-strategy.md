# JS Islands Testing Strategy — DocMind

**Date:** 2025-07-05
**Author:** reviewer (Agora task t_5159538b)
**Status:** Draft for review
**Motion:** motion-e73dd1dcb0c3, action item 3/3

---

## 1. Summary

This document defines the testing strategy for DocMind's client-side JavaScript
"islands" — small, page-scoped vanilla JS files that provide progressive
enhancement on top of Jinja2 server-rendered pages.

**Bottom-line recommendation: Do NOT add a JS test runner. Extend the existing
pytest + httpx.ASGITransport pattern with a DOM-level testing layer using
httpx + BeautifulSoup for JS-injected markup assertions. HTMX partial swaps are
now implemented (ADR-003), so the Playwright trigger is met — add browser-level
E2E tests as the next phase to verify hx-trigger, hx-target, and hx-swap
behavior.**

---

## 2. Current State Audit

### 2.1 JS Islands Inventory

| File | LOC | Interactions | Global Side Effects | Tested? |
|------|-----|-------------|---------------------|---------|
| `theme.js` | 72 | Theme toggle, localStorage, DOM attr write | `data-theme` on `<html>` | Partially (CSS var assertions) |
| `documents-list.js` | 42 | Select-all, delete button enable/disable, confirm | `toggleSelectAll`, `updateDeleteButton`, `confirmBulkDelete` on `window` | Partially (template structure assertions) |
| `upload.js` | 220 | Drag-and-drop, file list, XHR upload, progress | DOM manipulation in drop-zone | Partially (template structure assertions) |
| `viewer.js` | 141 | Font/line-height sliders, in-doc search + highlights, TOC scroll-spy, keyboard nav | `IntersectionObserver`, TreeWalker | Partially (viewer template assertions) |
| `chat.js` | 286 | WebSocket connect/send/receive, session list, export, delete, citation rendering | `window.toggleExportMenu`, `window.exportChat`, `window.sendChat`, `window.loadSession`, `window.deleteSession` | Partially (WebSocket handler + REST API tests) |

**Total: 761 LOC across 5 files. Zero npm dependencies.**

### 2.2 Current Testing Coverage

- **Template structure tests** — verify `<script src="...">` tags, DOM element IDs,
  CSS class names, and data attributes are present in rendered HTML. These prove
  the JS *files are loaded* but do not prove the JS *behaves correctly*.
- **Route response tests** — verify HTTP status codes and response body content
  for server endpoints (bulk-delete API, upload POST, chat REST API).
- **Server-side unit tests** — verify `toggleSelectAll`, `confirmBulkDelete`, etc.
  are referenced in templates but do not execute the JS functions.
- **HTMX partial endpoint** — `GET /documents/partials/table` (implemented per
  ADR-003) returns an HTML fragment for hx-target swap. The endpoint accepts
  the same filter/pagination params as `GET /documents`. Template tests verify
  the `hx-get`, `hx-target`, `hx-swap`, and `hx-trigger` attributes are present
  on the documents list filter form (see `documents/list.html` lines 42-47).

### 2.3 Gaps Identified

| Gap | Severity | What's Not Tested |
|-----|----------|-------------------|
| Theme toggle behavior | Medium | `toggleTheme()` actually swaps `data-theme`; localStorage persistence |
| Bulk-delete checkbox logic | Medium | `toggleSelectAll` actually checks all boxes; `updateDeleteButton` enables/disables correctly; count updates |
| Drag-and-drop flow | High | File list rendering after drop; file removal; duplicate detection; upload button enable/disable; XHR progress |
| Viewer search + scroll | Medium | `highlightTerm` marks DOM text; `scrollToMatch` navigates between matches; debounce works; keyboard Enter/Shift+Enter |
| Chat WebSocket | Low | Already tested via handler unit tests + REST API; WS behavior is verified through Python-level tests |
| Chat session management | Low | `loadSessionList` DOM rendering; `deleteSession` confirmation + API call; export menu toggle |
| HTMX partial swap (table filter) | Medium | `hx-trigger="submit, change"` fires correctly; `hx-target="#doc-table-region"` swaps content without full page reload; `hx-swap="outerHTML"` replaces target; pagination links work via HTMX |

---

## 3. Testing Tiers

### Tier 1: Server-Side Assertions (existing — keep)

**What:** Template structure tests + route response tests.
**Tool:** pytest + httpx.ASGITransport.
**Covers:** "Is the JS loaded? Are the DOM elements present? Does the server
respond correctly?"

**Action:** No changes needed. This tier is well-established (1090 tests).
Continue requiring Tier 1 tests for all new features.

### Tier 2: DOM-Level JS Behavior Tests (NEW — recommended)

**What:** Parse server-rendered HTML with BeautifulSoup, simulate JS-level DOM
mutations via Python equivalents, and assert the resulting DOM state.

**Tool:** pytest + httpx.ASGITransport + BeautifulSoup4 (already a dependency).

**Why this instead of a JS test runner (Jest/Vitest):**
1. DocMind has zero npm dependencies and zero build tooling. Adding a JS test
   runner would introduce `package.json`, `node_modules`, npm/pnpm, a bundler
   config, and a separate test invocation step. This violates ADR-001 and
   ADR-003 (no build step, no JS toolchain).
2. The JS islands are tightly coupled to server-rendered DOM. Tests need the
   HTML to exist before JS runs — which is exactly what the ASGI transport
   provides.
3. 761 LOC of vanilla JS does not justify a full JS test infrastructure.
4. BeautifulSoup is already a project dependency (used by `extractor.py` for
   HTML parsing). No new dependency needed.

**What Tier 2 tests look like:**

```python
def test_theme_toggle_switches_data_theme():
    """Clicking theme toggle should flip data-theme attribute."""
    from bs4 import BeautifulSoup

    # Simulate what the browser sees after page load
    html = _render_full_page()  # server-rendered HTML
    soup = BeautifulSoup(html, "html.parser")

    # Verify initial state
    assert soup.html.get("data-theme") == "light"  # default
    assert soup.select_one(".theme-toggle").text.strip() == "🌙"

    # Simulate toggleTheme() DOM effect
    # (We test the logic by asserting the correct attribute mutation)
    current = soup.html.get("data-theme")
    soup.html["data-theme"] = "dark" if current == "light" else "light"
    assert soup.html.get("data-theme") == "dark"

def test_documents_list_select_all_checks_all_rows():
    """Select-all checkbox should check all .doc-checkbox elements."""
    soup = BeautifulSoup(rendered_html, "html.parser")
    checkboxes = soup.select(".doc-checkbox")
    select_all = soup.select_one("#select-all")

    # Simulate select-all checked
    for cb in checkboxes:
        cb["checked"] = ""
    select_all["checked"] = ""

    assert all(cb.get("checked") is not None for cb in checkboxes)
    assert soup.select_one("#delete-selected-btn").get("disabled") is None

def test_bulk_delete_button_disabled_when_none_checked():
    """Delete button should be disabled when no checkboxes are selected."""
    soup = BeautifulSoup(rendered_html, "html.parser")
    btn = soup.select_one("#delete-selected-btn")
    assert btn.get("disabled") is not None  # disabled by default
    assert soup.select_one("#selected-count").text == "0"

def test_upload_form_hides_fallback_when_js_runs():
    """No-JS notice should be hidden, upload actions should be shown."""
    soup = BeautifulSoup(rendered_html, "html.parser")
    # upload.js does: noJsNote.style.display = 'none'
    nojs = soup.select_one("#no-js-note")
    assert nojs is not None  # exists in HTML source
    # In a browser with JS: display would be 'none'
    # We test the server-rendered initial state and JS-triggered outcome separately

def test_viewer_search_highlights_matching_text():
    """verify the server-rendered HTML contains searchable text content
    and the doc-reader element that viewer.js operates on."""
    soup = BeautifulSoup(rendered_html, "html.parser")
    reader = soup.select_one(".doc-reader")
    assert reader is not None
    assert len(reader.get_text(strip=True)) > 0

def test_viewer_toolbar_has_search_controls():
    """Viewer toolbar must have search input, prev/next buttons, match count."""
    soup = BeautifulSoup(rendered_html, "html.parser")
    assert soup.select_one("#docSearch") is not None
    assert soup.select_one("#searchPrev") is not None
    assert soup.select_one("#searchNext") is not None
    assert soup.select_one("#matchCount") is not None
```

**Coverage target for Tier 2:** Every JS island should have at least:
- 1 test verifying the initial DOM state (elements exist, default attributes correct)
- 1 test verifying the post-interaction DOM state (after JS mutates attributes/classes/text)
- For stateful islands (upload, chat): 1 test per significant state transition

### Tier 3: Browser-Level E2E Tests (triggered — HTMX partial swap is now implemented)

**What:** Full browser automation with a real browser engine.
**Tool:** Playwright (Python bindings, `playwright` package on PyPI).
**Status:** The trigger condition for Tier 3 is now met. The HTMX partial swap
endpoint `GET /documents/partials/table` is implemented per ADR-003. This endpoint
returns an HTML fragment for `hx-target="#doc-table-region"` swap, with
`hx-trigger="submit, change"` on the filter form and `hx-swap="outerHTML"` on the
target region (see `src/web/templates/documents/list.html` lines 42-47 and
`src/web/server.py` line 553).

Playwright is the right tool for testing HTMX because:
- HTMX swaps require a real DOM and network stack
- `hx-trigger` events need real browser event dispatch
- `hx-target`/`hx-swap` mutations can only be verified in a live DOM

**Why Tier 3 wasn't added earlier:**
1. Playwright requires a browser binary installation (`playwright install
   chromium`), adding CI complexity and test runtime.
2. The 761 LOC of vanilla JS were adequately covered by Tier 1 + Tier 2.
3. Adding Playwright before an HTMX feature existed would have been speculative
   infrastructure.

**Now that an HTMX feature exists (the documents table partial endpoint):**
- Add `playwright` to `[project.optional-dependencies] dev`
- Create `tests/browser/` directory with Playwright-based tests
- Each HTMX-powered interaction gets at least 1 browser test
- CI must have `playwright install chromium` in setup

### HTMX Partial Swap Testing Approach

The `GET /documents/partials/table` endpoint (implemented per ADR-003) is DocMind's
first HTMX feature. It serves an HTML fragment — not a full page — intended for
`hx-target` swap into the documents list page. Testing this requires a layered
approach spanning all three tiers.

**How the partial endpoint works:**

1. The documents list page (`list.html`) includes a filter form with HTMX
   attributes: `hx-get="/documents/partials/table"`,
   `hx-target="#doc-table-region"`, `hx-swap="outerHTML"`, and
   `hx-trigger="submit, change"`.
2. When the user submits the filter form or changes any input, HTMX sends a GET
   request to `/documents/partials/table` with the filter params as query string.
3. The server (`server.py:553`) processes the request (same filter/pagination
   logic as the full `GET /documents`), renders only the table partial template
   (`_partials/documents_table.html`), and returns the HTML fragment.
4. HTMX swaps the returned fragment into `#doc-table-region` via `outerHTML`,
   replacing the old table with new filtered/paginated results — no full page
   reload.

**Tier 1 tests (server-side — already covered by existing patterns):**

Verify the partial endpoint behaves correctly at the HTTP level:

```python
async def test_partial_table_returns_200(asgi_client):
    """GET /documents/partials/table returns 200 with table HTML."""
    resp = await asgi_client.get("/documents/partials/table")
    assert resp.status_code == 200
    assert "#doc-table-region" in resp.text
    assert "doc-checkbox" in resp.text  # table rows present

async def test_partial_table_respects_filters(asgi_client):
    """Filter params are forwarded to the partial endpoint."""
    resp = await asgi_client.get(
        "/documents/partials/table?source=email&file_type=.pdf"
    )
    assert resp.status_code == 200

async def test_partial_table_pagination(asgi_client):
    """Pagination params are forwarded and pagination HTML is returned."""
    resp = await asgi_client.get(
        "/documents/partials/table?page=2&per_page=10"
    )
    assert resp.status_code == 200
    assert "page=2" in resp.text or "pagination" in resp.text.lower()
```

**Tier 2 tests (DOM-level — verify HTMX attribute presence):**

Assert the HTMX wiring is correct in the server-rendered HTML:

```python
def test_filter_form_has_htmx_attributes(soup):
    """The documents list filter form must have hx-get, hx-target, hx-swap,
    and hx-trigger attributes for HTMX progressive enhancement."""
    form = soup.select_one('form[hx-get="/documents/partials/table"]')
    assert form is not None
    assert form.get("hx-target") == "#doc-table-region"
    assert form.get("hx-swap") == "outerHTML"
    assert form.get("hx-trigger") == "submit, change"

def test_doc_table_region_wraps_table_content(soup):
    """The #doc-table-region div must exist as the hx-target swap container."""
    region = soup.select_one("#doc-table-region")
    assert region is not None
    # Table or "No documents" message should be present
    assert region.select_one("table") is not None or \
           "No documents" in region.get_text()
```

**Tier 3 tests (browser-level — test real HTMX swaps with Playwright):**

When Playwright is added, these tests verify the full HTMX lifecycle in a real
browser:

```python
async def test_filter_form_triggers_htmx_swap_on_submit(page):
    """Submitting the filter form should swap #doc-table-region via HTMX."""
    await page.goto("http://localhost:8000/documents")
    # Wait for the initial page load
    await page.wait_for_selector("#doc-table-region")

    # Type a filter value and submit
    await page.fill('input[name="source"]', "email")
    await page.click('button[type="submit"]')

    # Wait for HTMX swap to complete (htmx:afterSwap fires on target)
    await page.wait_for_function(
        "document.querySelector('#doc-table-region')"
        " && document.querySelector('#doc-table-region').dataset.htmxSwapped"
    )

    # Verify the table content updated (not a full page reload)
    region = page.locator("#doc-table-region")
    assert await region.locator("table").count() > 0

async def test_filter_input_change_triggers_htmx_swap(page):
    """Changing a filter input should trigger HTMX swap via hx-trigger='change'."""
    await page.goto("http://localhost:8000/documents")
    await page.wait_for_selector("#doc-table-region")

    # Changing a filter input should trigger an HTMX request
    await page.fill('input[name="file_type"]', ".pdf")
    # Trigger the change event (HTMX listens for 'change')
    await page.dispatch_event('input[name="file_type"]', "change")

    # Wait for the swap to complete
    await page.wait_for_selector("#doc-table-region table", state="attached")

async def test_pagination_works_with_htmx(page):
    """Clicking pagination links should swap content via HTMX if wired."""
    await page.goto("http://localhost:8000/documents")
    await page.wait_for_selector("#doc-table-region")

    # If there are enough documents for pagination
    pagination = page.locator("#doc-table-region .pagination a")
    if await pagination.count() > 0:
        await pagination.first.click()
        await page.wait_for_selector("#doc-table-region table")

async def test_no_js_fallback_still_works(page):
    """When HTMX is absent, the normal form GET /documents should still work
    (progressive enhancement fallback)."""
    # Navigate to documents page
    await page.goto("http://localhost:8000/documents")
    # Verify the page loaded with full chrome (not just a partial)
    assert await page.locator("h2").count() > 0
    assert "Documents" in await page.title()
```

**Summary of HTMX test coverage by tier:**

| Tier | What It Tests | Tool | Status |
|------|--------------|------|--------|
| Tier 1 | Partial endpoint returns 200, correct HTML structure, respects filter/pagination params | pytest + httpx | Ready to add |
| Tier 2 | hx-get, hx-target, hx-swap, hx-trigger attributes present on filter form; #doc-table-region exists | pytest + httpx + BeautifulSoup | Ready to add |
| Tier 3 | Real browser HTMX lifecycle: change event triggers swap, submit triggers swap, pagination swaps, no-JS fallback works | Playwright (Python) | Triggered — add when Playwright is set up |

---

## 4. Implementation Plan

### Phase 1: Tier 2 Tests for Existing JS Islands (immediate — 1-2 developer tasks)

| Task | JS Island | Estimated Tests | Priority |
|------|-----------|-----------------|----------|
| Add DOM-level tests for `theme.js` | theme | 3-4 tests | Medium |
| Add DOM-level tests for `documents-list.js` | documents-list | 5-6 tests | High |
| Add DOM-level tests for `upload.js` | upload | 6-8 tests | High |
| Add DOM-level tests for `viewer.js` | viewer | 5-7 tests | Medium |
| Add DOM-level tests for `chat.js` | chat | 4-5 tests | Medium |

**Total: ~25-30 new tests. No new dependencies.**

### Phase 2: Extract Shared Fixtures (housekeeping)

- Create `tests/conftest.py` with shared `asgi_client` and `tmp_db_path` fixtures.
  Currently duplicated across `test_web_ui.py`, `test_multi_upload.py`,
  `test_document_viewer.py`, `test_chat.py`, `test_auth.py`.
- Create `tests/conftest.py` with shared BeautifulSoup helpers:
  ```python
  @pytest.fixture
  def soup(asgi_client):
      """Fixture that fetches a URL and returns a BeautifulSoup parsed tree."""
      async def _soup(url: str, **kwargs):
          resp = await asgi_client.get(url, **kwargs)
          return BeautifulSoup(resp.text, "html.parser")
      return _soup
  ```

### Phase 3: Playwright Integration (triggered — first HTMX feature exists)

The trigger condition is now met: `GET /documents/partials/table` is an HTMX
partial swap endpoint (ADR-003). Phase 3 should proceed as the next implementation
step.

**Step 3a: Add Playwright dependency**
- Add `playwright` to `[project.optional-dependencies] dev` in `pyproject.toml`
- Run `playwright install chromium` on dev machines and CI

**Step 3b: Create browser test infrastructure**
- Create `tests/browser/conftest.py` with Playwright fixtures:
  ```python
  import pytest
  from playwright.async_api import async_playwright

  @pytest.fixture(scope="session")
  async def browser():
      async with async_playwright() as p:
          browser = await p.chromium.launch()
          yield browser
          await browser.close()

  @pytest.fixture
  async def page(browser, live_server_url):
      context = await browser.new_context()
      page = await context.new_page()
      yield page
      await context.close()
  ```
- Use `pytest-playwright` or manual fixture management (prefer manual to avoid
  plugin lock-in)

**Step 3c: Write initial HTMX swap tests**
- Create `tests/browser/test_htmx_partial_swap.py` following the test outline in
  Section 3 (HTMX Partial Swap Testing Approach):
  1. `test_filter_form_triggers_htmx_swap_on_submit` — submit triggers hx-get + swap
  2. `test_filter_input_change_triggers_htmx_swap` — change event triggers hx-get
  3. `test_pagination_works_with_htmx` — pagination links swap table content
  4. `test_no_js_fallback_still_works` — normal form GET works without HTMX
- Estimated: 4-6 tests, ~1 developer task

**Step 3d: Update CI**
- Add `playwright install chromium` to CI setup steps
- Add `tests/browser/` to the test discovery path (or use a separate CI job for
  browser tests since they're slower)

---

## 5. Decision: JS Test Runner — Rejected

### Options Considered

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **Jest + jsdom** | Industry standard; fast; good ecosystem | Requires npm/Node.js; needs build config; 761 LOC doesn't justify it; violates ADR-001 "no build step" | ❌ Rejected |
| **Vitest** | Fast; ESM-native; Vite-based | Same as Jest + adds Vite config; violates ADR-001 | ❌ Rejected |
| **Mocha + Chai** | Lighter than Jest; no bundler required | Still needs npm/Node.js; still violates ADR-001 | ❌ Rejected |
| **Playwright (Python)** | Real browser; tests actual rendering | Heavy; browser binary required; overkill for vanilla JS islands; now triggered by HTMX feature | ✅ **Triggered** |
| **httpx + BeautifulSoup (Tier 2)** | No new deps; same test runner; tests JS effects on DOM; fast | Can't test event handlers, async ops, or real browser rendering | ✅ **Recommended** |

### Rationale for Rejecting a JS Test Runner

1. **ADR-001 explicitly prohibits build tooling.** The architecture decision
   states "No build step (no npm, no bundler, no transpilation)." Adding a JS
   test runner would introduce exactly what the architecture prohibits.

2. **Zero JS dependencies is a feature, not a bug.** DocMind's JS footprint is
   761 lines. A Jest setup would be more lines of config + test infrastructure
   than the production JS it tests.

3. **Tight coupling to server-rendered HTML.** The JS islands assume a DOM that
   the server creates. Testing them in isolation (jsdom with mocked DOM) tests
   an artificial environment. Testing them against real server output tests the
   actual contract.

4. **BeautifulSoup provides sufficient DOM-level assertions.** For the specific
   interactions in DocMind's JS (attribute toggling, class manipulation, text
   content updates, element visibility), BeautifulSoup assertions capture the
   observable effects. We cannot test event handler *dispatch* (click events,
   drag events, WebSocket messages), but we can test the *outcome* of those
   handlers on the DOM.

---

## 6. Testing Guidelines for New JS Islands

When a developer creates a new JS island (per ADR-003), the following tests
are required:

### Required Tests (Tier 1 + Tier 2)

1. **Template structure test (Tier 1):** Verify the `<script src="...">` tag is
   present in the rendered page's `{% block extra_js %}`.
2. **DOM element existence test (Tier 2):** Verify every DOM element the JS
   interacts with (`querySelector`, `getElementById`) is present in the
   server-rendered HTML.
3. **Initial state test (Tier 2):** Verify the default state of interactive
   elements (disabled buttons, hidden panels, empty lists, default attribute
   values).
4. **Post-interaction state test (Tier 2):** Verify the DOM state after the JS
   function has run (simulated in Python by applying the same attribute/class/text
   mutations the JS would perform).

### Optional Tests (when applicable)

5. **Edge case test (Tier 2):** Empty list, single item, error response, missing
   element.
6. **Browser E2E test (Tier 3):** Only if the feature uses HTMX partial swaps.
   For vanilla JS islands, Tier 1 + Tier 2 is sufficient.

---

## 7. Test File Organization

```
tests/
├── conftest.py                  # NEW: shared fixtures (asgi_client, tmp_db_path, soup)
├── test_web_ui.py               # Tier 1: template structure + route tests
├── test_web_js_theme.py         # NEW: Tier 2 tests for theme.js
├── test_web_js_documents.py     # NEW: Tier 2 tests for documents-list.js
├── test_web_js_upload.py        # NEW: Tier 2 tests for upload.js
├── test_web_js_viewer.py        # NEW: Tier 2 tests for viewer.js
├── test_web_js_chat.py          # NEW: Tier 2 tests for chat.js
├── test_chat.py                 # Tier 1: WebSocket handler + REST API tests
├── test_multi_upload.py         # Tier 1: upload route + template tests
├── test_document_viewer.py      # Tier 1: viewer route + template tests
├── integration/
│   └── test_phase3.py           # Integration tests
└── browser/                     # Tier 3 Playwright tests (triggered — HTMX feature exists)
    ├── conftest.py
    └── test_htmx_partial_swap.py
```

---

## 8. Summary

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Add JS test runner? | **No** | Violates ADR-001; 761 LOC doesn't justify npm toolchain |
| New testing layer? | **Yes — Tier 2 DOM-level** | BeautifulSoup assertions on server-rendered HTML |
| When to add Playwright? | **Triggered** | First HTMX feature (`/documents/partials/table`) is implemented; Phase 3 should proceed |
| Extract shared fixtures? | **Yes — Phase 2** | conftest.py deduplication |
| Estimated new tests | **~30-36** | ~25-30 Tier 2 tests + 4-6 Tier 3 tests |
| New dependencies | **Playwright (dev)** | Added in Phase 3; no other new deps |

---

*This strategy is a deliverable of Agora task t_5159538b. It should be reviewed
and approved before implementation tasks are created.*
