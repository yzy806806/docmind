# DocMind Architecture

> **Status:** Active — updated 2025-07-06
> **Audience:** All contributors (developer, architect, tester, reviewer, leader)

---

## 1. Overview

DocMind is an AI-powered document knowledge base. Documents are ingested
from multiple sources (WebDAV, local directories, PostgreSQL), text is
extracted and chunked, then indexed via both SQLite FTS5 (full-text) and
optional vector embeddings (semantic). A hybrid search engine merges both
indexes. An LLM layer provides summarisation and multi-turn Q&A with
citation tracking.

Three consumption surfaces sit on top of the core engine:

| Surface      | Technology            | Entry point                  |
|--------------|-----------------------|------------------------------|
| Web UI       | FastAPI + Jinja2 SSR  | `src/web/server.py`          |
| Hermes Tool  | Python plugin         | `src/hermes_plugin.py`       |
| CLI          | Rich-based commands   | `src/cli/main.py`            |

---

## 2. Module Map

```
src/
├── core/           # Engine — no web/CLI dependencies
│   ├── config.py         # Configuration management
│   ├── cache.py          # Query result caching (memory/Redis, cache-aside)
│   ├── db.py             # Database abstraction (DBAP)
│   ├── db_sqlite.py      # SQLite + FTS5 implementation
│   ├── storage.py        # Source adapters (WebDAV/dir/PG)
│   ├── extractor.py      # Text extraction (PDF/DOCX/HTML/MD/TXT)
│   ├── indexer.py        # Ingest + upsert + hash detection
│   ├── chunking.py       # Semantic chunking
│   ├── embeddings.py     # Vector embeddings (local/Ollama/OpenAI)
│   ├── search.py         # Hybrid search engine
│   ├── search_backend.py # Search backend abstraction
│   ├── summarizer.py     # LLM map-reduce summarisation
│   ├── llm_client.py     # OpenAI-compatible + Ollama client
│   ├── job_queue.py      # Async job queue
│   ├── models.py         # Data models
│   ├── parser_sandbox.py # Parser sandboxing
│   └── sanitizer.py      # Data sanitisation
├── web/            # Web layer — depends on core/
│   ├── server.py         # FastAPI app + routes (2470 LOC)
│   ├── rendering.py      # Jinja2 template engine + render helpers (1016 LOC)
│   ├── auth.py           # API Key + session auth (HMAC-SHA256)
│   ├── chat.py           # WebSocket Q&A with citations
│   ├── document_viewer.py # Paginated viewer + in-doc search
│   ├── services.py       # Export, summary, business services
│   ├── templates/        # 20 Jinja2 templates
│   │   ├── base.html         # Layout shell (CSS vars, dark mode, nav)
│   │   ├── _partials/        # Reusable components (pagination)
│   │   └── documents/        # List + detail pages
│   └── static/js/        # 5 vanilla JS files (761 LOC total)
│       ├── theme.js          # Dark mode toggle + persistence
│       ├── upload.js         # Multi-file drag-and-drop
│       ├── viewer.js         # Document viewer interaction
│       ├── documents-list.js # Document list page behaviour
│       └── chat.js           # WebSocket chat client
├── cli/            # CLI — depends on core/
│   ├── main.py
│   ├── services.py
│   └── formatters.py
└── hermes_plugin.py # Hermes tool registration
```

**Dependency rule:** `core` ← `web`, `core` ← `cli`, `core` ← `hermes_plugin`.
The `core` package must never import from `web`, `cli`, or `hermes_plugin`.

---

## 3. Data Flow

