# Changelog

All notable changes to DocMind are documented in this file. The project uses
calendar-based versioning: each section groups changes by the week they shipped.


## 2026-07-19 — Backend Fix Re-application + Frontend Merge

### Fixed — Backend regressions from frontend branch merge

The frontend optimization branch was based on a pre-v1.2.0 commit and
inadvertently reverted all backend LLM fixes from v1.2.0–v1.4.0. All
fixes have been re-applied on top of the frontend changes:

- **LLM config hydration on startup** — `_reload_llm_config_from_db()`
  called in lifespan startup alongside auth hydration. Without this,
  LLM settings saved via WebUI were lost on every restart.
- **Reasoning model support (Gemma-4)** — `content` empty → read
  `reasoning_content` fallback, in `_call_openai`, `_stream_openai`,
  `_SyncLLMAdapter`, and `detector._detect_llm`.
- **Streaming LLM calls** — all 3 call sites (`_SyncLLMAdapter.chat`,
  `LLMClient._call_openai`, `detector._detect_llm`) use `httpx.stream`
  with SSE parsing to avoid Cloudflare 524 gateway timeouts.
- **Hardcoded max_tokens removed** — `ChunkSummarizer` (`_single_pass`,
  `_summarize_chunk`, `_reduce_summaries`) and `detector` now use
  config-driven `max_tokens` instead of hardcoded 120/150/250/50.
- **`_SyncLLMAdapter` rewritten** — synchronous `httpx.stream` instead
  of cross-thread AsyncClient (fixes `RuntimeError: Event loop is closed`).
- **Background upload processing** — type detection + summary generation
  moved to `asyncio.create_task()` so upload endpoints return 202
  immediately instead of blocking on LLM calls.
- **Jobs marked completed** — `queue.complete(job.id, doc_id)` called
  after enqueue since document is already ingested. API returns
  `status="completed"` instead of `"pending"`.
- **Chinese prompts** — all summarizer and chat system prompts in Chinese.
- **Port 9980** — all references updated from 8080 to 9980 across code
  and docs.
- **max_tokens slider** — range 4000–64000 (step 500) instead of 100–4000.
- **LLM timeout** — default 3600s (1 hour) for reasoning models.
- **LLM max_tokens default** — 8000 instead of 1000.
- **Retry with backoff** — `_SyncLLMAdapter` retries on 502/503/524/429
  with exponential backoff (max 3 attempts).

### Added — Frontend (from frontend optimization branch, 51 commits)

- **Design token system** — 96 CSS custom properties in `:root` with
  dark theme parity, organized into 14 groups.
- **Base UI component system** — `.btn`, `.input`, `.card` primitives
  with modifiers using design tokens.
- **CSS transitions** — fluid transitions on all interactive elements
  (buttons, nav, cards, tags, badges, filter panels) with `:active`
  press feedback.
- **HTMX live search** — 250ms debounced live search with loading
  indicator, browser back-button support via `hx-push-url`, and
  fragment-only responses for HTMX swaps.
- **Optimistic UI** — instant feedback for HTMX mutations (delete, tag,
  move) with automatic rollback on failure.
- **Performance** — lazy-load fade-in, skeleton loaders, lazy images,
  scroll progress bar, smooth scroll-behavior, momentum scrolling.
- **263 new frontend tests** across 15+ test files covering design
  tokens, components, transitions, live search, optimistic UI,
  scroll smoothness, and mutation feedback.


## 2026-07-18 — Phase 9: Design Token Migration & CSS Transitions

The first phase of the frontend beautification initiative established the
CSS foundation. Every hardcoded colour, spacing value, and visual property
was migrated to a centralised design-token system, and interactive elements
gained fluid CSS transitions with composed token presets.

### Changed — Design Token Foundation

- **96 CSS custom properties** in `:root` organised into 14 groups: surfaces,
  text, header/nav, primary actions, accent/semantic, borders/inputs, badges,
  feedback, syntax highlighting, shadows, spacing, typography, radius,
  transitions, z-index, layout, focus ring, disabled state, border widths,
  lift amounts, component sizes, and scroll behaviour.
