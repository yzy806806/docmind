# Phase 4b/4c Architecture Design: Bulk Operations + Faceted Search

**Status:** Draft  
**Author:** architect  
**Date:** 2026-07-06  
**Applies to:** docmind `master` (post-Phase 4a OCR)  

---

## 1. Context & Goals

This document designs two high-priority features identified in `docs/gap-analysis.md`:

- **Phase 4b — Bulk Operations:** Extend the existing single bulk-delete into a full bulk-action toolbar (tag, move to collection, export, delete).
- **Phase 4c — Faceted Search:** Add facet counts (file type, source, tag, collection, date) to the search and documents pages so users can filter by clicking facet values.

**Design principles (ADR-003):**
- Hybrid Islands: vanilla JS in `src/web/static/js/`, HTMX for partial swaps, no build step.
- Progressive enhancement: every JS feature must have a non-JS fallback (form POST).
- SQLite-first: all aggregations must work on SQLite without external dependencies.

---

## 2. Phase 4b: Bulk Operations

### 2.1 Current State

- `DELETE /api/v1/documents/bulk` + `POST /documents/bulk-delete` exist.
- The documents table already has row-level checkboxes and a "Delete Selected" button.
- `documents-list.js` handles `toggleSelectAll`, `updateDeleteButton`, and `confirmBulkDelete`.

### 2.2 UI Design

#### 2.2.1 Documents Table (existing)

Replace the single "Delete Selected" button with a **bulk action toolbar** that appears when ≥1 row is checked:

```
┌─────────────────────────────────────────────────────────────┐
│ [✓] Select all   │  🏷 Tag Selected  │  📁 Move to Coll. │  ⬇ Export  │  🗑 Delete │
└─────────────────────────────────────────────────────────────┘
```

- **Visibility:** Hidden when no checkboxes are selected; slides in via CSS when `checked.length > 0`.
- **Position:** Fixed bar above the table (inside `#doc-table-region` so it swaps with HTMX).
- **Each action button** opens a small inline form (or modal) for the action parameters:
  - **Tag:** text input for tag name + "Add" / "Remove" toggle.
  - **Move to Collection:** `<select>` of existing collections.
  - **Export:** format selector (Markdown, JSON, CSV, TXT) + "Export" button.
  - **Delete:** confirmation dialog (reuse existing `confirmBulkDelete`).

#### 2.2.2 Non-JS Fallback

The bulk action bar is a `<form method="post" action="/documents/bulk-action">` with a hidden `action_type` field. Each action button is a submit button that sets `action_type`. The server routes to the correct handler. Without JS, the user sees a full-page form post with a success/error message.

### 2.3 API Design

**Decision: Unified bulk-action endpoint (avoids 4 near-identical endpoints).**

```
POST /api/v1/documents/bulk
Content-Type: application/json

Body:
{
  "action": "delete" | "tag" | "untag" | "move" | "export",
  "doc_ids": [1, 2, 3],
  // action-specific fields:
  "tag": "invoice",           // for "tag" / "untag"
  "collection_id": 5,         // for "move"
  "format": "csv"             // for "export" (csv, json, md, txt)
}
```

**Response (200 OK):**
```json
{
  "action": "tag",
  "requested_count": 3,
  "processed_count": 2,
  "skipped_count": 1,
  "skipped_reasons": [
    {"doc_id": 3, "reason": "document_not_found"}
  ],
  "result_url": null  // for export: URL to download the generated file
}
```

**Partial failure strategy:**
- Every doc ID is processed independently inside a single transaction per doc.
- If a doc is not found, it is recorded in `skipped_reasons` but does not fail the whole batch.
- If all doc IDs are invalid/not found, return 200 with `processed_count: 0` and all items in `skipped_reasons`.
- If the request itself is malformed (invalid action, missing required fields), return 400.

**Export-specific behavior:**
- `bulk-export` generates a ZIP file containing one file per document in the requested format.
- The ZIP is written to a temp directory, and `result_url` points to `/api/v1/documents/bulk-export/download/{token}`.
- Temp files are cleaned up after 1 hour (cron or at-exit cleanup).

### 2.4 Form Handler (non-JS)

```
POST /documents/bulk-action
Form data:
  action_type=tag&tag_name=invoice&doc_ids=1&doc_ids=2&doc_ids=3
```

Server behavior:
1. Parse `doc_ids[]` from form.
2. Validate each ID via `validate_doc_id`.
3. Dispatch to the appropriate DB method.
4. Redirect to `/documents?bulk_success=1&bulk_action=tag` with a flash message.

### 2.5 DB Layer Changes

Add to `src/core/db_sqlite.py`:

```python
async def bulk_tag(self, doc_ids: list[int], tag: str, *, remove: bool = False) -> dict[str, Any]:
    """Add or remove a tag from multiple documents.

    Returns a summary dict with processed/skip counts and per-doc status.
    """

async def bulk_move_to_collection(self, doc_ids: list[int], collection_id: int) -> dict[str, Any]:
    """Move multiple documents to a collection.

    Documents that don't exist or are already in the target collection are skipped.
    """

async def bulk_export(self, doc_ids: list[int], fmt: str) -> str:
    """Export multiple documents to a ZIP file.

    Returns a download token. The actual file generation is async (job queue).
    """
```

