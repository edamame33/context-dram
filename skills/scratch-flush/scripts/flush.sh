#!/bin/bash
# Delete the most-recent session scratchpad. Requires --yes.
set -u
SCRATCH="${CLAUDE_SCRATCHPAD_DIR:-}"
if [ -z "$SCRATCH" ]; then
    if [ -d "A:/Claude Scratchpad" ]; then SCRATCH="A:/Claude Scratchpad"; else SCRATCH="$HOME/.claude/scratchpad"; fi
fi
target=$(ls -t "$SCRATCH"/*.md 2>/dev/null | head -1)
[ -f "$target" ] || { echo "no scratchpad to flush" >&2; exit 1; }

if [ "${1:-}" != "--yes" ]; then
    echo "Would delete: $target"
    echo "Re-run with --yes to confirm."
    exit 2
fi
rm -v "$target"