- **Dark theme parity.** Every colour and shadow token has a corresponding
  override in `[data-theme="dark"]`. Spacing, typography, radius, and
  transition tokens are theme-independent. Adding a token follows a
  three-step documented process (define in `:root`, override in dark block,
  consume via `var()`).
- **Spacing scale migration.** All hardcoded `px` values replaced with
  `var(--space-N)` references across 20 fine-grained steps from `--space-0`
  to `--space-10` with half-step increments. Covers border widths, inline-code
  padding, icon gaps, and every layout value in the template system.
- **Zero hardcoded values.** No hex, rgba, or pixel values remain outside the
  `:root` and dark-theme declaration blocks.

### Changed — Base UI Component System

- **`.btn` class** — single button primitive with `--primary`, `--secondary`,
  `--danger`, and `--ghost` modifiers. Active state includes
  `transform: scale(0.97)` press feedback.
- **`.input` class** — unified form control with `--lg` and `--inline`
  modifiers. Consistent border, focus ring, and disabled styling.
- **`.card` component** — surface container with `--hover` modifier for
  lift-on-hover behaviour.
- **42 CSS component tests** (`test_base_components.py`) verify token usage,
  modifier combinations, and dark-theme rendering.

### Changed — CSS Transitions

- **Transition tokens** — `--transition-base` (180ms) and six composed
  presets: colour, opacity, press (colour + border + transform for `:active`),
  theme (colour + background for dark-mode toggle), lift (shadow + transform
  for card hover), and fast (150ms for micro-interactions). All durations
  under 200ms for perceived instant response.
- **Button transitions** — `.btn`, `.btn-secondary`, `.btn-danger`, and
  `.btn-ghost` with `:hover`, `:focus-visible`, and `:active` state
  transitions. `:active` includes `transform: scale(0.97)` for tactile
  press feedback.
- **Navigation & interactive elements** — nav links, pagination, tags, badges,
  and filter panels with colour, background, and opacity transitions on hover
  and active states. Text links with `opacity: 0.7` on `:active`.
- **Focus-visible compliance** — all interactive elements have visible focus
  ring transitions using `--focus-ring-width`, `--focus-ring-color`, and
  `--focus-ring-offset` design tokens, meeting WCAG 2.4.7.
- **24 CSS transition tests** verify token application across interactive
  element selectors.

### Tests

- **66 new CSS tests** across three test files: design token validity (24),
  base component token usage and dark-theme rendering (28), and CSS
  transition validity (14).


## 2026-07-19 — Frontend Smoothness Overhaul (Phases 10–15)

A comprehensive frontend smoothness and UX polish initiative, delivered across six
iterative phases. Every interactive surface was audited and improved: CSS
architecture was rebuilt on a design-token foundation, interactive elements gained
fluid transitions, search became instant with debounced live filtering, mutations
became optimistic with instant feedback, and scroll/load behaviour was upgraded
for a native-app feel. 263 new frontend tests verify the improvements.

### Changed — CSS Architecture: Design Token System

- **`--token` taxonomy.** 96 CSS custom properties in `:root` organised into 14
  groups: surfaces, text, header/nav, primary actions, accent/semantic, borders/inputs,
  badges, feedback, syntax highlighting, shadows, spacing (4px scale with half-steps),
  typography, radius, transitions, z-index, layout, focus ring, disabled state,
  border widths, lift amounts, component sizes, and scroll behaviour.
- **Theme isolation.** All color and shadow tokens have corresponding overrides in
  `[data-theme="dark"]`. Spacing, typography, radius, and transition tokens are
  theme-independent. Adding a new token is a three-step documented process
  (define, override if dark, consume via `var()`).
- **Zero hardcoded values.** No hex/rgba/pixel values appear outside the `:root`
  and dark-theme blocks. All 2,774 lines of CSS reference tokens.
- **Extended spacing scale.** From 8 generic steps to 20 fine-grained steps
  (`--space-0` through `--space-10` with half-step increments like `--space-1-25`,
  `--space-3-5`) covering border widths, inline-code padding, badge gaps, and
  every layout value in the template system.

### Changed — Unified UI Component System

- **`.btn` base class** — single button primitive with `--primary`, `--secondary`,
  `--danger`, `--ghost` modifiers. All 60+ buttons across 12 templates use the
  same class hierarchy. Active state includes `transform: scale(0.97)` press
  feedback.