**Implementation notes:**
- `bulk_tag`: Use `INSERT OR IGNORE` for adds; `DELETE` for removes. Wrap in a transaction for each doc.
- `bulk_move`: `UPDATE documents SET collection_id = ? WHERE id IN (...)` — single query, then verify which IDs were actually updated.
- `bulk_export`: Create a background job (`JobQueue`) that generates the ZIP. The API returns a 202 Accepted with a job ID. Poll `GET /api/v1/jobs/{job_id}` for status.

### 2.6 HTMX Partial Swap Strategy

The bulk action bar lives inside `#doc-table-region`. After any bulk action:
- On success: HTMX swaps the table region (which includes the action bar) with fresh data.
- On error: The error message is rendered inline above the table.
- For export: A download link appears in the action bar after the job completes.

### 2.7 JS Changes

Extend `src/web/static/js/documents-list.js`:

```javascript
// New functions:
function showBulkActionBar() { /* toggle visibility */ }
function hideBulkActionBar() { /* toggle visibility */ }
function getSelectedDocIds() { /* return array of checked doc IDs */ }
function submitBulkAction(actionType, params) { /* fetch POST to /api/v1/documents/bulk */ }
```

**No new dependencies.** All vanilla JS.

---

## 3. Phase 4c: Faceted Search

### 3.1 Current State

- `/search?q=...` uses FTS5 + BM25 ranking. No facets.
- `/documents` has filter inputs (source, tag, date range, file type) but no facet counts.
- `src/core/db_sqlite.py` has `_build_filter_clause` for filtering, but no aggregation methods.

### 3.2 Facet Design

Facets are **filter-value counts** shown as a sidebar. Clicking a facet value narrows the result set.

**Facets to implement:**

| Facet | Source Column | Example Values |
|-------|---------------|----------------|
| File Type | `ext` | .pdf, .docx, .txt |
| Source | `source_name` | api, webdav, local |
| Tag | `document_tags.tag` | invoice, contract |
| Collection | `collections.name` | Research, Personal |
| Date | `created_at` | 2026-07, 2026-06 |

### 3.3 Where to Show Facets

**Decision: Add facets to BOTH `/documents` and `/search` pages.**

- `/documents`: Facets help users browse and discover documents without a search query.
- `/search`: Facets help users narrow down search results.

Both pages share the same facet component (rendered via Jinja2 include).

### 3.4 DB Layer: Facet Count Queries

Add to `src/core/db_sqlite.py`:

```python
async def get_facet_counts(
    self,
    *,
    query: Optional[str] = None,  # if set, restrict to FTS-matched docs
    source: Optional[str] = None,
    collection_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    file_type: Optional[str] = None,
    tag: Optional[str] = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return facet counts for the current filter context.

    Returns a dict like:
    {
        "file_type": [{"value": ".pdf", "count": 42}, ...],
        "source": [{"value": "api", "count": 15}, ...],
        "tag": [{"value": "invoice", "count": 8}, ...],
        "collection": [{"value": "Research", "count": 3}, ...],
        "date_month": [{"value": "2026-07", "count": 12}, ...],
    }
    """
```

**Implementation strategy (SQLite):**

For each facet, run a separate `SELECT ... GROUP BY` query. SQLite handles this efficiently with the existing indexes.

**Example: File type facet**
```sql
SELECT ext AS value, COUNT(*) AS count
FROM documents
WHERE {existing_filter_clause}
GROUP BY ext
ORDER BY count DESC
LIMIT 20;
```

**Example: Tag facet**
```sql
SELECT dt.tag AS value, COUNT(DISTINCT dt.doc_id) AS count
FROM document_tags dt
JOIN documents d ON d.id = dt.doc_id
WHERE {existing_filter_clause}
GROUP BY dt.tag
ORDER BY count DESC
LIMIT 20;
```

**Example: Date month facet**
```sql
SELECT strftime('%Y-%m', created_at) AS value, COUNT(*) AS count
FROM documents
WHERE {existing_filter_clause}
GROUP BY value
ORDER BY value DESC
LIMIT 12;
```

**Performance consideration:**
- Run all 5 facet queries in parallel using `asyncio.gather()`.
- Each query is a single indexed scan. With the existing indexes (`idx_documents_status`, `idx_documents_source`, `idx_document_tags_tag`), this should be fast for datasets up to ~100k docs.
- If facet queries become slow, add a covering index: `CREATE INDEX idx_documents_facet ON documents(ext, source_name, created_at);`

### 3.5 UI Design

#### 3.5.1 Facet Sidebar Component

