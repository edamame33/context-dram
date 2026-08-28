#!/bin/bash
# Grep across all session scratchpads. Usage: grep.sh "query string"
set -u
SCRATCH="${CLAUDE_SCRATCHPAD_DIR:-}"
if [ -z "$SCRATCH" ]; then
    if [ -d "A:/Claude Scratchpad" ]; then SCRATCH="A:/Claude Scratchpad"; else SCRATCH="$HOME/.claude/scratchpad"; fi
fi
[ -z "${1:-}" ] && { echo "usage: grep.sh <query>" >&2; exit 2; }
grep -rin --color=never -B1 -A3 -- "$1" "$SCRATCH"/*.md 2>/dev/null | head -100
