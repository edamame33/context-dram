#!/bin/bash
# Show the full content of the current (default: most recent) or named scratchpad.
# Usage: show.sh                  -> most recent
#        show.sh <session_id>     -> specific session id (without .md)
SCRATCH="${CLAUDE_SCRATCHPAD_DIR:-}"
if [ -z "$SCRATCH" ]; then
    if [ -d "A:/Claude Scratchpad" ]; then SCRATCH="A:/Claude Scratchpad"; else SCRATCH="$HOME/.claude/scratchpad"; fi
fi
if [ -n "${1:-}" ]; then
    target="$SCRATCH/$1.md"
    [ -f "$target" ] || target="$1"
else
    target=$(ls -t "$SCRATCH"/*.md 2>/dev/null | head -1)
fi
[ -f "$target" ] || { echo "no scratchpad found" >&2; exit 1; }
cat "$target"
