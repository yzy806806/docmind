"""HTML rendering functions using Jinja2 templates.

All HTML generation has been moved out of server.py into this module
and the Jinja2 templates in the templates/ directory. The _render_*
functions prepare data and delegate to templates; SVG chart generators
produce inline SVG for analytics dashboards.

These functions are re-exported by server.py so existing imports
(e.g. ``from src.web.server import _base_page``) continue to work.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from ..core.models import JobRecord


# ── Template utilities ──────────────────────────────────────────


def _escape(text: str) -> str:
    """Basic HTML escaping."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _fmt_date(date_val) -> str:
    """Format a datetime value for display."""
    if not date_val:
        return ""
    if isinstance(date_val, datetime):
        return date_val.strftime("%Y-%m-%d %H:%M")
    try:
        # Try ISO format
        return str(date_val)[:19].replace("T", " ")
    except Exception:
        return str(date_val)


def _fmt_size(size: int) -> str:
    """Format bytes as human-readable size."""
    s = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if s < 1024:
            return f"{s:.1f} {unit}"
        s /= 1024
    return f"{s:.1f} TB"



# ── Jinja2 Template Environment ──────────────────────────────────

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _render_template(template_name: str, **context) -> str:
    """Render a Jinja2 template with the given context.

    This is the central template rendering helper used by all
    _render_* functions. It adds common utility functions to the
    context so templates can call them directly. It also injects the
    current ``auth_enabled`` flag so base.html can render a logout
    button when authentication is active.
    """
    template = _jinja_env.get_template(template_name)
    # Add utility functions to context for template use
    context.setdefault("escape", _escape)
    context.setdefault("fmt_date", _fmt_date)
    context.setdefault("fmt_size", _fmt_size)
    # Inject auth state for nav-bar rendering (base.html). Templates
    # that don't reference this variable simply ignore it.
    from ..core.config import config
    context.setdefault("auth_enabled", bool(config.auth.enabled))
    return template.render(**context)



# ── HTML Rendering (Jinja2 templates) ────────────────────────────


def _base_page(title: str, content: str, extra_head: str = "") -> str:
    """Render a base HTML page with dark-mode and responsive styling.

    Uses CSS custom properties (variables) for theming. A JavaScript
    toggle in the nav bar switches between light and dark, and the
    preference is persisted in localStorage under ``docmind-theme``.
    """
    return _render_template("base.html", title=title, content=content,
                           extra_head=extra_head)


def _svg_line_chart(data: list[dict], value_key: str, label_key: str = "date",
                    width: int = 600, height: int = 200, color: str = "#4a90d9") -> str:
    """Generate an inline SVG line chart from a list of dicts."""
    if not data:
        return '<div class="chart-empty">No data for this period</div>'

    values = [d.get(value_key, 0) for d in data]
    labels = [str(d.get(label_key, "")) for d in data]
    max_val = max(values) if values else 1
    if max_val == 0:
        max_val = 1

    margin_left = 40
    margin_right = 10
    margin_top = 15
    margin_bottom = 25
    chart_w = width - margin_left - margin_right
    chart_h = height - margin_top - margin_bottom

    n = len(values)
    step_x = chart_w / max(n - 1, 1) if n > 1 else chart_w

    points = []
    for i, v in enumerate(values):
        x = margin_left + (i * step_x if n > 1 else chart_w / 2)
        y = margin_top + chart_h - (v / max_val) * chart_h
        points.append((x, y))

    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)

    area_path = f"M {margin_left:.1f},{margin_top + chart_h:.1f} "
    area_path += " ".join(f"L {x:.1f},{y:.1f}" for x, y in points)
    area_path += f" L {margin_left + chart_w:.1f},{margin_top + chart_h:.1f} Z"

    y_labels = ""
    for frac, label in [(0, "0"), (0.5, str(int(max_val / 2))), (1, str(max_val))]:
        y = margin_top + chart_h - frac * chart_h
        y_labels += f'<text x="{margin_left - 5}" y="{y + 4:.1f}" text-anchor="end" class="chart-axis-label">{label}</text>'

    x_labels = ""
    label_indices = set()
    if n <= 7:
        label_indices = set(range(n))
    else:
        label_indices = {0, n // 2, n - 1}
    for i in label_indices:
        x = margin_left + (i * step_x if n > 1 else chart_w / 2)
        label = labels[i]
        if len(label) == 10 and label[4] == "-":
            label = label[5:]
        x_labels += f'<text x="{x:.1f}" y="{margin_top + chart_h + 18}" text-anchor="middle" class="chart-axis-label">{label}</text>'

    grid_lines = ""
    for frac in [0, 0.25, 0.5, 0.75, 1.0]:
        y = margin_top + chart_h - frac * chart_h
        grid_lines += f'<line x1="{margin_left}" y1="{y:.1f}" x2="{margin_left + chart_w}" y2="{y:.1f}" class="chart-grid"/>'

    circles = ""
    for x, y in points:
        circles += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}" class="chart-point"/>'

    return f"""<svg viewBox="0 0 {width} {height}" class="chart-svg" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Line chart">
        <defs><linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="{color}" stop-opacity="0.3"/>
            <stop offset="100%" stop-color="{color}" stop-opacity="0.05"/>
        </linearGradient></defs>
        {grid_lines}
        <path d="{area_path}" fill="url(#areaGrad)"/>
        <polyline points="{polyline}" fill="none" stroke="{color}" stroke-width="2" class="chart-line"/>
        {circles}
        {y_labels}
        {x_labels}
    </svg>"""


def _svg_bar_chart(data: list[dict], label_key: str, value_key: str,
                   width: int = 600, height: int = 200, color: str = "#6c5ce7") -> str:
    """Generate an inline SVG bar chart from a list of dicts."""
    if not data:
        return '<div class="chart-empty">No data available</div>'

    data = data[:15]
    values = [d.get(value_key, 0) for d in data]
    labels = [str(d.get(label_key, "")) for d in data]
    max_val = max(values) if values else 1
    if max_val == 0:
        max_val = 1

    margin_left = 80
    margin_right = 10
    margin_top = 10
    margin_bottom = 10
    chart_w = width - margin_left - margin_right
    chart_h = height - margin_top - margin_bottom
    n = len(values)
    bar_h = chart_h / n * 0.7
    gap = chart_h / n * 0.3

    bars = ""
    y_labels = ""
    for i, (v, label) in enumerate(zip(values, labels)):
        y = margin_top + i * (bar_h + gap)
        bar_w = (v / max_val) * chart_w
        display_label = label[:12] + "…" if len(label) > 13 else label
        bars += f'<rect x="{margin_left}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" fill="{color}" rx="3" class="chart-bar"/>'
        bars += f'<text x="{margin_left + bar_w + 5:.1f}" y="{y + bar_h * 0.75:.1f}" class="chart-bar-value">{v}</text>'
        y_labels += f'<text x="{margin_left - 5}" y="{y + bar_h * 0.75:.1f}" text-anchor="end" class="chart-axis-label">{display_label}</text>'

    return f"""<svg viewBox="0 0 {width} {height}" class="chart-svg" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Bar chart">
        {y_labels}
        {bars}
    </svg>"""


