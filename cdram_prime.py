#!/usr/bin/env python3
"""context-dram SessionStart hook - inject the hot working set as additionalContext.

Reads the cell store, pages in the hottest cells for this project, and returns
them as a compact index (titles + types + files, no bodies). Fast, synchronous,
read-only. Best-effort: emits nothing on any failure; set
CLAUDE_SCRATCHPAD_DEBUG=1 to log swallowed failures to <scratchpad>/.errors.log.

Registered as:  py -3.13 <this file>   (SessionStart hook; MUST stay synchronous
- an async SessionStart hook's output is discarded and the context is lost)
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# page_in's token budget is charged against full cell bodies, so it can admit
# far more index lines than a session prelude should carry; this caps the
# emitted lines (pinned lines are always kept)
MAX_INDEX_LINES = 150

DEBUG = os.environ.get("CLAUDE_SCRATCHPAD_DEBUG") == "1"
HOOK_NAME = "cdram_prime"


def _err_dir() -> Path:
    env = os.environ.get("CLAUDE_SCRATCHPAD_DIR")
    if env:
        return Path(env)
    legacy = Path(r"A:\Claude Scratchpad")
    if legacy.is_dir():
        return legacy
    return Path.home() / ".claude" / "scratchpad"


def _err(phase: str, exc: BaseException, sid: str = "") -> None:
    """One line per swallowed failure; never raises, no-op unless DEBUG."""
    if not DEBUG:
        return
    try:
        d = _err_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = d / ".errors.log"
        try:
            if p.exists() and p.stat().st_size > 262144:
                tail = p.read_bytes()[-65536:]
                tmp = p.with_name(".errors.log.tmp")
                tmp.write_bytes(tail)
                os.replace(tmp, p)
        except OSError:
            pass
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{ts}Z {HOOK_NAME} {phase} s={sid} "
                    f"{type(exc).__name__}: {str(exc)[:200]}\n")
    except Exception:
        pass


def build_context(cwd, session_id: str = ""):
    """Return the additionalContext string, or None if there's nothing hot."""
    from memory import Memory
    from cdram_config import DB_PATH, project_for

    if not DB_PATH.exists():
        return None
    m = Memory(str(DB_PATH))
    try:
        # fresh session: the turn clock restarts at 0, so cross-session selection
        # is driven by wall-clock (epoch) decay, not stale turn numbers; passing
        # the new session id makes that explicit for the session-scoped decay
        cells = m.page_in(project_for(cwd), now_turn=0, session_id=session_id or None)
    finally:
        m.close()
    if not cells:
        return None

    pinned_lines, other_lines = [], []
    for c in cells:
        files = f"  ({', '.join(c.files[:3])})" if c.files else ""
        line = f"- [{c.type}] {c.title}{files}"
        (pinned_lines if c.pinned else other_lines).append(line)
    budget = max(0, MAX_INDEX_LINES - len(pinned_lines))
    lines = pinned_lines + other_lines[:budget]
    return "## context-dram - hot working set (recalled from prior sessions)\n" + "\n".join(lines)


def _read_stdin_json():
    """Tolerant stdin read - hooks may receive UTF-8, a BOM, or UTF-16."""
    try:
        raw = sys.stdin.buffer.read()
    except Exception:
        return {}
    if not raw:
        return {}
    for enc in ("utf-8-sig", "utf-16", "utf-8"):
        try:
            return json.loads(raw.decode(enc))
        except Exception:
            continue
    _err("stdin-parse", ValueError("undecodable hook payload"))
    return {}


def main() -> int:
    data = _read_stdin_json()
    cwd = data.get("cwd") or os.getcwd()
    session_id = data.get("session_id") or ""
    try:
        ctx = build_context(cwd, session_id)
    except Exception as e:
        _err("top", e, session_id)
        ctx = None
    if ctx:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": ctx}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
