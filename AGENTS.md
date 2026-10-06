# agent-quota-maximizer

**Spend quota that would otherwise expire, on useful maintenance work, without ever blocking the user's interactive work.**

Claude and Codex subscriptions meter usage in 5-hour windows nested inside 7-day periods, and both are use-it-or-lose-it. Measured over the last month on this machine, Claude leaves about $139 of its week unspent and Codex about $98 — Codex wastes the larger share, Claude the larger amount. This project runs read-only maintenance tasks (bug hunts, doc-drift checks, test-coverage reviews) across the repos in `~/dev`, **as late as possible** in each period, so the leftovers get spent and the user never queues behind a bot.

Status: **P0–P2 built and verified: ingestion, prediction, budgeting and the whole pipeline skeleton, including the LaunchAgent and `install.sh`. Stages 4–6 (the two that act, plus planning) are design only, so a tick cannot yet spend anything.** `release/aqm/` is the whole implementation, one module per stage (`design/07_pipeline/DESIGN.md` §12). A forecasting study on 42 days of real history (`design/02_prediction/DESIGN.md` §8) compared five engines and concluded that this one is already at its measured optimum; it moved `MIN_HUMAN_RESERVE_PCT` from 10% to 25% instead, which also settled the `GUARD_PCT` conflict. The next step is P3 (window starter) or P4 (execution) — see `design/DESIGN_v2.md` §6.

## Repository layout

| Path | Contents |
|---|---|
| `AGENTS.md` / `CLAUDE.md` | This file — the entry point. `CLAUDE.md` is a symlink to it |
| [`design/`](design/) | Every design document, plus `PIPELINE_MAP.html`, the visual summary. No code, with one exception: [`02_prediction/`](design/02_prediction/lab/README.md) holds the forecasting bench — one notebook per candidate forecaster and the shared dataset they are all measured on, which belong beside the design question they investigate |
| [`release/`](release/) | The implementation and what gets deployed: the `aqm/` package (one module per stage), the `aqm-cli` launcher, `install.sh`, `uninstall.sh`, the LaunchAgent plist, the `config.json` template |
| [`test/`](test/) | Every test. Bash only, hermetic, no LLM is ever called ([`README`](test/README.md)) |
| [`notebook/`](notebook/) | Ad-hoc analysis, run by hand. **`explain_budget.ipynb` takes one decision apart**, measurement by measurement ([`README`](notebook/README.md)) |

Four commands are runnable now:

| Command | What it does |
|---|---|
| **`aqm pipeline`** | **The scheduled job.** Every built stage in one process: ~7 ms of work, ~57 ms wall clock. Writes both artifacts, a metrics line per agent, and refuses on a bad config, a stale reading or a held lock |
| `aqm ingest` | New meter readings into `data/<agent>/`. `--backfill` rebuilds the whole table from the recorded history in about two seconds |
| `aqm predict` | How much human demand is still coming before the window ends, as a p95 in percent |
| `aqm budget` | How much may be spent and by when, per agent |

`predict` and `budget` take `--at <iso>` to decide as of a past moment, which writes nothing unless `--out` is given; every command takes `--json`. Exit codes: `0` ok, `1` error, `2` refused by a guard, `3` locked.

**Deployment.** `bash release/install.sh` copies the `aqm/` package, `aqm-cli` and the plist into `~/opt/agent-quota-maximizer/`, symlinks the plist into `~/Library/LaunchAgents/` and `aqm` into `~/.local/bin/`, and schedules a tick every 5 minutes. It writes `config.json` with **`enabled: false`** on a first install and never overwrites an existing one, so a fresh install is inert until you fill in the repo list and flip the flag. `release/uninstall.sh` unloads the job and removes both symlinks, leaving data and logs in place.

Two environment variables place everything, which is why the tests need no fixtures on disk: **`$AQM_HOME`** is our own tree (default `~/opt/agent-quota-maximizer`, holding `data/`, `state/` and `artifacts/`) and **`$AQM_USAGE_DATA`** is where the raw logs are read from (default `~/opt/agent-usage-tracker`). Tests: `bash test/run.sh`.

`release/` will also gain `prompts/` when stage 6 lands (`design/07_pipeline/DESIGN.md` §10). Development happens here in `~/dev/agent-quota-maximizer`; `~/opt/` holds the scheduled runtime copy, its state and its logs (see `~/AGENTS.md`).

