# Phase 5b Architecture Design: LLM-Based Document Type Auto-Detection

**Status:** Draft
**Author:** developer (architect's design task t_65389e10 still pending — implemented from codebase patterns and motion requirements)
**Date:** 2026-07-06
**Applies to:** docmind `master` (post-Phase 5a caching)

---

## 1. Context & Goals

When documents are ingested, they are stored with basic metadata
(title, extension, mime type, body). However, the *semantic document type*
— invoice, contract, research paper, resume, email, etc. — is not
classified. This phase adds LLM-based document type auto-detection that
runs during ingestion and classifies each document into a predefined
category.

**Goals:**
1. Automatically classify documents by type during ingestion.
2. Store the detected type in the documents table (`document_type` column).
3. Allow filtering/searching by document type.
4. Gracefully degrade when no LLM is configured (keyword-based fallback).
5. Support manual re-detection via API.

**Design principles (ADR-003):**
- SQLite-first: no external services required.
- Graceful degradation: LLM not configured → keyword-based heuristic.
- Non-blocking: detection runs after indexing, doesn't block ingestion.

---

## 2. Document Type Taxonomy

Predefined types covering common document categories:

| Type Key | Display Name | Keywords (fallback heuristic) |
|----------|-------------|-------------------------------|
| invoice | Invoice | invoice, amount due, billing, remit |
| contract | Contract | agreement, parties, terms, whereas, hereby |
| resume | Resume | experience, education, skills, references |
| email | Email | from:, to:, subject:, sent, dear |
| research_paper | Research Paper | abstract, methodology, references, citation |
| report | Report | executive summary, findings, recommendations |
| receipt | Receipt | total, cash, card, change, thank you |
| letter | Letter | dear, sincerely, regards, to whom |
| form | Form | fill, checkbox, sign, date of birth |
| presentation | Presentation | slides, agenda, overview, key takeaways |
| spreadsheet | Spreadsheet | sheet, cell, formula, row, column |
| manual | Manual | instructions, steps, warning, caution |
| article | Article | byline, published, author, paragraph |
| other | Other | (fallback when no match) |

This taxonomy is extensible — new types can be added to
`DocumentTypeDetector.DOCUMENT_TYPES` without schema changes.

---

## 3. Component Design

### 3.1 DocumentDetector (`src/core/detector.py`)

```python
class DocumentDetector:
    """LLM-powered document type detection with keyword fallback."""

    DOCUMENT_TYPES: dict[str, str]  # type_key -> display_name
    KEYWORD_MAP: dict[str, list[str]]  # type_key -> keywords

    def __init__(self, llm_client: LLMClient | None = None):
        ...

    async def detect(
        self,
        title: str,
        body: str,
        *,
        ext: str = "",
        max_body_chars: int = 2000,
    ) -> str:
        """Detect document type. Returns a type_key string.

        If LLM is configured, sends a classification prompt with the
        first N chars of the document body. If not configured or the
        LLM call fails, falls back to keyword-based heuristic.
        """
        ...

    def _detect_keyword(self, title: str, body: str) -> str:
        """Keyword-based fallback heuristic."""
        ...

    def _build_detection_prompt(
        self, title: str, body_excerpt: str, ext: str
    ) -> list[dict[str, str]]:
        """Build LLM messages for document type classification."""
        ...

    def _parse_llm_response(self, response: str) -> str:
        """Extract type_key from LLM response, validate against taxonomy."""
        ...
```

### 3.2 Prompt Strategy

The LLM receives:
1. A system prompt listing the valid type keys.
2. A user message with the document title, file extension, and a body
   excerpt (truncated to 2000 chars to control token cost).

The LLM is instructed to respond with **only** the type_key, nothing else.

```
System: You are a document classification assistant. Classify the
document into exactly one of these types: invoice, contract, resume,
email, research_paper, report, receipt, letter, form, presentation,
spreadsheet, manual, article, other. Respond with ONLY the type key,
no explanation.

User:
Title: {title}
Extension: {ext}
Body (first 2000 chars):
{body_excerpt}
```

### 3.3 Fallback Handling

When no LLM is configured (`LLMClient.is_configured == False`):
- Run `_detect_keyword()` which scores each type by counting keyword
  occurrences in title + body (case-insensitive).
- Return the highest-scoring type, or "other" if all scores are 0.

When the LLM call fails (network error, timeout, invalid response):
- Log the error, fall back to keyword heuristic.
- Never raise — detection failure must not break ingestion.

### 3.4 Non-Text Documents

For image-only files (scanned PDFs, images) where OCR produced empty
or minimal text:
- If body is empty or very short (<50 chars), skip LLM detection.
- Use file extension as a weak signal: `.pdf` → "other", images → "other".
- Store `document_type = "other"` and allow manual re-detection later
  after OCR improves the text.

---

## 4. Integration Points

### 4.1 Ingestion Pipeline

Detection runs **after** `upsert_document()` succeeds, as a
non-blocking post-processing step:

```
StorageConnector.scan_directory() / scan_webdav()
  → Extractor.extract() → body text
  → indexer.upsert_document() → doc_id
  → DocumentDetector.detect(title, body, ext) → type_key  [NEW]
  → db.update_document_type(doc_id, type_key)  [NEW]
```

**Key decision: synchronous, not background job.**
- Detection is a single LLM call with short body excerpt (~2K chars).
- Running it inline keeps the pipeline simple.
- For batch ingestion of many files, the LLM calls are sequential but
  fast (classification is a simple prompt, not generation).
- If this becomes a bottleneck, a future optimization can batch-classify
  multiple documents in a single LLM call.

### 4.2 API Endpoint

```
POST /api/v1/documents/{doc_id}/detect-type
```

Re-runs detection on an existing document and updates the stored type.
Useful when:
- LLM was not configured during initial ingestion.
- OCR was improved and text is now available.
- User disagrees with the auto-detected type.

**Response (200 OK):**
```json
{
  "doc_id": 42,
  "previous_type": "other",
  "detected_type": "invoice",
  "detection_method": "llm"  // or "keyword"
}
```

### 4.3 Manual Type Override

```
PATCH /api/v1/documents/{doc_id}/type
Body: {"document_type": "contract"}
```

Allows users to manually set the type, bypassing auto-detection.

---

## 5. DB Schema Changes

### 5.1 New Column

```sql
ALTER TABLE documents ADD COLUMN document_type TEXT DEFAULT 'other';
```

### 5.2 Migration

The `db_sqlite.py` `_init_schema` already uses `CREATE TABLE IF NOT EXISTS`
with explicit column definitions. For existing databases, a migration
adds the column if it doesn't exist:

```python
async def _ensure_document_type_column(self):
    """Add document_type column if missing (migration)."""
    async with self.connection() as conn:
        cursor = await conn.execute("PRAGMA table_info(documents)")
        columns = await cursor.fetchall()
        col_names = {c["name"] for c in columns}
        if "document_type" not in col_names:
            await conn.execute(
                "ALTER TABLE documents ADD COLUMN document_type TEXT DEFAULT 'other'"
            )
            await conn.commit()
```

### 5.3 New DB Methods

```python
async def update_document_type(self, doc_id: int, doc_type: str) -> bool:
    """Update the document_type column for a document."""

async def get_documents_by_type(
    self, doc_type: str, *, limit: int = 50, offset: int = 0
) -> list[dict]:
    """Fetch all documents of a given type."""

async def get_document_type_facet(self) -> list[dict]:
    """Return document_type counts for faceted filtering."""
    # [{"value": "invoice", "count": 12}, ...]
```

---

## 6. Configuration

New `AutoDetectionConfig` dataclass in `config.py`:

```python
@dataclass
class AutoDetectionConfig:
    """LLM-based document type auto-detection settings."""
    enabled: bool = True           # DOCMIND_AUTODETECT_ENABLED
    max_body_chars: int = 2000     # DOCMIND_AUTODETECT_MAX_BODY_CHARS
    # Uses the existing LLMConfig for LLM provider settings.
    # If LLM is not configured, falls back to keyword heuristic.
```

Added to `Config`:
```python
auto_detection: AutoDetectionConfig = field(default_factory=AutoDetectionConfig)
```

---

## 7. Cache Invalidation

When `update_document_type()` is called, the following cache keys
must be invalidated:
- `docmind:doc:get:{doc_id}` — document single
- `docmind:doc:by_path:{path_hash}` — document by path
- `docmind:doc:type:facet` — document type facet counts

The existing `_invalidate_document_mutations()` method already
covers doc_id-based keys. The type facet needs a separate key
invalidation.

---

## 8. Acceptance Criteria

- [ ] `DocumentDetector.detect()` classifies documents using LLM when configured.
- [ ] Falls back to keyword heuristic when LLM is not configured or fails.
- [ ] `document_type` column added to documents table with migration.
- [ ] `update_document_type()`, `get_documents_by_type()`, `get_document_type_facet()` DB methods work.
- [ ] Detection runs during ingestion (scan_directory, scan_webdav).
- [ ] `POST /api/v1/documents/{id}/detect-type` re-runs detection.
- [ ] `PATCH /api/v1/documents/{id}/type` allows manual override.
- [ ] Non-text documents get `document_type = "other"` without errors.
- [ ] All new code has tests (unit + integration).
- [ ] No regressions in existing test suite.
- [ ] Cache keys invalidated on type update.
