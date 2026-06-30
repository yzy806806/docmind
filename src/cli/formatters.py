"""Output formatters for the DocMind CLI.

Supports three output formats:
- json: Machine-readable JSON output
- table: Tabulated human-readable output
- rich: Colorized rich terminal output (requires `rich` library, gracefully degrades)
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

# ── Format dispatch ──────────────────────────────────────────────


def format_output(
    data: Any,
    fmt: str = "table",
    *,
    title: Optional[str] = None,
) -> str:
    """Format data according to the requested format.

    Args:
        data: The data to format (dict, list, or plain value).
        fmt: One of 'json', 'table', 'rich'.
        title: Optional title for table/rich output.

    Returns:
        Formatted string ready for stdout.
    """
    if fmt == "json":
        return _format_json(data)
    elif fmt == "rich":
        return _format_rich(data, title=title)
    else:  # default: table
        return _format_table(data, title=title)


def _format_json(data: Any) -> str:
    """Format as pretty-printed JSON."""
    return json.dumps(data, indent=2, default=str, ensure_ascii=False)


# ── Table formatter ─────────────────────────────────────────────


def _format_table(data: Any, *, title: Optional[str] = None) -> str:
    """Format as a plain-text table using aligned columns."""
    lines: list[str] = []

    if title:
        lines.append(title)
        lines.append("-" * len(title))
        lines.append("")

    if isinstance(data, dict):
        lines.extend(_format_dict_table(data))
    elif isinstance(data, list):
        lines.extend(_format_list_table(data))
    else:
        lines.append(str(data))

    return "\n".join(lines)


def _format_dict_table(d: dict) -> list[str]:
    """Format a dict as key-value rows."""
    lines: list[str] = []
    max_key_len = max((len(str(k)) for k in d.keys()), default=0)

    for key, value in d.items():
        if isinstance(value, (list, dict)):
            value_str = json.dumps(value, default=str)
            if len(value_str) > 80:
                value_str = value_str[:77] + "..."
        else:
            value_str = str(value)
        lines.append(f"{str(key):<{max_key_len}} : {value_str}")

    return lines


def _format_list_table(items: list) -> list[str]:
    """Format a list of dicts as a table."""
    if not items:
        return ["(no results)"]

    if isinstance(items[0], dict):
        return _format_dict_list_table(items)
    else:
        return [f"  {i}: {item}" for i, item in enumerate(items)]


def _format_dict_list_table(items: list[dict]) -> list[str]:
    """Format a list of dicts with aligned columns."""
    if not items:
        return ["(no results)"]

    # Determine columns from common keys
    # Prioritize common fields for document listing
    priority_keys = ["id", "doc_id", "title", "path", "source", "status", "summary"]
    all_keys = set()
    for item in items:
        all_keys.update(item.keys())

    # Order keys: priority first, then alphabetical
    columns = [k for k in priority_keys if k in all_keys]
    columns += sorted(k for k in all_keys if k not in priority_keys)

    # Limit to reasonable number of columns
    if len(columns) > 6:
        columns = columns[:6]

    # Compute column widths
    widths: dict[str, int] = {}
    for col in columns:
        widths[col] = max(
            len(col),
            max((len(str(item.get(col, ""))[:40]) for item in items), default=0),
        )

    lines: list[str] = []

    # Header
    header = "  " + "  ".join(col.ljust(widths[col]) for col in columns)
    lines.append(header)
    lines.append("  " + "  ".join("-" * widths[col] for col in columns))

    # Rows
    for item in items:
        row_parts: list[str] = []
        for col in columns:
            val = str(item.get(col, ""))
            # Truncate long values
            if len(val) > 40:
                val = val[:37] + "..."
            row_parts.append(val.ljust(widths[col]))
        lines.append("  " + "  ".join(row_parts))

    return lines


# ── Rich formatter ──────────────────────────────────────────────


def _format_rich(data: Any, *, title: Optional[str] = None) -> str:
    """Format using the `rich` library if available, falling back to table."""
    try:
        from rich.console import Console
        from rich.table import Table
        from rich.text import Text
        from rich.panel import Panel
        from io import StringIO

        console = Console(file=StringIO(), force_terminal=True, width=120)
        return _render_rich(console, data, title=title)
    except ImportError:
        return _format_table(data, title=title)


def _render_rich(console, data: Any, *, title: Optional[str] = None) -> str:
    """Render data through a Rich Console, returning the string output."""
    from io import StringIO
    from rich.table import Table
    from rich.panel import Panel

    output = StringIO()
    console.file = output

    if title:
        console.print(f"[bold]{title}[/bold]")

    if isinstance(data, dict):
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Key", style="green")
        table.add_column("Value")
        for key, value in data.items():
            val_str = json.dumps(value, default=str) if isinstance(value, (list, dict)) else str(value)
            table.add_row(str(key), val_str[:200])
        console.print(table)

    elif isinstance(data, list) and data and isinstance(data[0], dict):
        # Determine columns
        priority_keys = ["id", "doc_id", "title", "path", "source", "status", "summary", "confidence"]
        all_keys = set()
        for item in data:
            all_keys.update(item.keys())
        columns = [k for k in priority_keys if k in all_keys]
        columns += sorted(k for k in all_keys if k not in priority_keys)[:4]

        table = Table(show_header=True, header_style="bold cyan", show_lines=False)
        for col in columns:
            table.add_column(col, style="white", no_wrap=False, max_width=40)

        for item in data:
            row = []
            for col in columns:
                val = item.get(col, "")
                if isinstance(val, dict):
                    # Special handling for citation dicts
                    conf = val.get("confidence", "")
                    if conf:
                        style = {
                            "exact_match": "green",
                            "high": "blue",
                            "medium": "yellow",
                            "low": "red",
                        }.get(conf, "white")
                        row.append(f"[{style}]{conf}[/{style}]")
                    else:
                        row.append(str(val)[:60])
                else:
                    row.append(str(val)[:60])
            table.add_row(*row)
        console.print(table)

    else:
        console.print(data)

    return output.getvalue()


# ── Specialized formatters ───────────────────────────────────────


def format_search_results(
    results: list[dict],
    total: int,
    query: str,
    fmt: str = "table",
) -> str:
    """Format search results with citations for CLI display."""
    if fmt == "json":
        return json.dumps({
            "query": query,
            "total": total,
            "results": results,
        }, indent=2, default=str, ensure_ascii=False)

    lines: list[str] = []
    lines.append(f'Search: "{query}"')
    lines.append(f"Found {total} result(s)")
    lines.append("=" * 60)

    for i, result in enumerate(results):
        doc_id = result.get("doc_id", "?")
        title = result.get("title", "Untitled")
        snippet = result.get("snippet", "")
        path = result.get("path", "")
        citation = result.get("citation", {})

        lines.append(f"\n#{i + 1}  [{doc_id}] {title}")
        if snippet:
            lines.append(f"     {snippet[:200]}")
        if path:
            lines.append(f"     Path: {path}")

        # Citation badge
        if citation:
            conf = citation.get("confidence", "low")
            badges = {
                "exact_match": "✓ EXACT",
                "high": "⬆ HIGH",
                "medium": "≈ MEDIUM",
                "low": "? LOW",
            }
            lines.append(f"     Source: {badges.get(conf, conf)}")

    return "\n".join(lines)


def format_list(
    documents: list[dict],
    total: int,
    fmt: str = "table",
) -> str:
    """Format document listing for CLI display."""
    if fmt == "json":
        return json.dumps({
            "total": total,
            "documents": documents,
        }, indent=2, default=str, ensure_ascii=False)

    output = format_output(
        [
            {
                "id": d.get("id", "?"),
                "title": d.get("title", ""),
                "status": d.get("status", ""),
                "source": d.get("source_name", d.get("source_type", "")),
                "ext": d.get("ext", ""),
            }
            for d in documents
        ],
        fmt=fmt,
        title=f"Documents ({total} total)",
    )
    return output


def format_document(doc: dict, fmt: str = "table") -> str:
    """Format a single document's details for CLI display."""
    if fmt == "json":
        return json.dumps(doc, indent=2, default=str, ensure_ascii=False)

    lines: list[str] = []
    lines.append(f"Document: {doc.get('title', 'Untitled')}")
    lines.append("=" * 60)
    lines.append(f"  ID:      {doc.get('id', '?')}")
    lines.append(f"  Path:    {doc.get('path', '')}")
    lines.append(f"  Status:  {doc.get('status', '')}")
    lines.append(f"  Source:  {doc.get('source_name', doc.get('source_type', ''))}")
    lines.append(f"  Type:    {doc.get('ext', '')} ({doc.get('mime_type', '')})")

    summary = doc.get("summary")
    if summary:
        lines.append(f"\n  Summary:\n  {summary}")

    body = doc.get("body", "")
    if body:
        preview = body[:1000]
        if len(body) > 1000:
            preview += "..."
        lines.append(f"\n  Body ({len(body)} chars):\n  {preview}")

    return "\n".join(lines)