```
┌─────────────────────────────┐
│ Filters                     │
│ ┌─────────────────────────┐ │
│ │ File Type               │ │
│ │ ☑ .pdf (42)             │ │
│ │ ☐ .docx (15)            │ │
│ │ ☐ .txt (8)              │ │
│ └─────────────────────────┘ │
│ ┌─────────────────────────┐ │
│ │ Source                  │ │
│ │ ☑ api (15)              │ │
│ │ ☐ webdav (3)           │ │
│ └─────────────────────────┘ │
│ ┌─────────────────────────┐ │
│ │ Tags                    │ │
│ │ ☐ invoice (8)           │ │
│ │ ☐ contract (5)          │ │
│ └─────────────────────────┘ │
│ ...                         │
└─────────────────────────────┘
```

- **Collapsible panels:** Each facet is a `<details>` element (consistent with existing filter panel on `/documents`).
- **Active state:** Checked facets are highlighted. Clicking again removes the filter.
- **Count display:** `(count)` next to each value.
- **HTMX:** Clicking a facet value triggers an HTMX GET to `/documents/partials/table` (or `/search/partials/results`) with the new filter params. The facet sidebar itself is also re-rendered with updated counts.

#### 3.5.2 Search Results Page

Add a facet sidebar to `search_results.html`:

```
┌─────────────────────────────────────────────────────────────┐
│ Search Results                                               │
│ ┌──────────────┬──────────────────────────────────────────┐  │
│ │ Facets       │ Results                                  │  │
│ │              │                                          │  │
│ │ [File Type]  │  Result 1                                │  │
│ │   .pdf (42)  │  Result 2                                │  │
│ │   .docx (15) │  ...                                     │  │
│ │              │                                          │  │
│ └──────────────┴──────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### 3.6 HTMX Partial Swap Strategy

**New endpoint:**

```
GET /search/partials/results?q=...&facet_file_type=...&facet_tag=...
```

Returns: HTML fragment of search results + facet sidebar.

**Existing endpoint (extended):**

```
GET /documents/partials/table?...&facet_file_type=...&facet_tag=...
```

Returns: HTML fragment of document table + facet sidebar.

**Swap targets:**
- `#search-results-region` for search page.
- `#doc-table-region` for documents page (already exists).

### 3.7 URL Parameter Design

Facet filters are URL query parameters:

```
/documents?facet_file_type=.pdf&facet_tag=invoice&facet_source=api
/search?q=contract&facet_file_type=.pdf&facet_collection=5
```

This ensures:
- Bookmarkable/shareable filtered views.
- Back button works naturally.
- Non-JS fallback works (normal GET form submission).

### 3.8 Form Handler (non-JS)

The facet sidebar is a `<form method="get">` with checkbox inputs. Submitting the form reloads the page with the new query params. HTMX-enhanced clients get partial swaps.

---

## 4. Dependency Table

| # | Task | Assignee | Depends on | Est. Effort |
|---|------|----------|------------|-------------|
| 1 | Extend `documents-list.js` for bulk action bar | developer | — | 2h |
| 2 | Add `POST /api/v1/documents/bulk` unified endpoint | developer | — | 3h |
| 3 | Add `POST /documents/bulk-action` form handler | developer | #2 | 1h |
| 4 | Add DB methods: `bulk_tag`, `bulk_move`, `bulk_export` | developer | — | 3h |
| 5 | Add bulk export ZIP generation (background job) | developer | #4 | 3h |
| 6 | Update `documents_table.html` partial with bulk action bar | developer | #1 | 2h |
| 7 | Add `get_facet_counts` DB method | developer | — | 2h |
| 8 | Add facet sidebar Jinja2 component | developer | #7 | 2h |
| 9 | Integrate facets into `/documents` page | developer | #8 | 2h |
| 10 | Add `/search/partials/results` endpoint | developer | #8 | 2h |
| 11 | Integrate facets into `/search` page | developer | #10 | 2h |
| 12 | Add tests for bulk operations API | tester | #2, #3 | 3h |
| 13 | Add tests for facet counts | tester | #7 | 2h |
| 14 | Add tests for facet UI/HTMX | tester | #9, #11 | 2h |
| 15 | Update ADR-003 docs if conventions change | writer | — | 1h |

**Total estimated effort:** ~34 dev hours + ~7 test hours.

---

## 5. Open Questions

1. **Bulk export file size limit:** Should we cap the ZIP size (e.g., 100MB) or number of documents? → Recommend: cap at 500 docs or 100MB, whichever comes first.
2. **Facet count caching:** Should we cache facet counts for 30 seconds to reduce DB load? → Recommend: defer caching until performance is proven to be a problem. SQLite is fast enough for now.
3. **Date facet granularity:** Month-level is proposed. Should we also support year and day? → Recommend: start with month; add year/day if users ask.

---

## 6. Acceptance Criteria

- [ ] Bulk action bar appears on `/documents` when checkboxes are selected.
- [ ] Bulk tag, move, export, and delete all work via both JS and non-JS paths.
- [ ] Partial failures in bulk operations are reported per-document.
- [ ] Facet sidebar shows on both `/documents` and `/search` pages.
- [ ] Clicking a facet value filters results and updates URL params.
- [ ] HTMX partial swaps work for both table and search results.
- [ ] All new endpoints have tests (API + form handlers).
- [ ] No new dependencies introduced (stays within ADR-003 constraints).
