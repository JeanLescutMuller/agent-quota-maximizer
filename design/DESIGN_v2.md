# Agent Quota Maximizer — Design v2 (overview)

Status: **ready to implement.** Supersedes `DESIGN_v1.md` (predictive planner), kept for reference.

This file holds only what belongs to the whole system: the objective, the scope, the shared vocabulary, the stage map, the implementation plan and the open points. **Every stage is designed in its own folder**, one per pipeline verb (§4), so the stages can be built and reviewed independently.

Facts marked **[verified]** were checked against this machine on 2026-09-23 rather than assumed. Each fact and rule is stated once, in the document that owns it; everything else refers to it.

## 1. Objective and guiding principle

Spend quota that would otherwise expire, on useful maintenance work, without ever blocking the user's interactive work.

**Principle:** at every tick, compute how much of the remaining weekly quota cannot survive until the reset, and spend it **as late as possible**. The timing needs no forecast to be safe — today's prediction stage only extrapolates the recent past — but everything is written against the prediction output of `02_prediction/DESIGN.md` §1 so a real predictor can replace it without touching another stage. Postponing is what protects the user: quota spent at the last moment is quota no later window could have absorbed, so the only thing that ever puts the system in the user's way is the time it needs to burn that amount — the lead time of `05_planning/DESIGN.md` §1 (`CONSIDERATIONS.md` §4).

## 2. Scope

| In the MVP                                                                       | Deferred                                                    |
| -------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| Claude **and Codex**, planned independently                                      | A third agent                                               |
| Read-only tasks producing Markdown reports                                       | Tasks that write to a repo                                  |
| **Parallel execution**, capped per agent and globally, at most one task per repo | Parallelism across machines, or several tasks per repo      |
| A prediction stage shaped like a forecast, trivially implemented                 | A learned predictor (`02_prediction/PREVIOUS_IDEAS.md`)     |
| Static quiet hours                                                               | A learned activity profile                                  |
| A supervisor working to a fixed plan                                             | Live room re-check during a run (`06_execution/DESIGN.md` §5) |
| This machine only                                                                | The Debian VM, cloud scheduling                             |

## 3. Concepts

The shared vocabulary. Anything defined here is used with exactly this meaning in every folder.

| Term         | Definition                                                                                                                                                |
| ------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Unit         | The quota of one full 5-hour window = 100% of the 5-hour meter ≈ $32 on Claude Pro, ≈ $20.5 on Codex Plus; a Claude week ≈ 8.85 units, a Codex week ≈ 6.2 |
| Organic work | Anything the user runs interactively                                                                                                                      |
| Extra work   | Anything this system runs (tasks and window-opening messages)                                                                                              |
| Window       | A 5-hour quota window; it exists only once a message opens it                                                                                              |
| Window        | A window, or the part of a window on one side of the 7-day reset                                                                                          |
| Ceiling      | The most extra work a window may hold, after the user's predicted demand                                                                                   |
| `extra_quota_to_spend_units` | Units the budget assigns to the current window; the plan clamps it to `amount`                                                                             |
| `spend_by_ts`   | End of the current window, minus `DEADLINE_MARGIN_SECONDS`                                                                                                         |
| Lead time    | How long before the deadline work must start                                                                                                              |
| Surplus      | Remaining weekly quota that the remaining windows still to come cannot absorb                                                                                        |
| Tick         | One run of `aqm pipeline`                                                                                                                                 |
| Room         | `1 − window_used − predicted human p95`: what the current window can still give extra work                                                              |

## 4. The pipeline, and where each stage is designed

The subcommands **are** the stages, and each stage is one folder. The full command table, with every flag, is in `07_pipeline/DESIGN.md` §1.1.

| #   | Stage          | Command             | Question it answers                                   | Folder              |
| --- | -------------- | ------------------- | ----------------------------------------------------- | ------------------- |
| 1   | Ingestion      | `aqm ingest`        | What happened?                                        | `01_ingestion/`     |
| 2   | Prediction     | `aqm predict`       | How much will the user still want?                    | `02_prediction/`    |
| 3   | Budgeting      | `aqm budget`        | **How much may be spent, and by when?**               | `03_budgeting/`     |
| 4   | Window starter | `aqm start_windows` | Is a window open? *(acts)*                            | `04_start_windows/` |
| 5   | Planning       | `aqm plan`          | **From when, what, where, with which AI parameters?** | `05_planning/`      |
| 6   | Execution      | `aqm execute`       | Run it *(acts)*                                       | `06_execution/`     |
| —   | Pipeline       | `aqm pipeline`      | All of the above, plus every cross-cutting rule       | `07_pipeline/`      |

Each stage consumes the artifact of the stage before it and writes a new immutable one, so any stage can be run, inspected and backtested on its own.

## 5. Document map

| File | What it holds |
|---|---|
| `CONSIDERATIONS.md` | Every fact and constraint the design rests on — no design choices |
| `CLAUDE_AND_CODEX.md` | Both agents side by side: measured data, the mechanics that differ, and what those differences force |
| `0N_*/DESIGN.md` | The design of that stage — the only place its rules are stated |
| `02_prediction/PREVIOUS_IDEAS.md` | Eight candidate forecasting methods with their independent drawbacks; none built |
| `03_budgeting/PREVIOUS_IDEAS.md` | The scheduling policies considered; the one in use is Policy D |
| `04_start_windows/CONSIDERATIONS.md` | Why windows must be opened deliberately, with the cost arithmetic |
| `DESIGN_v1.md` | The superseded first design, kept for reference |