- **`.input` base class** — unified form control with `--lg` and `--inline`
  modifiers. Consistent border, focus ring, and disabled styling.
- **`.card` component** — surface container with `--hover` modifier for
  lift-on-hover (`--lift-amount: -2px` translate + box-shadow transition).
- **42 CSS component tests** (`test_base_components.py`) verify token usage,
  modifier combinations, and dark-theme rendering.

### Changed — CSS Transitions & Interactivity

- **Transition token presets** — `--transition-fast` (150ms, opacity/transform
  micro-interactions) and `--transition-base` (180ms, color/background/border).
  All durations stay under 200ms for perceived instant response.
- **Composed transitions** — `--transition-theme`, `--transition-color`,
  `--transition-opacity`, `--transition-press` (color + border + transform for
  `:active` scale), `--transition-lift` (shadow + transform for card hover).
- **Interactive elements with fluid feedback** — buttons, nav links, pagination,
  tags/badges, filter panels, cards, table rows, search results, and collection
  trees all use composeable transition tokens. Every `:hover`, `:focus-visible`,
  and `:active` state has a visible transition.
- **Focus ring for keyboard nav** — `--focus-ring-width`, `--focus-ring-color`,
  `--focus-ring-offset` tokens with WCAG 2.4.7 compliance.

### Added — Optimistic UI for Mutations

- **Instant visual feedback** on delete (single + bulk): rows fade out immediately
  on click. Tag add/remove: badges appear/disappear instantly. Collection move:
  display updates before the server confirms.
- **Progressive enhancement** — `data-optimistic` attribute on forms. If JS
  is absent or fails, forms submit normally with server-rendered response.
- **Snapshot/restore pattern** — mutations snapshot the DOM state, optimistically
  apply changes, and restore on server error with a toast notification.
- **3-second toast notifications** with error/undo support.
- **73 tests** (`test_optimistic_ui.py`) covering delete, tag, collection move,
  bulk operations, error recovery, and concurrent operations.

### Added — Debounced Live Search

- **250ms debounce** on the search input — typed characters no longer fire
  individual server requests. Uses the shared `DocMindPerf.debounce()` utility.
- **Live loading indicator** with CSS-only spinner animation that replaces
  the search results area during fetch.
- **hx-push-url for filter/search** — browser URL updates on filter changes
  (source, file type, date range, tag, collection, vector weight slider)
  without full page reload. Back/forward buttons work correctly.
- **14 tests** (`test_search_push_url.py`) verify URL parameter propagation
  and back-button behaviour.

### Changed — Scroll Smoothness

- **Native CSS smooth scrolling** — `scroll-behavior: smooth` on `html` for
  anchor navigation and `scrollIntoView` calls. Overridden to `auto` when
  `prefers-reduced-motion: reduce` is active.
- **Scroll padding** — `scroll-padding-top: 84px` so anchor targets aren't
  hidden under the sticky header (16px header padding + 32px h1 + 28px nav
  row + 8px visual gap).
- **Momentum/overscroll** — `-webkit-overflow-scrolling: touch` on list
  containers for iOS momentum. `overscroll-behavior-y: none` on `html` and
  `overscroll-behavior: contain` on scrollable regions to prevent accidental
  page bounce and scroll-chaining glitches on infinite-list views.

### Changed — Loading States & Microcopy

- **Specific loading copy** across all HTMX mutation operations: "Saving…",
  "Deleting…", "Establishing connection…", "Reconnecting…", "Loading
  conversations…". Replaced silent waits and generic "Loading…" text.
- **Actionable empty states** — "No documents found" explains the current
  filter context; "No tags yet" suggests adding one; "No search results"
  suggests adjusting terms. Each includes a call-to-action link.
- **Search text improvements** — placeholder text, aria-labels, and helper
  text updated for clarity.

### Added — Performance Utilities

- **`DocMindPerf` module** (`perf-utils.js`) — shared `debounce`, `throttle`,
  and `rAFThrottle` utilities used across all JS modules. Each preserves
  `this` binding, forwards arguments, and exposes `.cancel()` for cleanup.
