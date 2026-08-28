#!/usr/bin/env python3
"""context-dram Stop hook - capture the finished turn into the cell store.

Foreground call: read the hook JSON, spawn a DETACHED worker, exit instantly -
non-blocking, no console window (honours the 'no flashes' rule). The worker
distills the latest turn and writes a cell. Capture is best-effort: any failure
is swallowed so it can never break or delay the session.

Registered as:  py -3.13 <this file>      (Stop hook)
The worker re-invocation is:  py -3.13 <this file> --worker <transcript>
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def capture(transcript_path: str):
    """The real work (runs in the detached worker). Returns the new cell id or None."""
    from memory import Memory
    from distiller import distill
    from cdram_config import DB_PATH, ensure_db_dir, project_for
    from prune_lines import _is_user_prompt

    rows = []
    try:
        with open(transcript_path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        rows.append(json.loads(ln))
                    except json.JSONDecodeError:
                        pass
    except OSError:
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

    ensure_db_dir()
    m = Memory(str(DB_PATH))
    try:
        cid = m.write(session_id=session_id, project=project_for(cwd), now_turn=turn, **d)
        if d["files"]:
            m.touch_by_file(d["files"], now_turn=turn)   # refresh cells sharing those files
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
        except Exception:
            pass                      # never surface an error to the session
        return 0
    # foreground hook: spawn + return immediately
    data = _read_stdin_json()
    transcript = data.get("transcript_path") or ""
    if transcript:
        try:
            _spawn_worker(transcript)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
