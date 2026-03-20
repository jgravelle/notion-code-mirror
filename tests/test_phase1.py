"""Tests for phase1_gather.py helper functions (pure logic, no MCP calls)."""

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from phase1_gather import (
    RepoData,
    _extract_top_dirs,
    _find_entry_files,
    _ENTRY_BASENAMES,
)


# ── _extract_top_dirs ─────────────────────────────────────────────────────────

def test_extract_dirs_from_entries_type_directory():
    file_tree = {
        "entries": [
            {"path": "src", "type": "directory"},
            {"path": "tests", "type": "directory"},
            {"path": "README.md", "type": "file"},
        ]
    }
    dirs = _extract_top_dirs(file_tree, {})
    assert "src" in dirs
    assert "tests" in dirs
    assert "README.md" not in dirs


def test_extract_dirs_is_dir_flag():
    file_tree = {
        "entries": [
            {"path": "lib", "is_dir": True},
            {"path": "main.py", "is_dir": False},
        ]
    }
    dirs = _extract_top_dirs(file_tree, {})
    assert "lib" in dirs
    assert "main.py" not in dirs


def test_extract_dirs_kind_dir():
    file_tree = {
        "entries": [
            {"name": "pkg", "kind": "dir"},
            {"name": "go.mod", "kind": "file"},
        ]
    }
    dirs = _extract_top_dirs(file_tree, {})
    assert "pkg" in dirs


def test_extract_dirs_skips_hidden():
    file_tree = {
        "entries": [
            {"path": ".github", "type": "directory"},
            {"path": "src", "type": "directory"},
        ]
    }
    dirs = _extract_top_dirs(file_tree, {})
    assert ".github" not in dirs
    assert "src" in dirs


def test_extract_dirs_fallback_to_outline():
    file_tree = {}  # No entries
    outline = {
        "directories": [
            {"path": "src", "file_count": 10},
            {"path": "lib", "file_count": 5},
        ]
    }
    dirs = _extract_top_dirs(file_tree, outline)
    assert "src" in dirs
    assert "lib" in dirs


def test_extract_dirs_outline_string_list():
    file_tree = {}
    outline = {"directories": ["src", "tests", "docs"]}
    dirs = _extract_top_dirs(file_tree, outline)
    assert "src" in dirs
    assert "tests" in dirs


def test_extract_dirs_strips_trailing_slash():
    file_tree = {
        "entries": [{"path": "src/", "type": "directory"}]
    }
    dirs = _extract_top_dirs(file_tree, {})
    assert "src" in dirs
    assert "src/" not in dirs


def test_extract_dirs_max_12():
    entries = [{"path": f"dir{i}", "type": "directory"} for i in range(20)]
    dirs = _extract_top_dirs({"entries": entries}, {})
    assert len(dirs) <= 12


def test_extract_dirs_empty_inputs():
    dirs = _extract_top_dirs({}, {})
    assert dirs == []


# ── _find_entry_files ─────────────────────────────────────────────────────────

def test_find_entry_files_main_py():
    symbols = [
        {"id": "1", "name": "main", "file": "src/main.py"},
        {"id": "2", "name": "helper", "file": "src/utils.py"},
    ]
    entries = _find_entry_files(symbols, {})
    assert "src/main.py" in entries
    # main.py should come first (priority)
    assert entries[0] == "src/main.py"


def test_find_entry_files_app_py():
    symbols = [
        {"id": "1", "name": "run", "file": "app.py"},
        {"id": "2", "name": "helper", "file": "utils.py"},
    ]
    entries = _find_entry_files(symbols, {})
    assert entries[0] == "app.py"


def test_find_entry_files_no_entry_point():
    symbols = [
        {"id": "1", "name": "foo", "file": "foo.py"},
        {"id": "2", "name": "bar", "file": "bar.py"},
    ]
    entries = _find_entry_files(symbols, {})
    # No entry points found, return first files by centrality order
    assert entries[0] == "foo.py"
    assert len(entries) <= 5


def test_find_entry_files_deduplicates():
    # Same file appears in multiple symbols
    symbols = [
        {"id": "1", "name": "a", "file": "main.py"},
        {"id": "2", "name": "b", "file": "main.py"},
        {"id": "3", "name": "c", "file": "utils.py"},
    ]
    entries = _find_entry_files(symbols, {})
    assert entries.count("main.py") == 1


def test_find_entry_files_max_5():
    symbols = [{"id": str(i), "name": f"sym{i}", "file": f"file{i}.py"} for i in range(20)]
    entries = _find_entry_files(symbols, {})
    assert len(entries) <= 5


def test_find_entry_files_empty():
    entries = _find_entry_files([], {})
    assert entries == []


def test_find_entry_files_typescript():
    symbols = [
        {"id": "1", "name": "start", "file": "src/index.ts"},
        {"id": "2", "name": "helper", "file": "src/utils.ts"},
    ]
    entries = _find_entry_files(symbols, {})
    assert "src/index.ts" in entries
    assert entries[0] == "src/index.ts"


def test_entry_basenames_coverage():
    """Ensure key entry point names are in the set."""
    assert "main.py" in _ENTRY_BASENAMES
    assert "__main__.py" in _ENTRY_BASENAMES
    assert "server.py" in _ENTRY_BASENAMES
    assert "app.py" in _ENTRY_BASENAMES
    assert "index.ts" in _ENTRY_BASENAMES
    assert "main.go" in _ENTRY_BASENAMES


# ── RepoData dataclass ────────────────────────────────────────────────────────

def test_repo_data_instantiation():
    """Verify RepoData can be constructed with all fields."""
    rd = RepoData(
        repo_key="owner/repo",
        git_head="abc123",
        is_github=True,
        languages={"python": 90, "shell": 10},
        file_count=42,
        symbol_count=200,
        outline={},
        file_tree_root={},
        dir_trees={"src": {}},
        all_symbols=[{"id": "1", "name": "foo", "kind": "function", "file": "src/foo.py"}],
        classes=[],
        class_hierarchies={},
        dep_graphs={},
        context_bundle="## foo\nDoes stuff.",
        top_dirs=["src", "tests"],
        entry_files=["src/main.py"],
    )
    assert rd.repo_key == "owner/repo"
    assert rd.file_count == 42
    assert rd.languages["python"] == 90
    assert len(rd.all_symbols) == 1
