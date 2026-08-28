---
name: scratch-flush
description: Destructively wipe session scratchpad files. Use only when the user explicitly types `/scratch-flush` or `/scratch-flush all`, or asks to "clear the scratchpad". Never auto-fires - user must invoke directly.
disable-model-invocation: true
---

# scratch-flush

Destructive companion to [[scratch]]. Split out as a separate skill so Claude cannot auto-fire a wipe based on a misread "clear my notes" message. Both scripts also refuse without `--yes` as a second safety layer.

## Commands

### `/scratch-flush` - delete current (most-recent) session scratchpad

```bash
bash scripts/flush.sh --yes
```

The script refuses without `--yes` and prints what it would delete, so a misfire still requires explicit re-run.

### `/scratch-flush all` - wipe every scratchpad file

```bash
bash scripts/flush_all.sh --yes
```

Same `--yes` safety. Confirm with the user before invoking — though `disable-model-invocation: true` already means they typed the slash command directly.

## Why a separate skill

Per the audit rule from Anthropic's Skills best-practices doc:
> Use `disable-model-invocation: true` for workflows with side effects or that you want to control timing, like `/commit`, `/deploy`, or `/send-slack-message`.

Wiping scratchpads is irreversible; same risk class.

The read-only counterpart [[scratch]] (`/scratch <query>`, `/scratch list`, `/scratch show`) remains auto-invocable for "what did we do earlier?"-style queries.
