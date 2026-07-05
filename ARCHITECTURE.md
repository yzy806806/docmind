# DocMind Architecture

> **Status:** Active — updated 2025-07-05
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
  └── {% block extra_js %} ← per-page JS loading
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
1. No JavaScript frameworks (React, Vue, Alpine, htmx runtime).
2. No build step (no npm, no bundler, no transpilation).
3. No npm `package.json` — the project has zero JS dependencies.
4. JS files are loaded via `<script src="/static/js/...">` with `defer`.
5. Progressive enhancement: pages must function without JS; JS only
   enhances the experience (drag-and-drop, WebSocket, pagination).
6. New JS files go in `src/web/static/js/` and are registered in
   `base.html` or per-template `{% block extra_js %}`.

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
| New CSS                   | CSS custom properties in `base.html` or `{extra_head}`  |
| New REST endpoint         | `/api/v1/` prefix, JSON response                        |
| Schema change             | Add to `migrate()` with `IF NOT EXISTS` guard           |
| New dependency            | Raise an Agora motion before adding to `pyproject.toml` |
| New architecture decision | Document as an ADR in this file (section 4)             |
| Reusable UI fragment      | Place in `src/web/templates/_partials/`                 |

---

## 9. Revision History

| Date       | Author    | Change                                             |
|------------|-----------|----------------------------------------------------|
| 2025-07-05 | architect | Created. Documented ADR-001 (Jinja2 SSR) and ADR-002 (hand-rolled migrations) per motions motion-d9138a198276 and motion-1a1689af9142. |