- **Wired to all islands** — debounce (search-as-you-type, autosave),
  throttle (scroll/resize/upload progress), rAFThrottle (visual DOM updates
  from slider input, streaming text append). Pauses when the tab is hidden.
- **Progress bar** — top-of-page animated bar that activates on HTMX/fetch
  requests and completes on response. Uses CSS `transform: scaleX()` for
  GPU-composited animation. Auto-clears after 600ms. `z-index: 9999`.
- **Skeleton loaders** — content-placeholder animations during initial page
  loads and between-page navigation.
- **Native lazy loading** — `loading="lazy"` attribute on all `<img>` tags
  for below-fold images.

### Added — Loading Feedback for HTMX Mutations

- **Button state transitions** — buttons show "Saving…" / "Deleting…" text
  and disabled state during HTMX request lifecycle. Revert to original text
  on response.
- **htmx:beforeRequest / htmx:afterRequest hooks** trigger progress bar and
  button feedback globally.

### Tests

- **263 new frontend tests** across 10 test files: design tokens (24), base
  components (28), CSS transitions (15 + 22 extended), fluidity (15),
  interactive states (22), optimistic UI (73), scroll smoothness (17),
  search push URL (14), browser smoothness (33).


## 2026-07-06 — Phase 9: Responsive Design, Lazy Loading, Keyboard Shortcuts

### Added — Responsive Design Validation

- Validated layout across 4 CSS breakpoints (1024px, 768px, 640px, 480px)
  on 7 pages plus the document viewer.
- Hamburger navigation on small screens.
- `prefers-reduced-motion` media query honoured — transitions and scroll
  smoothness disabled for users who request reduced motion.
- Touch target sizing for mobile — all interactive elements meet the 44px
  minimum in compact viewports.

### Added — Infinite Scroll & Lazy Loading

- **Documents list infinite scroll** — IntersectionObserver on a sentinel
  element triggers fetch of next page of `<tr>` rows from
  `/documents/partials/rows`. Filters (source, tag, collection, date range,
  file type) carry through to subsequent page requests.
- **Search results lazy loading** — "Load More" button fetches additional
  results from `/search/partials/results`.
- **Content preview lazy loading** — document content chunks load on demand.
- **Progressive enhancement** — without IntersectionObserver, standard
  pagination links work. Without JS, first page renders server-side.

### Added — Gmail-Style Keyboard Shortcuts

- **g-prefix navigation** — `g d` → Dashboard, `g s` → Search, `g D` →
  Documents, `g u` → Upload, `g e` → Email, `g j` → Jobs, `g a` →
  Analytics, `g c` → Chat, `g x` → Settings. 700ms timeout for the second key.
- **Quick actions** — `/` focuses search input, `?` toggles shortcuts help
  modal, `Esc` closes modal or blurs focused input.
- **Document operations** (on documents page) — `e` focus export, `t` focus
  tag input, `m` focus move select, `Del` trigger bulk delete.
- **Input suppression** — shortcuts suppressed when focus is in an input,
  textarea, select, or contentEditable element (except Escape).
- **Help modal** with visual keyboard shortcut reference displayed on `?`.

## 2026-07-06 — Phase 8: Email Ingestion

### Added — IMAP Email Ingestion

DocMind can now automatically ingest email from IMAP accounts (Gmail,
Outlook, self-hosted servers). A background worker polls configured
accounts on a configurable interval, converting email bodies and
attachments into searchable documents.

- **In-process async worker.** Polling runs as a background task in
  FastAPI's `lifespan` context — no separate process or external
  scheduler required. Accounts are polled sequentially on the
  configured interval.
- **3-layer deduplication.** Message-ID header hash (SHA256) as primary
  key, with account+folder+UID and content hash as fallbacks for edge
  cases. Each ingested email is logged to `email_ingestion_log` for
  auditability.
- **Document creation.** Email bodies become documents with
  `source_type="email"`. Supported attachments (PDF, DOCX, TXT, etc.)
  become separate documents. All documents from the same email share a
  `thread_id` for future thread-based grouping.
- **Post-fetch actions.** `mark_seen` (default), `delete`, or
  `move_folder` (deferred) after successful ingestion.
