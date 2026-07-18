"""Document viewer — formatted content rendering, pagination, and TOC.

This module powers the GET /documents/{doc_id}/view route. It takes a
document dict (as returned by ``Database.get_document``) and produces an
HTML reader page that:

* Renders content according to file type (markdown → HTML, CSV → table,
  JSON → syntax-highlighted, XML → pretty-printed, HTML → sanitized,
  plain text → monospace with line numbers, PDF/DOCX → extracted text).
* Paginates long content by character count (default 5000 chars/page).
* Generates a table of contents sidebar from markdown H1-H3 headers.
* Provides a JavaScript search-within-document feature (highlight +
  prev/next navigation, match count).
* Provides reading-mode controls (adjustable font size + line height).
* Shows word count and estimated reading time.

The module deliberately avoids adding a ``markdown`` third-party
dependency — a small, safe subset of CommonMark is implemented inline.
HTML sanitization uses ``beautifulsoup4`` (already a dependency).
"""

from __future__ import annotations

import csv
import html as _html_lib
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .server import _base_page, _escape, _fmt_date, _fmt_size, _render_template


# ── Pagination ───────────────────────────────────────────────────


@dataclass
class PaginationState:
    """Result of paginating content by character count."""

    page: int
    per_page: int
    total_chars: int
    total_pages: int
    chunk: str  # the content slice for this page
    char_start: int  # 0-indexed char offset of chunk in full body
    char_end: int  # exclusive end

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages


