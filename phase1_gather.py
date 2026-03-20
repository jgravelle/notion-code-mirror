"""Phase 1: Gather all repo data from jcodemunch-mcp → RepoData dataclass."""

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from mcp_client import jcodemunch_session


@dataclass
class RepoData:
    """All gathered data from Phase 1 jcodemunch-mcp calls."""

    repo_key: str          # e.g. "owner/repo" or local path slug
    git_head: str          # tree SHA or empty string
    is_github: bool
    languages: dict        # {"python": 85, "shell": 15}
    file_count: int
    symbol_count: int
    outline: dict          # raw get_repo_outline result
    file_tree_root: dict   # raw get_file_tree result for root
    dir_trees: dict        # dirname → raw get_file_tree result
    all_symbols: list      # up to 100 symbols sorted by centrality
    classes: list          # up to 30 class symbols
    class_hierarchies: dict  # class_id → hierarchy result
    dep_graphs: dict       # file → dependency graph result
    context_bundle: str    # markdown context for top symbols
    top_dirs: list[str]    # top-level directory names
    entry_files: list[str]  # likely entry point files


async def gather(
    source: str,
    use_ai_summaries: bool = True,
    is_local: bool = False,
) -> RepoData:
    """Run all Phase 1 jcodemunch-mcp calls and return a populated RepoData.

    Args:
        source: GitHub URL or local filesystem path.
        use_ai_summaries: Whether to request AI-generated summaries.
        is_local: True if source is a local path (uses index_folder instead of index_repo).
    """
    async with jcodemunch_session() as mcp:
        # ── Step 1: Index the repository ─────────────────────────────────────
        print("  [1/8] Indexing repository...")
        if is_local:
            idx = await mcp.call(
                "index_folder",
                path=source,
                use_ai_summaries=use_ai_summaries,
                incremental=True,
            )
        else:
            idx = await mcp.call(
                "index_repo",
                url=source,
                use_ai_summaries=use_ai_summaries,
                incremental=True,
            )

        if idx.get("success") is False:
            raise RuntimeError(f"Indexing failed: {idx.get('error', 'unknown error')}")

        repo_key = idx.get("repo", source)
        git_head = idx.get("git_head", "")

        changed = idx.get("changed", -1)
        new_files = idx.get("new", -1)
        if changed == 0 and new_files == 0 and idx.get("deleted", 0) == 0:
            # No changes — will be detected by caller via git_head comparison
            pass

        # ── Step 2: Repository outline ────────────────────────────────────────
        print("  [2/8] Getting repo outline...")
        outline = await mcp.call("get_repo_outline", repo=repo_key)

        languages = outline.get("languages", {})
        file_count = outline.get("file_count", 0)
        symbol_count = outline.get("symbol_count", 0)

        # ── Step 3: Root file tree ────────────────────────────────────────────
        print("  [3/8] Getting root file tree...")
        file_tree_root = await mcp.call(
            "get_file_tree",
            repo=repo_key,
            path_prefix="",
        )

        top_dirs = _extract_top_dirs(file_tree_root, outline)

        # ── Step 4: Per-directory trees (top 12 dirs) ─────────────────────────
        print(f"  [4/8] Getting {min(len(top_dirs), 12)} directory trees...")
        dir_trees: dict = {}
        for dirname in top_dirs[:12]:
            tree = await mcp.call(
                "get_file_tree",
                repo=repo_key,
                path_prefix=dirname,
                include_summaries=True,
            )
            dir_trees[dirname] = tree

        # ── Step 5: All symbols sorted by centrality (max 100) ────────────────
        # Note: search_symbols clamps max_results to 100 internally
        print("  [5/8] Searching all symbols by centrality...")
        sym_result = await mcp.call(
            "search_symbols",
            repo=repo_key,
            query="",
            max_results=100,
            detail_level="standard",
        )
        all_symbols: list = sym_result.get("results", [])

        # ── Step 6: Classes + hierarchy for top 15 classes ───────────────────
        print("  [6/8] Getting class hierarchies...")
        cls_result = await mcp.call(
            "search_symbols",
            repo=repo_key,
            query="",
            kind="class",
            max_results=30,
        )
        classes: list = cls_result.get("results", [])

        class_hierarchies: dict = {}
        for cls in classes[:15]:
            cls_id = cls.get("id", "")
            if cls_id:
                hier = await mcp.call(
                    "get_class_hierarchy",
                    repo=repo_key,
                    class_id=cls_id,
                )
                if hier and not hier.get("error"):
                    class_hierarchies[cls_id] = hier

        # ── Step 7: Dependency graphs for top 5 entry files ──────────────────
        print("  [7/8] Getting dependency graphs...")
        entry_files = _find_entry_files(all_symbols, file_tree_root)

        dep_graphs: dict = {}
        for ef in entry_files[:5]:
            graph = await mcp.call(
                "get_dependency_graph",
                repo=repo_key,
                file=ef,
                direction="both",
                depth=2,
            )
            if graph and not graph.get("error"):
                dep_graphs[ef] = graph

        # ── Step 8: Context bundle for top 5 symbols ─────────────────────────
        print("  [8/8] Getting context bundle...")
        context_bundle = ""
        top_ids = [s["id"] for s in all_symbols[:5] if s.get("id")]
        if top_ids:
            bundle = await mcp.call(
                "get_context_bundle",
                repo=repo_key,
                symbol_ids=top_ids,
                output_format="markdown",
            )
            # Handle different response shapes
            if isinstance(bundle, dict):
                context_bundle = (
                    bundle.get("content")
                    or bundle.get("markdown")
                    or bundle.get("raw")
                    or json.dumps(bundle, indent=2)
                )
            elif isinstance(bundle, str):
                context_bundle = bundle

    return RepoData(
        repo_key=repo_key,
        git_head=git_head,
        is_github=not is_local,
        languages=languages,
        file_count=file_count,
        symbol_count=symbol_count,
        outline=outline,
        file_tree_root=file_tree_root,
        dir_trees=dir_trees,
        all_symbols=all_symbols,
        classes=classes,
        class_hierarchies=class_hierarchies,
        dep_graphs=dep_graphs,
        context_bundle=context_bundle,
        top_dirs=top_dirs[:12],
        entry_files=entry_files[:5],
    )