- **Attachment filtering.** Per-account whitelist and blacklist globs
  control which attachment types are ingested.
- **Configuration via env vars.** All settings use the
  `DOCMIND_EMAIL_*` prefix with indexed account pattern
  (`ACCOUNT_<N>_<FIELD>`). No YAML config needed.

### Added — Email Account Management UI

- Web UI pages for creating, editing, and deleting email accounts
- Connection test endpoint (`POST /api/v1/email-accounts/{id}/test`)
- Manual sync trigger (`POST /api/v1/email-accounts/{id}/sync`)
- Ingestion log viewer (`GET /api/v1/email-accounts/{id}/logs`)
- Email metadata displayed on document detail pages (From, To, Subject,
  Date, Message-ID)
- Email accounts integrated into search results and filtering

### Added — Fernet Credential Encryption

IMAP passwords are encrypted at rest using Fernet symmetric encryption:

- **Per-instance encryptor** — each `Database` instance owns its
  encryptor, preventing cross-database key leakage in tests
  (commit 15d6075)
- Encryption key injected via `DOCMIND_EMAIL_ENCRYPTION_KEY` env var
- All email account CRUD methods pass `db=self` to use the
  instance-level encryptor
- Backward-compatible fallback to module-level singleton for
  migration paths

### Configuration reference

```bash
# Enable email ingestion (default: false)
DOCMIND_EMAIL_ENABLED=true

# Poll interval in seconds (default: 600)
DOCMIND_EMAIL_POLL_INTERVAL=300

# Encryption key for IMAP passwords (required for encrypted storage)
DOCMIND_EMAIL_ENCRYPTION_KEY="your-generated-key"

# Account 0 (Gmail example)
DOCMIND_EMAIL_ACCOUNT_0_NAME="Work Gmail"
DOCMIND_EMAIL_ACCOUNT_0_HOST="imap.gmail.com"
DOCMIND_EMAIL_ACCOUNT_0_PORT="993"
DOCMIND_EMAIL_ACCOUNT_0_USERNAME="you@gmail.com"
DOCMIND_EMAIL_ACCOUNT_0_PASSWORD="abcd efgh ijkl mnop"
```

See `docs/architecture/email-ingestion.md` for full documentation
including provider-specific setup, security guidance, and monitoring.

---

## 2026-07-06 — Phase 7: Search Relevance Tuning

### Added — User-Tunable Search Weights

Users can now adjust the balance between keyword search (FTS5) and
semantic search (vector embeddings) in real time using a slider control
in the search interface. The `vector_weight` parameter ranges from 0
(pure keyword) to 1 (pure semantic), defaulting to 0.5 (balanced).

- **Engine level:** `HybridSearchEngine.search()` accepts `vector_weight`
  parameter and applies it to the score fusion formula (commit fbaae79)
- **API level:** `/search` endpoint parses, clamps, and validates
  `vector_weight` query parameter with 400 on invalid input
  (commits 6df8293, 9ad75e8)
- **UI level:** Slider control on search page and dashboard, with
  ARIA accessibility attributes (commits 50b5af5, a3ff4fa)
- **Search path fix:** Both the search page and chat now use the
  hybrid engine with vector semantic search and score fusion
  (commits 3b0ca0e, 0633cb3)

### Added — Competitive Landscape Research

Created `docs/research-phase7-competitive-landscape.md` documenting
competitive positioning across Paperless-ngx, Teedy, Docspell, and
Mayan EDMS.

---

## 2026-07-06 — Phase 6: Security & Hardening

### Added — API Rate Limiting (Phase 6a)

A per-IP sliding-window rate limiter protects the DocMind API from abuse.
When enabled, each client IP is capped at a configurable number of
requests per rolling 60-second window. Requests exceeding the limit
receive an `HTTP 429 Too Many Requests` response with a `Retry-After`
header so well-behaved clients can back off.

- **Disabled by default** — matches the open behaviour of a self-hosted
  single-user deployment. Enable with `DOCMIND_RATE_LIMIT_ENABLED=true`.
- **Sliding window algorithm.** Per-client-IP timestamp buckets. Old
  entries are pruned on every check — no hard reset at minute boundaries.
