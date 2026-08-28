"""
prune_jsonl - lossless page-out for a Claude Code session.

The page-OUT wing of context-dram. Claude Code stores each session as a JSONL
(one message per line) under ~/.claude/projects/<slug>/. Several top-level
fields on those lines are written for the UI / rewind and are *never sent back
to the model* - the biggest being `toolUseResult`, which holds full Edit diffs
(oldString/newString/structuredPatch), ~6.5 KB per edit. Deleting it is
information-lossless to the model and reclaims 5-50% on edit-heavy sessions.

Safety model: this tool only strips *fields within* lines. It NEVER removes a
line, so there is no parentUuid chain to repair and no tool_use/tool_result
pair to dangle. That makes it the safe half of page-out; line-removing
strategies (summary-collapse, age-stub) are a separate, more careful tool.

- dry-run by default; --execute to write
- atomic os.replace, .bak backup first
- refuses to edit a session modified in the last 120s (racing a live session
  corrupts it)

stdlib only. Target: py -3.13.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
from pathlib import Path


# --------------------------------------------------------------------------- #
# Pure transform - no IO (trivially testable)
# --------------------------------------------------------------------------- #
def _dumps(obj) -> str:
    """Compact JSONL serialization - matches Claude Code's on-disk format
    (no spaces) so re-serializing never bloats unchanged lines."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def strip_obj(obj: dict, *, strip_tool_result: bool = True,
              strip_metadata: bool = False) -> dict:
    """Remove non-model-visible fields from one parsed session line (in place).

    `toolUseResult` is a top-level sibling of `message` and is never part of
    the model-visible `message.content` - safe to drop on any line.
    The metadata fields are API response accounting, not re-sent as input.
    """
    if strip_tool_result:
        obj.pop("toolUseResult", None)
    if strip_metadata:
        obj.pop("costUSD", None)
        obj.pop("durationMs", None)
        msg = obj.get("message")
        if isinstance(msg, dict):
            for k in ("usage", "stop_reason", "stop_sequence"):
                msg.pop(k, None)
    return obj


def process_lines(lines, *, strip_tool_result: bool = True,
                  strip_metadata: bool = False):
    """Transform every line. Returns (out_lines, bytes_saved, n_lines_changed).

    Line count is preserved exactly. Empty and unparseable lines pass through
    verbatim - we never guess at malformed data.
    """
    out, saved, changed = [], 0, 0
    for line in lines:
        s = line.rstrip("\n")
        if not s.strip():
            out.append(s)
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            out.append(s)
            continue
        before = len(s)
        strip_obj(obj, strip_tool_result=strip_tool_result, strip_metadata=strip_metadata)
        new = _dumps(obj)
        if len(new) < before:
            changed += 1
        saved += max(0, before - len(new))
        out.append(new)
    return out, saved, changed


# --------------------------------------------------------------------------- #
# Session resolution + IO
# --------------------------------------------------------------------------- #
def slugify_cwd(cwd) -> str:
    """Claude Code's project-dir slug: every non-alphanumeric char -> '-'.
    e.g. C:\\Users\\alice -> C--Users-alice"""
    return re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))


def find_session(project_dir: str | None = None, cwd: str | None = None) -> Path:
    base = Path.home() / ".claude" / "projects"
    if project_dir:
        d = Path(project_dir)
    else:
        d = base / slugify_cwd(cwd or Path.cwd())
    if not d.is_dir():
        opts = sorted(p.name for p in base.glob("*") if p.is_dir()) if base.is_dir() else []
        raise SystemExit(f"project dir not found: {d}\navailable:\n  " + "\n  ".join(opts))
    sessions = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not sessions:
        raise SystemExit(f"no .jsonl sessions in {d}")
    return sessions[0]


def looks_active(path: Path, window: float = 120.0) -> bool:
    return (time.time() - path.stat().st_mtime) < window


def write_atomic(path: Path, lines, backup: bool = True) -> None:
    if backup:
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)   # atomic on Windows


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="Lossless page-out: strip non-model-visible fields from a "
                    "Claude Code session JSONL.")
    ap.add_argument("session", nargs="?",
                    help="path to a .jsonl (default: newest session for the current cwd)")
    ap.add_argument("--project-dir", help="override the ~/.claude/projects/<slug> dir")
    ap.add_argument("--cwd", help="cwd to derive the project slug from")
    ap.add_argument("--metadata", action="store_true",
                    help="also strip usage/costUSD/durationMs/stop_reason (off by default)")
    ap.add_argument("--execute", action="store_true",
                    help="write the change (default: dry-run report only)")
    ap.add_argument("--no-backup", action="store_true", help="skip the .bak (with --execute)")
    ap.add_argument("--force", action="store_true",
                    help="allow editing a session modified <120s ago (RACE RISK)")
    args = ap.parse_args(argv)

    path = Path(args.session) if args.session else find_session(args.project_dir, args.cwd)
    if not path.is_file():
        raise SystemExit(f"not a file: {path}")

    lines = path.read_text(encoding="utf-8").splitlines()
    out, saved, changed = process_lines(lines, strip_metadata=args.metadata)
    size = path.stat().st_size

    print(f"session : {path}")
    print(f"          {len(lines)} lines, {size / 1024:.1f} KB")
    print(f"strip   : toolUseResult{' + metadata' if args.metadata else ''}")
    print(f"affected: {changed} lines")
    print(f"saved   : ~{saved / 1024:.1f} KB  (~{saved // 4:,} tokens)")

    if not args.execute:
        print("\n[dry run] nothing written. re-run with --execute, "
              "then /exit && claude --resume.")
        return

    if looks_active(path) and not args.force:
        raise SystemExit(
            "\nREFUSED: session modified <120s ago - looks live. Editing a running "
            "session's JSONL races Claude and can corrupt it.\nExit the session first, "
            "or pass --force only if you're sure it's idle.")

    write_atomic(path, out, backup=not args.no_backup)
    print(f"\nwritten. backup: {'(skipped)' if args.no_backup else path.name + '.bak'}")
    print("now: /exit, then  claude --resume  to load the slimmed session.")


if __name__ == "__main__":
    main()