```
Source (WebDAV / dir / PG)
  │
  ▼
Storage adapter ──► Extractor ──► Chunker
  │                                   │
  │                                   ▼
  │                              Indexer (upsert by SHA256)
  │                                   │
  │                    ┌──────────────┴──────────────┐
  │                    ▼                             ▼
  │              FTS5 index                   Vector embeddings
  │            (SQLite)                    (sentence-transformers /
  │                                               Ollama / OpenAI)
  │                    │                             │
  │                    └──────────┬──────────────────┘
  │                               ▼
  │                        Hybrid Search
  │                               │
  │                    ┌──────────┼──────────┐
  │                    ▼          ▼          ▼
  │                  Web UI    Hermes     CLI
  │               (Jinja2)    Tool      (Rich)
  │                    │
  │                    ▼
  └─────────────► LLM (summary / Q&A / multi-turn filter)
                     │
                     ▼
               Citation-tracked response
```

### 3.1 Caching Layer (Phase 5a)

The `Database` class wraps all read operations with a cache-aside layer:

```
Read request (get_document, list_documents, get_stats, ...)
  │
  ├── Cache hit? ──► Return cached result (no DB query)
  │
  └── Cache miss
        │
        ▼
      Query SQLite ──► Store result in cache ──► Return result
```

All 14 mutation paths (upload, delete, tag add/remove, collection CRUD,
chat mutations, job state changes) call centralized invalidation helpers
(`_invalidate_document_mutations`, `_invalidate_tag_mutations`, etc.)
to remove affected cache keys. The cache never serves stale data.

**Backend selection:**
- `DOCMIND_CACHE_BACKEND=memory` (default): In-process dict with TTL + LRU eviction
- `DOCMIND_CACHE_BACKEND=redis`: External Redis, suitable for multi-worker deployments
- `DOCMIND_CACHE_ENABLED=false`: All cache operations become no-ops

**TTL policy:** Dynamic data (document lists, jobs) expire in 30-60 seconds;
stable metadata (collections, settings, tag cloud) expire in 300-600 seconds.
See `src/core/cache.py` → `CacheTTLConfig` for the full table.

**Design doc:** `docs/architecture/caching.md`

---

## 4. Architecture Decisions

### ADR-001: Jinja2 SSR as the UI framework

**Date:** 2025-07-05
**Motion:** motion-d9138a198276 (adopted — unanimous, 6/6 participants)
**Status:** Active

**Context.** The team needed to decide whether to use server-side rendering
(Jinja2 templates) or add a frontend framework (React/Vue) for the Web UI.

**Decision.** Adopt Jinja2 SSR as the UI framework. Future feature work
defaults to server-side rendering with vanilla JS. An SPA framework is only
revisited if a specific feature demands client-side reactivity that cannot
be met with progressive enhancement.

**Rationale.** The codebase audit was conclusive:
- 20 Jinja2 templates, 761 lines of vanilla JS across 5 files, zero build
  tooling.
- 1016 passing tests with established patterns for template/UI testing.
- The rendering layer (`rendering.py`, 1016 LOC) already encapsulates all
  HTML generation via a single `_render_template` helper and `_jinja_env`.
- CSS custom properties in `base.html` provide theming (dark mode) without
  a CSS framework.
- The only stateful client-side component is the WebSocket chat (`chat.js`),
  which is already well-served by vanilla JS.
- Adding a frontend framework would introduce: a build step (npm/Vite),
  a dependency tree, an API contract layer, and a testing paradigm shift —
  none of which are justified by current or planned features.

**Implications.**
1. New pages and features must use Jinja2 templates served by FastAPI
   routes — not client-side routing.
2. Client-side interactivity is added via small, focused vanilla JS files
   in `src/web/static/js/`, loaded via `{% block extra_js %}` in
   `base.html`.
3. The template rendering pipeline (`rendering.py` → `_render_template` →
   Jinja2 `Environment`) is the single entry point for all HTML output.
4. Reusable UI fragments go in `src/web/templates/_partials/`.
5. CSS theming uses the CSS custom properties pattern established in
   `base.html` — no CSS-in-JS or framework stylesheets.

