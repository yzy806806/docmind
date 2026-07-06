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
| Search relevance tuning / boosting | 🟡 Partial | Medium | Phase 7: vector_weight parameter implemented at engine and API level (fbaae79, 6df8293, 9ad75e8); UI control in progress |
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
| Search path architectural disconnect (web search bypasses HybridSearchEngine) | ✅ Fixed | - | HybridSearchEngine is now wired into the web search endpoint (commits 3b0ca0e, 0633cb3). The search page and chat both use the hybrid engine with vector semantic search and score fusion. |

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

8. **Search relevance tuning** — Phase 7 in progress. The `vector_weight` query parameter is implemented at the engine and API levels (commits fbaae79, 6df8293, 9ad75e8), giving users tunable control over FTS5 vs. vector score weighting. UI control is the remaining piece.

9. **Search path architectural disconnect** — ✅ Fixed (Phase 7). HybridSearchEngine is now wired into the web search endpoint (commits 3b0ca0e, 0633cb3). Both the search page and chat use the hybrid engine with vector semantic search and score fusion.

### Lower Priority Gaps

8. **Email ingestion** — Nice to have but not critical for initial parity.

9. **Workflow automation** — Advanced feature, not required for basic parity.

10. **Keyboard shortcuts** — UX polish, not competitive-critical.

---

## Recommendation

**Phase 4 (Document Processing Pipeline) is complete.** OCR (4a), bulk operations (4b), and faceted search (4c) are all implemented and tested.

**Phase 5 (Performance & Intelligence) is complete.** Query result caching (5a) and LLM-based document type auto-detection (5b) are both implemented, tested, and committed. 1823 tests pass.

**Phase 6a is complete.** API rate limiting is implemented with in-memory per-IP sliding window middleware, returning HTTP 429 with Retry-After header when limits are exceeded. 41 tests cover the sliding-window logic, middleware integration, and 429 response shape.

**Phase 7 (Search Relevance) is in progress.** The `vector_weight` query parameter is implemented at the engine level (`HybridSearchEngine.search()`) and exposed via the `/search` API endpoint, giving users tunable control over FTS5 vs. vector score weighting. The UI slider control is the next deliverable.

**Remaining gaps to close:**
1. **Responsive design polish** — validate and improve mobile UX (medium priority)
2. **Search relevance tuning** — complete the UI control for `vector_weight` (medium priority, Phase 7 in progress)
3. **Email ingestion** — close the feature gap with Paperless-ngx and Docspell (medium priority)
4. **Keyboard shortcuts** — UX polish (low priority)
5. **Workflow automation** — advanced rules engine (low priority)