def _svg_pie_chart(data: list[tuple[str, float]], width: int = 200, height: int = 200) -> str:
    """Generate an inline SVG pie/donut chart from (label, value) tuples."""
    if not data:
        return '<div class="chart-empty">No data available</div>'

    total = sum(v for _, v in data)
    if total == 0:
        return '<div class="chart-empty">No data available</div>'

    colors = ["#4a90d9", "#6c5ce7", "#00b894", "#fdcb6e", "#e17055",
              "#0984e3", "#e84393", "#00cec9", "#fab1a0", "#74b9ff"]
    cx, cy, r = width / 2, height / 2, min(width, height) / 2 - 5
    inner_r = r * 0.55

    slices = ""
    legend = ""
    angle = -90.0
    for i, (label, value) in enumerate(data):
        if value == 0:
            continue
        frac = value / total
        sweep = frac * 360
        color = colors[i % len(colors)]

        start_rad = math.radians(angle)
        end_rad = math.radians(angle + sweep)

        x1 = cx + r * math.cos(start_rad)
        y1 = cy + r * math.sin(start_rad)
        x2 = cx + r * math.cos(end_rad)
        y2 = cy + r * math.sin(end_rad)

        ix1 = cx + inner_r * math.cos(start_rad)
        iy1 = cy + inner_r * math.sin(start_rad)
        ix2 = cx + inner_r * math.cos(end_rad)
        iy2 = cy + inner_r * math.sin(end_rad)

        large_arc = 1 if sweep > 180 else 0

        if sweep >= 360:
            slices += f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color}" class="chart-slice"/>'
            slices += f'<circle cx="{cx}" cy="{cy}" r="{inner_r}" fill="var(--surface)" />'
        else:
            path = f"M {x1:.1f},{y1:.1f} A {r},{r} 0 {large_arc} 1 {x2:.1f},{y2:.1f} L {ix2:.1f},{iy2:.1f} A {inner_r},{inner_r} 0 {large_arc} 0 {ix1:.1f},{iy1:.1f} Z"
            slices += f'<path d="{path}" fill="{color}" class="chart-slice"/>'

        pct = f"{frac * 100:.1f}%"
        legend += f'<div class="pie-legend-item"><span class="pie-legend-color" style="background:{color}"></span>{label} ({pct})</div>'
        angle += sweep

    return f"""<div class="pie-chart-container">
        <svg viewBox="0 0 {width} {height}" class="chart-svg pie-svg" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Pie chart">
            {slices}
        </svg>
        <div class="pie-legend">{legend}</div>
    </div>"""


def _render_dashboard(
    stats: dict,
    recent: list[dict],
    doc_growth: list[dict] | None = None,
    tag_dist: list[dict] | None = None,
    storage: dict | None = None,
    search_stats: dict | None = None,
    popular_queries: list[dict] | None = None,
    search_trend: list[dict] | None = None,
    chat_activity: list[dict] | None = None,
    job_stats: dict | None = None,
) -> str:
    """Render the enhanced dashboard with analytics charts."""
    doc_growth = doc_growth or []
    tag_dist = tag_dist or []
    storage = storage or {"total_size": 0, "by_type": {}, "avg_doc_size": 0, "doc_count": 0}
    search_stats = search_stats or {"total_searches": 0, "avg_results": 0.0, "unique_queries": 0}
    popular_queries = popular_queries or []
    search_trend = search_trend or []
    chat_activity = chat_activity or []
    job_stats = job_stats or {"by_state": {}, "total": 0, "success_rate": 0.0,
                              "avg_processing_time_seconds": 0.0, "recent_failures": []}

    recent_rows = ""
    for doc in recent:
        status_class = f"badge-{doc.get('status', 'pending')}"
        recent_rows += f"""
        <tr>
            <td><a href="/documents/{doc['id']}">[{doc['id']}] {doc.get('title', 'Untitled')}</a></td>
            <td><span class="badge {status_class}">{doc.get('status', '')}</span></td>
            <td>{doc.get('ext', '')}</td>
            <td>{_fmt_date(doc.get('created_at', ''))}</td>
        </tr>"""

    growth_chart = _svg_line_chart(doc_growth, "count", "date", color="#4a90d9")
    search_chart = _svg_line_chart(search_trend, "count", "date", color="#00b894")
    chat_chart = _svg_line_chart(chat_activity, "message_count", "date", color="#e84393")
    tag_chart = _svg_bar_chart(tag_dist[:10], "tag", "count", color="#6c5ce7")

    storage_by_type = storage.get("by_type", {})
    pie_data = [(ext, size) for ext, size in storage_by_type.items()][:8]
    storage_pie = _svg_pie_chart(pie_data)

    popular_rows = ""
    for q in popular_queries[:5]:
        popular_rows += f"""
        <tr>
            <td>{_escape(q['query'])}</td>
            <td>{q['count']}</td>
            <td>{q.get('avg_results', 0)}</td>
        </tr>"""
    popular_html = f"""<table><tr><th>Query</th><th>Searches</th><th>Avg Results</th></tr>{popular_rows}</table>""" if popular_rows else "<p>No searches logged yet.</p>"

    by_state = job_stats.get("by_state", {})
    job_states_html = " · ".join(f"{s}: {c}" for s, c in by_state.items()) or "No jobs"
    success_rate = job_stats.get("success_rate", 0)
    avg_time = job_stats.get("avg_processing_time_seconds", 0)

    failures_html = ""
    for f in job_stats.get("recent_failures", [])[:3]:
        failures_html += f'<div class="job-failure"><strong>{_escape(f.get("document_title", ""))}</strong>: {_escape(f.get("error", "")[:100])}</div>'
    if not failures_html:
        failures_html = "<p>No recent failures.</p>"

    status_pie_data = [
        ("Pending", stats.get("pending", 0)),
        ("Indexed", stats.get("indexed", 0)),
        ("Summarized", stats.get("summarized", 0)),
        ("Error", stats.get("error", 0)),
    ]
    status_pie = _svg_pie_chart(status_pie_data, width=160, height=160)

    return _render_template("dashboard.html",
        stats=stats, search_stats=search_stats, success_rate=success_rate,
        growth_chart=growth_chart, search_chart=search_chart, chat_chart=chat_chart,
        tag_chart=tag_chart, status_pie=status_pie, storage_pie=storage_pie,
        total_size=_fmt_size(storage.get('total_size', 0)),
        avg_doc_size=_fmt_size(storage.get('avg_doc_size', 0)),
        popular_html=popular_html, job_states_html=job_states_html,
        avg_time=avg_time, failures_html=failures_html, recent_rows=recent_rows,
    )


