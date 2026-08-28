---
name: scratch
description: RAM-layer session scratchpad — query, list, or show recent captures. Auto-captures every turn (user prompts, tool calls, assistant responses) to a TTL'd disk store. Sits between live context and long-term memory; never auto-promoted. Use when the user types `/scratch <query>`, `/scratch list`, `/scratch show`, or asks "what did we do earlier", "did I mention X this session", "show recent scratchpad". For destructive wipe operations, see [[scratch-flush]] (disable-model-invocation; user-only).
---

# scratch — session RAM-layer recall

A working-memory layer between live conversation context and whatever long-term store you keep (a knowledge graph, notes vault, or nothing at all).

**Auto-capture:** Stop hook writes every completed turn to `${CLAUDE_SCRATCHPAD_DIR:-~/.claude/scratchpad}/${session_id}.md` — user prompt, tool calls (name + key arg), assistant response (truncated). No LLM involvement, ~50ms file I/O.

**TTL:** `CLAUDE_SCRATCHPAD_TTL_HOURS` controls decay. Default `shutdown` — the pad survives sleep and restart, and resets only on a full power-off (Windows-only detection; on macOS/Linux it behaves like `session`). Set `session` to never decay, or an integer for N-hour eviction.

**Auto-prime:** SessionStart hook reloads the scratchpad on `resume`, or finds the most recent file matching `cwd` on `startup`. `clear` wipes the current session's file.

**No silent promotion.** The scratchpad never promotes itself to long-term storage. Moving something worth keeping into your permanent store is always a deliberate, user-initiated act.

## Commands

All read-only ops ship as shell scripts under `scripts/` — Claude calls the script instead of reconstructing the heredoc each invocation. Quoting bugs in `grep_scratch "query with 'apostrophes'"` are handled by the script's `"$@"`, not by Claude's prose-to-bash conversion.

### `/scratch <query>` — grep across all scratchpads

```bash
bash scripts/grep.sh "your query"
```

Report findings with file:line citations. If nothing matches, say so plainly.

### `/scratch list` — show all scratchpads with metadata + headers

```bash
bash scripts/list.sh
```

### `/scratch show [session_id]` — dump full content

```bash
bash scripts/show.sh              # most recent
bash scripts/show.sh <session_id> # specific (without .md)
```

### Destructive ops — see [[scratch-flush]]

`/scratch flush` and `/scratch flush all` moved to the [[scratch-flush]] skill (`disable-model-invocation: true`), so Claude cannot auto-fire a wipe on a misread message. The user types `/scratch-flush` or `/scratch-flush all` directly.

## Format of scratchpad files

```markdown
---
session_id: <uuid>
cwd: <working dir at session start>
started: 2026-05-11 14:23:01
---

## [14:23:05]
**User:** <prompt verbatim, truncated 600 chars>

**Tools:**
  - Read(~/.claude/settings.json)
  - Bash(ls ~/.claude/scratchpad/)

**Asst:** <assistant response, truncated 1000 chars>

## [14:23:42]
...
```

## When NOT to use this skill

- For long-term knowledge persistence → save deliberately to your permanent store
- For curated notes → your notes system of choice
- For querying knowledge across many old sessions → your long-term store's search, if you have one

## Relationship to other memory layers

| Layer | Scope | TTL | Auto-write | Auto-recall | Promotion |
|---|---|---|---|---|---|
| **Live context** | Single turn | until response | — | — | — |
| **Scratchpad (this)** | Session | power-off / configurable | Yes (Stop hook) | Yes (SessionStart) | Manual only |
| **Long-term store** (optional) | Forever | none | No (deliberate) | On query | n/a |