**Escape hatch.** If a future feature requires substantial client-side
reactivity (e.g. real-time collaborative editing, complex drag-and-drop
reordering with optimistic updates, or offline-first PWA behaviour), a
new ADR must be raised via an Agora motion to evaluate adding a framework
component. The motion must demonstrate that the feature cannot be
reasonably implemented with SSR + progressive enhancement.

---

### ADR-002: Hand-rolled schema migrations (no Alembic)

**Date:** 2025-07-05
**Motion:** motion-1a1689af9142 (rejected — unanimous, 3/3 voters)
**Status:** Active

**Context.** The team considered adopting Alembic for database schema
migrations.

**Decision.** Keep hand-rolled schema migrations. Do not adopt Alembic.

**Rationale.** The current `migrate()` function in `db_sqlite.py` handles
schema evolution with conditional `ALTER TABLE` statements. The schema is
stable, the migration surface is small (one SQLite database), and Alembic
would add operational complexity (migration files, version tracking,
downgrade scripts) disproportionate to the project's needs. Unanimous
rejection by architect, developer, and tester.

**Implications.** Schema changes go directly into `migrate()` with
`IF NOT EXISTS` / `IF EXISTS` guards. Contributors should not re-propose
Alembic without new technical justification.

---

### ADR-003: Hybrid Islands Architecture for the Web UI

**Date:** 2025-07-05
**Motion:** motion-e73dd1dcb0c3 (adopted)
**Status:** Active
**Supersedes:** None — extends ADR-001 with the concrete interaction pattern.

**Context.** ADR-001 established Jinja2 SSR as the UI framework and
proscribed JavaScript frameworks. Since then, the codebase has converged
on a specific pattern: server-rendered HTML pages with small, focused
"islands" of client-side interactivity loaded per-page via
`{% block extra_js %}`. Five JS files (theme, upload, viewer,
documents-list, chat) now follow this pattern. The team discussed
formalising the approach and defining the boundary at which a full SPA
framework would become justified.

**Decision.** Adopt the **Hybrid Islands Architecture** as DocMind's
official client-side interaction model:

1. **Jinja2 SSR is the foundation.** Every page is a server-rendered
   HTML document. No client-side routing, no virtual DOM.
2. **`{% block extra_js %}` defines interactivity islands.** Each
   template that needs client-side behaviour overrides the block to load
   a dedicated JS file from `static/js/` or a small inline `<script>`.
   The JS is scoped to that page's DOM — it does not leak globally.
3. **`static/js/` holds shared scripts.** Reusable JS modules live in
   `src/web/static/js/` and are served via FastAPI's StaticFiles mount
   at `/static`. Files are loaded with `<script src="..." defer>` so
   they execute after DOM parse, preventing FOUC.
4. **HTMX is permitted for partial swaps.** When a page region needs to
   update without a full page reload (e.g. live search results, inline
   form submission, progressive list loading), HTMX attributes
   (`hx-get`, `hx-post`, `hx-target`, `hx-swap`) are the preferred
   tool. HTMX does not require a build step or npm dependency — it is a
   single static file served from `static/vendor/htmx.min.js`.
5. **Vanilla JS remains the default.** HTMX is used only when a partial
   swap is genuinely simpler than a full page reload. For stateful,
   long-lived interactions (WebSocket chat, drag-and-drop upload,
   paginated viewer), vanilla JS in `static/js/` is the right tool.

**Rationale.**
- The islands pattern is already how the codebase works — 5 templates
  use `{% block extra_js %}` to load page-specific scripts. Formalising
  it makes the convention explicit for future contributors.
- `{% block extra_js %}` provides a clean seam: each page owns its
  interactivity without a global router or shared state store.
- HTMX fills the gap between "full page reload" and "SPA" — it handles
  partial swaps with declarative attributes, no build step, and
  degrades gracefully (links/forms still work without JS).
- The approach has zero npm dependencies, zero build tooling, and is
  fully testable with the existing pytest template-assertion pattern.