def _render_analytics_page(
    stats: dict,
    doc_growth: list[dict],
    tag_dist: list[dict],
    storage: dict,
    search_stats: dict,
    popular_queries: list[dict],
    search_trend: list[dict],
    chat_activity: list[dict],
    job_stats: dict,
    days: int = 30,
) -> str:
    """Render the full analytics page with detailed charts and tables."""
    growth_chart = _svg_line_chart(doc_growth, "count", "date", color="#4a90d9")
    search_chart = _svg_line_chart(search_trend, "count", "date", color="#00b894")
    chat_chart = _svg_line_chart(chat_activity, "message_count", "date", color="#e84393")
    tag_chart = _svg_bar_chart(tag_dist[:15], "tag", "count", color="#6c5ce7", height=300)

    storage_by_type = storage.get("by_type", {})
    pie_data = [(ext, size) for ext, size in storage_by_type.items()][:8]
    storage_pie = _svg_pie_chart(pie_data)

    status_pie_data = [
        ("Pending", stats.get("pending", 0)),
        ("Indexed", stats.get("indexed", 0)),
        ("Summarized", stats.get("summarized", 0)),
        ("Error", stats.get("error", 0)),
    ]
    status_pie = _svg_pie_chart(status_pie_data)

    popular_rows = ""
    for q in popular_queries:
        popular_rows += f"""
        <tr>
            <td>{_escape(q['query'])}</td>
            <td>{q['count']}</td>
            <td>{q.get('avg_results', 0)}</td>
        </tr>"""
    popular_html = f"""<table><tr><th>Query</th><th>Searches</th><th>Avg Results</th></tr>{popular_rows}</table>""" if popular_rows else "<p>No searches logged yet.</p>"

    by_state = job_stats.get("by_state", {})
    job_states_html = " · ".join(f"{s}: {c}" for s, c in by_state.items()) or "No jobs"
    success_rate = job_stats.get("success_rate", 0)
    avg_time = job_stats.get("avg_processing_time_seconds", 0)

    tag_rows = ""
    for t in tag_dist:
        tag_rows += f"<tr><td><a href='/documents?tag={_escape(t['tag'])}'>{_escape(t['tag'])}</a></td><td>{t['count']}</td></tr>"
    tag_table = f"<table><tr><th>Tag</th><th>Documents</th></tr>{tag_rows}</table>" if tag_rows else "<p>No tags yet.</p>"

    storage_rows = ""
    for ext, size in sorted(storage_by_type.items(), key=lambda x: x[1], reverse=True):
        storage_rows += f"<tr><td>{ext}</td><td>{_fmt_size(size)}</td><td>{storage.get('doc_count', 0)}</td></tr>"
    storage_table = f"<table><tr><th>File Type</th><th>Total Size</th><th>Documents</th></tr>{storage_rows}</table>" if storage_rows else "<p>No storage data.</p>"

    failures_html = ""
    for f in job_stats.get("recent_failures", []):
        failures_html += f'<div class="job-failure"><strong>{_escape(f.get("document_title", ""))}</strong> ({_fmt_date(f.get("created_at", ""))}): {_escape(f.get("error", "")[:150])}</div>'
    if not failures_html:
        failures_html = "<p>No recent failures.</p>"

    range_links = ""
    for d in [7, 30, 90]:
        active = "active" if d == days else ""
        range_links += f'<a href="/analytics?days={d}" class="{active}">{d} days</a>'

    return _render_template("analytics.html",
        stats=stats, search_stats=search_stats, success_rate=success_rate,
        growth_chart=growth_chart, search_chart=search_chart, chat_chart=chat_chart,
        tag_chart=tag_chart, status_pie=status_pie, storage_pie=storage_pie,
        total_size=_fmt_size(storage.get('total_size', 0)),
        avg_doc_size=_fmt_size(storage.get('avg_doc_size', 0)),
        popular_html=popular_html, job_states_html=job_states_html,
        avg_time=avg_time, failures_html=failures_html, days=days,
        range_links=range_links, tag_table=tag_table, storage_table=storage_table,
        job_stats=job_stats,
    )


def _render_search_form(error: str = "") -> str:
    return _render_template("search_form.html", error=error)


def _render_search_result_row(r: dict) -> str:
    """Render a single search result as an HTML <div> fragment.

    Used by the lazy-loading partial endpoint to return individual result
    rows that get appended to #search-results-list via fetch + DOM append.
    """
    rid = r.get("id", "?")
    title = r.get("title", "Untitled")
    snippet = r.get("snippet", r.get("raw_preview", ""))
    summary = r.get("summary", "")
    status = r.get("status", "pending")
    rank = r.get("rank", 0)
    rank_html = f' | Score: {rank:.2f}' if rank else ''
    summary_html = f'<div class="snippet"><strong>Summary:</strong> {_escape(summary)}</div>' if summary else ''
    snippet_html = f'<div class="snippet">{_escape(snippet[:300])}</div>' if snippet else ''
    return (
        f'<div class="result">'
        f'<h3><a href="/documents/{rid}">[{rid}] {_escape(title)}</a></h3>'
        f'<div class="meta">'
        f'Status: <span class="badge badge-{_escape(status)}">{_escape(status)}</span>'
        f'{rank_html}'
        f'</div>'
        f'{summary_html}'
        f'{snippet_html}'
        f'</div>'
    )


