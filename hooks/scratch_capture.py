"""Stop hook — append the just-completed turn to a session scratchpad.

Captures: user prompt, tool calls made, assistant response (truncated).
Writes to: $CLAUDE_SCRATCHPAD_DIR (default ~/.claude/scratchpad/${session_id}.md).
Never crashes the session — all exceptions exit 0; set CLAUDE_SCRATCHPAD_DEBUG=1
to log swallowed failures to <scratchpad>/.errors.log.

Runs synchronously and inline: for the overwhelmingly common transcript size
(< ~3.5 MB) the parse+append is single-digit milliseconds, cheaper than paying
a second Python interpreter to do it detached. Only pathologically large
transcripts (16k+ turns) make the inline parse costly; if that ever bites, mark
this hook "async": true in settings.json so it leaves the blocking path
entirely, rather than spawning a duplicate interpreter per turn.
"""
import hashlib
import json
import os
import sys
import time
from pathlib import Path


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


SCRATCHPAD_DIR = _scratchpad_dir()
MAX_USER_CHARS = 600
MAX_ASST_CHARS = 1000
MAX_TOOL_ARG_CHARS = 240

DEBUG = os.environ.get("CLAUDE_SCRATCHPAD_DEBUG") == "1"
HOOK_NAME = "scratch_capture"


def _err(phase: str, exc: BaseException, sid: str = "") -> None:
    """One line per swallowed failure; never raises, no-op unless DEBUG."""
    if not DEBUG:
        return
    try:
        d = _scratchpad_dir()
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


def extract_text(msg):
    """Pull the text content out of a transcript message."""
    content = msg.get("message", {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            c.get("text", "") for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
    return ""


def extract_tool_calls(msg):
    """List tool_use blocks from an assistant message."""
    content = msg.get("message", {}).get("content")
    if not isinstance(content, list):
        return []
    return [c for c in content if isinstance(c, dict) and c.get("type") == "tool_use"]


def is_real_user_message(msg):
    """True only for messages that are an actual user prompt (not a tool_result
    wrapped as user, not an injected meta row, not a compaction summary)."""
    if (msg.get("type") or msg.get("role")) != "user":
        return False
    if msg.get("isMeta") or msg.get("isCompactSummary"):
        return False
    content = msg.get("message", {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        has_text = any(isinstance(c, dict) and c.get("type") == "text" for c in content)
        only_tool_results = all(
            isinstance(c, dict) and c.get("type") == "tool_result" for c in content
        )
        return has_text and not only_tool_results
    return False


def summarize_tool(tool):
    """One-line summary of a tool call: 'Name(key_arg)'."""
    name = tool.get("name", "?")
    inp = tool.get("input", {}) or {}
    key_args = {
        "Read": "file_path", "Edit": "file_path", "Write": "file_path",
        "Glob": "pattern", "Grep": "pattern",
        "Bash": "command", "PowerShell": "command",
        "WebFetch": "url", "WebSearch": "query",
        "Skill": "skill",
    }
    key = key_args.get(name)
    val = ""
    if key and key in inp:
        val = str(inp[key])
    elif inp:
        val = json.dumps(inp, default=str)
    val = val.replace("\n", " ")[:MAX_TOOL_ARG_CHARS]
    return f"{name}({val})" if val else name


def main():
    payload = _read_stdin_json()
    session_id = payload.get("session_id") or "unknown"
    transcript_path = payload.get("transcript_path")
    cwd = payload.get("cwd", "")

    if not transcript_path:
        return
    tpath = Path(transcript_path)
    if not tpath.exists():
        return

    msgs = []
    n_prompts = 0
    try:
        with open(tpath, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msgs.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        _err("tx-open", e, session_id)
        return

    last_user_idx = -1
    for i, m in enumerate(msgs):
        if is_real_user_message(m):
            last_user_idx = i
            n_prompts += 1

    last_user = msgs[last_user_idx] if last_user_idx >= 0 else None
    asst_msgs = [
        m for m in msgs[last_user_idx + 1:]
        if (m.get("type") or m.get("role")) == "assistant"
    ]
    if not asst_msgs:
        return
    last_asst = asst_msgs[-1]
    last_tools = []
    for am in asst_msgs:
        last_tools.extend(extract_tool_calls(am))

    asst_text_full = extract_text(last_asst)
    SCRATCHPAD_DIR.mkdir(parents=True, exist_ok=True)
    cursor = SCRATCHPAD_DIR / f".cursor_{session_id}"
    asst_uuid = last_asst.get("uuid") or last_asst.get("message", {}).get("id", "")
    if not asst_uuid:
        # a transcript without uuids must still dedup turn-by-turn; an empty
        # key would compare equal to every later empty key and silently drop
        # every turn after the first
        asst_uuid = "h:" + hashlib.sha256(
            f"{n_prompts}|{asst_text_full[:500]}".encode()).hexdigest()[:24]

    if cursor.exists():
        try:
            if cursor.read_text(encoding="utf-8").strip() == asst_uuid:
                return
        except Exception:
            pass

    user_text = extract_text(last_user).strip().replace("\n", " ")[:MAX_USER_CHARS] if last_user else ""
    asst_text = asst_text_full.strip().replace("\n", " ")[:MAX_ASST_CHARS]
    tool_lines = [f"  - {summarize_tool(t)}" for t in last_tools]

    scratch = SCRATCHPAD_DIR / f"{session_id}.md"
    is_new = not scratch.exists()
    try:
        with open(scratch, "a", encoding="utf-8") as f:
            if is_new:
                f.write(
                    f"---\nsession_id: {session_id}\ncwd: {cwd}\n"
                    f"started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n---\n\n"
                )
            ts = time.strftime("%H:%M:%S")
            f.write(f"## [{ts}]\n")
            if user_text:
                f.write(f"**User:** {user_text}\n\n")
            if tool_lines:
                f.write("**Tools:**\n" + "\n".join(tool_lines) + "\n\n")
            if asst_text:
                f.write(f"**Asst:** {asst_text}\n\n")
    except Exception as e:
        _err("md-append", e, session_id)
        return

    try:
        cursor.write_text(asst_uuid, encoding="utf-8")
    except Exception as e:
        _err("cursor-write", e, session_id)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _err("top", e)
    sys.exit(0)