**The SPA Boundary — when would a full framework become justified?**

A new ADR (via Agora motion) must be raised before introducing a full
SPA framework (React, Vue, Svelte, etc.). The motion must demonstrate
that the feature cannot be reasonably implemented with the hybrid
islands model. The following triggers would justify the discussion:

| Trigger                                              | Why islands can't handle it                          |
|------------------------------------------------------|------------------------------------------------------|
| **Real-time collaborative editing**                   | Requires shared state sync, operational transforms,  |
|                                                      | and conflict resolution across multiple clients.     |
| **Complex optimistic UI with rollback**               | Requires a client-side state store with transaction  |
|                                                      | semantics and rollback that exceeds Fetch + DOM.     |
| **Offline-first PWA**                                | Requires service workers, client-side routing, and   |
|                                                      | local persistence — fundamentally a client app.      |
| **Interactive data visualisation (dashboards)**       | Requires reactive component composition, virtual     |
|                                                      | DOM diffing for large datasets, and widget libraries |
|                                                      | that assume a framework ecosystem.                   |
| **Multi-step wizard with shared cross-page state**    | Requires client-side state that survives page        |
|                                                      | transitions — islands are per-page by design.        |

If none of these triggers are met, the feature should be implemented
with SSR + islands + HTMX. The burden of proof is on the proposal to
show why the existing model is insufficient.

**HTMX usage guidelines.**
1. HTMX is loaded once in `base.html` via a `<script>` tag from
   `static/vendor/htmx.min.js` (not a CDN — self-hosted).
2. HTMX targets server endpoints that return HTML fragments, not JSON.
   The endpoint can be a dedicated route or a `?partial=true` query
   parameter on an existing route.
3. HTMX is used for progressive enhancement: the page must still
   function with a full page reload if HTMX is absent.
4. No HTMX extensions (`htmx-ext-*`) without an ADR — keep the surface
   minimal.

---

### ADR-004: Cache-aside at the Database Layer

**Date:** 2025-07-06
**Motion:** motion-aaa5420f752c (adopted — unanimous, 7/7 participants)
**Status:** Active

**Context.** The team needed to reduce redundant SQLite queries for read-heavy
operations — dashboard stats, document lists, search results — without adding
external infrastructure requirements or changing the route handler API.

**Decision.** Implement a cache-aside pattern at the `Database` class level with
a pluggable backend architecture:

1. **Cache-aside at the Database layer, not route handlers.**
   All 24 read methods check the cache before querying SQLite. Route handlers
   don't need to know caching exists — the `Database` class is the single
   integration point. This keeps caching transparent and avoids scattering
   cache logic across the web layer.

2. **In-memory dict as default backend.**
   Zero external dependencies, zero configuration. The application works
   out of the box with TTL + LRU eviction in-process.

3. **Optional Redis backend via lazy import.**
   The `redis` package is only imported when `DOCMIND_CACHE_BACKEND=redis`.
   If Redis is unavailable at startup, the backend falls back to in-memory
   with a warning — the application never fails to start due to cache
   misconfiguration.

4. **Explicit invalidation on every mutation path.**
   All 14 write operations call one of five centralized `_invalidate_*`
   helpers. There is no automatic cache synchronization — consistency is
   maintained by disciplined invalidation at every mutation site.

5. **Category-aware TTLs.**
   Each of the 22 cache categories has a purpose-fit TTL ranging from 30
   seconds (dynamic lists) to 600 seconds (stable metadata). TTLs are
   defined in a single `CacheTTLConfig` dataclass for discoverability.

6. **NoopCache for graceful disable.**
   When `DOCMIND_CACHE_ENABLED=false`, all cache operations become no-ops
   with zero overhead. This is useful for debugging, testing, or deployments
   where caching is handled externally (e.g. a reverse proxy).

