# Phase 7 Competitive Landscape Research Report

**Date:** 2026-07-06
**Researcher:** researcher (DocMind Agora team)
**Scope:** Competitive parity analysis for Phase 7 direction

---

## 1. Search Relevance Features in Competing Document/Knowledge Tools

### What Competitors Offer

| Tool | Hybrid Search | Relevance Tuning | Ranking Controls |
|------|-------------|------------------|------------------|
| **Paperless-ngx** (25k+ stars) | Whoosh/Elasticsearch — yes | Advanced search syntax (field-specific, boolean) | Configurable via search backend |
| **Teedy** (5k+ stars) | Lucene — yes | Basic relevance scoring | Limited user controls |
| **Docspell** (3k+ stars) | Solr/Elasticsearch — yes | Relevance configuration | Backend-tunable weights |
| **Mayan EDMS** (4k+ stars) | Elasticsearch/Whoosh — yes | Search backend abstraction with tuning | Configurable per-backend |

### DocMind Current State

- **HybridSearchEngine exists** (`src/core/search.py:334`) with FTS5 + vector fusion
- **vector_weight is hardcoded to 0.6** — no user exposure
- **No UI controls** for relevance tuning (sliders, per-field boost, query-time weights)
- **Web search endpoint** (`/search` at `src/web/server.py:504`) now uses `HybridSearchEngine` (fixed in commit `3b0ca0e`), but still passes no tuning parameters
- **36 tests** in `tests/test_hybrid_search.py` cover the engine, but no integration tests for user-tunable weights

### Evidence
```python
# src/core/search.py:358 — hardcoded weight
vector_weight: float = 0.6,

# src/web/server.py:526-528 — search endpoint uses hybrid engine but no weight param
hybrid_engine = getattr(app.state, "hybrid_engine", None)
if hybrid_engine is not None:
    raw_results = await hybrid_engine.search(validated_q, top_k=20)
```

### Competitive Assessment
**Gap severity: MEDIUM-HIGH.** DocMind has the plumbing (hybrid search) but lacks user-facing controls. Competitors offer at least backend-tunable relevance; Paperless-ngx offers advanced search syntax. This is a **differentiation gap** — users cannot fine-tune results.

---

## 2. Email Ingestion: Standard Feature or Niche Add-on?

### Competitor Analysis

| Tool | Email Ingestion | Implementation |
|------|-----------------|----------------|
| **Paperless-ngx** | ✅ Yes | IMAP polling, attachment extraction, email threading, consumption rules |
| **Docspell** | ✅ Yes | Email integration (IMAP), attachment handling |
| **Teedy** | ❌ No | Not mentioned in feature set |
| **Mayan EDMS** | ❌ No | Not a core feature |

### Assessment
- **2 out of 4 major competitors** offer email ingestion (Paperless-ngx, Docspell)
- It is **not universal** — Teedy and Mayan EDMS do not prioritize it
- Where it exists, it is **deep integration** (IMAP polling, rules, deduplication)
- **Effort estimate: HIGH** — requires IMAP client, attachment parsing, email threading, deduplication, background polling

### DocMind Current State
- **No email ingestion infrastructure** exists
- Only references to "email" are: document type label (`detector.py`), user model field (`models.py`), and search analytics
- No IMAP/SMTP configuration in `config.py`

### Competitive Assessment
**Gap severity: MEDIUM.** Nice-to-have for parity with Paperless-ngx, but not a table-stakes feature. Two of four competitors lack it.

---

## 3. Keyboard Shortcuts / Workflow Automation: Expected or Nice-to-Have?

### Competitor Analysis

| Tool | Keyboard Shortcuts | Workflow Automation |
|------|-------------------|---------------------|
| **Paperless-ngx** | Limited | ✅ Yes — "consumers" (rules-based processing) |
| **Teedy** | Not emphasized | Not emphasized |
| **Docspell** | Not emphasized | Not emphasized |
| **Mayan EDMS** | Not emphasized | ✅ Yes — full workflow designer |

### Assessment
- **Workflow automation** is an **advanced feature** present in only 2/4 competitors (Paperless-ngx, Mayan)
- **Keyboard shortcuts** are **not a competitive differentiator** in this category
- No competitor advertises keyboard shortcuts as a core feature
- Users in this space prioritize: upload → OCR → search → retrieve

### DocMind Current State
- **No keyboard shortcuts** exist beyond: Enter to send chat (`chat.html:46`), viewer search on keydown (`viewer.js:115`)
- **No workflow automation** exists
- `grep -rn "keyboard\|shortcut\|hotkey" src/web/` returns zero results

### Competitive Assessment
**Gap severity: LOW.** Neither feature is expected by users in document management. Workflow automation is advanced; keyboard shortcuts are UX polish.

---

## 4. Responsive Design Status

### DocMind Current State
- **Phase 6b complete** (commit `5b7e18c` + `5eef54e`)
- CSS extracted to `src/web/static/css/styles.css` (1481 lines)
- Breakpoints: 480px, 768px, 1024px
- **143 responsive tests** pass across `test_responsive.py` (1128 lines) and `test_responsive_design.py` (390 lines)
- Touch targets, dark theme, reduced motion all covered

### Competitive Assessment
**Gap: CLOSED.** Responsive design is now at parity with competitors.

---

## Summary Table

| Feature | DocMind Status | Competitor Prevalence | Priority |
|---------|---------------|----------------------|----------|
| Hybrid search (backend) | ✅ Complete | 4/4 | — |
| Search relevance tuning (user-facing) | 🟡 Partial — hardcoded weight | 3/4 have some form | **MEDIUM-HIGH** |
| Responsive design | ✅ Complete | 3/4 | CLOSED |
| Email ingestion | 🔴 Missing | 2/4 | MEDIUM |
| Keyboard shortcuts | 🔴 Missing | 0/4 advertise | LOW |
| Workflow automation | 🔴 Missing | 2/4 | LOW |

---

## Recommendation for Phase 7

**Primary: Search Relevance Tuning**

Rationale:
1. **Highest impact for user experience** — DocMind already has hybrid search plumbing; exposing it to users is a thin layer
2. **Lowest effort among remaining MEDIUM+ gaps** — Backend exists; only needs query param + UI slider
3. **Direct competitive parity** — Closes gap with Paperless-ngx, Docspell, Mayan EDMS
4. **Architect confirms thin implementation** — "thin API+UI layer on top of existing plumbing"

**Secondary (deferred): Email Ingestion**
- HIGH effort, MEDIUM impact
- Only 2/4 competitors have it
- Better suited for Phase 8 or later

**Tertiary (deferred): Keyboard Shortcuts / Workflow Automation**
- LOW competitive priority
- Not expected by users in this category
