#!/bin/bash
# List session scratchpads (newest first) with their YAML headers.
SCRATCH="${CLAUDE_SCRATCHPAD_DIR:-}"
if [ -z "$SCRATCH" ]; then
    if [ -d "A:/Claude Scratchpad" ]; then SCRATCH="A:/Claude Scratchpad"; else SCRATCH="$HOME/.claude/scratchpad"; fi
fi
ls -lt "$SCRATCH"/*.md 2>/dev/null | head -20
echo
for f in $(ls -t "$SCRATCH"/*.md 2>/dev/null | head -5); do
    echo "=== $(basename "$f") ==="
    head -6 "$f"
    echo
done