**Rationale.**
- The Database layer is the natural integration point: every read path
  passes through `Database`, and every write path already updates the
  database. Adding cache logic in route handlers would scatter the concern
  across 20+ endpoints.
- In-memory default preserves the project's zero-dependency philosophy
  (no PostgreSQL, no Redis required). Users who want Redis can opt in.
- Explicit invalidation is simpler and more predictable than TTL-only
  approaches. It guarantees cache consistency without a distributed
  invalidation protocol.
- The design was validated with 96 tests covering both backends, all TTL
  categories, and cache-miss/hit/invalidation scenarios for every major
  entity type.

**Implications.**
1. New read methods added to `Database` should follow the cache-aside
   pattern: check cache, query DB on miss, store result.
2. New mutation methods must call the appropriate `_invalidate_*` helper.
3. The `CacheBackend` ABC is the extension point for future backends.
4. Redis is optional — the `redis` package must not appear in
   `pyproject.toml` as a required dependency.
5. Full architecture spec at `docs/architecture/caching.md`.

---

## 5. Web UI Architecture (detail)

This section expands ADR-001 with the concrete patterns that implement
the SSR convention.

### 5.1 Request lifecycle

```
HTTP request
  │
  ▼
FastAPI route handler (server.py)
  │
  ├── Business logic: call core/ services (db, search, summarizer)
  ├── Prepare context dict
  │
  ▼
_render_template("template.html", **context)   ← rendering.py
  │
  ├── _jinja_env.get_template()
  ├── Inject utility filters (escape, fmt_date, fmt_size)
  ├── Inject auth_enabled flag
  │
  ▼
HTMLResponse(template.render(**context))
  │
  ▼
Browser renders HTML + loads static JS (progressive enhancement)
```

### 5.2 Template hierarchy

```
base.html
  ├── {extra_head}    ← per-page CSS injections
  ├── {% block content %}  ← page body
  └── {% block extra_js %} ← interactivity island (per-page JS)
```

Every page extends `base.html`. The base template provides:
- HTML document shell (`<!DOCTYPE>`, `<head>`, `<body>`)
- CSS custom property theming (light/dark via `[data-theme]`)
- Navigation bar with auth-aware links
- Mobile responsive layout
- Theme toggle (`theme.js`)
- Footer

### 5.3 JavaScript conventions

| File              | Purpose                              | Pattern                        |
|-------------------|--------------------------------------|--------------------------------|
| `theme.js`        | Dark mode toggle + localStorage      | IIFE, no dependencies          |
| `upload.js`       | Multi-file drag-and-drop             | Event listeners, FormData      |
| `viewer.js`       | Document viewer pagination/search    | Fetch API + DOM manipulation   |
| `documents-list.js` | Document list page behaviour       | Event listeners, DOM manipulation |
| `chat.js`        | WebSocket Q&A with citations         | WebSocket + DOM append         |

**Rules:**
1. No JavaScript SPA frameworks (React, Vue, Svelte, Alpine). HTMX is
   permitted for partial swaps — see ADR-003 for guidelines.
2. No build step (no npm, no bundler, no transpilation).
3. No npm `package.json` — the project has zero JS dependencies. HTMX
   is a single self-hosted static file, not an npm package.
4. JS files are loaded via `<script src="/static/js/...">` with `defer`.
5. Progressive enhancement: pages must function without JS; JS only
   enhances the experience (drag-and-drop, WebSocket, pagination).
6. New JS files go in `src/web/static/js/` and are registered in
   `base.html` or per-template `{% block extra_js %}`.
7. Each `{% block extra_js %}` override is an **interactivity island** —
   scoped to the page's DOM, no global state leakage (see ADR-003).

### 5.4 CSS conventions

1. All styling uses CSS custom properties defined in `base.html` under
   `:root` (light) and `[data-theme="dark"]` (dark).