def _render_search_results(
    query: str,
    results: list[dict],
    vector_weight: float | None = None,
    *,
    offset: int = 0,
    limit: int = 20,
    total: int | None = None,
) -> str:
    # Prepare results with escaped fields for template
    prepared = []
    for r in results:
        prepared.append({
            "id": r.get("id", "?"),
            "title": r.get("title", "Untitled"),
            "snippet": r.get("snippet", r.get("raw_preview", "")),
            "summary": r.get("summary", ""),
            "status": r.get("status", "pending"),
            "rank": r.get("rank", 0),
        })
    # Determine the vector_weight to display in the UI slider.
    # When the user provided a value, show it back; otherwise show the
    # engine default (0.6) so the slider starts at a sensible position.
    # Pass as float — the template uses "%.2f"|format() which requires a
    # number, not a string.
    vw_current = vector_weight if vector_weight is not None else 0.6
    # Total count for lazy-loading sentinel (defaults to results length
    # when not explicitly provided — e.g. older callers that don't paginate).
    actual_total = total if total is not None else len(prepared)
    return _render_template(
        "search_results.html",
        query=query,
        results=prepared,
        vw_current=vw_current,
        vw_default=0.6,
        offset=offset,
        limit=limit,
        total=actual_total,
        vw_str=f"{vw_current:.2f}",
    )


def _render_search_results_fragment(
    query: str,
    results: list[dict],
    vector_weight: float | None = None,
    *,
    offset: int = 0,
    limit: int = 20,
    total: int | None = None,
) -> str:
    """Render only the results portion (no page chrome or form) for HTMX swaps.

    Returns the export bar + results list as an HTML fragment suitable for
    ``hx-swap="innerHTML"`` into ``#search-live-region``.  This is used by
    keyup-triggered live search so the page doesn't reload the form or
    surrounding layout.
    """
    prepared = []
    for r in results:
        prepared.append({
            "id": r.get("id", "?"),
            "title": r.get("title", "Untitled"),
            "snippet": r.get("snippet", r.get("raw_preview", "")),
            "summary": r.get("summary", ""),
            "status": r.get("status", "pending"),
            "rank": r.get("rank", 0),
        })
    vw_current = vector_weight if vector_weight is not None else 0.6
    actual_total = total if total is not None else len(prepared)
    return _render_template(
        "search_results_fragment.html",
        query=query,
        results=prepared,
        vw_current=vw_current,
        vw_default=0.6,
        offset=offset,
        limit=limit,
        total=actual_total,
        vw_str=f"{vw_current:.2f}",
    )


def _find_collection_name(
    tree: list[dict], target_id: int
) -> str | None:
    """Recursively find a collection name by id in the tree."""
    for node in tree:
        if node["id"] == target_id:
            return node.get("name", "")
        found = _find_collection_name(
            node.get("children", []), target_id
        )
        if found:
            return found
    return None


def _build_collection_tree_html(
    tree: list[dict],
    counts: dict[int, int],
    active_id: int | None,
    *,
    _level: int = 0,
) -> str:
    """Build an HTML <ul> tree of collections with document counts.

    Mirrors the tag_cloud_html pattern: produces a self-contained
    ``<div class="collection-tree">`` block ready for the template.
    """
    if not tree and _level == 0:
        # Also show the "All" + "Unassigned" links even when no collections exist
        pass

    def _render_node(node: dict, level: int) -> str:
        col_id = node["id"]
        name = node.get("name", "Untitled")
        count = counts.get(col_id, 0)
        active_class = " active" if col_id == active_id else ""
        indent = f"margin-left:{level * 16}px;"
        children_html = ""
        children = node.get("children", [])
        if children:
            inner = "".join(
                _render_node(child, level + 1) for child in children
            )
            children_html = f'<ul class="collection-tree-children">{inner}</ul>'
        return (
            f'<li><a href="/documents?collection_id={col_id}" '
            f'class="collection-tree-item{active_class}" style="{indent}"'
            f'>{_escape(name)} '
            f'<span class="collection-count">({count})</span></a>'
            f'<span class="collection-tree-actions">'
            f'<a href="/collections/{col_id}/edit" class="collection-action-link" title="Edit collection">✏️</a>'
            f'</span>'
            f'{children_html}</li>'
        )

    items = "".join(_render_node(node, 0) for node in tree)
    # "Show all" link (clears collection filter)
    all_active = " active" if active_id is None else ""
    all_link = (
        f'<li><a href="/documents" '
        f'class="collection-tree-item{all_active}">'
        f'All Documents</a></li>'
    )
    # "Unassigned" link (collection_id=0)
    unassigned_count = 0  # computed below from total docs minus assigned
    unassigned_active = " active" if active_id == 0 else ""
    unassigned_link = (
        f'<li><a href="/documents?collection_id=0" '
        f'class="collection-tree-item{unassigned_active}">'
        f'Unassigned</a></li>'
    )
    # "New Collection" button — links to the create form
    new_btn = (
        f'<a href="/collections/new" class="btn-new-collection">+ New Collection</a>'
    )
    return f"""
    <div class="collection-tree">
        <div class="collection-tree-header">
            <h3>Collections</h3>
            {new_btn}
        </div>
        <ul class="collection-tree-list">
            {all_link}
            {items}
            {unassigned_link}
        </ul>
    </div>"""


def _build_collection_breadcrumb_html(
    collection_path: list[dict],
) -> str:
    """Build breadcrumb navigation showing the collection path.

    Given the output of ``db.get_collection_path()`` — a root-first list
    ``[root, ..., parent, self]`` — produce a self-contained
    ``<div class="collection-breadcrumb">`` block.

    Ancestor collections are clickable links to
    ``/documents?collection_id=N``; the current (deepest) collection is
    rendered as a non-clickable ``<span>``.  A leading "All Documents"
    link points to ``/documents`` (clears the collection filter).

    Returns an empty string when *collection_path* is empty.
    """
    if not collection_path:
        return ""

    # "All Documents" link at the start of the trail
    parts = [
        f'<a href="/documents" class="collection-breadcrumb-link">All</a>'
    ]

    for i, col in enumerate(collection_path):
        col_id = col["id"]
        name = col.get("name", "Untitled")
        is_last = i == len(collection_path) - 1
        if is_last:
            # Current collection: non-clickable span
            parts.append(
                f'<span class="collection-breadcrumb-sep">/</span>'
                f'<span class="collection-breadcrumb-current">{_escape(name)}</span>'
            )
        else:
            # Ancestor: clickable link
            parts.append(
                f'<span class="collection-breadcrumb-sep">/</span>'
                f'<a href="/documents?collection_id={col_id}" '
                f'class="collection-breadcrumb-link">{_escape(name)}</a>'
            )

    return (
        '<div class="collection-breadcrumb">'
        + "".join(parts)
        + "</div>"
    )