- **429 response with Retry-After.** JSON body includes `error` (`"RATE_LIMIT"`),
  `message`, and `retry_after` (seconds). The standard `Retry-After` HTTP
  header carries the same value.
- **Exempt paths.** `/health`, `/login`, `/logout`, `/docs`, `/redoc`,
  `/openapi.json`, and `/static/*` are never rate limited — mirroring the
  auth middleware's public path list.
- **Zero external dependencies.** The limiter runs entirely in-process —
  no Redis, no `slowapi`, no additional Python packages.
- **41 tests** covering sliding-window logic, middleware integration,
  429 response shape, Retry-After header, per-IP isolation, exempt paths,
  and config/env var parsing.

### Configuration reference

```bash
# Enable rate limiting (default: false)
DOCMIND_RATE_LIMIT_ENABLED=true

# Max requests per client IP per 60-second window (default: 60)
DOCMIND_RATE_LIMIT_REQUESTS_PER_MINUTE=120
```

See `docs/architecture/rate-limiting.md` for full documentation.

---

## 2026-07-06 — Phase 5: Performance & Intelligence

### Added — Query Result Caching (Phase 5a)

A transparent caching layer now sits between the Database class and SQLite.
Repeated reads — dashboard stats, document lists, search results, tag clouds —
return from cache instead of hitting the database, cutting response times for
read-heavy pages by up to an order of magnitude.

- **Cache-aside pattern at the Database layer.** Read methods (`get_document`,\n  `list_documents_paginated`, `get_stats`, `search_documents`, and more) check the cache\n  before querying SQLite. Route handlers don't need to know caching exists.
- **Pluggable backends.** In-memory dict by default (zero-config, works out of the box).
  Optional Redis backend for multi-process deployments — swap `DOCMIND_CACHE_BACKEND=redis`
  and point at a Redis instance.
- **Explicit invalidation on every mutation path.** All 14 write operations (upload,
  delete, tag add/remove, collection CRUD, chat mutations, job state changes)
  invalidate affected cache keys so stale data is never returned. Five centralized
  `_invalidate_*` helpers ensure consistency.
- **Category-aware TTLs.** Dynamic lists expire in 30 seconds; stable metadata like
  collections and settings expire in 600 seconds. Each cache category
  has a purpose-fit TTL.
- **Graceful disable.** Set `DOCMIND_CACHE_ENABLED=false` and all cache operations
  become no-ops — zero overhead, zero behaviour change.

### Configuration reference

```bash
# Enable or disable the caching layer (default: on)
DOCMIND_CACHE_ENABLED=true

# Backend: "memory" (default) or "redis"
DOCMIND_CACHE_BACKEND=memory

# Redis connection (only used when backend=redis)
DOCMIND_CACHE_REDIS_URL=redis://localhost:6379/0

# Max entries for the in-memory cache (default: 10000)
DOCMIND_CACHE_MAX_SIZE=10000
```

### Added — LLM-based Document Type Auto-Detection (Phase 5b)

Documents are now classified by type during ingestion. When an LLM provider is
configured, the system sends the document title and first 2000 characters for
classification. When no LLM is available, a keyword-based heuristic provides
a reasonable fallback. Detected types include contracts, reports, meeting minutes,
financial statements, and more.

- Enable/disable with `DOCMIND_AUTODETECT_ENABLED=true` (default: on)
- Adjust the analysed body length with `DOCMIND_AUTODETECT_MAX_BODY_CHARS` (default: 2000)
- Uses the existing LLM configuration — no separate endpoint needed
- Detected types populate the type filter on the documents page

---

## 2026-07-06 — Phase 4: Bulk Operations & OCR

### Added — Tesseract OCR for Scanned PDFs

Scanned PDFs and image-only documents are now processed through Tesseract OCR.
Documents that yield no extractable text through normal PDF parsing are
automatically routed through OCR, recovering content from scanned contracts,
forms, and reports that were previously invisible to search.

- Requires `tesseract` binary on the host (or in the Docker image)
- Falls back gracefully: documents with extractable text skip OCR entirely
- Tested with English and Chinese-language scanned documents

### Added — Bulk Actions Bar

The documents page now includes a bulk actions bar for multi-select operations:

