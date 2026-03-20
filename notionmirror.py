#!/usr/bin/env python3
"""NotionCodeMirror — Auto-generate a living code documentation workspace in Notion.

Usage:
    python notionmirror.py <github-url-or-local-path> [options]

Environment variables:
    ANTHROPIC_API_KEY   Required for Phase 2 (Claude agent)
    NOTION_API_KEY      Required for Phase 2 (Notion API)
    GITHUB_TOKEN        Optional; raises GitHub rate limit to 5000 req/hr
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


def _check_env() -> list[str]:
    """Return a list of missing required environment variables."""
    missing = []
    for var in ("ANTHROPIC_API_KEY", "NOTION_API_KEY"):
        if not os.environ.get(var):
            missing.append(var)
    return missing


def _is_local_path(source: str) -> bool:
    """Return True if source looks like a local filesystem path."""
    p = Path(source)
    if p.exists():
        return True
    # Not a URL
    return "://" not in source and not source.startswith("github.com/")


def _repo_slug(source: str) -> str:
    """Derive a filesystem-safe slug from the source."""
    if "github.com" in source:
        # https://github.com/owner/repo → owner__repo
        parts = source.rstrip("/").split("/")
        if len(parts) >= 2:
            return f"{parts[-2]}__{parts[-1]}"
    return Path(source).name.replace(" ", "_")


async def main_async(args: argparse.Namespace) -> int:
    source = args.source
    is_local = _is_local_path(source)

    # ── Dry-run check ─────────────────────────────────────────────────────────
    if not args.dry_run:
        missing = _check_env()
        if missing:
            print(f"Error: missing environment variable(s): {', '.join(missing)}", file=sys.stderr)
            return 1

    if not args.dry_run and not args.notion_parent_id:
        print("Error: --notion-parent-id is required unless using --dry-run", file=sys.stderr)
        return 1

    # ── Phase 1: Gather data ──────────────────────────────────────────────────
    from phase1_gather import gather, RepoData
    from state import load_state, save_state

    print(f"\n🔍 Phase 1: Gathering data from {'local path' if is_local else 'GitHub'}...")
    print(f"   Source: {source}")

    try:
        data: RepoData = await gather(
            source=source,
            use_ai_summaries=not args.no_ai_summaries,
            is_local=is_local,
        )
    except* RuntimeError as eg:
        for e in eg.exceptions:
            print(f"Error during indexing: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"Error during indexing: {e}", file=sys.stderr)
        return 1

    print(f"\n   ✓ Repo: {data.repo_key}")
    print(f"   ✓ Files: {data.file_count}, Symbols: {data.symbol_count}")
    print(f"   ✓ Languages: {data.languages}")
    print(f"   ✓ Top dirs: {data.top_dirs[:6]}")
    print(f"   ✓ Symbols gathered: {len(data.all_symbols)}")

    # ── Dry-run: print gathered data and exit ─────────────────────────────────
    if args.dry_run:
        output = {
            "repo_key": data.repo_key,
            "git_head": data.git_head,
            "file_count": data.file_count,
            "symbol_count": data.symbol_count,
            "languages": data.languages,
            "top_dirs": data.top_dirs,
            "entry_files": data.entry_files,
            "symbol_sample": data.all_symbols[:5],
            "classes_count": len(data.classes),
            "dep_graphs_count": len(data.dep_graphs),
            "context_bundle_length": len(data.context_bundle),
        }
        print("\n── Gathered Data (JSON) ──")
        print(json.dumps(output, indent=2))
        return 0

    # ── Incremental sync: check git_head ──────────────────────────────────────
    existing_state = load_state(data.repo_key)

    if args.sync and existing_state:
        if (
            not args.force
            and existing_state.get("git_head") == data.git_head
            and data.git_head
        ):
            print("\n✅ No changes detected (git_head unchanged). Workspace is up to date.")
            root_id = existing_state.get("notion_root_page_id", "")
            if root_id:
                print(f"   Workspace: https://notion.so/{root_id.replace('-', '')}")
            return 0
    elif not args.sync and existing_state and not args.force:
        print(
            "\nNote: A previous run exists for this repo. "
            "Use --sync to update it or --force to re-create."
        )

    # ── Phase 2: Write Notion workspace ──────────────────────────────────────
    from phase2_write import run_phase2

    print(f"\n✍️  Phase 2: Writing Notion workspace...")
    print(f"   Parent page: {args.notion_parent_id}")

    try:
        new_state = await run_phase2(
            data=data,
            parent_id=args.notion_parent_id,
            state=existing_state if args.sync else None,
            sync=bool(args.sync and existing_state),
        )
    except* RuntimeError as eg:
        for e in eg.exceptions:
            print(f"Error during Notion write: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"Error during Notion write: {e}", file=sys.stderr)
        return 1

    # ── Save state ────────────────────────────────────────────────────────────
    save_state(data.repo_key, new_state)

    # ── Success ───────────────────────────────────────────────────────────────
    root_id = new_state.get("notion_root_page_id", "")
    notion_url = f"https://notion.so/{root_id.replace('-', '')}" if root_id else "(unknown)"

    print(f"\n✅ Done. Workspace: {notion_url}")
    print(f"   Root page ID:       {root_id}")
    print(f"   Overview page:      {new_state.get('overview_page_id', '')}")
    print(f"   Architecture page:  {new_state.get('architecture_page_id', '')}")
    print(f"   API Reference DB:   {new_state.get('api_db_id', '')}")
    print(f"   Module pages:       {len(new_state.get('module_page_ids', {}))}")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="notionmirror",
        description="Auto-generate a living Notion code documentation workspace from any GitHub repo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python notionmirror.py https://github.com/jgravelle/jcodemunch-mcp \\
      --notion-parent-id abc123def456

  python notionmirror.py ./my-local-project \\
      --notion-parent-id abc123def456 --no-ai-summaries

  python notionmirror.py https://github.com/owner/repo \\
      --notion-parent-id abc123def456 --sync

  python notionmirror.py https://github.com/owner/repo --dry-run

Environment variables:
  ANTHROPIC_API_KEY   Required (Claude API)
  NOTION_API_KEY      Required (Notion API)
  GITHUB_TOKEN        Optional (higher GitHub rate limit)
""",
    )

    parser.add_argument(
        "source",
        help="GitHub repo URL (https://github.com/owner/repo) or local path",
    )
    parser.add_argument(
        "--notion-parent-id",
        metavar="PAGE_ID",
        help="Notion page ID to create the workspace under",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Update existing workspace (requires a prior run)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run Phase 2 even if no code changes detected",
    )
    parser.add_argument(
        "--no-ai-summaries",
        action="store_true",
        help="Skip AI-generated summaries in Phase 1 (faster indexing)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run Phase 1 only and print gathered data as JSON (no Notion writes)",
    )

    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
