# DocMind Competitive Gap Analysis

## Competitor Feature Comparison

### Paperless-ngx (most popular, 25k+ stars)
- **Core features**: Document ingestion (email, upload, consume), OCR (Tesseract), full-text search (Whoosh/Elasticsearch), tagging, metadata extraction, document types, correspondents, consumers
- **Notable UX**: Dashboard with statistics, drag-and-drop upload, bulk operations, document viewer with OCR text overlay, email consumption, workflow automation (rules)
- **Performance**: Background task processing (Celery), Redis caching, database indexing, lazy loading for large document sets

### Teedy (5k+ stars)
- **Core features**: Document management, OCR (Tesseract), full-text search, tagging, metadata, sharing, workflow
- **Notable UX**: Clean web UI, responsive design, bulk upload, document preview, user management
- **Performance**: Lucene search index, background processing, caching

### Docspell (3k+ stars)
- **Core features**: Document ingestion, OCR (Tesseract/OCRmyPDF), full-text search (Solr/Elasticsearch), tagging, metadata extraction, file types, user management
- **Notable UX**: Web UI, email integration, bulk operations, document viewer, search with filters
- **Performance**: Background processing (Joob), database indexing, search backend abstraction

### Mayan EDMS (4k+ stars)
- **Core features**: Document management, OCR (Tesseract), full-text search (Elasticsearch/Whoosh), metadata, workflows, digital signatures, retention policies, ACLs
- **Notable UX**: Web UI, document preview, bulk operations, advanced search, workflow designer
- **Performance**: Celery task queue, Redis caching, database optimization, search backend abstraction

---

## DocMind Current Feature Status

### What DocMind Has (from codebase analysis)

| Feature | Status | Evidence |
|---------|--------|----------|
| Multi-source ingestion (WebDAV, local dir, PostgreSQL) | ✅ Have | `src/core/storage.py` |
| Multi-format extraction (PDF, DOCX, HTML, MD, TXT) | ✅ Have | `src/core/extractor.py` |
| FTS5 full-text search | ✅ Have | `src/core/db_sqlite.py` |
| Vector semantic search | ✅ Have | `src/core/search.py`, `src/core/embeddings.py` |
| Hybrid search (FTS5 + vector) | ✅ Have | `src/core/search.py` |
| Document chunking | ✅ Have | `src/core/chunking.py` |
| LLM summarization | ✅ Have | `src/core/summarizer.py` |
| Web UI (Jinja2 SSR) | ✅ Have | `src/web/server.py`, templates/ |
| Document viewer with pagination | ✅ Have | `src/web/document_viewer.py` |
| Chat/Q&A with citations | ✅ Have | `src/web/chat.py` |
| Analytics dashboard | ✅ Have | `src/web/server.py` (dashboard, analytics) |
| Dark mode | ✅ Have | `theme.js` |
| API key auth | ✅ Have | `src/web/auth.py` |
| Collections (hierarchical) | ✅ Have | `src/web/server.py` (collection routes) |
| Tags and metadata | ✅ Have | `src/web/server.py` (tag routes) |
| Bulk delete | ✅ Have | `src/web/server.py` (bulk delete) |
| Export (Markdown, JSON, CSV, TXT) | ✅ Have | `src/web/services.py` |
| Job queue / async processing | ✅ Have | `src/core/job_queue.py` |
| Hermes tool integration | ✅ Have | `src/hermes_plugin.py` |
| Docker support | ✅ Have | Dockerfile, docker-compose.yml |
| Rate limiting (TPM for LLM) | ✅ Have | `src/core/summarizer.py` |

### What DocMind Is Missing (gaps vs competitors)

