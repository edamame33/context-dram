# Install

Four hooks, two skills, zero dependencies. Ten minutes.

## 0. Prerequisites

- Claude Code installed and run at least once (`~/.claude/` exists).
- Python 3.10+ on PATH. Windows: the `py` launcher. macOS/Linux: `python3`.
- Clone this repo somewhere permanent — the hooks run straight from the clone, so don't move it afterwards:

```bash
git clone https://github.com/edamame33/context-dram.git
```

## 1. Register the hooks

Open `~/.claude/settings.json` and merge the following into the `"hooks"` object. If you already have `Stop` or `SessionStart` entries, append these to the existing `"hooks"` arrays instead of replacing them.

**Windows** (adjust the clone path):

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "py -3.13 \"C:\\path\\to\\context-dram\\hooks\\scratch_capture.py\"", "timeout": 15 },
          { "type": "command", "command": "py -3.13 \"C:\\path\\to\\context-dram\\cdram_capture.py\"", "timeout": 15 }
        ]
      }
    ],
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "py -3.13 \"C:\\path\\to\\context-dram\\hooks\\scratch_prime.py\"", "timeout": 15 },
          { "type": "command", "command": "py -3.13 \"C:\\path\\to\\context-dram\\cdram_prime.py\"", "timeout": 15 }
        ]
      }
    ]
  }
}
```

**macOS / Linux:**

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "python3 \"/path/to/context-dram/hooks/scratch_capture.py\"", "timeout": 15 },
          { "type": "command", "command": "python3 \"/path/to/context-dram/cdram_capture.py\"", "timeout": 15 }
        ]
      }
    ],
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "python3 \"/path/to/context-dram/hooks/scratch_prime.py\"", "timeout": 15 },
          { "type": "command", "command": "python3 \"/path/to/context-dram/cdram_prime.py\"", "timeout": 15 }
        ]
      }
    ]
  }
}
```

What each one does: `scratch_capture` appends the finished turn to the session transcript file; `cdram_capture` distills the same turn into a memory cell (in a detached background worker, so the session never waits); `scratch_prime` re-injects the last transcript at session start and runs the decay sweep; `cdram_prime` injects the hottest cells for the current project.

Hooks take effect on the **next** session start.

## 2. Install the skills

Copy the two skill folders into your skills directory:

```bash
# macOS / Linux
cp -r skills/scratch skills/scratch-flush ~/.claude/skills/

# Windows (PowerShell)
Copy-Item -Recurse skills\scratch, skills\scratch-flush "$env:USERPROFILE\.claude\skills\"
```

This gives you `/scratch <query>`, `/scratch list`, `/scratch show`, and the user-only `/scratch-flush`.

## 3. Optional environment variables

| Variable | Default | Meaning |
|---|---|---|
| `CLAUDE_SCRATCHPAD_DIR` | `~/.claude/scratchpad` | where transcripts + the cell DB live |
| `CLAUDE_SCRATCHPAD_TTL_HOURS` | `shutdown` | decay mode: `shutdown` (reset only on full power-off — **Windows-only**, degrades to `session` elsewhere), `session` (never decay), or an integer (N-hour TTL) |
| `CDRAM_DB_DIR` | `<scratchpad>/memory` | cell DB location override |

On macOS/Linux, set the TTL explicitly — `session` or a number — since `shutdown` can't detect power-offs there.

## 4. Verify

Start a session, do two or three real turns, exit. Then:

```bash
# transcript captured?
ls ~/.claude/scratchpad/          # or your CLAUDE_SCRATCHPAD_DIR

# cells written?
python3 -c "import sqlite3, pathlib; db = pathlib.Path.home()/'.claude/scratchpad/memory/cells.sqlite3'; print(sqlite3.connect(db).execute('select count(*) from cells').fetchone())"
```

Start a second session in the same project directory. It should open with a `## Scratchpad recall` block (the last transcript) and, from the session after that, a `## context-dram - hot working set` block (the recalled cells). That's the whole system working.

## Rollback

Delete the four hook entries from `settings.json` and the two skill folders. The scratchpad directory is yours to keep or delete.
