"""SessionStart hook - prime context from scratchpad + run decay sweep.

Behavior by source:
  - resume:  reload this session's scratchpad
  - startup: find most recent scratchpad with matching cwd, reload it
  - clear:   delete this session's scratchpad (fresh start)
  - compact: silent (compaction handles its own context)

Decay: CLAUDE_SCRATCHPAD_TTL_HOURS controls eviction (default "shutdown").
  - "shutdown": reset only on a full power-off; survives sleep AND restart.
                Windows-only (reads the System event log via wevtutil); on
                macOS/Linux it behaves like "session" and says so once.
  - "session" : never decay; only an explicit /clear evicts.
  - <int>     : legacy - decay files older than N hours.

Set CLAUDE_SCRATCHPAD_DEBUG=1 to log swallowed failures to
<scratchpad>/.errors.log.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


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
# Decay mode (CLAUDE_SCRATCHPAD_TTL_HOURS):
#   "shutdown" (default) - reset only on a full power-off; survives sleep AND restart
#   "session"            - never decay; only an explicit /clear evicts
#   <int>                - legacy: decay files older than N hours
_TTL_RAW = os.environ.get("CLAUDE_SCRATCHPAD_TTL_HOURS", "shutdown").strip().lower()
if _TTL_RAW == "shutdown":
    DECAY_MODE, TTL_HOURS = "shutdown", None
elif _TTL_RAW == "session":
    DECAY_MODE, TTL_HOURS = "session", None
else:
    try:
        DECAY_MODE, TTL_HOURS = "hours", int(_TTL_RAW or "24")
    except ValueError:
        DECAY_MODE, TTL_HOURS = "hours", 24
MAX_PRIME_CHARS = 8000  # ~2000 tokens injected

DEBUG = os.environ.get("CLAUDE_SCRATCHPAD_DEBUG") == "1"
HOOK_NAME = "scratch_prime"


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


def _wipe_all():
    """Delete every scratchpad file (the RAM-layer reset)."""
    if not SCRATCHPAD_DIR.exists():
        return
    for f in list(SCRATCHPAD_DIR.glob("*.md")) + list(SCRATCHPAD_DIR.glob(".cursor_*")):
        try:
            f.unlink()
        except Exception as e:
            _err("wipe", e)


def ttl_sweep():
    """Legacy hours-based decay: delete files older than TTL_HOURS."""
    if TTL_HOURS is None or not SCRATCHPAD_DIR.exists():
        return
    cutoff = time.time() - TTL_HOURS * 3600
    for f in SCRATCHPAD_DIR.glob("*.md"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                cursor = SCRATCHPAD_DIR / f".cursor_{f.stem}"
                if cursor.exists():
                    cursor.unlink()
        except Exception as e:
            _err("wipe", e)


_EVENT_ID = re.compile(r"<EventID[^>]*>(\d+)</EventID>")
_RECORD_ID = re.compile(r"<EventRecordID>(\d+)</EventRecordID>")


def _last_poweroff_id():
    """RecordId of the most recent TRUE power-off in the Windows System log, else None.

    True power-off = Event 1074 whose message says 'power off' (user chose Shut down),
    or Event 6008 (dirty/unexpected shutdown - the box lost power). A *restart* logs
    1074 with 'restart' and is ignored on purpose, so restarts never reset the pad.

    Uses wevtutil (native, ~30ms) instead of a powershell.exe pipeline (~300ms+).
    XML output is required: the text format omits EventRecordID entirely.
    """
    try:
        r = subprocess.run(
            ["wevtutil", "qe", "System",
             "/q:*[System[(EventID=1074 or EventID=6008)]]",
             "/c:40", "/rd:true", "/f:xml"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        best = None
        for block in (r.stdout or "").split("</Event>"):
            em = _EVENT_ID.search(block)
            rm = _RECORD_ID.search(block)
            if not em or not rm:
                continue
            eid = int(em.group(1))
            if eid == 6008 or (eid == 1074 and "power off" in block.lower()):
                rid = int(rm.group(1))
                best = rid if best is None else max(best, rid)
        return best
    except Exception:
        return None


def _poweroff_reset_check():
    """Authoritative power-off comparison against the .poweroff_epoch marker.

    Rules (each one closes a verified data-destruction or dead-sweep path):
      - query failed (rid None): NO INFORMATION - keep the pad, do NOT touch
        the marker (writing 'none' used to poison it and wipe on recovery)
      - first run (no marker): baseline only, never wipe
      - rid > prev: a NEW power-off appeared - wipe once, advance the marker
      - rid == prev: same power session - keep the pad
      - rid < prev: the System log was cleared and RecordIds restarted -
        re-baseline downward WITHOUT wiping (otherwise the sweep goes dead
        until RecordIds catch back up)
    """
    rid = _last_poweroff_id()
    if rid is None:
        return
    marker = SCRATCHPAD_DIR / ".poweroff_epoch"
    try:
        prev = marker.read_text(encoding="utf-8").strip()
    except Exception:
        prev = ""
    if prev.isdigit():
        if rid > int(prev):
            _wipe_all()
        elif rid == int(prev):
            return                      # marker already current; skip the write
    try:
        marker.write_text(str(rid), encoding="utf-8")
    except Exception as e:
        _err("marker", e)


def shutdown_sweep():
    """Reset the pad once per full power-off; survive sleep and restart.

    On non-Windows platforms there is no System event log to consult, so
    'shutdown' honestly degrades to 'session' (never decay) and says so once.
    """
    if not SCRATCHPAD_DIR.exists():
        return
    if os.name != "nt":
        sentinel = SCRATCHPAD_DIR / ".platform_warned"
        if not sentinel.exists():
            try:
                sentinel.write_text("", encoding="utf-8")
            except Exception:
                pass
            print("(scratchpad: TTL mode 'shutdown' is Windows-only; "
                  "behaving as 'session'. Set CLAUDE_SCRATCHPAD_TTL_HOURS "
                  "to 'session' or an hour count to silence this.)")
        return
    _poweroff_reset_check()


def decay_sweep():
    """Dispatch to the configured decay strategy."""
    if DECAY_MODE == "shutdown":
        shutdown_sweep()
    elif DECAY_MODE == "hours":
        ttl_sweep()
    # "session": no decay


def emit(label, content):
    """Print scratchpad content as a system-reminder-style block."""
    snippet = content[-MAX_PRIME_CHARS:]
    print(f"## Scratchpad recall - {label}")
    print()
    print(snippet)
    print()
    print("---")
    print("(Auto-loaded session scratchpad. Use `/scratch <query>` for keyword search, "
          "`/scratch flush` to clear.)")


def main():
    payload = _read_stdin_json()
    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd", "")
    source = payload.get("source", "startup")

    decay_sweep()

    if source == "clear":
        if session_id:
            f = SCRATCHPAD_DIR / f"{session_id}.md"
            if f.exists():
                f.unlink()
            cursor = SCRATCHPAD_DIR / f".cursor_{session_id}"
            if cursor.exists():
                cursor.unlink()
        return

    if source == "compact":
        return

    if not SCRATCHPAD_DIR.exists():
        return

    if source == "resume" and session_id:
        own = SCRATCHPAD_DIR / f"{session_id}.md"
        if own.exists():
            try:
                emit("resumed session", own.read_text(encoding="utf-8"))
            except Exception as e:
                _err("prime-read", e, session_id)
            return

    candidates = sorted(
        SCRATCHPAD_DIR.glob("*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for f in candidates:
        try:
            head = f.read_text(encoding="utf-8")[:800]
            # line-anchored frontmatter match: a bare substring test wrongly
            # recalled C:\proj2's pad for a session in C:\proj
            if cwd and f"\ncwd: {cwd}\n" in head:
                age_h = (time.time() - f.stat().st_mtime) / 3600
                emit(f"most recent in this cwd, {age_h:.1f}h ago", f.read_text(encoding="utf-8"))
                return
        except Exception as e:
            _err("prime-read", e, session_id)
            continue


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _err("top", e)
    sys.exit(0)