def _render_collection_detail(
    collection: dict,
    collection_path: list[dict],
    child_collections: list[dict],
    collection_counts: dict[int, int],
    documents: list[dict],
    tags_map: dict[int, list[str]],
    *,
    page: int = 1,
    per_page: int = 20,
    total: int = 0,
    total_pages: int = 0,
    collection_tree: list[dict] | None = None,
) -> str:
    """Render the collection detail page (GET /collections/{id}).

    Shows:
      - Collection name, description, parent collection link
      - Breadcrumb path (root > ... > parent > self)
      - Child collections as a list with document counts
      - Documents in this collection (reusing the documents table partial)
      - Edit / Delete buttons linking to the management forms

    The collection-tree sidebar is shared with /documents for navigation
    consistency. Active state highlights the current collection.
    """
    tags_map = tags_map or {}
    collection_tree = collection_tree or []

    # Breadcrumb navigation (reuses the existing renderer so the trail
    # matches /documents?collection_id=N exactly).
    breadcrumb_html = _build_collection_breadcrumb_html(collection_path)

    # Child collections list with counts
    child_items_html = ""
    if child_collections:
        rows = ""
        for child in child_collections:
            cid = child["id"]
            cname = child.get("name", "Untitled")
            cdesc = child.get("description", "") or ""
            ccount = collection_counts.get(cid, 0)
            rows += (
                f'<tr><td><a href="/collections/{cid}">{_escape(cname)}</a></td>'
                f'<td>{_escape(cdesc)}</td>'
                f'<td>{ccount}</td></tr>'
            )
        child_items_html = (
            '<table><tr><th>Name</th><th>Description</th>'
            '<th>Documents</th></tr>'
            f'{rows}</table>'
        )
    else:
        child_items_html = "<p>No sub-collections.</p>"

    # Parent collection link
    parent_id = collection.get("parent_id")
    parent_link_html = ""
    if parent_id is not None:
        # Find parent name in the collection_path (parent is second-to-last)
        if len(collection_path) >= 2:
            parent_col = collection_path[-2]
            parent_link_html = (
                f'<a href="/collections/{parent_col["id"]}" '
                f'class="btn-view-link">{_escape(parent_col.get("name", "Parent"))}</a>'
            )
        else:
            parent_link_html = (
                f'<a href="/collections/{parent_id}" class="btn-view-link">'
                f'Parent (id {parent_id})</a>'
            )
    else:
        parent_link_html = '<span class="meta">— (root)</span>'

    # Documents table — reuse the partial renderer so the table markup
    # stays in sync with /documents (single source of truth).
    documents_table_html = _render_documents_table_partial(
        documents, page=page, per_page=per_page, total=total,
        total_pages=total_pages, tags_map=tags_map,
        active_collection_id=collection["id"],
    )

    # Collection-tree sidebar (active = current collection)
    collection_tree_html = _build_collection_tree_html(
        collection_tree, collection_counts, collection["id"],
    )

    return _render_template("collections/detail.html",
        title=f"Collection: {collection.get('name', 'Untitled')}",
        collection=collection,
        breadcrumb_html=breadcrumb_html,
        child_items_html=child_items_html,
        parent_link_html=parent_link_html,
        documents_table_html=documents_table_html,
        collection_tree_html=collection_tree_html,
        doc_count=total,
        page=page, per_page=per_page,
        total=total, total_pages=total_pages,
    )


def _render_documents_list(
    documents: list[dict],
    source: str,
    page: int = 1,
    per_page: int = 20,
    total: int = 0,
    total_pages: int = 0,
    *,
    tags_map: dict[int, list[str]] | None = None,
    all_tags: list[dict] | None = None,
    active_tag: str = "",
    collection_tree: list[dict] | None = None,
    collection_counts: dict[int, int] | None = None,
    active_collection_id: int | None = None,
    collection_path: list[dict] | None = None,
    date_from: str = "",
    date_to: str = "",
    file_type: str = "",
    file_type_facets: list[dict] | None = None,
    source_facets: list[dict] | None = None,
    all_collections_list: list[dict] | None = None,
) -> str:
    tags_map = tags_map or {}
    all_tags = all_tags or []
    collection_tree = collection_tree or []
    collection_counts = collection_counts or {}
    collection_path = collection_path or []

    # Build tag badges HTML for each document
    for doc in documents:
        doc_tags = tags_map.get(doc["id"], [])
        if doc_tags:
            tag_badges = '<div class="doc-tags">' + "".join(
                f'<a href="/documents?tag={_escape(t)}" class="tag-pill">{_escape(t)}</a>'
                for t in doc_tags
            ) + "</div>"
        else:
            tag_badges = ""
        doc["_tag_badges_html"] = tag_badges

    # Build tag cloud
    tag_cloud_html = ""
    if all_tags:
        tag_items = ""
        for t in all_tags:
            tag_name = t["tag"]
            count = t["count"]
            active_class = " active" if tag_name == active_tag else ""
            tag_items += (
                f'<a href="/documents?tag={_escape(tag_name)}" '
                f'class="tag-cloud-item{active_class}">'
                f'{_escape(tag_name)} <span class="tag-count">({count})</span></a>'
            )
        tag_cloud_html = f"""
        <div class="tag-cloud">
            <h3>Tags</h3>
            <div class="tag-cloud-items">
                {tag_items}
            </div>
            {'<p style="margin-top:8px;"><a href="/documents">← Show all documents</a></p>' if active_tag else ''}
        </div>"""

    # Build collection tree sidebar
    collection_tree_html = _build_collection_tree_html(
        collection_tree, collection_counts, active_collection_id
    )

    # Build collection breadcrumb navigation
    collection_breadcrumb_html = _build_collection_breadcrumb_html(
        collection_path
    )

    # Build filter label
    filter_label = ""
    if active_tag:
        filter_label = f" — tag: {_escape(active_tag)}"
    elif source:
        filter_label = f" — {_escape(source)}"
    elif active_collection_id is not None:
        if active_collection_id == 0:
            filter_label = " — unassigned"
        else:
            # Find collection name in the tree
            col_name = _find_collection_name(
                collection_tree, active_collection_id
            )
            if col_name:
                filter_label = f" — collection: {_escape(col_name)}"

    # Build extra filter label for date/type filters
    extra_filter_parts: list[str] = []
    if date_from:
        extra_filter_parts.append(f"from {_escape(date_from)}")
    if date_to:
        extra_filter_parts.append(f"to {_escape(date_to)}")
    if file_type:
        extra_filter_parts.append(f"type: {_escape(file_type)}")
    if extra_filter_parts:
        if filter_label:
            filter_label += " (" + ", ".join(extra_filter_parts) + ")"
        else:
            filter_label = " — " + ", ".join(extra_filter_parts)

    # Build pagination
    source_param = f"&source={_escape(source)}" if source else ""
    tag_param = f"&tag={_escape(active_tag)}" if active_tag else ""
    col_param = (
        f"&collection_id={active_collection_id}"
        if active_collection_id is not None
        else ""
    )
    date_from_param = f"&date_from={_escape(date_from)}" if date_from else ""
    date_to_param = f"&date_to={_escape(date_to)}" if date_to else ""
    file_type_param = f"&file_type={_escape(file_type)}" if file_type else ""
    all_filter_params = (
        source_param + tag_param + col_param
        + date_from_param + date_to_param + file_type_param
    )
    pagination_html = _render_pagination(
        page, per_page, total, total_pages, all_filter_params
    )

    start = (page - 1) * per_page + 1 if total > 0 else 0
    end = min(page * per_page, total)

    tags_col_header = "<th>Tags</th>" if tags_map else ""

    return _render_template("documents/list.html",
        documents=documents, filter_label=filter_label,
        start=start, end=end, total=total,
        page=page, per_page=per_page, total_pages=total_pages,
        tags_col_header=tags_col_header,
        tag_cloud_html=tag_cloud_html,
        collection_tree_html=collection_tree_html,
        collection_breadcrumb_html=collection_breadcrumb_html,
        pagination_html=pagination_html,
        date_from=date_from, date_to=date_to, file_type=file_type,
        active_source=source, active_tag=active_tag,
        active_collection_id=active_collection_id,
        all_collections_list=all_collections_list or [],
        file_type_facets=file_type_facets or [],
        source_facets=source_facets or [],
    )


