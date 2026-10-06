# Agent Quota Maximizer — Design v2 (overview)

Status: **ready to implement. Revised 2026-10-06 (§8): no forecast, the bot runs on the Debian VM, fewer parameters.** Supersedes `DESIGN_v1.md` (predictive planner), kept for reference.

This file holds only what belongs to the whole system: the objective, the scope, the shared vocabulary, the stage map, the implementation plan and the open points. **Every stage is designed in its own folder**, one per pipeline verb (§4), so the stages can be built and reviewed independently.

Facts marked **[verified]** were checked against this machine on 2026-09-23 rather than assumed. Each fact and rule is stated once, in the document that owns it; everything else refers to it.

## 1. Objective and guiding principle

Spend quota that would otherwise expire, on useful maintenance work, without ever blocking the user's interactive work.

**Principle:** at every tick, compute how much of the remaining weekly quota cannot survive until the reset, and spend it **as late as possible**. Postponing is what protects the user: quota spent at the last moment is quota no later window could have absorbed, so the only thing that ever puts the system in the user's way is the time it needs to burn that amount — the lead time of `05_planning/DESIGN.md` §1 (`CONSIDERATIONS.md` §4).

**Since 2026-10-06 there is no forecast** (§8). A fixed rule replaces it: start just in time, keep 25% of every window for the user, and never start while the user is active. That rule is enough **as long as the bot burns fast enough** — at 0.5 unit/h or more the lead time stays under about two hours; below that, the user's activity would have to be predicted again (§8.2). Stage 2 is parked, not deleted, for that case.

## 2. Scope

| In the MVP                                                                       | Deferred                                                    |
| -------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| Claude **and Codex**, planned independently                                      | A third agent                                               |
| Read-only tasks producing Markdown reports                                       | Tasks that write to a repo                                  |
| **Parallel execution**, capped per agent and globally, at most one task per repo | Parallelism across machines, or several tasks per repo      |
| **No forecast**: a fixed rule (§8.2); stage 2 parked                             | Any predictor (`02_prediction/DESIGN.md` §9 says when it comes back) |
| Static quiet hours                                                               | A learned activity profile                                  |
| A supervisor working to a fixed plan                                             | Noticing a user who arrives during a run (`06_execution/DESIGN.md` §5) |
| **The Debian VM** runs stages 1–6; the Mac only develops (§8.1)                  | Several bot machines, cloud scheduling                      |

## 3. Concepts

The shared vocabulary. Anything defined here is used with exactly this meaning in every folder.

| Term         | Definition                                                                                                                                                |
| ------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
`VOCABULARY.md` owns every name; this table only lists the few the whole design leans on.

| Term         | Definition                                                                                                                                                |
| ------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Unit         | The quota of one full 5-hour window = 100% of the 5-hour meter ≈ $32 on Claude Pro, ≈ $20.5 on Codex Plus. How many units a week holds is **measured**, not fixed (§8.3) |
| Human work   | Anything the user runs interactively                                                                                                                      |
| Bot work     | Anything this system runs (tasks and window-opening messages)                                                                                              |
| Window       | A 5-hour quota window; it exists only once a message opens it. The part of a window on one side of the 7-day reset is planned separately                  |
| `max_spend_units` | The most bot work a window may hold, after the user's reserve                                                                                       |
| `extra_quota_to_spend_units` | Units the budget assigns to the current window                                                                                                |
| `spend_by_ts`   | End of the current window (or the weekly reset, if sooner), minus `DEADLINE_MARGIN_SECONDS`                                                             |
| Lead time    | How long before `spend_by_ts` work must start: the amount divided by the bot's burn rate, × 1.5                                                            |
| `already_lost_units` | Remaining weekly quota that no remaining window can absorb                                                                                         |
| Tick         | One run of `aqm pipeline`                                                                                                                                 |

## 4. The pipeline, and where each stage is designed

The subcommands **are** the stages, and each stage is one folder. The full command table, with every flag, is in `07_pipeline/DESIGN.md` §1.1.