| Feature | Status | Priority | Notes |
|---------|--------|----------|-------|
| OCR for scanned PDFs/images | ✅ Have | - | Tesseract OCR via pytesseract, scanned PDF fallback, image file support (Phase 4a done) |
| Email ingestion | 🔴 Missing | Medium | Paperless-ngx, Docspell have this |
| Bulk operations (beyond delete) | ✅ Have | - | Bulk tag, move, export all implemented (Phase 4b done) |
| Faceted search (filters by type, date, tags) | ✅ Have | - | file_type and source facets with UI, faceted-filters.js (Phase 4c done) |
| Search relevance tuning / boosting | 🟡 Partial | Medium | BM25 weighting exists but no user-tunable relevance |
| Document type detection | ✅ Have | - | LLM-based auto-detection with keyword fallback (Phase 5b done) |
| Workflow automation / rules | 🔴 Missing | Low | Paperless-ngx consumers, Mayan workflows |
| Redis caching | ✅ Have | - | In-memory dict cache with pluggable Redis backend, TTL eviction, invalidation on all mutations (Phase 5a done) |
| Database query optimization / indexing | 🟡 Partial | Medium | Basic indexes exist, no advanced optimization |
| Responsive design | 🟡 Partial | Medium | Mobile layout in base.html but not thoroughly tested |
| Keyboard shortcuts | 🔴 Missing | Low | No keyboard shortcut system found |
| Lazy loading for large lists | 🟡 Partial | Medium | Pagination exists but no infinite scroll |
| REST API coverage (complete CRUD) | 🟡 Partial | Medium | Most endpoints exist, some gaps in collection detail route |
| Rate limiting (API) | ✅ Have | - | Per-IP sliding window middleware, 429 + Retry-After, configurable via env vars (Phase 6a done) |
| Full-text search in document content | ✅ Have | - | Already implemented |
| Search path architectural disconnect (web search bypasses HybridSearchEngine) | 🔴 Tech Debt | Medium | server.py:511 calls db.fulltext_search() directly, skipping vector semantic search that HybridSearchEngine provides. Chat uses the hybrid engine; the search page does not. |

---

## Gap Analysis Summary

### Highest Priority Gaps (blocking competitive parity)

All three highest-priority gaps from the original analysis have been closed:

1. **OCR for scanned documents** — ✅ Complete (Phase 4a). Tesseract OCR via pytesseract, scanned PDF fallback, and image file support are all implemented and tested.

2. **Bulk operations** — ✅ Complete (Phase 4b). Bulk tag, bulk move to collection, and bulk export are all implemented with server-side endpoints and a bulk actions bar in the documents table.

3. **Faceted search** — ✅ Complete (Phase 4c). File type and source facets with a faceted filters UI (`faceted-filters.js`) are integrated into the search interface.

### Medium Priority Gaps (next steps)

4. **Redis/caching layer** — ✅ Complete (Phase 5a). In-memory dict cache with pluggable Redis backend, TTL eviction, and invalidation on all 14 mutation paths.

5. **Document type auto-detection** — ✅ Complete (Phase 5b). LLM-based classification with keyword fallback during ingestion.

6. **Responsive design polish** — While base.html has mobile styles, the UX on mobile needs validation and likely improvement.

7. **API rate limiting** — ✅ Complete (Phase 6a). Per-IP sliding window middleware. 429 response with Retry-After header. 41 tests.

8. **Search relevance tuning** — BM25 weighting exists but no user-tunable relevance controls.

9. **Search path architectural disconnect** — The web search endpoint (`/search` at server.py:511) calls `db.fulltext_search()` directly, bypassing the `HybridSearchEngine` in `src/core/search.py`. This means the search page only uses FTS5/BM25 keyword search, missing out on the vector semantic search and score fusion that the chat feature (`src/web/chat.py:265`) already uses via `HybridSearchEngine`. The hybrid engine exists and is tested (36 tests in `tests/test_hybrid_search.py`), but it's not wired into the main search UI. Fixing this would give all users the improved ranking quality currently reserved for chat.

### Lower Priority Gaps

8. **Email ingestion** — Nice to have but not critical for initial parity.

9. **Workflow automation** — Advanced feature, not required for basic parity.

10. **Keyboard shortcuts** — UX polish, not competitive-critical.

---

## Recommendation

**Phase 4 (Document Processing Pipeline) is complete.** OCR (4a), bulk operations (4b), and faceted search (4c) are all implemented and tested.

**Phase 5 (Performance & Intelligence) is complete.** Query result caching (5a) and LLM-based document type auto-detection (5b) are both implemented, tested, and committed. 1582 tests pass.

**Phase 6a is complete.** API rate limiting is implemented with in-memory per-IP sliding window middleware, returning HTTP 429 with Retry-After header when limits are exceeded. 41 tests cover the sliding-window logic, middleware integration, and 429 response shape.

**Next phase should focus on: Polish & Feature Parity**

Remaining gaps to close:
1. **Responsive design polish** — validate and improve mobile UX (medium priority)
2. **Search relevance tuning** — add user-tunable relevance controls (medium priority)
3. **Search path architectural disconnect** — wire HybridSearchEngine into the web search endpoint (medium priority, tech debt)
4. **Email ingestion** — close the feature gap with Paperless-ngx and Docspell (medium priority)
5. **Keyboard shortcuts** — UX polish (low priority)
6. **Workflow automation** — advanced rules engine (low priority)