def _render_documents_table_partial(
    documents: list[dict],
    page: int = 1,
    per_page: int = 20,
    total: int = 0,
    total_pages: int = 0,
    *,
    tags_map: dict[int, list[str]] | None = None,
    active_tag: str = "",
    source: str = "",
    active_collection_id: int | None = None,
    date_from: str = "",
    date_to: str = "",
    file_type: str = "",
    file_type_facets: list[dict] | None = None,
    source_facets: list[dict] | None = None,
    all_collections_list: list[dict] | None = None,
) -> str:
    """Render ONLY the document table region as an HTML fragment.

    This is the partial-swap renderer for HTMX (ADR-003). It returns
    the inner content of the ``#doc-table-region`` div — the count info,
    the document table, bulk-delete form, and pagination — without the
    surrounding page chrome (header, nav, sidebars, filter panel).

    The endpoint ``GET /documents/partials/table`` calls this and returns
    the fragment. The client-side ``hx-target="#doc-table-region"`` swaps
    the old table region with the new one.

    Data preparation mirrors ``_render_documents_list`` but skips tag
    cloud, collection tree, and breadcrumb (those sidebars don't change
    when filters are applied — only the table does).
    """
    tags_map = tags_map or {}

    # Build tag badges HTML for each document (same as full-page renderer)
    for doc in documents:
        doc_tags = tags_map.get(doc["id"], [])
        if doc_tags:
            tag_badges = '<div class="doc-tags">' + "".join(
                f'<a href="/documents?tag={_escape(t)}" class="tag-pill">{_escape(t)}</a>'
                for t in doc_tags
            ) + "</div>"
        else:
            tag_badges = ""
        doc["_tag_badges_html"] = tag_badges

    # Build pagination with filter params
    source_param = f"&source={_escape(source)}" if source else ""
    tag_param = f"&tag={_escape(active_tag)}" if active_tag else ""
    col_param = (
        f"&collection_id={active_collection_id}"
        if active_collection_id is not None
        else ""
    )
    date_from_param = f"&date_from={_escape(date_from)}" if date_from else ""
    date_to_param = f"&date_to={_escape(date_to)}" if date_to else ""
    file_type_param = f"&file_type={_escape(file_type)}" if file_type else ""
    all_filter_params = (
        source_param + tag_param + col_param
        + date_from_param + date_to_param + file_type_param
    )
    pagination_html = _render_pagination(
        page, per_page, total, total_pages, all_filter_params
    )

    start = (page - 1) * per_page + 1 if total > 0 else 0
    end = min(page * per_page, total)
    tags_col_header = "<th>Tags</th>" if tags_map else ""

    return _render_template("_partials/documents_table.html",
        documents=documents,
        start=start, end=end, total=total,
        page=page, per_page=per_page, total_pages=total_pages,
        tags_col_header=tags_col_header,
        pagination_html=pagination_html,
        active_tag=active_tag, source=source,
        active_collection_id=active_collection_id,
        date_from=date_from, date_to=date_to, file_type=file_type,
        file_type_facets=file_type_facets or [],
        source_facets=source_facets or [],
        all_collections_list=all_collections_list or [],
    )


def _render_document_rows_partial(
    documents: list[dict],
    *,
    tags_map: dict[int, list[str]] | None = None,
) -> str:
    """Render ONLY the <tr> rows for one page of the document table.

    This is the infinite-scroll fragment renderer for Phase 9 lazy loading.
    Returns just the ``<tr>`` elements (no ``<table>``, ``<tbody>``, or page
    chrome) so the client can append them via ``hx-swap="beforeend"`` to the
    existing ``#doc-tbody``.

    Tag-badge preparation mirrors ``_render_documents_list``.
    """
    tags_map = tags_map or {}
    for doc in documents:
        doc_tags = tags_map.get(doc["id"], [])
        if doc_tags:
            tag_badges = '<div class="doc-tags">' + "".join(
                f'<a href="/documents?tag={_escape(t)}" class="tag-pill">{_escape(t)}</a>'
                for t in doc_tags
            ) + "</div>"
        else:
            tag_badges = ""
        doc["_tag_badges_html"] = tag_badges

    return _render_template("_partials/document_rows.html", documents=documents)


