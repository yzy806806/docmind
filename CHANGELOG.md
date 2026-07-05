# Changelog

All notable changes to DocMind are documented in this file. The project uses
calendar-based versioning: each section groups changes by the week they shipped.

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

---

## 2026-07-01 — Initial Scaffold

- Project scaffold: OpenAPI spec, PostgreSQL job queue, sanitization layer
- Text extraction pipeline: PDF, DOCX, HTML, Markdown, TXT support
- Search backbone: FTS5 full-text index + backend abstraction
- LLM summarisation with TPM rate limiting
- Hermes Tool plugin: `kb_search`, `kb_list`, `kb_read`, `kb_ingest`
- CLI interface
- 144 integration tests