## 6. Implementation plan

| Phase  | Deliverable                                                                                                                                                                                                      | Acceptance                                                                                                                                                                        |
| ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **P0** ✅ | `01_ingestion/`: tail-and-watermark readers for both account logs, drop_stale_readings, window-key normalisation and attribution, the JSONL slot table, `meter_now` / `burn_rate` / `window_state`, backfill of the recorded month | **Done 2026-10-04** (`release/aqm.py`, 70 assertions in `test/`). Backfill 2.1 s over 40.8 days → 1,158 meter rows and 21,856 slots; live tick **6 ms of work, ~50 ms wall**. A 576-tick replay reproduces the backfill table with **zero differing rows**, and `sum(slot_window_used_pct)` matches per-window peaks exactly on both agents; 46 Claude windows = 1.13/day, matching `adhoc_quotas_analysis` independently |
| **P1** ✅ | `03_budgeting/` and `aqm budget`, both agents, unit tests. No prediction yet, so every margin is `MIN_HUMAN_RESERVE_PCT`                                                                                                    | **Done 2026-10-04** (`release/aqm.py`, 48 assertions in `test/test_budget.sh` + `test_real_data.sh`). Every expected figure is hand-computed beside its assertion; `planned + unreachable = week_left_units` holds on fixtures and on both real meters; nothing acts. On the real meter it found **0.85 units due now and 1.30 unreachable on Claude** — the project's premise, measured (`03_budgeting/DESIGN.md` §3). Work **2.2 ms**, which confirmed one process per tick: 41 ms each as two processes against 45 ms for both in one. Building it corrected the ceiling formula, which prorated quota by time *and* subtracted usage (`03_budgeting/DESIGN.md` §2) |
| **P2** ✅ | `02_prediction/` and `aqm predict`, pipeline skeleton with locks, guards and housekeeping, config, logging, metrics, plist, `install.sh` — dry-run only                                                          | **Built 2026-10-04** (`release/aqm.py`, `install.sh`, the plist and the config template; 193 assertions, 65 of them new). `aqm pipeline` runs ingest → predict → budget in one process in **7.3 ms of work, 57 ms wall clock**, writes both artifacts and a metrics line per agent per tick, and refuses on a bad config, a stale reading or a held lock. Building it found two real bugs: `was_human_active` was counted *after* the drop_stale_readings, which silently deleted the only case the S2 tripwire exists for (`01_ingestion/DESIGN.md` §6.4), and the prediction tail was sized for ingestion, making predict the most expensive stage in the pipeline at 10.9 ms (`02_prediction/DESIGN.md` §7). **Still owed before this phase is closed:** the week of real ticks, and the p95 calibration — neither can be checked the day it is written |
| **P3** | `04_start_windows/`, both agents, including the Codex period rule                                                                                                                                                | A window opens within one tick of the previous one expiring; a Codex period is started after a reset; cost ≈ $0.10/day/agent                                                      |
| **P4** | `06_execution/`: supervisor, worker pool, repo locks, slot semaphore, reservation, meter and cost capture, burn-rate samples, reports with scrubbing. One worker first, then 2 per agent and both agents at once. | Budget respected within 1% of the meter with workers overlapping; a second task on a busy repo never starts; `GUARD_PCT` stops launches when the window fills                     |
| **P5** | `05_planning/`: catalog, ranking, prompts and reports, `aqm plan`                                                                                                                                                 | Reports produced nightly and readable in a few minutes                                                                                                                            |
| **P6** | Backtest chaining the read-only stages with `--at`, the notebook's metrics, parameter tuning                                                                                                                      | Waste falls week over week on both agents, with zero blocking incidents                                                                                                           |

The phases are not the folder order: budgeting is built before prediction because it is what proves the arithmetic, and planning comes late because it needs the repo list and the measured burn rate.

Codex is worth building alongside Claude rather than after it: more than half of its $127 week is lost most weeks, and entire weeks are lost completely (`CLAUDE_AND_CODEX.md` §1).

## 7. Open points

Each stage keeps its own open points at the end of its DESIGN.md. The ones that belong to no single stage:

- **Codex unknowns** (`CLAUDE_AND_CODEX.md` §7): whether `codex exec` opens a window exactly like an interactive message, how fast the meter reflects it, whether a started period can be left unused with no penalty, whether `codex-auto-review` bills to the same meter, and the assumed dollar scale.
- **`agent-auto-resume`**: it also opens windows; beyond the cooldown, whether to read its pending-resume queue directly is undecided.
- **Failure notifications:** `agent-notify` exists and could surface a stuck job; whether to notify, and how loudly, is undecided.
- **Multi-machine:** state assumes one machine; the VM shares the account but has no reader, so its usage appears only through the meter.
- **A real forecast** writes the same prediction artifact as today's (`02_prediction/DESIGN.md` §1), optionally with per-window values; the trigger for building it is P6 showing that `MIN_HUMAN_RESERVE_PCT` and static quiet hours are what limit the result.
- **Still owed by the user before P5:** the in-scope repo list with weights, and any repo to exclude for secrets.