## How it works, in one pass

Six stages, run every 5 minutes by a single LaunchAgent. Each stage is a CLI verb, reads the previous stage's artifact and writes its own, so any one can be run and inspected alone.

```
ingest → predict → budget → start_windows → plan → execute
```

The guiding principle is **postponement**: at every tick, work out how much of the remaining weekly quota cannot survive until the reset, and spend only that, at the last safe moment. Quota spent late is quota no later window could have absorbed, so the only way the system can get in the user's way is the time it needs to burn the amount — which is why lead time and burn rate, not lowered ceilings, are the levers.

## Reviewing what it did

Stage 1 writes four CSVs, one directory per agent; the pipeline adds an artifact per stage per tick and a metrics line per agent:

```text
~/opt/agent-quota-maximizer/
├── data/                       ingested history — rebuildable, safe to delete
│   ├── claude/
│   │   ├── meter.csv           ~24 rows/day   every reading that survived the drop_stale_readings
│   │   └── slots.csv         288 rows/day   the tidy 5-minute table
│   └── codex/                  the same, five times sparser
├── artifacts/                  immutable: one file per stage per tick
│   ├── predictions/<date>/<time>.json
│   └── budgets/<date>/<time>.json
├── logs/
│   ├── metrics.jsonl           one line per agent per tick — the notebook's input
│   └── pipeline.log            one line per tick
├── config.json                 the on/off switch and any parameter override
└── state/                      what the pipeline mutates — not rebuildable
    ├── latest/{prediction,budget}.json   symlinks → the newest of each
    └── locks/                  flock files, released by the kernel when a holder dies
```

`aqm budget` prints the decision as a table; `state/latest/budget.json` is the same for a script, and `artifacts/` is the audit trail — "why did it spend 0.6 units at 03:12?" is one file to open rather than a replay to trust. **A gap in `metrics.jsonl` is how a silently dead job is noticed**, which is why a line is appended even on a tick that decides nothing:

```bash
# the last few decisions, one line per agent per tick
tail -6 ~/opt/agent-quota-maximizer/logs/metrics.jsonl

# or just watch one tick happen
aqm pipeline
```

**To see *why* a decision came out the way it did**, open
[`notebook/explain_budget.ipynb`](notebook/README.md): it walks one decision from
the meter readings through the forecast to `extra_quota_to_spend_units`, showing every intermediate
value, and its last section replays two days of decisions so the behaviour over time
is visible rather than inferred.

`aqm ingest --backfill` rebuilds all of `data/` from the raw logs in under two seconds, so deleting it is a valid recovery step. `state/` is not rebuildable, which is why the two are separate directories.

**Every row starts with `observed_dt`** — local time with its UTC offset, e.g. `2026-10-04 14:20:00 +0200` — followed by the same instant as epoch seconds. The epoch value is what everything computes on; the rendering is there so the file reads without a tool. There is no `agent` column: the directory carries it.

An empty cell means *no value*, not zero. That matters most for `window_used_pct`, which is empty whenever no window is open — a closed window's level is not the current one.

```bash
cd ~/opt/agent-quota-maximizer/data/claude

# the last readings, as a table
(head -1 meter.csv; tail -8 meter.csv) | column -s, -t

# one line per 5-hour window, with its peak
/usr/bin/python3 -c 'import csv,collections
w=collections.defaultdict(lambda:["",0.0])
for r in csv.DictReader(open("meter.csv")):
    if r["window_end_ts"]:
        e=w[r["window_end_ts"]]; e[0]=r["window_end_dt"]; e[1]=max(e[1],float(r["window_used_pct"]))
for k in sorted(w,key=int): print("%s  %5.0f%%" % (w[k][0], w[k][1]))'

# only the slots where the meter moved  (column 7 is slot_window_used_pct)
awk -F, 'NR==1 || $7+0 > 0' slots.csv | column -s, -t | less -S

# or just open it
open slots.csv
```

About **45% of Claude slots and 68% of Codex ones have `is_window_open: false`** — for more than half the week no 5-hour window is even running, which is the waste this project exists to recover. (An earlier count said 76%; it was inflated by status-line rows that report a null reset while a window is still open — `design/01_ingestion/DESIGN.md` §5.) The check worth running yourself is that `sum(slot_window_used_pct)` equals the sum of per-window peaks; `test/test_real_data.sh` asserts it for both agents.

