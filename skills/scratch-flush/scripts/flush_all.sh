#!/bin/bash
# Delete EVERY session scratchpad. Requires --yes.
set -u
SCRATCH="${CLAUDE_SCRATCHPAD_DIR:-}"
if [ -z "$SCRATCH" ]; then
    if [ -d "A:/Claude Scratchpad" ]; then SCRATCH="A:/Claude Scratchpad"; else SCRATCH="$HOME/.claude/scratchpad"; fi
fi
n=$(ls "$SCRATCH"/*.md 2>/dev/null | wc -l)

if [ "${1:-}" != "--yes" ]; then
    echo "Would delete $n scratchpad file(s) in: $SCRATCH"
    echo "Re-run with --yes to confirm."
    exit 2
fi
rm -fv "$SCRATCH"/*.md "$SCRATCH"/.cursor_* 2>/dev/null
