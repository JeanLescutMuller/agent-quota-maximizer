# agent-quota-maximizer

**Spend quota that would otherwise expire, on useful maintenance work, without ever blocking the user's interactive work.**

Claude and Codex subscriptions meter usage in 5-hour windows nested inside 7-day periods, and both are use-it-or-lose-it. Measured over the last month on this machine, Claude leaves about $139 of its week unspent and Codex about $98 — Codex wastes the larger share, Claude the larger amount. This project runs read-only maintenance tasks (bug hunts, doc-drift checks, test-coverage reviews) across the repos in `~/dev`, **as late as possible** in each period, so the leftovers get spent and the user never queues behind a bot.

Status: **design complete, implementation not started.** `release/` is empty. The next step is P0 (ingestion).

## Repository layout

| Path | Contents |
|---|---|
| `AGENTS.md` / `CLAUDE.md` | This file — the entry point. `CLAUDE.md` is a symlink to it |
| [`design/`](design/) | Every design document. No code |
| [`release/`](release/) | The implementation. Empty until P0 |

`release/` will hold what `install.sh` copies into `~/opt/agent-quota-maximizer/`: the `aqm/` package, `install.sh`, `uninstall.sh`, the LaunchAgent plist, `prompts/` and the `config.json` template (`design/07_pipeline/DESIGN.md` §10). Development happens here in `~/dev/agent-quota-maximizer`; `~/opt/` holds the scheduled runtime copy, its state and its logs (see `~/AGENTS.md`).

## How it works, in one pass

Six stages, run every 5 minutes by a single LaunchAgent. Each stage is a CLI verb, reads the previous stage's artifact and writes its own, so any one can be run and inspected alone.

```
ingest → predict → budget → start_windows → plan → execute
```

The guiding principle is **postponement**: at every tick, work out how much of the remaining weekly quota cannot survive until the reset, and spend only that, at the last safe moment. Quota spent late is quota no later window could have absorbed, so the only way the system can get in the user's way is the time it needs to burn the amount — which is why lead time and burn rate, not lowered ceilings, are the levers.

## Table of contents

### Start here

| File | What it holds |
|---|---|
| [`design/DESIGN_v2.md`](design/DESIGN_v2.md) | **The overview.** Objective, scope, shared vocabulary, stage map, implementation plan, cross-cutting open points |
| [`design/CONSIDERATIONS.md`](design/CONSIDERATIONS.md) | Every fact and constraint the design rests on — quota mechanics, measured usage, risks. **No design choices**; every choice elsewhere should be justifiable from here |
| [`design/CLAUDE_AND_CODEX.md`](design/CLAUDE_AND_CODEX.md) | **Both agents side by side.** Measured data, the mechanics that differ, and what each difference forces on the design |

### The pipeline, one folder per stage

| Stage | Command | Design | Also in the folder |
|---|---|---|---|
| 1. Ingestion | `aqm ingest` | [`design/01_ingestion/DESIGN.md`](design/01_ingestion/DESIGN.md) | — |
| 2. Prediction | `aqm predict` | [`design/02_prediction/DESIGN.md`](design/02_prediction/DESIGN.md) | [`PREVIOUS_IDEAS.md`](design/02_prediction/PREVIOUS_IDEAS.md) — eight forecasting approaches, none built |
| 3. Budgeting | `aqm budget` | [`design/03_budgeting/DESIGN.md`](design/03_budgeting/DESIGN.md) | [`PREVIOUS_IDEAS.md`](design/03_budgeting/PREVIOUS_IDEAS.md) — the policies considered; the one in use is Policy D |
| 4. Window starter | `aqm start_windows` | [`design/04_start_windows/DESIGN.md`](design/04_start_windows/DESIGN.md) | [`CONSIDERATIONS.md`](design/04_start_windows/CONSIDERATIONS.md) — why windows must be opened on purpose, with the cost arithmetic |
| 5. Planning | `aqm plan` | [`design/05_planning/DESIGN.md`](design/05_planning/DESIGN.md) | — |
| 6. Execution | `aqm execute` | [`design/06_execution/DESIGN.md`](design/06_execution/DESIGN.md) | — |
| — Plumbing | `aqm pipeline` | [`design/07_pipeline/DESIGN.md`](design/07_pipeline/DESIGN.md) | Interface, guards, scheduling, locks, state, config, observability, deployment, **the parameter table** |

### Archive

| File | Why it is kept |
|---|---|
| [`design/DESIGN_v1.md`](design/DESIGN_v1.md) | The superseded first design (a predictive planner). Referenced by both `PREVIOUS_IDEAS.md` files |

## Where to look for a given question

| Question | File |
|---|---|
| What does the whole thing do? | `design/DESIGN_v2.md` §1–§4 |
| What does a word mean (unit, chunk, room, ceiling)? | `design/DESIGN_v2.md` §3 |
| Why is it built this way rather than another? | `design/CONSIDERATIONS.md`, then the stage's own DESIGN.md |
| What is every CLI flag? | `design/07_pipeline/DESIGN.md` §1.1 |
| What is the value of `MIN_MARGIN` / `GUARD_PCT` / any parameter? | `design/07_pipeline/DESIGN.md` §11 |
| What runs when, and what stops it? | `design/07_pipeline/DESIGN.md` §2, §4 |
| How does Claude differ from Codex here? | `design/CLAUDE_AND_CODEX.md` §2 |
| Which source gives percent / tokens / USD, and where exactly? | `~/dev/agent-usage-tracker/USAGE_DATA_SOURCES.md` (what exists upstream) and `~/dev/agent-usage-tracker/USAGE_DATA_REFERENCE.md` (what agent-usage-tracker captures, and where) |
| What is still undecided? | The end of each stage's DESIGN.md, plus `design/DESIGN_v2.md` §7 |
| What should I build next? | `design/DESIGN_v2.md` §6 — phases P0…P6 |

## Conventions in these documents

- **Facts marked [verified]** were measured on this machine on 2026-09-23, not assumed. Treat anything unmarked as a design claim, and anything marked **[derived]** as arithmetic on measurements.
- **Each rule is stated once**, in the document that owns it; everywhere else refers to it as `<folder>/DESIGN.md §N`. When editing, fix the owning document rather than restating the rule.
- **`CONSIDERATIONS.md` holds no design choices.** New constraints and measurements go there; new decisions go in a stage's DESIGN.md.
- **Section numbers are load-bearing** — they are used as cross-references across files. Renumbering a section means sweeping every `§N` that points at it. In `CONSIDERATIONS.md` only, `§N.M` means *bullet M of section N*.
- **`plan`, `budget` and `execute` are reserved words** with exact meanings (stage, artifact, CLI verb). Budget decides *how much and by when*; plan decides *what runs where*. Do not use them loosely.
- Quota arithmetic is done in **meter percent**; units and dollars are for display only.

## Related projects on this machine

| Project | Relation |
|---|---|
| `agent-usage-tracker` | Owns all usage tracking (split out of `agent-statusline` on 2026-09-30) and writes the data files this project reads, under `~/opt/agent-usage-tracker/data/`. **`USAGE_DATA_SOURCES.md` there is the canonical description of what usage data exists upstream, in which unit, at which granularity; `USAGE_DATA_REFERENCE.md` is the canonical description of what it captures and where** — this project links to them rather than restating them. `adhoc_quotas_analysis/` holds the validated pricing and envelope logic, and is the authority for every measured figure |
| `agent-auto-resume` | Also opens quota windows, so it can race stage 4 |
| `agent-notify` | Could surface a stuck job; not wired in |
