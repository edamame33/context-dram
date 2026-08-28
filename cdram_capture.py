#!/usr/bin/env python3
"""context-dram Stop hook - capture the finished turn into the cell store.

Foreground call: read the hook JSON, spawn a DETACHED worker, exit instantly -
non-blocking, no console window (honours the 'no flashes' rule). The worker
distills the latest turn and writes a cell. Capture is best-effort: any failure
is swallowed so it can never break or delay the session; set
CLAUDE_SCRATCHPAD_DEBUG=1 to log swallowed failures to <scratchpad>/.errors.log.

The worker also owns store maintenance on a counter cadence: every ~20th
capture runs sweep + lossless archive-prune (cells exported to an fsync'd
JSONL sidecar before deletion), every ~50th runs VACUUM. Maintenance never
runs on the blocking hook path.

Set CDRAM_CAPTURE_ALL=1 to disable the noise gate and capture every turn.

Registered as:  py -3.13 <this file>      (Stop hook; mark it "async": true)
The worker re-invocation is:  py -3.13 <this file> --worker <transcript>
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# -- env-gated failure log (self-contained; must not depend on the DB dir) ----
DEBUG = os.environ.get("CLAUDE_SCRATCHPAD_DEBUG") == "1"
HOOK_NAME = "cdram_capture"


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


# -- noise gate: don't mint a cell for content-free acknowledgement turns -----
_FILLER_PREFIXES = ("sure", "done", "okay", "ok.", "ok,", "perfect", "let me",
                    "looking at", "great question", "sounds good", "will do",
                    "got it", "on it")


def _worth_capturing(d: dict, text: str) -> bool:
    """A turn earns a cell if it carries evidence (facts/files/offsets) or is
    substantive prose. Prefix-matched filler under ~400 chars is dropped."""
    if os.environ.get("CDRAM_CAPTURE_ALL") == "1":
        return True
    if d.get("facts") or d.get("files"):
        return True
    from distiller import extract_offsets
    if extract_offsets(text):
        return True
    if len(text) >= 400:
        return True
    low = (d.get("title") or "").strip().lower()
    return not any(low.startswith(p) for p in _FILLER_PREFIXES)


def capture(transcript_path: str):
    """The real work (runs in the detached worker). Returns the new cell id or None."""
    from memory import Memory
    from distiller import distill
    from cdram_config import DB_PATH, ensure_db_dir, project_for
    from prune_lines import _is_user_prompt

    rows = []
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        rows.append(json.loads(ln))
                    except json.JSONDecodeError:
                        pass
    except (OSError, ValueError) as e:
        _err("tx-open", e)
        return None
    if not rows:
        return None

    cwd = next((o.get("cwd") for o in reversed(rows) if o.get("cwd")), os.getcwd())
    session_id = next((o.get("sessionId") for o in reversed(rows) if o.get("sessionId")), "unknown")
    turn = sum(1 for o in rows if _is_user_prompt(o))
    last_prompt = max((i for i, o in enumerate(rows) if _is_user_prompt(o)), default=0)

    texts, files = [], []
    for o in rows[last_prompt:]:                       # the current turn slice
        if o.get("type") != "assistant":               # distill the WORK done, not the prompt
            continue
        content = o.get("message", {}).get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    texts.append(b.get("text", ""))
                elif b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    for k in ("file_path", "path", "notebook_path"):
                        if isinstance(inp.get(k), str):
                            files.append(inp[k])
    text = "\n".join(t for t in texts if t).strip()
    if not text:
        return None

    d = distill(text)
    for fp in files:
        if fp not in d["files"]:
            d["files"].append(fp)

    if not _worth_capturing(d, text):      # gate runs AFTER the tool-file merge
        return None

    ensure_db_dir()
    m = Memory(str(DB_PATH))
    try:
        try:
            cid = m.capture_turn(session_id=session_id, project=project_for(cwd),
                                 now_turn=turn, files_touched=d["files"], **d)
        except Exception as e:
            _err("db-write", e, session_id)
            return None
        try:
            n = m.bump_counter("maint")
            if n % 20 == 0:
                m.maintenance(now_turn=turn, session_id=session_id,
                              sidecar_path=str(DB_PATH.parent / "archived.jsonl"))
            if n % 50 == 0:
                m.vacuum()
        except Exception as e:
            _err("maintenance", e, session_id)
        return cid
    finally:
        m.close()


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


def _spawn_worker(transcript_path: str) -> None:
    flags = 0
    if os.name == "nt":
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
                 | subprocess.CREATE_NEW_PROCESS_GROUP)
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--worker", transcript_path],
                     creationflags=flags, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


def main() -> int:
    # detached worker branch
    if len(sys.argv) >= 3 and sys.argv[1] == "--worker":
        try:
            capture(sys.argv[2])
        except Exception as e:
            _err("top", e)                # never surface an error to the session
        return 0
    # foreground hook: spawn + return immediately
    data = _read_stdin_json()
    transcript = data.get("transcript_path") or ""
    if transcript:
        try:
            _spawn_worker(transcript)
        except Exception as e:
            _err("spawn", e, data.get("session_id") or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