# ── Internal helpers ─────────────────────────────────────────────────────────

def _extract_top_dirs(file_tree_root: dict, outline: dict) -> list[str]:
    """Extract top-level directory names from get_file_tree or outline."""
    dirs: list[str] = []

    # Try from file_tree entries (files at root often imply dirs)
    entries = file_tree_root.get("entries", file_tree_root.get("items", []))
    for entry in entries:
        if isinstance(entry, dict):
            is_dir = (
                entry.get("type") == "directory"
                or entry.get("is_dir")
                or entry.get("kind") == "dir"
            )
            if is_dir:
                name = entry.get("path", entry.get("name", ""))
                if name and not name.startswith("."):
                    dirs.append(name.rstrip("/"))
        elif isinstance(entry, str) and "/" not in entry and not entry.startswith("."):
            dirs.append(entry)

    # Fallback: try outline.directories
    if not dirs:
        for d in outline.get("directories", []):
            if isinstance(d, dict):
                name = d.get("path", d.get("name", ""))
            elif isinstance(d, str):
                name = d
            else:
                continue
            if name and not name.startswith("."):
                dirs.append(name.rstrip("/"))

    # Fallback: infer from symbol file paths
    if not dirs:
        dir_set: set[str] = set()
        files_raw = file_tree_root.get("files", [])
        for entry in files_raw:
            path = entry.get("path", entry) if isinstance(entry, dict) else entry
            if "/" in path:
                top = path.split("/")[0]
                if not top.startswith("."):
                    dir_set.add(top)
        dirs = sorted(dir_set)

    return dirs[:12]


_ENTRY_BASENAMES = {
    "main.py", "__main__.py", "server.py", "app.py", "cli.py",
    "index.py", "run.py", "start.py", "manage.py", "wsgi.py", "asgi.py",
    "main.ts", "index.ts", "server.ts", "app.ts",
    "main.js", "index.js", "server.js", "app.js",
    "main.go", "main.rs", "lib.rs",
}


def _find_entry_files(symbols: list[dict], file_tree: dict) -> list[str]:
    """Identify likely entry point files from symbol file list."""
    # Collect unique files preserving centrality order
    seen: set[str] = set()
    all_files: list[str] = []
    for sym in symbols:
        f = sym.get("file", "")
        if f and f not in seen:
            seen.add(f)
            all_files.append(f)

    # Prioritize entry-point-named files
    entry: list[str] = []
    rest: list[str] = []
    for f in all_files:
        basename = f.split("/")[-1].lower()
        if basename in _ENTRY_BASENAMES:
            entry.append(f)
        else:
            rest.append(f)

    return (entry + rest)[:5]
