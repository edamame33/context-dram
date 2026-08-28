"""
prune_lines - line-level page-out (the careful half).

Two strategies that go beyond field-stripping:

  * age-stub      : rewrite the *content* of tool results older than N turns
                    into a short stub (Claude can re-run the tool). Structurally
                    safe - it edits content, never removes a line, keeps every
                    tool_use_id, so no pairing can dangle.
  * compact-collapse : drop the message prefix that sits before the last
                    compact_boundary - those messages are already inside the
                    compaction summary. The big (85-95%) reclaim. This DOES
                    remove lines, so it runs behind a wall of guards.

Safety wall for collapse:
  - never touch a protected message (the summary, the boundary, snapshots...)
  - keep the last metadata singleton (permission-mode, title...) if absent after
  - re-thread parentUuid over the survivors so the chain never breaks
  - refuse if the kept region is branched (a /rewind fork)
  - validate the result (>=1 user + >=1 assistant, no broken chain, no dangling
    tool_use/tool_result pair); if validation fails, collapse is NOT applied

dry-run by default; --execute writes (atomic, .bak). Refuses a live session.
Reuses resolution/IO from prune_jsonl. stdlib only. Target: py -3.13.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from prune_jsonl import _dumps, find_session, looks_active, write_atomic


# --------------------------------------------------------------------------- #
# Protected set + classification (ported from the cozempic analysis)
# --------------------------------------------------------------------------- #
PROTECTED_TYPES = {"content-replacement", "marble-origami-commit",
                   "marble-origami-snapshot", "worktree-state", "task-summary"}
METADATA_SINGLETONS = ("last-prompt", "pr-link", "custom-title", "ai-title",
                       "attribution-snapshot", "permission-mode")


def is_protected(obj: dict) -> bool:
    t = obj.get("type")
    if t in PROTECTED_TYPES:
        return True
    if t == "user" and obj.get("isCompactSummary"):
        return True
    if t == "system" and obj.get("subtype") in ("compact_boundary", "microcompact_boundary"):
        return True
    if obj.get("isVisibleInTranscriptOnly"):
        return True
    return False


def _is_user_prompt(obj: dict) -> bool:
    """A real human turn (not a tool-result envelope, not meta)."""
    if obj.get("type") != "user" or obj.get("isMeta") or obj.get("isCompactSummary"):
        return False
    content = obj.get("message", {}).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "text" for b in content)
    return False


# --------------------------------------------------------------------------- #
# age-stub - structurally safe content rewrite
# --------------------------------------------------------------------------- #
def assign_ages(objs: list) -> list:
    """age = number of user-prompts at or after this message (0 = most recent block)."""
    ages = [0] * len(objs)
    counter = 0
    for i in range(len(objs) - 1, -1, -1):
        ages[i] = counter
        if objs[i] is not None and _is_user_prompt(objs[i]):
            counter += 1
    return ages


def stub_tool_results(obj: dict, age: int, stub_age: int, minify_age: int) -> int:
    """Rewrite old tool_result content. Returns chars saved. Keeps tool_use_id."""
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return 0
    content = msg.get("content")
    if not isinstance(content, list):
        return 0
    saved = 0
    for b in content:
        if not (isinstance(b, dict) and b.get("type") == "tool_result"):
            continue
        c = b.get("content")
        text = c if isinstance(c, str) else (_dumps(c) if c is not None else "")
        n = len(text)
        if age >= stub_age and n > 80:
            b["content"] = (f"[elided by context-dram: tool result ~{n} chars, "
                            f"age {age} turns - re-run the tool to restore]")
            saved += n - len(b["content"])
        elif age >= minify_age and n > 600:
            b["content"] = text[:400] + f"...[+{n - 400} chars truncated by context-dram, age {age}]"
            saved += n - len(b["content"])
    return saved


# --------------------------------------------------------------------------- #
# compact-collapse - structural prefix removal
# --------------------------------------------------------------------------- #
def _find_last_boundary(objs: list) -> int:
    idx = -1
    for i, o in enumerate(objs):
        if o and o.get("type") == "system" and \
                o.get("subtype") in ("compact_boundary", "microcompact_boundary"):
            idx = i
    return idx


def _has_preserved(obj: dict) -> bool:
    return bool(obj.get("hasPreservedSegment")
               or obj.get("message", {}).get("hasPreservedSegment"))


def _has_branching(objs: list) -> bool:
    """A fork: two messages claiming the same parent (a /rewind branch)."""
    seen = {}
    for o in objs:
        if not o:
            continue
        p = o.get("parentUuid")
        if p is None:
            continue
        seen[p] = seen.get(p, 0) + 1
    return any(v > 1 for v in seen.values())


def _rethread(objs: list) -> None:
    """Re-link parentUuid down the survivor sequence; first uuid-bearing -> root."""
    prev = None
    for o in objs:
        if o is not None and "uuid" in o:
            o["parentUuid"] = prev
            prev = o["uuid"]


def apply_collapse(objs: list):
    """Drop the non-protected prefix before the last compact_boundary. Returns
    (new_objs, report). Does not validate - the caller does and may reject."""
    bidx = _find_last_boundary(objs)
    if bidx < 0:
        return objs, {"applied": False, "reason": "no compact_boundary found"}
    if _has_preserved(objs[bidx]):
        return objs, {"applied": False, "reason": "boundary has a preserved segment"}

    pre, post = objs[:bidx], objs[bidx:]
    if _has_branching(post):
        return objs, {"applied": False, "reason": "branching (rewind fork) in kept region"}

    post_types = {o.get("type") for o in post}
    keep_singleton_types = {m for m in METADATA_SINGLETONS if m not in post_types}
    last_singleton_idx = {}
    for i, o in enumerate(pre):
        if o.get("type") in keep_singleton_types:
            last_singleton_idx[o.get("type")] = i
    keep_idx = set(last_singleton_idx.values())

    kept = [o for i, o in enumerate(pre) if is_protected(o) or i in keep_idx]
    survivors = kept + post
    _rethread(survivors)
    return survivors, {"applied": True, "dropped": len(objs) - len(survivors),
                       "boundary_index": bidx, "kept_from_prefix": len(kept)}


# --------------------------------------------------------------------------- #
# Validation - the abort gate
# --------------------------------------------------------------------------- #
def validate(objs: list):
    real = [o for o in objs if o]
    users = [o for o in real if o.get("type") == "user" and not o.get("isCompactSummary")]
    assts = [o for o in real if o.get("type") == "assistant"]
    if not users or not assts:
        return False, "would not leave >=1 user prompt and >=1 assistant message"

    uuids = {o["uuid"] for o in real if "uuid" in o}
    for o in real:
        p = o.get("parentUuid")
        if "uuid" in o and p is not None and p not in uuids:
            return False, "parentUuid chain break"

    use_ids, res_ids = set(), set()
    for o in real:
        content = o.get("message", {}).get("content")
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and "id" in b:
                    use_ids.add(b["id"])
                elif b.get("type") == "tool_result" and "tool_use_id" in b:
                    res_ids.add(b["tool_use_id"])
    if use_ids != res_ids:
        return False, (f"dangling tool pairs (tool_use without result: "
                       f"{len(use_ids - res_ids)}, result without use: {len(res_ids - use_ids)})")
    return True, "ok"


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def transform(objs: list, *, stub_age: int = 40, minify_age: int = 15,
              do_collapse: bool = True) -> tuple:
    """Apply age-stub (always) then collapse (gated + validated). Returns
    (out_objs, report). out_objs is structurally valid by construction."""
    report = {"age_saved_chars": 0, "collapse": {"applied": False, "reason": "disabled"}}

    ages = assign_ages(objs)
    for o, age in zip(objs, ages):
        if o is not None:
            report["age_saved_chars"] += stub_tool_results(o, age, stub_age, minify_age)

    out = objs
    if do_collapse:
        if any(o is None for o in objs):
            report["collapse"] = {"applied": False, "reason": "file has unparseable lines"}
        else:
            cand, rpt = apply_collapse([dict(o) for o in objs])   # copies; rethread is non-destructive
            if rpt.get("applied"):
                ok, why = validate(cand)
                if ok:
                    out, report["collapse"] = cand, rpt
                else:
                    report["collapse"] = {"applied": False, "reason": f"validation failed: {why}"}
            else:
                report["collapse"] = rpt

    if all(o is not None for o in out):
        report["valid"], report["valid_reason"] = validate(out)
    else:
        report["valid"], report["valid_reason"] = True, "skipped (raw lines present)"
    return out, report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="Line-level page-out: age-stub old tool results + collapse "
                    "the pre-compaction prefix.")
    ap.add_argument("session", nargs="?",
                    help="path to a .jsonl (default: newest session for the current cwd)")
    ap.add_argument("--project-dir")
    ap.add_argument("--cwd")
    ap.add_argument("--stub-age", type=int, default=40, help="stub tool results older than N turns")
    ap.add_argument("--minify-age", type=int, default=15, help="truncate results older than N turns")
    ap.add_argument("--no-collapse", action="store_true", help="age-stub only, skip compact-collapse")
    ap.add_argument("--execute", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--force", action="store_true", help="allow a session modified <120s ago (RACE RISK)")
    args = ap.parse_args(argv)

    path = Path(args.session) if args.session else find_session(args.project_dir, args.cwd)
    if not path.is_file():
        raise SystemExit(f"not a file: {path}")

    import json
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    objs = []
    for s in raw_lines:
        s = s.rstrip("\n")
        if not s.strip():
            objs.append(None)
            continue
        try:
            objs.append(json.loads(s))
        except json.JSONDecodeError:
            objs.append(None)

    before_bytes = path.stat().st_size
    out, report = transform(objs, stub_age=args.stub_age, minify_age=args.minify_age,
                             do_collapse=not args.no_collapse)
    out_text = "\n".join(_dumps(o) if o is not None else "" for o in out) + "\n"
    after_bytes = len(out_text.encode("utf-8"))
    saved = before_bytes - after_bytes

    col = report["collapse"]
    print(f"session   : {path}")
    print(f"            {len(raw_lines)} lines in, {len(out)} lines out, {before_bytes / 1024:.1f} KB")
    print(f"age-stub  : ~{report['age_saved_chars'] / 1024:.1f} KB rewritten "
          f"(stub>={args.stub_age} turns, minify>={args.minify_age})")
    if col.get("applied"):
        print(f"collapse  : applied - dropped {col['dropped']} pre-compaction lines")
    else:
        print(f"collapse  : skipped - {col['reason']}")
    print(f"validation: {'OK' if report['valid'] else 'FAILED: ' + report['valid_reason']}")
    print(f"total     : ~{saved / 1024:.1f} KB  (~{max(0, saved) // 4:,} tokens)")

    if not args.execute:
        print("\n[dry run] nothing written. re-run with --execute, then /exit && claude --resume.")
        return
    if not report["valid"]:
        raise SystemExit("\nREFUSED: result failed validation - not writing.")
    if looks_active(path) and not args.force:
        raise SystemExit("\nREFUSED: session modified <120s ago - looks live. "
                         "Exit it first, or pass --force if you're sure it's idle.")

    out_lines = [_dumps(o) if o is not None else "" for o in out]
    write_atomic(path, out_lines, backup=not args.no_backup)
    print(f"\nwritten. backup: {'(skipped)' if args.no_backup else path.name + '.bak'}")
    print("now: /exit, then  claude --resume.")


if __name__ == "__main__":
    main()