def _render_document_detail(
    doc: dict,
    tags: list[str] | None = None,
    current_collection: dict | None = None,
    all_collections: list[dict] | None = None,
) -> str:
    tags = tags or []
    all_collections = all_collections or []
    full_body = doc.get("body", "") or ""
    excerpt = full_body[:500]
    if len(full_body) > 500:
        excerpt = excerpt.rstrip() + "…"

    # Build tag badges with remove buttons
    tag_badges_html = ""
    if tags:
        tag_badges_html = '<div class="doc-tags">'
        for t in tags:
            tag_badges_html += (
                f'<span class="tag-pill">{_escape(t)}'
                f'<form action="/documents/{doc.get("id", "?")}/tags/{_escape(t)}/delete" '
                f'method="post" style="display:inline;">'
                f'<button type="submit" class="tag-remove" title="Remove tag" data-optimistic-action="tag-remove">✕</button>'
                f'</form></span>'
            )
        tag_badges_html += "</div>"

    from .document_viewer import word_count, reading_time_minutes

    wc = word_count(full_body)
    rt = reading_time_minutes(full_body)

    # Extract email metadata for email-sourced documents. The metadata
    # dict (parsed from the JSON column by db_sqlite) contains keys like
    # email_sender, email_subject, email_thread_id, etc. when the document
    # was ingested from an email account.
    email_meta = None
    if doc.get("source_type") == "email":
        meta = doc.get("metadata") or {}
        if isinstance(meta, dict) and any(k.startswith("email_") for k in meta):
            email_meta = meta

    return _render_template("documents/detail.html",
        doc=doc, tag_badges_html=tag_badges_html,
        excerpt=excerpt, wc=wc, rt=rt,
        current_collection=current_collection,
        all_collections=all_collections,
        email_meta=email_meta,
    )


def _render_upload_form(error: str = "") -> str:
    return _render_template("upload_form.html", error=error)


def _render_upload_success(title: str, doc_id: int, job_id: str) -> str:
    return _render_template("upload_success.html", title=title, doc_id=doc_id, job_id=job_id)


def _render_upload_batch(
    results: list[dict] | None = None,
    errors: list[dict] | None = None,
) -> str:
    """Render the batch upload results page.

    ``results`` is a list of ``{title, doc_id, job_id}`` dicts for files
    that were accepted. ``errors`` is a list of ``{filename, error}`` dicts
    for files that were rejected.
    """
    return _render_template(
        "upload_batch.html",
        results=results or [],
        errors=errors or [],
        total_ok=len(results or []),
        total_err=len(errors or []),
    )


def _render_pagination(
    page: int,
    per_page: int,
    total: int,
    total_pages: int,
    extra_params: str = "",
) -> str:
    """Render pagination navigation with prev/next and page numbers."""
    if total_pages <= 1:
        return ""

    base = f"?per_page={per_page}{extra_params}"

    parts: list[str] = ['<div class="pagination">']

    # Prev button
    if page > 1:
        parts.append(f'<a href="{base}&page={page - 1}">← 上一页</a>')
    else:
        parts.append('<span class="disabled">← 上一页</span>')

    # Page numbers (show up to 7 pages with ellipsis)
    max_show = 7
    if total_pages <= max_show:
        for p in range(1, total_pages + 1):
            if p == page:
                parts.append(f'<span class="current">{p}</span>')
            else:
                parts.append(f'<a href="{base}&page={p}">{p}</a>')
    else:
        half = max_show // 2
        start_page = max(1, page - half)
        end_page = min(total_pages, page + half)
        if start_page > 1:
            parts.append(f'<a href="{base}&page=1">1</a>')
            if start_page > 2:
                parts.append('<span class="disabled">…</span>')
        for p in range(start_page, end_page + 1):
            if p == page:
                parts.append(f'<span class="current">{p}</span>')
            else:
                parts.append(f'<a href="{base}&page={p}">{p}</a>')
        if end_page < total_pages:
            if end_page < total_pages - 1:
                parts.append('<span class="disabled">…</span>')
            parts.append(f'<a href="{base}&page={total_pages}">{total_pages}</a>')

    # Next button
    if page < total_pages:
        parts.append(f'<a href="{base}&page={page + 1}">下一页 →</a>')
    else:
        parts.append('<span class="disabled">下一页 →</span>')

    parts.append("</div>")
    return "\n".join(parts)


def _render_delete_success(doc_id: int) -> str:
    return _render_template("delete_success.html", doc_id=doc_id)


def _render_chat_page() -> str:
    return _render_template("chat.html")


# ── Settings page renderer ──────────────────────────────────────


def _mask_api_key(key: str) -> str:
    """Mask an API key for display, showing only the last 4 characters."""
    if not key:
        return ""
    if len(key) <= 4:
        return "****" + key
    return "****" + key[-4:]


def _render_login_page(*, error: str = "") -> str:
    """Render the login page, extending base.html for shared theme support.

    The login page uses base.html's CSS variables and theme-toggle JS
    so dark-mode behaviour is consistent with the rest of the app.
    Login-specific styling (centered card, form layout) is injected via
    the ``extra_head`` block.
    """
    return _render_template("login.html", title="Login", error=error)


def _render_settings_page(settings: dict[str, str], *, success: bool = False) -> str:
    """Render the LLM settings page (now including an auth section)."""
    provider = settings.get("llm_provider", "")
    model = settings.get("llm_model", "")
    raw_api_key = settings.get("llm_api_key", "")
    base_url = settings.get("llm_base_url", "")
    max_tokens = settings.get("llm_max_tokens", "1000")
    temperature = settings.get("llm_temperature", "0.3")
    chat_fallback = settings.get("llm_chat_fallback", "1")

    masked_key = _mask_api_key(raw_api_key)
    fallback_checked = "checked" if chat_fallback == "1" else ""

    show_base_url = provider in ("openai-compat", "ollama")
    base_url_row_display = "block" if show_base_url else "none"

    # ── Auth settings ──────────────────────────────────────────────
    auth_enabled = settings.get("auth_enabled", "0") == "1"
    auth_enabled_checked = "checked" if auth_enabled else ""
    auth_api_key = settings.get("auth_api_key", "")
    masked_auth_key = _mask_api_key(auth_api_key)

    return _render_template("settings.html",
        provider=provider, model=model, masked_key=masked_key,
        base_url=base_url, max_tokens=max_tokens, temperature=temperature,
        fallback_checked=fallback_checked,
        base_url_row_display=base_url_row_display,
        auth_enabled_checked=auth_enabled_checked,
        masked_auth_key=masked_auth_key,
        success=success,
    )


