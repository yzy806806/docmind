"""DocMind CLI — command-line interface for the document knowledge base.

Usage:
    docmind ingest <path>           Scan and index a directory or file
    docmind search <query>          Search the knowledge base
    docmind list [--source <name>]  List indexed documents
    docmind show <doc_id>           Show document details
    docmind summarize <doc_id>      Generate/show a document summary
    docmind stats                   Show knowledge base statistics

Options:
    --format json|table|rich        Output format (default: table)
    --top-k N                       Max search results (default: 10)
    --source NAME                   Filter by source name
    --db-path PATH                  SQLite index path (default: data/docmind.db)
    --force                         Force re-summarization
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from .formatters import (
    format_document,
    format_list,
    format_output,
    format_search_results,
)
from .services import DocMindService, get_service


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="docmind",
        description="DocMind — AI-powered enterprise document knowledge base",
    )

    parser.add_argument(
        "--format",
        choices=["json", "table", "rich"],
        default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--db-path",
        default="data/docmind.db",
        help="Path to the SQLite index database",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # ── ingest ────────────────────────────────────────────────
    ingest_parser = sub.add_parser("ingest", help="Index a file or directory")
    ingest_parser.add_argument("path", help="File or directory path to index")
    ingest_parser.add_argument(
        "--source-name",
        default="cli",
        help="Source name for tracking (default: cli)",
    )

    # ── search ────────────────────────────────────────────────
    search_parser = sub.add_parser("search", help="Search the knowledge base")
    search_parser.add_argument("query", help="Search query string")
    search_parser.add_argument(
        "--top-k",
        type=int,
        default=10,
        help="Maximum number of results (default: 10)",
    )

    # ── list ──────────────────────────────────────────────────
    list_parser = sub.add_parser("list", help="List indexed documents")
    list_parser.add_argument(
        "--source",
        default=None,
        help="Filter by source name",
    )
    list_parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max documents to list (default: 100)",
    )

    # ── show ──────────────────────────────────────────────────
    show_parser = sub.add_parser("show", help="Show document details")
    show_parser.add_argument("doc_id", help="Document ID")

    # ── summarize ─────────────────────────────────────────────
    summarize_parser = sub.add_parser(
        "summarize", help="Generate or show document summary"
    )
    summarize_parser.add_argument("doc_id", help="Document ID")
    summarize_parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-summarization even if cached",
    )

    # ── stats ─────────────────────────────────────────────────
    sub.add_parser("stats", help="Show knowledge base statistics")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point for the DocMind CLI.

    Returns:
        Exit code (0 on success, 1 on error).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    fmt = args.format
    db_path = getattr(args, "db_path", "data/docmind.db")
    search_db_path = db_path.replace(".db", "_fts.db")

    svc = DocMindService(
        index_db_path=db_path,
        search_db_path=search_db_path,
    )

    try:
        exit_code = _dispatch(args, svc, fmt)
    except Exception as e:
        error_output = {
            "error": type(e).__name__,
            "message": str(e),
        }
        if fmt == "json":
            print(format_output(error_output, fmt="json"))
        else:
            print(f"Error: {e}", file=sys.stderr)
        exit_code = 1
    finally:
        svc.close()

    return exit_code


def _dispatch(args: argparse.Namespace, svc: DocMindService, fmt: str) -> int:
    """Dispatch to the appropriate handler based on subcommand."""
    if args.command == "ingest":
        return _cmd_ingest(args, svc, fmt)
    elif args.command == "search":
        return _cmd_search(args, svc, fmt)
    elif args.command == "list":
        return _cmd_list(args, svc, fmt)
    elif args.command == "show":
        return _cmd_show(args, svc, fmt)
    elif args.command == "summarize":
        return _cmd_summarize(args, svc, fmt)
    elif args.command == "stats":
        return _cmd_stats(args, svc, fmt)
    else:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        return 1


def _cmd_ingest(args, svc: DocMindService, fmt: str) -> int:
    """Handle the `ingest` command."""
    result = svc.ingest_path(args.path, source_name=args.source_name)
    print(format_output(result, fmt=fmt, title=f"Ingested: {args.path}"))
    return 0


def _cmd_search(args, svc: DocMindService, fmt: str) -> int:
    """Handle the `search` command."""
    results = svc.search(args.query, top_k=args.top_k)
    print(format_search_results(results, len(results), args.query, fmt=fmt))
    return 0


def _cmd_list(args, svc: DocMindService, fmt: str) -> int:
    """Handle the `list` command."""
    documents = svc.list_documents(source=args.source, limit=args.limit)
    print(format_list(documents, len(documents), fmt=fmt))
    return 0


def _cmd_show(args, svc: DocMindService, fmt: str) -> int:
    """Handle the `show` command."""
    doc = svc.get_document(args.doc_id)
    print(format_document(doc, fmt=fmt))
    return 0


def _cmd_summarize(args, svc: DocMindService, fmt: str) -> int:
    """Handle the `summarize` command."""
    result = svc.summarize_document(args.doc_id, force=args.force)
    if fmt == "json":
        print(format_output(result, fmt="json"))
    else:
        print(f"Summary for [{result['doc_id']}] {result['title']}")
        print("=" * 60)
        if result.get("cached"):
            print("(cached)")
        print(result.get("summary", "(no summary available)"))
    return 0


def _cmd_stats(args, svc: DocMindService, fmt: str) -> int:
    """Handle the `stats` command."""
    stats = svc.get_stats()
    print(format_output(stats, fmt=fmt, title="DocMind Knowledge Base Stats"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
