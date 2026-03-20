"""Tests for state.py — load/save/clear repo state."""

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import state as state_mod


def _patch_state_dir(tmp_path: Path):
    """Context manager that redirects STATE_DIR to a temp directory."""
    return mock.patch.object(state_mod, "STATE_DIR", tmp_path)


def test_slug_github():
    slug = state_mod._slug("owner/repo")
    assert slug == "owner__repo"
    assert "/" not in slug


def test_slug_local_path():
    slug = state_mod._slug("C:\\some\\path")
    assert "\\" not in slug


def test_slug_with_colon():
    slug = state_mod._slug("C:/my:repo")
    assert ":" not in slug


def test_save_and_load(tmp_path):
    with _patch_state_dir(tmp_path):
        payload = {"git_head": "abc123", "notion_root_page_id": "page-id-1"}
        state_mod.save_state("owner/repo", payload)
        loaded = state_mod.load_state("owner/repo")
        assert loaded == payload


def test_load_nonexistent(tmp_path):
    with _patch_state_dir(tmp_path):
        result = state_mod.load_state("nobody/nothing")
        assert result is None


def test_load_corrupt_json(tmp_path):
    with _patch_state_dir(tmp_path):
        path = tmp_path / "owner__repo.json"
        path.write_text("this is not json {{{")
        result = state_mod.load_state("owner/repo")
        assert result is None


def test_clear_state(tmp_path):
    with _patch_state_dir(tmp_path):
        state_mod.save_state("owner/repo", {"foo": "bar"})
        state_mod.clear_state("owner/repo")
        assert state_mod.load_state("owner/repo") is None


def test_clear_nonexistent_is_safe(tmp_path):
    with _patch_state_dir(tmp_path):
        # Should not raise
        state_mod.clear_state("nobody/nothing")


def test_save_overwrites(tmp_path):
    with _patch_state_dir(tmp_path):
        state_mod.save_state("owner/repo", {"version": 1})
        state_mod.save_state("owner/repo", {"version": 2})
        loaded = state_mod.load_state("owner/repo")
        assert loaded["version"] == 2


def test_multiple_repos_independent(tmp_path):
    with _patch_state_dir(tmp_path):
        state_mod.save_state("owner/repo-a", {"id": "a"})
        state_mod.save_state("owner/repo-b", {"id": "b"})
        assert state_mod.load_state("owner/repo-a")["id"] == "a"
        assert state_mod.load_state("owner/repo-b")["id"] == "b"


def test_state_dir_created_automatically(tmp_path):
    new_dir = tmp_path / "subdir" / "state"
    with mock.patch.object(state_mod, "STATE_DIR", new_dir):
        assert not new_dir.exists()
        state_mod.save_state("owner/repo", {"x": 1})
        assert new_dir.exists()


def test_full_state_schema(tmp_path):
    """Verify a full state dict round-trips correctly."""
    with _patch_state_dir(tmp_path):
        full_state = {
            "git_head": "deadbeef1234",
            "notion_root_page_id": "root-page-id",
            "overview_page_id": "overview-page-id",
            "architecture_page_id": "arch-page-id",
            "api_db_id": "db-id",
            "module_page_ids": {"src": "src-page-id", "tests": "tests-page-id"},
        }
        state_mod.save_state("jgravelle/jcodemunch-mcp", full_state)
        loaded = state_mod.load_state("jgravelle/jcodemunch-mcp")
        assert loaded == full_state
        assert loaded["module_page_ids"]["src"] == "src-page-id"