def _render_settings_redirect() -> str:
    """Render a minimal HTML page with a meta-refresh redirect."""
    return (
        '<!DOCTYPE html><html><head>'
        '<meta http-equiv="refresh" content="0; url=/settings?saved=1">'
        '<title>Redirecting…</title></head>'
        '<body>Settings saved. <a href="/settings?saved=1">Continue</a>.</body>'
        '</html>'
    )


def _reload_llm_config_from_db(settings: dict[str, str]) -> None:
    """Reload the in-memory LLMConfig from DB-stored settings."""
    from ..core.config import config

    config.llm.provider = settings.get("llm_provider", config.llm.provider)
    config.llm.model = settings.get("llm_model", config.llm.model)
    stored_key = settings.get("llm_api_key")
    if stored_key and not stored_key.startswith("****"):
        config.llm.api_key = stored_key
    config.llm.base_url = settings.get("llm_base_url", config.llm.base_url)

    try:
        config.llm.max_tokens = int(
            settings.get("llm_max_tokens", config.llm.max_tokens)
        )
    except (ValueError, TypeError):
        pass

    try:
        config.llm.temperature = float(
            settings.get("llm_temperature", config.llm.temperature)
        )
    except (ValueError, TypeError):
        pass


def _render_jobs_page(
    jobs: list[JobRecord],
    state_filter: str,
    page: int,
    per_page: int,
    total: int,
    total_pages: int,
    has_active: bool,
) -> str:
    """Render the job processing status page."""
    states = [
        ("", "All"),
        ("pending", "Pending"),
        ("processing", "Processing"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    ]
    state_options = []
    for val, label in states:
        selected = " selected" if val == state_filter else ""
        state_options.append((val, label, selected))

    refresh_meta = has_active

    state_param = f"&state={_escape(state_filter)}" if state_filter else ""
    pagination_html = _render_pagination(
        page, per_page, total, total_pages, state_param
    )

    start = (page - 1) * per_page + 1 if total > 0 else 0
    end = min(page * per_page, total)

    return _render_template("jobs.html",
        jobs=jobs, state_options=state_options, per_page=per_page,
        start=start, end=end, total=total,
        refresh_meta=refresh_meta, pagination_html=pagination_html,
    )


def _render_job_detail(job: JobRecord, document: dict | None = None) -> str:
    """Render a single job's detail page."""
    # Associated document section
    if document:
        doc_id = document.get("id", "?")
        doc_title = document.get("title", "Untitled")
        doc_status = document.get("status", "")
        doc_status_class = f"badge-{doc_status}"
        doc_html = f"""
        <div class="card">
            <h3>📄 Associated Document</h3>
            <div class="field"><span class="field-label">Document:</span> <a href="/documents/{doc_id}">[{doc_id}] {_escape(doc_title)}</a></div>
            <div class="field"><span class="field-label">Status:</span> <span class="badge {doc_status_class}">{doc_status}</span></div>
            <div class="field"><span class="field-label">Path:</span> {_escape(document.get('path', ''))}</div>
        </div>"""
    elif job.document_id is not None:
        doc_html = """
        <div class="card">
            <h3>📄 Associated Document</h3>
            <p class="pagination-info">Document was deleted (id: {}).</p>
        </div>""".format(job.document_id)
    else:
        doc_html = """
        <div class="card">
            <h3>📄 Associated Document</h3>
            <p class="pagination-info">No document linked yet — job may still be processing.</p>
        </div>"""

    return _render_template("job_detail.html", job=job, doc_html=doc_html)


def _render_collection_form(
    *,
    mode: str = "create",
    collection: dict | None = None,
    parent_choices: list[dict] | None = None,
) -> str:
    """Render the shared create/edit collection form.

    Parameters
    ----------
    mode:
        ``"create"`` for a new collection, ``"edit"`` for an existing one.
    collection:
        The collection dict (from ``db.get_collection``). Required for
        edit mode (provides name, description, parent_id, id). Should be
        ``None`` for create mode.
    parent_choices:
        Flat list of all collections (from ``db.list_collections()``) with
        an added ``indented_name`` field showing the hierarchy depth.
        The collection being edited is excluded from its own parent list.
    """
    parent_choices = parent_choices or []
    if mode == "edit":
        page_title = "Edit Collection"
        col_id = collection["id"] if collection else 0
        form_action = f"/collections/{col_id}/edit"
        delete_action = f"/collections/{col_id}/delete"
    else:
        page_title = "New Collection"
        form_action = "/collections/create"
        delete_action = ""

    return _render_template(
        "collections/form.html",
        mode=mode,
        page_title=page_title,
        collection=collection,
        parent_choices=parent_choices,
        form_action=form_action,
        delete_action=delete_action,
    )


def _render_email_accounts_list(accounts: list[dict]) -> str:
    """Render the email accounts list page.

    Expects accounts to be a list of email account dicts (as returned
    by db.list_email_accounts()) with the password key already
    stripped by the caller (server.py route handler).
    """
    return _render_template("email/accounts_list.html", accounts=accounts)


def _render_email_account_form(
    mode: str = "create",
    account: dict | None = None,
    error: str | None = None,
) -> str:
    """Render the create/edit email account form."""
    if mode == "edit":
        page_title = "Edit Email Account"
        acct_id = account["id"] if account else 0
        form_action = f"/email-accounts/{acct_id}/edit"
    else:
        page_title = "New Email Account"
        form_action = "/email-accounts/create"

    return _render_template(
        "email/account_form.html",
        mode=mode,
        page_title=page_title,
        account=account,
        error=error,
        form_action=form_action,
    )


def _render_email_logs(
    account: dict,
    logs: list[dict],
    total: int,
    status_filter: str = "",
    page: int = 1,
    per_page: int = 50,
) -> str:
    """Render the ingestion logs page for a single email account."""
    import math
    total_pages = max(1, math.ceil(total / per_page)) if per_page > 0 else 1
    return _render_template(
        "email/logs.html",
        account=account,
        logs=logs,
        total=total,
        status_filter=status_filter,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )


def _render_error(title: str, message: str) -> str:
    return _render_template("error.html", title=title, message=message)



