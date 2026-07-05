# DocMind Phase 6 Gap Analysis — Updated

## Status: Phase 4 & 5 Complete (1582 tests pass)

From codebase inspection (`git log`, `src/core/`, `src/web/server.py`, `src/docmind/api/app.py`):

- **Phase 4a (OCR)**: ✅ Complete — `src/core/extractor.py` has Tesseract integration
- **Phase 4b (Bulk ops)**: ✅ Complete — bulk tag, move, export in server.py
- **Phase 4c (Faceted search)**: ✅ Complete — file_type/source facets with faceted-filters.js
- **Phase 5a (Caching)**: ✅ Complete — `src/core/cache.py` with InMemoryCache, RedisCache, NoopCache
- **Phase 5b (Document type auto-detection)**: ✅ Complete — `src/core/detector.py` with LLM + keyword fallback

---

## Remaining Gaps vs Competitors (Ranked by Competitive Impact)

### 1. API Rate Limiting (✅ COMPLETE — Phase 6a)

**Implemented**: `src/web/rate_limit.py` — in-memory per-IP sliding window rate limiter. Registered as FastAPI middleware in `src/web/server.py`. Returns 429 with `Retry-After` header. Configurable via `DOCMIND_RATE_LIMIT_ENABLED` and `DOCMIND_RATE_LIMIT_REQUESTS_PER_MINUTE` env vars. 41 tests in `tests/test_rate_limit.py`.

**Docs**: `docs/architecture/rate-limiting.md`, `README.md`, `ARCHITECTURE.md`, `CHANGELOG.md`. Commit: 9a85a8f.

---

### 2. Search Relevance Tuning (MEDIUM-HIGH — user-facing feature gap)

**Current state**: BM25 weighting exists in FTS5 (`src/core/db_sqlite.py`), and `HybridSearchEngine` in `src/core/search.py:334` has hardcoded `vector_weight=0.6`. However, **users cannot tune search relevance** — no UI controls for weight adjustment, no per-field boosting, no query-time tuning.

**Evidence**:
- `src/core/search.py:358`: `vector_weight: float = 0.6` — hardcoded
- `src/core/search.py:374-375`: `self.vector_weight = max(0.0, min(1.0, vector_weight))`
- No UI elements for relevance tuning in templates
- No API parameter for users to adjust weights

**Competitor parity**: Paperless-ngx has advanced search syntax (field-specific, boolean). Mayan has search backend abstraction with tuning. Docspell has Solr/Elasticsearch with relevance configuration.

**Effort**: MEDIUM — requires both backend (query parameter parsing) and UI (sliders/controls in search page).

---

### 3. Responsive/Mobile Design (MEDIUM — usability gap)

**Current state**: `base.html` has `@media (max-width: 640px)` queries and viewport meta tag. `viewer.html` and `analytics.html`/`dashboard.html` also have mobile breakpoints. However, **no mobile-specific testing or optimization** is evident.

**Evidence**:
- `src/web/templates/base.html:5`: `<meta name="viewport" content="width=device-width, initial-scale=1.0">`
- `src/web/templates/base.html:268-269`: `@media (max-width: 640px)`
- `src/web/templates/viewer.html:80`: `@media (max-width: 900px)`
- `src/web/templates/analytics.html:30`: `@media (max-width: 768px)`

**Competitor parity**: Teedy explicitly advertises "responsive design" as a feature. Paperless-ngx has mobile-optimized UI. This is a **usability gap** for users on tablets/phones.

**Effort**: LOW-MEDIUM — mostly CSS adjustments and testing on actual devices.

---

### 4. Email Ingestion (MEDIUM — feature gap)

**Current state**: No email ingestion infrastructure exists. The only reference to "email" is in `src/core/detector.py` where "email" is a document type classification, and `src/docmind/models.py` where users have an email field.

**Evidence**:
- No IMAP/SMTP ingestion code anywhere in `src/`
- No email-related configuration in `config.py`
- `src/core/detector.py:28`: `"email": "Email"` — just a document type label

**Competitor parity**: Paperless-ngx and Docspell both have email consumption (IMAP polling, attachment extraction). This is a **feature gap** but not critical for basic parity.

**Effort**: HIGH — requires IMAP client, attachment parsing, email threading, deduplication, and background polling.

---

### 5. Workflow Automation / Rules (LOW — advanced feature)

**Current state**: No workflow engine exists. No rules, triggers, or automation.

**Competitor parity**: Paperless-ngx has "consumers" (rules-based processing). Mayan EDMS has a full workflow designer. This is an **advanced feature** not required for basic parity.

**Effort**: VERY HIGH — would require a rules engine, trigger system, and action framework.

---

### 6. Keyboard Shortcuts (LOW — UX polish)

**Current state**: Only two keyboard event listeners exist: chat send on Enter (`chat.html:46`) and viewer search on keydown (`viewer.js:115`). No system-wide keyboard shortcuts.

**Competitor parity**: Nice-to-have, not competitive-critical.

**Effort**: LOW — can be added incrementally.

---

## Recommendation: Phase 6 Scope

### Phase 6a: API Rate Limiting (must-fix, LOW effort, HIGH impact)
- Implement FastAPI middleware for request rate limiting
- Use existing config fields (`rate_limit_enabled`, `rate_limit_requests_per_minute`)
- In-memory storage with sliding window (sufficient for single-instance deployment)
- Return 429 with `RateLimitError` (already defined)

### Phase 6b: Search Relevance Tuning (user-facing, MEDIUM effort, MEDIUM-HIGH impact)
- Add `?vector_weight=` query parameter to search endpoints
- Add slider in search UI for FTS vs vector weight
- Consider per-field boost controls (title vs body vs summary)

### Phase 6c: Responsive Design Polish (LOW-MEDIUM effort, MEDIUM impact)
- Test on actual mobile devices/tablets
- Fix any layout issues in document viewer, search, and dashboard
- Add mobile-specific navigation (hamburger menu)

### Deferred to Phase 7 or later:
- Email ingestion (HIGH effort, MEDIUM impact)
- Workflow automation (VERY HIGH effort, LOW impact for basic parity)
- Keyboard shortcuts (LOW effort, LOW impact)
