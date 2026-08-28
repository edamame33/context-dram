# context-dram

Session recovery and RAM-modeled working memory for Claude Code. Two hooks flight-record everything a session does; two more distill each turn into charge-ranked memory *cells*; every new session boots preloaded with the tail of the last transcript plus whatever cells are still hot. Crash recovery and "opening the laptop tomorrow" become the same operation — there is no restore procedure, booting *is* restoring.

Stdlib Python only. No dependencies, no daemons, no API calls.

## Why

Claude Code forgets everything when a session ends, and `/compact` is a blind, lossy, in-band rewrite triggered by a token threshold. The fix is a memory hierarchy, the same shape a computer uses:

| Tier | Analogy | What lives there | Lifetime |
|---|---|---|---|
| Live context window | row buffer / L1 | the current conversation | until close or compaction |
| Scratchpad (`hooks/`) | flight recorder | full transcript of every turn, per session | power-off (configurable) |
| Cell store (repo root) | the DRAM array | one distilled cell per turn, charge-ranked | fades over days unless re-touched |
| Your long-term store | disk / archive | whatever you deliberately promote | forever |

Nothing promotes itself to the long-term tier. That stays a human decision, because automatic summarizers lose the wrong things.

## The two layers

**Scratchpad — the flight recorder.** A Stop hook appends every completed turn (user prompt, tool calls, truncated response) to a per-session markdown file. A SessionStart hook injects the tail of the most recent file for the current project directory, so a new session opens already knowing where the last one left off. Decay is configurable: survive until full power-off (default, Windows), never decay, or N-hour TTL.

**context-dram — the DRAM array.** A second Stop hook distills each finished turn into a cell (title, type, file paths) stored in SQLite with a charge value between 0 and 1. Charge leaks exponentially (half-life: 7 turns, plus wall-clock aging between sessions) and is restored to full whenever the cell's topic is referenced again — so the store continuously sorts itself into a hot working set versus cooling history. A second SessionStart hook injects the hottest cells for the current project as a compact index.

## The model

| RAM | here | effect |
|---|---|---|
| capacitor voltage | `charge` (0-1) | how relevant a cell is *right now* |
| leakage | exponential decay per turn | untouched cells cool off on their own |
| refresh | `refresh()` on reference | what you keep using stays hot (a working set) |
| row buffer | `page_in()` (token-capped) | the live window holds one hot topic-cluster |
| destructive read + write-back | lossless eviction | drop from window only after it's saved |
| RAS/CAS address | stable `id` + FTS5 | O(1) random access; recall by id/query |

Decay is **lazy** — charge is stored with the turn it was set and computed on read, so nothing walks every cell every turn (that blind sweep is exactly the dumb DRAM refresh this design avoids). The knob you set is the **half-life in turns**; lambda is derived from it.

## Layout

```
hooks/
  scratch_capture.py   Stop hook: append the turn to the session scratchpad
  scratch_prime.py     SessionStart hook: re-inject the last transcript + decay sweep
skills/
  scratch/             /scratch <query> | list | show   (read-only recall)
  scratch-flush/       /scratch-flush [all]             (wipe; user-invoked only)
cdram_capture.py       Stop hook: distill the turn into a cell (detached worker)
cdram_prime.py         SessionStart hook: inject the hot working set
cdram_config.py        shared paths (CLAUDE_SCRATCHPAD_DIR / CDRAM_DB_DIR)
memory.py              the charge engine + controller (schema, effective_charge,
                       refresh, page_in, recall, fetch, sweep). 18 tests.
distiller.py           raw turn -> cell (heuristic: paths, offsets, type, title;
                       pluggable model_fn slot for LLM distillation). 10 tests.
prune_jsonl.py         lossless page-out: strip toolUseResult from a session JSONL.
                       dry-run default, atomic, .bak. 9 tests.
prune_lines.py         line-level page-out: age-stub old tool results + compact-collapse
                       behind a validation wall. 14 tests.
test_*.py              the suites - stdlib unittest.
```

## Install

See [INSTALL.md](INSTALL.md). Short version: register four hooks in `~/.claude/settings.json` (two Stop, two SessionStart), copy the two skills into `~/.claude/skills/`, done.

## Where data lives

Everything stays local. `CLAUDE_SCRATCHPAD_DIR` sets the scratchpad root (default `~/.claude/scratchpad`); the cell DB sits next to it at `<scratchpad>/memory/cells.sqlite3` (override: `CDRAM_DB_DIR`). This repo contains tooling only — captured transcripts and cells are never written inside it, and `.gitignore` excludes them belt-and-braces.

## Config knobs (memory.Config)

| knob | default | meaning |
|---|---|---|
| `half_life_turns` | 7 | turns-untouched until a cell is half-forgotten |
| `session_gap_decay` | 0.70 | per-day aging across sessions |
| `neighbor_boost` | 0.30 | spatial-locality bump to row neighbors on refresh |
| `hot/warm/cold_threshold` | 0.60 / 0.20 / 0.05 | charge -> tier boundaries |
| `hot_token_budget` | 40000 | row-buffer width (max hot tokens injected) |
| `row_join_threshold` | 0.30 | Jaccard overlap to join an existing topic-row |
| `strengthening` | off | frequently-used cells leak slower |

## Platform notes

- Works on Windows, macOS, and Linux. The detached capture worker and console-window suppression are guarded per-platform.
- The `shutdown` TTL mode (reset only on true power-off, survive sleep and restart) reads the Windows System event log, so it is **Windows-only**. On macOS/Linux it behaves like `session` (never decay) — set `CLAUDE_SCRATCHPAD_TTL_HOURS=session` explicitly, or an integer for N-hour decay.
- Python 3.10+ with an FTS5-enabled sqlite3 (true for python.org builds and modern distros).

## Manual page-out CLI

Page-out only acts at a resume boundary, so it stays manual by design:

```
python3 prune_jsonl.py    # lossless field-strip of a session JSONL. Dry-run default;
                          # --execute, then /exit && claude --resume
python3 prune_lines.py    # age-stub + compact-collapse. Dry-run default, .bak backup,
                          # refuses a live session, aborts on a broken parentUuid chain
```

## Run the tests

```
python3 -m unittest discover -v      # or: py -3.13 -m unittest discover -v
python3 memory.py                    # prints the discharge-curve demo
```

## Open ends

- `/recall` skill — `memory.recall` / `fetch` are built and tested but not yet packaged as an on-demand mid-session pull.
- Cross-session turn-rebasing — loaded cells keep their originating session's turn index; epoch decay covers cross-session aging.

## Lineage

The page-out strategies (toolUseResult strip, compact-summary-collapse, age-stub, protected-message validation) lift ideas from **cozempic**; the cell/observation shape and tiered "IDs are the currency" retrieval follow **claude-mem**. Neither is installed — both fight this hook setup, so only the ideas were taken.