- **Bulk tag:** Assign or remove tags from multiple documents at once
- **Bulk move:** Reassign multiple documents to a different collection
- **Bulk export:** Download selected documents as a ZIP archive
- **Bulk delete:** Delete multiple documents with a single confirmation

### Added — Faceted Search UI

The documents page sidebar now shows live facet counts for file types and data
sources. Click a facet to filter the document list. Counts update as you apply
other filters, so you always see how many documents match.

### Fixed — Silent data loss in storage scanner

Documents with empty extracted bodies (e.g. corrupt or password-protected PDFs)
were silently dropped during the storage scan. They are now skipped with a warning
log entry, preserving the database record so administrators can investigate.

---

## 2026-07-05 — Phase 3: Collections & UI Polish

### Added — Document Collections

Documents can now be organised into hierarchical collections. Key features:

- **Nested tree structure:** Collections can contain sub-collections
- **Tree sidebar:** Browse and filter by collection on the documents page
- **Breadcrumb navigation:** Shows the full path from root to current collection
- **Collection detail pages:** View collection metadata and member documents
- **CRUD operations:** Create, rename, reparent, and delete collections via REST
  API and HTML form endpoints
- 14 Database methods, 9 REST endpoints, 60+ tests

### Added — Hybrid Islands Architecture (ADR-003)

Formalised the client-side interaction model as the Hybrid Islands Architecture:

- Jinja2 SSR as the foundation — no client-side routing, no virtual DOM
- `{% block extra_js %}` defines interactivity islands scoped to individual pages
- HTMX permitted for partial page swaps (live search results, inline form submission)
- Vanilla JS remains the default for stateful interactions (chat, upload, viewer)
- Zero npm dependencies, zero build tooling

### Added — Architecture Decision Records

Created `ARCHITECTURE.md` with formal ADRs:

- **ADR-001:** Jinja2 SSR as the UI framework (no React/Vue)
- **ADR-002:** Hand-rolled schema migrations (no Alembic)
- **ADR-003:** Hybrid Islands Architecture for client-side interactivity

### Added — Collection Management UI

HTML form endpoints for creating and editing collections directly from the web
interface, plus a dedicated collection detail page.

---

## 2026-07-05 — Phase 2: Search, Viewer & Export

### Added

- **Vector/semantic search** with sentence-transformers embeddings and hybrid
  scoring (FTS5 keywords + vector similarity, configurable weight)
- **Document chunking** for granular search and RAG retrieval
- **Document viewer** with formatted content rendering, pagination, table of
  contents sidebar, in-document search, and reading mode
- **Analytics dashboard** with usage statistics, document growth charts,
  search heatmaps, and tag distribution visualisations
- **Answer export** (Markdown/JSON/TXT) and **document summarisation**
  (LLM-driven map-reduce pipeline)
- **API key authentication** (HMAC-SHA256 sessions + X-API-Key header)

### Changed

- Extracted all inline HTML from `server.py` into Jinja2 templates
  (3542 → 1408 LOC in the main server file)

---

## 2026-07-05 — Phase 1: Foundation

### Changed

- **Replaced PostgreSQL with SQLite** for zero-dependency standalone operation.
  No database server needed — just a file at `data/docmind.db`.
- **Recovered Phase 1 features:** SQLite adapter, web UI scaffold, LLM chat
  with WebSocket, chat history persistence, settings management, document tags,
  and job status page.

### Added

- **Dark mode toggle** with automatic persistence via localStorage
- **Pagination** on document lists
- **Document delete** from the web UI
- **Docker support** via multi-stage Dockerfile + docker-compose
- **Multi-file drag-and-drop upload** UI
- **Bulk document delete** (multi-select checkboxes + API endpoint)


## 2026-07-01 — Initial Scaffold

- Project scaffold: OpenAPI spec, PostgreSQL job queue, sanitization layer
- Text extraction pipeline: PDF, DOCX, HTML, Markdown, TXT support
- Search backbone: FTS5 full-text index + backend abstraction
- LLM summarisation with TPM rate limiting
- Hermes Tool plugin: `kb_search`, `kb_list`, `kb_read`, `kb_ingest`
- CLI interface
- 144 integration tests