| #   | Stage          | Command             | Question it answers                                   | Folder              |
| --- | -------------- | ------------------- | ----------------------------------------------------- | ------------------- |
| 1   | Ingestion      | `aqm ingest`        | What happened?                                        | `01_ingestion/`     |
| 2   | Prediction     | `aqm predict`       | How much will the user still want? **Parked 2026-10-06** (§8) | `02_prediction/`    |
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
| **P2b** | **Move to the VM (§8.1)**: Codex installed and logged in, a meter reader for both agents, the repos cloned, a systemd user timer instead of the LaunchAgent. Then the §8 changes in code: stage 2 out of the tick, the week capacity measured, the week meter's "never decreases" rule, the window that outlives the reset | `aqm pipeline` ticks on the VM from its own meter readings; the Mac runs nothing scheduled for this project |
| **P3** | `04_start_windows/`, both agents, including the Codex period rule                                                                                                                                                | A window opens within one tick of the previous one expiring; a Codex period is started after a reset; cost ≈ $0.10/day/agent                                                      |
| **P4** | `06_execution/`. **Its first deliverable is the measured burn rate per agent**, with 1, 2 and 3 parallel workers — it decides whether the no-forecast rule stands (§8.2). Then supervisor, worker pool, repo locks, slot semaphore, reservation, meter and cost capture, reports with scrubbing | Burn rate ≥ 0.5 unit/h per agent, or a recorded decision to bring prediction back; budget respected within 1% of the meter with workers overlapping; a second task on a busy repo never starts |
| **P5** | `05_planning/`: catalog, ranking, prompts and reports, `aqm plan`                                                                                                                                                 | Reports produced nightly and readable in a few minutes                                                                                                                            |
| **P6** | Backtest chaining the read-only stages with `--at`, the notebook's metrics, parameter tuning                                                                                                                      | Waste falls week over week on both agents, with zero blocking incidents                                                                                                           |

The phases are not the folder order: budgeting is built before prediction because it is what proves the arithmetic, and planning comes late because it needs the repo list and the measured burn rate.

Codex is worth building alongside Claude rather than after it: more than half of its $127 week is lost most weeks, and entire weeks are lost completely (`CLAUDE_AND_CODEX.md` §1).

## 7. Open points

Each stage keeps its own open points at the end of its DESIGN.md. The ones that belong to no single stage:

