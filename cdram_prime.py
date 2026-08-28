#!/usr/bin/env python3
"""context-dram SessionStart hook - inject the hot working set as additionalContext.

Reads the cell store, pages in the hottest cells for this project, and returns
them as a compact index (titles + types + files, no bodies). Fast, synchronous,
read-only. Best-effort: emits nothing on any failure.

Registered as:  py -3.13 <this file>   (SessionStart hook)
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def build_context(cwd):
    """Return the additionalContext string, or None if there's nothing hot."""
    from memory import Memory
    from cdram_config import DB_PATH, project_for

    if not DB_PATH.exists():
        return None
    m = Memory(str(DB_PATH))
    try:
        # fresh session: the turn clock restarts at 0, so cross-session selection
        # is driven by wall-clock (epoch) decay, not stale turn numbers.
        cells = m.page_in(project_for(cwd), now_turn=0)
    finally:
        m.close()
    if not cells:
        return None

    lines = []
    for c in cells:
        files = f"  ({', '.join(c.files[:3])})" if c.files else ""
        lines.append(f"- [{c.type}] {c.title}{files}")
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
    return {}


def main() -> int:
    data = _read_stdin_json()
    cwd = data.get("cwd") or os.getcwd()
    try:
        ctx = build_context(cwd)
    except Exception:
        ctx = None
    if ctx:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": ctx}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