2. No external CSS frameworks (no Bootstrap, Tailwind, etc.).
3. Per-page CSS is injected via `{extra_head}` in the template.
4. The colour palette, spacing, and component styles (`.card`, `.stat`,
   `.badge`, `.tag-pill`, `.collection-tree`, etc.) are defined in
   `base.html` and reused across all templates.

### 5.5 REST API and JSON endpoints

The Web UI layer also serves a REST API under `/api/v1/`. These endpoints
return JSON and are consumed by:
- The Hermes tool plugin (`kb_search`, `kb_list`, `kb_read`, `kb_ingest`)
- Programmatic clients
- Occasional AJAX calls from JS (e.g. collection tree fetching)

JSON endpoints live alongside HTML routes in `server.py`. Both share the
same `core/` business logic — the only difference is the response format
(HTML via Jinja2 vs JSON via FastAPI's `JSONResponse`).

---

## 6. Testing Strategy

- **Framework:** pytest with `pytest-asyncio` (async mode auto).
- **Scope:** 1016 tests across 30 test files.
- **UI tests:** Template structure assertions, route response assertions,
  and integration tests that verify rendered HTML content.
- **Core tests:** Unit tests for storage, extraction, indexing, search,
  and summarisation.
- **API tests:** REST endpoint contract tests (request/response shapes).

When adding a new feature:
1. Add core logic tests first (TDD).
2. Add route tests that verify the HTML response or JSON payload.
3. Add template structure tests if new templates are introduced.

---

## 7. Deployment

- **Native:** `python -m src.web.server` (port 8080)
- **Docker:** `docker-compose up -d` (port 8000, multi-stage Dockerfile)
- **Config:** `config/config.yaml` (see `config/config.example.yaml`)
- **Database:** SQLite at `data/docmind.db` (FTS5 enabled)

---

## 8. Conventions for Contributors

| Convention                | Rule                                                    |
|---------------------------|---------------------------------------------------------|
| New UI page               | Jinja2 template extending `base.html`                   |
| New client-side behaviour | Vanilla JS in `src/web/static/js/`, loaded via `defer`  |
| New interactivity island  | Override `{% block extra_js %}` in the page template    |
| Partial page update       | HTMX attributes (`hx-get`/`hx-post` + `hx-target`/`hx-swap`) |
| New CSS                   | CSS custom properties in `base.html` or `{extra_head}`  |
| New REST endpoint         | `/api/v1/` prefix, JSON response                        |
| Schema change             | Add to `migrate()` with `IF NOT EXISTS` guard           |
| New dependency            | Raise an Agora motion before adding to `pyproject.toml` |
| New architecture decision | Document as an ADR in this file (section 4)             |
| Reusable UI fragment      | Place in `src/web/templates/_partials/`                 |
| SPA framework proposal    | Raise ADR via Agora motion; must meet a trigger in ADR-003 |
| New Database read method  | Follow cache-aside pattern: get → miss → query → set; add TTL to `CacheTTLConfig` |
| New Database mutation     | Call the appropriate `_invalidate_*` helper after the write |

---

## 9. Revision History

| Date       | Author    | Change                                             |
|------------|-----------|----------------------------------------------------|
| 2025-07-05 | architect | Created. Documented ADR-001 (Jinja2 SSR) and ADR-002 (hand-rolled migrations) per motions motion-d9138a198276 and motion-1a1689af9142. |
| 2025-07-05 | architect | Added ADR-003 (Hybrid Islands Architecture) per motion-e73dd1dcb0c3. Updated Section 5.3 rules to permit HTMX for partial swaps. Added SPA boundary trigger table and HTMX usage guidelines. Updated conventions table with islands, HTMX, and SPA-proposal rows. |
| 2025-07-06 | writer    | Added ADR-004 (Cache-aside at the Database Layer) per motion-aaa5420f752c. Updated Section 2 module map with `cache.py`, Section 3 data flow with caching layer diagram and backend/TTL documentation, and Section 8 conventions with cache read/mutation rules. |