- **Codex unknowns** (`CLAUDE_AND_CODEX.md` §7): whether `codex exec` opens a window exactly like an interactive message, how fast the meter reflects it, whether a started period can be left unused with no penalty, whether `codex-auto-review` bills to the same meter, and the assumed dollar scale.
- **`agent-auto-resume`**: it also opens windows; beyond the cooldown, whether to read its pending-resume queue directly is undecided.
- **Failure notifications:** `agent-notify` exists and could surface a stuck job; whether to notify, and how loudly, is undecided.
- **The VM runs the bot, the Mac is where the user works** (§8.1). Whether the VM's Claude login is the same account as the Mac's is still to confirm, and so is how the VM reads the meter (agent-usage-tracker's pollers deployed there, or `aqm` calling the two endpoints itself).
- **Prediction comes back only if the bot is slow** (§8.2): measured below ~0.5 unit/h per agent even with parallel workers. It would write the same artifact as before (`02_prediction/DESIGN.md` §1).
- **A user who arrives during a run** is not noticed by the VM: the meter moves for both of them, and the VM cannot see the Mac's sessions. With a fast bot this exposure is short; how to detect it is open (`06_execution/DESIGN.md` §5).
- **Still owed by the user before P5:** the in-scope repo list with weights, and any repo to exclude for secrets.

## 8. Decisions of 2026-10-06: no forecast, the VM, fewer parameters

A review of stages 1–3 replayed the **whole decision** — budget, a just-in-time start, a bot burning quota — over the 42 recorded days of real human usage, instead of scoring one stage on its own. Every figure below comes from `../lab/07_pipeline/pipeline_replay.py` (`/usr/bin/python3 lab/07_pipeline/pipeline_replay.py`, about 10 seconds). This section is the index of what changed and why; each rule is stated in the document that owns it.

| Decision | Why, in one line | Rule stated in |
|---|---|---|
| The bot runs on the **Debian VM**, not the Mac | The Mac is asleep about half the time and most of every night; on the Mac the replay leaves 41% (Claude) and 36% (Codex) of today's waste, on an always-on machine about 1% and 0% | §8.1, `CONSIDERATIONS.md` §20 |
| **No forecast**: stage 2 is parked, not deleted | The forecast lowers the amount on 3.1% (Claude) and 0.1% (Codex) of ticks, and at a burn rate of 0.5 unit/h or more it changes no outcome | `02_prediction/DESIGN.md` §9 |
| A fixed rule replaces it | Start just in time, keep 25% of every window for the user, never start while the user is active, and give the window that outlives the weekly reset only its share before the reset | §8.2, `03_budgeting/DESIGN.md` §2, `05_planning/DESIGN.md` §1 |
| **The bot's burn rate is measured first** | It decides whether the rule is enough or prediction must come back | §8.2, `06_execution/DESIGN.md` §3 |
| How many units a week holds (`week_capacity_units`) is **measured**, not hard-coded | 8.85 was assumed; the last three Claude weeks measured 8.2, 7.3 and 7.6 | `03_budgeting/DESIGN.md` §2 |
| **Fewer parameters**: `GUARD_PCT` and `MAX_DAILY_UNITS` removed, the forecast's five parked | Each one either repeated another or contradicted the budget | §8.3, `07_pipeline/DESIGN.md` §11 |
| The week meter obeys **"never decreases"**, like the window meter | Two upstream sources disagree by one point, and without the rule the weekly movement double-counts: 111% for a week that peaked at 73% | `01_ingestion/DESIGN.md` §5, `CONSIDERATIONS.md` §22 |
| **Human/bot attribution is not built** | No decision reads it, and on the VM the bot's spend is simply what the VM ran | `01_ingestion/DESIGN.md` §1 |

**The code does not follow yet.** `release/aqm/` still runs stage 2 every tick and budget still reads its forecast; the changes land in P2b (§6).

### 8.1 Where the bot runs: the VM

The Mac sleeps whenever its lid is closed on battery, which is most nights, and `caffeinate` cannot prevent that (`CONSIDERATIONS.md` §20). A bot on the Mac can only spend in the hours the user is at the Mac — which are also the hours it competes with them. The VM is always on.

```
  Mac (development, the user's own work)            VM H-Frank-1 (always on)
  ────────────────────────────────────────           ────────────────────────────────────────
  asleep 52% of the time, ~86% of 00:00–08:00        up 9 weeks without a reboot
  lid closed on battery: caffeinate cannot help      Claude Code 2.1.240, logged in (Pro)
                                                     Codex: not installed yet
                 \                                      /
                  \____  the meter is server-side and account-wide  ____/
                         Claude  GET api.anthropic.com/api/oauth/usage
                         Codex   codex app-server
```

| What the decision needs | Where it comes from | On the VM |
|---|---|---|
| Week and window percent, with their resets | The two endpoints above, with the account's login | Claude: yes. Codex: after installing and logging in |
| What the bot itself ran | The VM's own processes | Yes |
| The repos to review | `git clone` from GitHub | To do |
| Whether the user is typing right now | The Mac's session files | **No** — the meter is enough: when the bot is idle, any movement is the user (§8.2) |

The Mac alone remains a fallback: it recovers about 60% of the waste instead of about 99%.

### 8.2 The rule, and the one condition that would bring prediction back

The rule, in four parts:

1. **Budget** — spend now = max(0, what is left of the week − what the later windows can take), capped at what this window can take after keeping `MIN_HUMAN_RESERVE_PCT` (25%) for the user.
2. **Budget** — the window that outlives the weekly reset is only given its share before the reset. A weekly reset does not reset the 5-hour window (`CONSIDERATIONS.md` §13), so filling it to 75% just before the reset hands the user a window three quarters full at the start of their new week: that was the main source of collisions once the bot ran on an always-on machine.
3. **Plan** — start when the time left is just enough to burn the amount at the measured burn rate, × 1.5.
4. **Plan** — never start while the user is active: they moved the meter or sent a prompt in the last `HUMAN_IDLE_MINUTES` (30). This is an observation, not a forecast, and it is what the user asked for: an hour before the reset, while they are working, what is left is theirs.

Part 4 is justified by how different the next hours look when the user is active right now — on Claude about ten times riskier:

| Claude: the user uses more than 25% of a window in the next… | 1 h | 1.5 h | 3 h | 5 h |
|---|---|---|---|---|
| …if active in the last 30 minutes | **19%** | 28% | 45% | 52% |
| …if not | **2%** | 3% | 7% | 13% |

(Codex: 1% against 0% at one hour, 13% against 2% at three.) Its measured cost is 2–3 points of extra waste.

**Either the bot burns fast, or the system must predict the user.** The rule only protects the user while the bot's lead time is short: the longer the bot has to run before the deadline, the more likely the user arrives in the middle. Replayed on an always-on machine:

| Bot burn rate | Lead time for 0.75 unit | Rule, no forecast — Claude | Rule — Codex | Today's forecast — Claude |
|---|---|---|---|---|
| 1 unit/h | ~1 h | 0 collisions, 2% waste left | 0 collisions, 3% | 0 collisions, 1% |
| 0.5 unit/h | ~2¼ h | 0 collisions, 2% | 1 collision (0.1 h), 0% | 0 collisions, 1% |
| 0.25 unit/h | ~4½ h | **3 collisions, 5.4 h of waiting** | 2 collisions, 4.7 h | 2 collisions, 1.1 h |

A collision is the user hitting a full 5-hour window the bot had spent in; the waiting is until that window's reset, summed over six weeks. Hence the order of work: **P4 measures the burn rate first**, per agent, with 1, 2 and 3 parallel workers (parallel workers raise the burn rate without raising any limit). At 0.5 unit/h or more the rule stands. Below it, add workers; if that is still not enough, prediction comes back (`02_prediction/DESIGN.md` §9).

Limits of the replay: six weeks of one person; demand blocked by a full window is dropped rather than postponed; the bot's burn rate is assumed; the Mac's sleep is estimated from the pollers' rows, which count 60% of the time as awake against 48% from `pmset`, so the Mac figures are the optimistic case.

### 8.3 What was removed, and why nothing is lost

One concern per stage: **budget** keeps the user's share, **plan** decides when to start, **execution** makes sure its own tasks fit in what they were given. A margin so that our own task does not stall mid-run is execution's business, not a budget parameter.

| Removed | What it did | Why it can go |
|---|---|---|
| Stage 2 from the tick, with `SAFETY_MULTIPLIER`, `BURN_LOOKBACK_MINUTES`, `MIN_RATE_DENOMINATOR_MINUTES`, `ACTIVE_HUMAN_BURN_RATE`, `ARTIFACT_MAX_AGE_SECONDS` | Forecast the user's demand to the window's end | §8.2. Parked, not deleted |
| `GUARD_PCT` (15%) | Killed the newest worker when the window had less than 15% left | Same job as the 25% reserve; the replay is identical with the guard set equal to the reserve. Set apart from it, it contradicted the budget: a 10% reserve with a 15% guard left 6% waste instead of 1% |
| `MAX_DAILY_UNITS` (3 units) | Emergency brake on bot spend per day | The 5-hour windows already cap a day at about 3.6 units, and the replay's last day before a reset needs up to 3.75 — the brake would have caused waste |
| `WEEK_CAPACITY_UNITS` as a constant | Converted the week's percent into units | Replaced by a measurement (`03_budgeting/DESIGN.md` §2) |
| The room test (`05_planning/DESIGN.md` §3) | Re-checked the forecast before planning | The reserve is already in the budget, and there is no forecast |
| Dollar apportionment of overlapping slots (`01_ingestion/DESIGN.md` §1.3–1.4) | Split a slot's movement between the user and the bot | No decision reads it |

One safeguard was considered and **not** added: a weekly guard stopping the bot near 100% of the week. Budget re-reads the real week percent every tick, so the amount falls to 0 on its own as the week fills.
