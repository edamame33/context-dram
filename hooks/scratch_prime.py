"""SessionStart hook - prime context from scratchpad + run decay sweep.

Behavior by source:
  - resume:  reload this session's scratchpad
  - startup: find most recent scratchpad with matching cwd, reload it
  - clear:   delete this session's scratchpad (fresh start)
  - compact: silent (compaction handles its own context)

Decay: CLAUDE_SCRATCHPAD_TTL_HOURS controls eviction (default "shutdown").
  - "shutdown": reset only on a full power-off; survives sleep AND restart.
                Windows-only (reads the System event log); on macOS/Linux it
                behaves like "session" - set "session" or an integer there.
  - "session" : never decay; only an explicit /clear evicts.
  - <int>     : legacy - decay files older than N hours.
"""
import ctypes
import json
import os
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


def _wipe_all():
    """Delete every scratchpad file (the RAM-layer reset)."""
    if not SCRATCHPAD_DIR.exists():
        return
    for f in list(SCRATCHPAD_DIR.glob("*.md")) + list(SCRATCHPAD_DIR.glob(".cursor_*")):
        try:
            f.unlink()
        except Exception:
            pass


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
        except Exception:
            pass


def _last_poweroff_id():
    """RecordId of the most recent TRUE power-off in the Windows System log, else None.

    True power-off = Event 1074 whose message says 'power off' (user chose Shut down),
    or Event 6008 (dirty/unexpected shutdown - the box lost power). A *restart* logs
    1074 with 'restart' and is ignored on purpose, so restarts never reset the pad.
    """
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$e = Get-WinEvent -FilterHashtable @{LogName='System';Id=1074,6008} -MaxEvents 40;"
        "$p = $e | Where-Object { $_.Id -eq 6008 -or ($_.Id -eq 1074 -and $_.Message -match 'power off') } |"
        " Sort-Object RecordId -Descending | Select-Object -First 1;"
        "if ($p) { $p.RecordId }"
    )
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (r.stdout or "").strip()
        return int(out) if out.isdigit() else None
    except Exception:
        return None


def _uptime_ms():
    """Milliseconds since boot (resets to ~0 on every reboot). None off-Windows."""
    try:
        k = ctypes.windll.kernel32
        k.GetTickCount64.restype = ctypes.c_uint64
        return int(k.GetTickCount64())
    except Exception:
        return None


def _poweroff_reset_check():
    """Authoritative power-off comparison. Wipes the pad once if a NEW full power-off
    has appeared since the .poweroff_epoch marker. First run only baselines."""
    rid = _last_poweroff_id()
    cur = str(rid) if rid is not None else "none"
    marker = SCRATCHPAD_DIR / ".poweroff_epoch"
    try:
        prev = marker.read_text(encoding="utf-8").strip()
    except Exception:
        prev = ""
    if prev == cur:
        return  # nothing changed since last session -> keep the pad
    if prev != "" and rid is not None:
        _wipe_all()  # a NEW full power-off appeared since last session -> reset
    try:
        marker.write_text(cur, encoding="utf-8")
    except Exception:
        pass


def shutdown_sweep():
    """Reset the pad once per full power-off; survive sleep and restart.

    Fast path: within a single boot a power-off is physically impossible, so if
    uptime shows we are still in the same boot as the last check, skip the
    (subprocess) event-log query entirely. Only a reboot runs the authoritative
    check, which ignores restarts and resets only on a true power-off.
    """
    if not SCRATCHPAD_DIR.exists():
        return
    up = _uptime_ms()
    if up is not None:
        umark = SCRATCHPAD_DIR / ".uptime"
        try:
            prev_up = int(umark.read_text(encoding="utf-8").strip())
        except Exception:
            prev_up = None
        try:
            umark.write_text(str(up), encoding="utf-8")
        except Exception:
            pass
        if prev_up is not None and up >= prev_up:
            return  # same power session -> no power-off possible -> keep pad (fast)
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
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return

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
            except Exception:
                pass
            return

    candidates = sorted(
        SCRATCHPAD_DIR.glob("*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for f in candidates:
        try:
            head = f.read_text(encoding="utf-8")[:800]
            if cwd and cwd in head:
                age_h = (time.time() - f.stat().st_mtime) / 3600
                emit(f"most recent in this cwd, {age_h:.1f}h ago", f.read_text(encoding="utf-8"))
                return
        except Exception:
            continue


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