## Table of contents

### Start here

| File | What it holds |
|---|---|
| [`design/DESIGN_v2.md`](design/DESIGN_v2.md) | **The overview.** Objective, scope, shared vocabulary, stage map, implementation plan, cross-cutting open points |
| [`design/VOCABULARY.md`](design/VOCABULARY.md) | **Every name, what it measures, in which unit.** The naming grammar, every CSV column and artifact field with a description and an example, and the words that were retired. Read this before adding a field |
| [`design/PIPELINE_MAP.html`](design/PIPELINE_MAP.html) | **The whole design as one page of diagrams.** What each stage answers, what it needs from the one before and why, with an interactive backwards fill. Open with `open design/PIPELINE_MAP.html`. Update it whenever a stage's design or a VOCABULARY.md name changes |
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
| What does a name mean, and in what unit? | `design/VOCABULARY.md` — the grammar is §1 |
| Why is it built this way rather than another? | `design/CONSIDERATIONS.md`, then the stage's own DESIGN.md |
| What is every CLI flag? | `design/07_pipeline/DESIGN.md` §1.1 |
| What is the value of `MIN_HUMAN_RESERVE_PCT` / `GUARD_PCT` / any parameter? | `design/07_pipeline/DESIGN.md` §11 |
| What runs when, and what stops it? | `design/07_pipeline/DESIGN.md` §2, §4 |
| How does Claude differ from Codex here? | `design/CLAUDE_AND_CODEX.md` §2 |
| Which source gives percent / tokens / USD, and where exactly? | `~/dev/agent-usage-tracker/USAGE_DATA_SOURCES.md` (what exists upstream) and `~/dev/agent-usage-tracker/USAGE_DATA_REFERENCE.md` (what agent-usage-tracker captures, and where) |
| What is still undecided? | The end of each stage's DESIGN.md, plus `design/DESIGN_v2.md` §7 |
| What should I build next? | `design/DESIGN_v2.md` §6 — phases P0…P6 |

## Conventions in these documents

- **Facts marked [verified]** were measured on this machine on 2026-09-23, not assumed. Treat anything unmarked as a design claim, and anything marked **[derived]** as arithmetic on measurements.
- **One concept, one word, and `design/VOCABULARY.md` owns it.** Every name carries its unit and its span (`slot_window_human_pct`, `week_left_units`, `predicted_p95_human_usage_pct`); booleans start `is_`/`was_`/`has_`; `_ts` is epoch seconds and `_dt` is local text. A new field that cannot be named with that grammar means the grammar needs extending there first. Renaming one is a breaking change — the CSV header tests exist to make it one.
- **Each rule is stated once**, in the document that owns it; everywhere else refers to it as `<folder>/DESIGN.md §N`. When editing, fix the owning document rather than restating the rule.
- **`CONSIDERATIONS.md` holds no design choices.** New constraints and measurements go there; new decisions go in a stage's DESIGN.md.
- **Section numbers are load-bearing** — they are used as cross-references across files. Renumbering a section means sweeping every `§N` that points at it. In `CONSIDERATIONS.md` only, `§N.M` means *bullet M of section N*.
- **`plan`, `budget` and `execute` are reserved words** with exact meanings (stage, artifact, CLI verb). Budget decides *how much and by when*; plan decides *what runs where*. Do not use them loosely.
- Quota arithmetic is done in **meter percent**; units and dollars are for display only.

## Related projects on this machine

| Project | Relation |
|---|---|
| `agent-usage-tracker` | Owns all usage tracking (split out of `agent-statusline` on 2026-09-30) and writes the data files this project reads, under `~/opt/agent-usage-tracker/data/`. **`USAGE_DATA_SOURCES.md` there is the canonical description of what usage data exists upstream, in which unit, at which granularity; `USAGE_DATA_REFERENCE.md` is the canonical description of what it captures and where** — this project links to them rather than restating them. `adhoc_quotas_analysis/` holds the validated pricing and drop_stale_readings logic, and is the authority for every measured figure |
| `agent-auto-resume` | Also opens quota windows, so it can race stage 4 |
| `agent-notify` | Could surface a stuck job; not wired in |
