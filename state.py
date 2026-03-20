"""Load/save run state to ~/.notion-code-mirror/{repo_slug}.json."""

import json
from pathlib import Path
from typing import Optional

STATE_DIR = Path.home() / ".notion-code-mirror"


def _slug(repo_key: str) -> str:
    """Convert 'owner/repo' to 'owner__repo' for safe filenames."""
    return repo_key.replace("/", "__").replace("\\", "__").replace(":", "__")


def load_state(repo_key: str) -> Optional[dict]:
    """Load saved state for a repo. Returns None if not found."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{_slug(repo_key)}.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_state(repo_key: str, state: dict) -> None:
    """Persist state for a repo."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{_slug(repo_key)}.json"
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def clear_state(repo_key: str) -> None:
    """Delete saved state for a repo."""
    path = STATE_DIR / f"{_slug(repo_key)}.json"
    if path.exists():
        path.unlink()
