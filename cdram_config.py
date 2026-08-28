"""Shared config for the context-dram hooks.

DB lives next to the raw scratchpad by default; override with CDRAM_DB_DIR
(the tests point it at a temp dir). Project key = the cwd slug, so capture and
prime agree on which 'bank' a session belongs to.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from prune_jsonl import slugify_cwd   # noqa: E402  (path set above)

def _scratchpad_dir() -> Path:
    """Resolve the scratchpad root: env var, then the legacy A: location if it
    exists (original install), then a portable per-user default."""
    env = os.environ.get("CLAUDE_SCRATCHPAD_DIR")
    if env:
        return Path(env)
    legacy = Path(r"A:\Claude Scratchpad")
    if legacy.is_dir():
        return legacy
    return Path.home() / ".claude" / "scratchpad"


_env_db = os.environ.get("CDRAM_DB_DIR")
DB_DIR = Path(_env_db) if _env_db else _scratchpad_dir() / "memory"
DB_PATH = DB_DIR / "cells.sqlite3"


def ensure_db_dir() -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)


def project_for(cwd) -> str:
    return slugify_cwd(cwd)