def paginate_content(
    body: str,
    *,
    page: int = 1,
    per_page: int = 5000,
) -> PaginationState:
    """Slice ``body`` into pages of ``per_page`` characters.

    ``per_page`` is clamped to [500, 50000] to prevent abuse; ``page`` is
    clamped to >= 1 and capped at the last page when out of range.
    """
    per_page = max(500, min(int(per_page), 50000))
    total_chars = len(body)
    total_pages = max(1, (total_chars + per_page - 1) // per_page)
    page = max(1, min(int(page), total_pages))
    char_start = (page - 1) * per_page
    char_end = min(char_start + per_page, total_chars)
    chunk = body[char_start:char_end]
    return PaginationState(
        page=page,
        per_page=per_page,
        total_chars=total_chars,
        total_pages=total_pages,
        chunk=chunk,
        char_start=char_start,
        char_end=char_end,
    )


# ── Word count / reading time ─────────────────────────────────────


def word_count(body: str) -> int:
    """Return a rough word count for ``body``.

    Whitespace-separated tokens; CJK characters are counted per-character
    so reading time for Chinese/Japanese docs is not wildly underestimated.
    """
    if not body:
        return 0
    # Count CJK characters individually (they have no spaces between words)
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", body))
    # Strip CJK so they aren't double-counted as part of whitespace tokens
    non_cjk = re.sub(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", " ", body)
    tokens = non_cjk.split()
    return cjk + len(tokens)


def reading_time_minutes(body: str) -> int:
    """Estimate reading time in whole minutes (200 wpm)."""
    wc = word_count(body)
    minutes = wc / 200.0
    return max(1, round(minutes)) if wc else 0


# ── Table of contents ────────────────────────────────────────────


@dataclass
class TocEntry:
    level: int  # 1, 2, or 3
    text: str
    anchor: str  # safe id for href


@dataclass
class Toc:
    entries: list[TocEntry] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.entries


def slugify(text: str) -> str:
    """Make a URL-safe anchor id from header text."""
    s = re.sub(r"[^\w\s-]", "", text.lower().strip())
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "section"


def build_toc_from_markdown(body: str) -> Toc:
    """Extract H1-H3 headers from markdown source.

    Recognizes both ATX (``# Title``) and Setext (``Title\\n===``) headers.
    Returns a Toc with stable, unique anchors.
    """
    toc = Toc()
    seen: dict[str, int] = {}
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^(#{1,3})\s+(.+?)\s*#*\s*$", line)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
        elif i + 1 < len(lines) and line.strip():
            nxt = lines[i + 1]
            if re.match(r"^=+\s*$", nxt) and line.strip():
                level = 1
                text = line.strip()
            elif re.match(r"^-+\s*$", nxt) and line.strip():
                level = 2
                text = line.strip()
            else:
                i += 1
                continue
        else:
            i += 1
            continue
        anchor = slugify(text)
        if anchor in seen:
            seen[anchor] += 1
            anchor = f"{anchor}-{seen[anchor]}"
        else:
            seen[anchor] = 0
        toc.entries.append(TocEntry(level=level, text=text, anchor=anchor))
        i += 1
    return toc


# ── Markdown renderer (small, safe subset) ───────────────────────

# Order matters: code blocks must be extracted before inline formatting
# so their contents are not mangled.

_CODE_FENCE_RE = re.compile(r"^```(\w*)[ \t]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+?)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_HR_RE = re.compile(r"^(?:---|\*\*\*|___)\s*$", re.MULTILINE)
_HEAD_RE = re.compile(r"^(#{1,6})\s+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_SETTEXT_H1_RE = re.compile(r"^(.+?)\n=+\s*$", re.MULTILINE)
_SETTEXT_H2_RE = re.compile(r"^(.+?)\n-+\s*$", re.MULTILINE)
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+\.)\s+(.+)$", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^>\s?(.*)$", re.MULTILINE)

def _placeholder(store: list[str], content: str) -> str:
    """Stash ``content`` and return a unique placeholder token."""
    token = f"\x00CODEBLOCK{len(store)}\x00"
    store.append(content)
    return token


def _restore(text: str, store: list[str]) -> str:
    for i, content in enumerate(store):
        text = text.replace(f"\x00CODEBLOCK{i}\x00", content)
    return text


def _inline(text: str) -> str:
    """Apply inline markdown formatting to ``text`` (already HTML-escaped)."""
    # Inline code first so its contents aren't further formatted
    text = _INLINE_CODE_RE.sub(
        lambda m: f'<code class="md-inline-code">{_html_lib.escape(m.group(1))}</code>',
        text,
    )
    # Images before links (link regex would eat the ! prefix otherwise)
    text = _IMAGE_RE.sub(
        lambda m: f'<img src="{_html_lib.escape(m.group(2), quote=True)}" alt="{_html_lib.escape(m.group(1), quote=True)}" loading="lazy" class="md-image">',
        text,
    )
    text = _LINK_RE.sub(
        lambda m: f'<a href="{_html_lib.escape(m.group(2), quote=True)}">{m.group(1)}</a>',
        text,
    )
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _STRIKE_RE.sub(r"<del>\1</del>", text)
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)
    return text


def render_markdown(body: str) -> str:
    """Render a small CommonMark subset to HTML.

    Supports: fenced code blocks, headings (ATX + setext), bold, italic,
    strikethrough, inline code, links, images, unordered/ordered lists,
    blockquotes, and horizontal rules. Output is HTML-safe — all literal
    text is escaped before tags are inserted.
    """
    if not body:
        return ""
    store: list[str] = []

    # 1. Extract fenced code blocks
    def _stash_fence(m: re.Match[str]) -> str:
        lang = m.group(1).strip()
        code = _html_lib.escape(m.group(2))
        cls = f" class=\"language-{_html_lib.escape(lang, quote=True)}\"" if lang else ""
        return _placeholder(store, f"<pre><code{cls}>{code}</code></pre>")

    work = _CODE_FENCE_RE.sub(_stash_fence, body)

    # 1b. Extract blockquotes (before escaping — '>' becomes '&gt;')
    def _stash_bq(m: re.Match[str]) -> str:
        inner = _inline(_html_lib.escape(m.group(1)))
        return _placeholder(store, f"<blockquote>{inner}</blockquote>")

    work = _BLOCKQUOTE_RE.sub(_stash_bq, work)

    # 2. Escape everything else
    work = _html_lib.escape(work)

    # 3. Restore code blocks into the escaped stream
    work = _restore(work, store)
    # Re-stash so later block transforms don't touch them
    store2: list[str] = []
    work = re.sub(
        r"<pre><code.*?</code></pre>",
        lambda m: _placeholder(store2, m.group(0)),
        work,
        flags=re.DOTALL,
    )

    # 4. Headings (ATX)
    def _head(m: re.Match[str]) -> str:
        level = min(6, len(m.group(1)))
        text = _inline(m.group(2).strip())
        anchor = slugify(_html_lib.unescape(m.group(2).strip()))
        return f'<h{level} id="{anchor}">{text}</h{level}>'

    work = _HEAD_RE.sub(_head, work)
    # Setext H1/H2
    work = _SETTEXT_H1_RE.sub(
        lambda m: f'<h1 id="{slugify(m.group(1).strip())}">{_inline(m.group(1).strip())}</h1>',
        work,
    )
    work = _SETTEXT_H2_RE.sub(
        lambda m: f'<h2 id="{slugify(m.group(1).strip())}">{_inline(m.group(1).strip())}</h2>',
        work,
    )

    # 5. Horizontal rules
    work = _HR_RE.sub("<hr>", work)

    # 6. (Blockquotes were extracted in step 1b before escaping)

    # 7. Lists — group consecutive list items into <ul>/<ol>
    def _render_lists(text: str) -> str:
        lines = text.split("\n")
        out: list[str] = []
        i = 0
        while i < len(lines):
            m = _LIST_RE.match(lines[i])
            if not m:
                out.append(lines[i])
                i += 1
                continue
            # Collect consecutive list lines at the same indent level
            base_indent = len(m.group(1))
            ordered = re.match(r"\d+\.", m.group(2)) is not None
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            while i < len(lines):
                lm = _LIST_RE.match(lines[i])
                if lm and len(lm.group(1)) == base_indent:
                    items.append(f"<li>{_inline(lm.group(3))}</li>")
                    i += 1
                elif lm and len(lm.group(1)) > base_indent:
                    # Nested list — append raw; the next iteration will
                    # group it. For simplicity we just include the text.
                    items.append(f"<li>{_inline(lm.group(3))}</li>")
                    i += 1
                elif lines[i].strip() == "":
                    # Blank line — check if list continues
                    i += 1
                    if i < len(lines) and _LIST_RE.match(lines[i]):
                        continue
                    else:
                        break
                else:
                    break
            out.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
        return "\n".join(out)

    work = _render_lists(work)

    # 8. Paragraphs — wrap loose text blocks
    # Split on blank lines, but skip lines that start a block tag
    paragraphs: list[str] = []
    for block in re.split(r"\n\s*\n", work):
        block = block.strip()
        if not block:
            continue
        if re.match(r"^<(h\d|ul|ol|li|pre|blockquote|hr|table|div|p)", block):
            paragraphs.append(block)
        else:
            paragraphs.append(f"<p>{_inline(block)}</p>")
    work = "\n".join(paragraphs)

    # 9. Restore code blocks
    work = _restore(work, store2)
    return work


# ── CSV / JSON / XML / HTML formatters ───────────────────────────


def render_csv_table(body: str) -> str:
    """Render CSV content as an HTML table."""
    if not body.strip():
        return '<p class="muted"><em>Empty CSV.</em></p>'
    reader = csv.reader(io.StringIO(body))
    rows = list(reader)
    if not rows:
        return '<p class="muted"><em>No rows.</em></p>'
    header = rows[0]
    body_rows = rows[1:]
    parts = ['<table class="csv-table"><thead><tr>']
    parts.extend(f"<th>{_escape(c)}</th>" for c in header)
    parts.append("</tr></thead><tbody>")
    for row in body_rows:
        parts.append("<tr>")
        # pad row to header length for tidy rendering
        for i in range(len(header)):
            cell = row[i] if i < len(row) else ""
            parts.append(f"<td>{_escape(cell)}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def render_json_highlighted(body: str) -> str:
    """Pretty-print and syntax-highlight JSON."""
    if not body.strip():
        return '<p class="muted"><em>Empty JSON.</em></p>'
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError) as e:
        return (
            f'<p class="error">Invalid JSON: {_escape(str(e))}</p>'
            f'<pre class="json-raw">{_escape(body)}</pre>'
        )
    pretty = json.dumps(data, ensure_ascii=False, indent=2)
    return f'<pre class="json-highlight"><code>{_json_highlight(pretty)}</code></pre>'


def _json_highlight(pretty: str) -> str:
    """Add syntax-highlight spans to pretty-printed JSON."""
    # Escape first so the regex replacements insert safe tags
    s = _html_lib.escape(pretty)
    # Strings (keys and values)
    s = re.sub(
        r'(&quot;.*?&quot;)(\s*:)',
        r'<span class="json-key">\1</span>\2',
        s,
    )
    s = re.sub(
        r'(:\s*)(&quot;.*?&quot;)',
        r'\1<span class="json-string">\2</span>',
        s,
    )
    # Booleans and null
    s = re.sub(
        r'\b(true|false|null)\b',
        r'<span class="json-bool">\1</span>',
        s,
    )
    # Numbers
    s = re.sub(
        r'(?<![\w"])(-?\d+\.?\d*(?:[eE][+-]?\d+)?)',
        r'<span class="json-num">\1</span>',
        s,
    )
    return s


def render_xml_highlighted(body: str) -> str:
    """Pretty-print XML with syntax highlighting."""
    if not body.strip():
        return '<p class="muted"><em>Empty XML.</em></p>'
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(body, "xml")
        pretty = soup.prettify()
    except Exception:
        pretty = body
    s = _html_lib.escape(pretty)
    # Tags
    s = re.sub(
        r"(&lt;/?[\w:-]+)",
        r'<span class="xml-tag">\1</span>',
        s,
    )
    s = re.sub(
        r"(/?&gt;)",
        r'<span class="xml-tag">\1</span>',
        s,
    )
    # Attributes
    s = re.sub(
        r"([\w:-]+)(=)(&quot;.*?&quot;)",
        r'<span class="xml-attr">\1</span>\2<span class="xml-string">\3</span>',
        s,
    )
    return f'<pre class="xml-highlight"><code>{s}</code></pre>'


def render_text_with_line_numbers(body: str) -> str:
    """Render plain text in a monospace block with line numbers."""
    if not body:
        return '<p class="muted"><em>Empty document.</em></p>'
    lines = body.splitlines()
    rows: list[str] = []
    for i, line in enumerate(lines, start=1):
        rows.append(
            f'<tr><td class="line-no">{i}</td>'
            f'<td class="line-text">{_escape(line)}</td></tr>'
        )
    return (
        '<table class="text-lines"><tbody>'
        + "".join(rows)
        + "</tbody></table>"
    )


def sanitize_html(body: str) -> str:
    """Sanitize HTML content, stripping scripts and unsafe tags."""
    if not body.strip():
        return '<p class="muted"><em>Empty HTML document.</em></p>'
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(body, "html.parser")
        # Remove dangerous tags entirely
        for tag in soup(
            ["script", "style", "iframe", "object", "embed", "form",
             "input", "button", "meta", "link", "base"]
        ):
            tag.decompose()
        # Strip event-handler attributes (on*) and javascript: URLs
        for tag in soup.find_all(True):
            for attr in list(tag.attrs):
                if attr.lower().startswith("on"):
                    del tag.attrs[attr]
                val = tag.attrs.get(attr)
                if isinstance(val, list):
                    val = " ".join(val)
                if isinstance(val, str) and "javascript:" in val.lower():
                    del tag.attrs[attr]
        return str(soup)
    except Exception:
        return f'<pre>{_escape(body)}</pre>'


# ── Content dispatch ─────────────────────────────────────────────


def render_content(body: str, ext: str) -> tuple[str, str]:
    """Dispatch on file extension; return (html_body, mode).

    ``mode`` is a short label used for CSS classing and tests:
    ``markdown``, ``html``, ``csv``, ``json``, ``xml``, ``text``,
    ``extracted`` (for pdf/docx).
    """
    ext = (ext or "").lower()
    if ext == ".md":
        return render_markdown(body), "markdown"
    if ext in (".html", ".htm"):
        return sanitize_html(body), "html"
    if ext == ".csv":
        return render_csv_table(body), "csv"
    if ext == ".json":
        return render_json_highlighted(body), "json"
    if ext == ".xml":
        return render_xml_highlighted(body), "xml"
    if ext in (".pdf", ".docx"):
        return render_text_with_line_numbers(body), "extracted"
    # Default: plain text with line numbers
    return render_text_with_line_numbers(body), "text"


# ── TOC sidebar HTML ─────────────────────────────────────────────


def render_toc_sidebar(toc: Toc) -> str:
    """Render the table of contents sidebar HTML (empty string if no TOC)."""
    if toc.is_empty:
        return ""
    items: list[str] = []
    for e in toc.entries:
        indent = (e.level - 1) * 12
        items.append(
            f'<li class="toc-level-{e.level}" style="margin-left:{indent}px">'
            f'<a href="#{e.anchor}" data-anchor="{e.anchor}">'
            f'{_escape(e.text)}</a></li>'
        )
    return (
        '<aside class="doc-toc" id="docToc">'
        "<h3>Contents</h3>"
        '<ul class="toc-list">' + "".join(items) + "</ul>"
        "</aside>"
    )


# ── Pagination nav ───────────────────────────────────────────────


def render_viewer_pagination(
    doc_id: int,
    state: PaginationState,
    per_page: int,
) -> str:
    """Render the in-document pagination bar (prev/next + jump-to)."""
    if state.total_pages <= 1:
        return (
            f'<div class="viewer-pagination">'
            f'<span class="pagination-info">Page 1 of 1</span>'
            f"</div>"
        )
    base = f"/documents/{doc_id}/view?per_page={per_page}"
    parts = ['<div class="viewer-pagination">']
    if state.has_prev:
        parts.append(f'<a href="{base}&page={state.page - 1}" class="vp-btn">← Prev</a>')
    else:
        parts.append('<span class="vp-btn disabled">← Prev</span>')
    parts.append(
        f'<span class="pagination-info">Page {state.page} of {state.total_pages}</span>'
    )
    # Jump-to-page input
    parts.append(
        f'<input type="number" min="1" max="{state.total_pages}" '
        f'value="{state.page}" id="pageJump" '
        f'onchange="window.location.href=\'{base}&page=\'+this.value" '
        f'class="page-jump" aria-label="Jump to page">'
    )
    if state.has_next:
        parts.append(f'<a href="{base}&page={state.page + 1}" class="vp-btn">Next →</a>')
    else:
        parts.append('<span class="vp-btn disabled">Next →</span>')
    parts.append("</div>")
    return "".join(parts)


# ── Main render ──────────────────────────────────────────────────


def render_document_viewer(
    doc: dict,
    *,
    page: int = 1,
    per_page: int = 5000,
) -> str:
    """Render the full document viewer page using Jinja2 template."""
    body = doc.get("body", "") or ""
    ext = doc.get("ext", "") or ""
    doc_id = doc.get("id", 0)

    state = paginate_content(body, page=page, per_page=per_page)

    # Build TOC from the *full* markdown source
    toc = Toc()
    if ext.lower() == ".md":
        toc = build_toc_from_markdown(body)

    content_html, mode = render_content(state.chunk, ext)

    wc = word_count(body)
    rt = reading_time_minutes(body)

    title = doc.get("title", "Untitled")

    # Toolbar
    toolbar_html = """
    <div class="viewer-toolbar">
        <div class="tool-group">
            <label for="fontSizeSlider">Aa</label>
            <input type="range" id="fontSizeSlider" min="12" max="24" value="16" step="1">
        </div>
        <div class="tool-group">
            <label for="lineHeightSlider">\u2195</label>
            <input type="range" id="lineHeightSlider" min="1.2" max="2.4" value="1.7" step="0.1">
        </div>
        <div class="search-in-doc">
            <input type="search" id="docSearch" placeholder="Search in document\u2026" aria-label="Search in document">
            <span class="match-count" id="matchCount"></span>
            <div class="search-nav-btns">
                <button id="searchPrev" title="Previous match (Shift+Enter)" disabled>\u25b2</button>
                <button id="searchNext" title="Next match (Enter)" disabled>\u25bc</button>
            </div>
        </div>
    </div>
    """

    meta_html = """
    <div class="viewer-meta">
        <span>\U0001f4c4 """ + _escape(ext or 'unknown') + """</span>
        <span>\U0001f4dd """ + f"{wc:,}" + """ words</span>
        <span>\u23f1 ~""" + str(rt) + """ min read</span>
        <span>\U0001f4ca """ + _fmt_size(doc.get('size', 0)) + """</span>
        <span>\U0001f5d3 """ + _fmt_date(doc.get('created_at', '')) + """</span>
    </div>
    """

    toc_html = render_toc_sidebar(toc)
    pagination_html = render_viewer_pagination(doc_id, state, per_page)

    return _render_template("viewer.html",
        doc_id=doc_id, title=title, meta_html=meta_html,
        toolbar_html=toolbar_html, toc_html=toc_html,
        content_html=content_html, mode=mode,
        pagination_html=pagination_html,
    )
