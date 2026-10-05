# 03 — Budgeting (`aqm budget`)

Stage 3 answers **how much may be spent, and by when.** It is the stage that implements the project's guiding principle: spend only what cannot survive until the reset, and spend it as late as possible.

| | |
|---|---|
| Command | `aqm budget [--at <iso>] [--prediction PATH] [--out PATH]` |
| Reads | `data/<agent>/slots.csv`, the latest prediction (`../02_prediction/DESIGN.md` §1) |
| Writes | `artifacts/budgets/<date>/<time>.json` |
| Acts | No |
| Consumed by | Planning (`../05_planning/DESIGN.md`) |
| **Runtime** | **Milliseconds**, every tick (§6) |

It does **not** decide what to run — that is planning — and it does not decide *when to start*, only the deadline to finish by. The policies that were considered and rejected before this one, including the nightly top-up and the availability-weighted ceilings, are in `PREVIOUS_IDEAS.md`; this design is Policy D there.

## 1. Definitions used here

| Term | Definition |
|---|---|
| Unit | The quota of one full 5-hour window = 100% of the 5-hour meter |
| Window | A window, or the part of a window on one side of the 7-day reset |
| Ceiling | The most extra work a window may hold, after the user's predicted demand |
| Surplus | Remaining weekly quota that the remaining windows still to come cannot absorb |

## 2. The algorithm

```text
1. PROJECT the chain of remaining windows from now to the 7-day reset:
     first window = the open window (its real end), or a hypothetical window starting now
     next remaining windows = 5h windows separated by WINDOW_GAP_SECONDS
     the window containing the reset is split; only the part before the reset is planned

2. MAX SPEND of each window — what extra work may take, after the user's predicted demand
   (a full window is 1 unit, so 0.9 is what `MIN_HUMAN_RESERVE_PCT = 10` leaves).
   Two independent limits, and the smaller wins:
     human_reserve_pct(window) = max(MIN_HUMAN_RESERVE_PCT, predicted_p95_human_usage_pct)
                                                            # no per-window forecast yet, so every
                                                            # projected window uses the floor
     max_spend_units_by_quota  = (100 − human_reserve_pct − window_used_pct) / 100
                                                            # window_used_pct only for the open window
     max_spend_units_by_time   = window duration in hours × BOT_BURN_UNITS_PER_HOUR
     max_spend_units(window)   = max(0, min(by_quota, by_time))

   Both reserves are in **percent**, so the comparison needs no conversion -- which is
   also what makes the `GUARD_PCT` conflict in §9 visible at a glance.

3. BACK-FILL from the last window before the reset, backwards:
     remaining = remaining weekly quota (units)
     for window in reversed(remaining windows):
         planned[window] = max(0, min(ceiling[window], remaining))
         remaining     -= planned[window]

4. OUTPUT: extra_quota_to_spend_units = planned[current window]
           deadline   = end(current window) − DEADLINE_MARGIN_SECONDS
           unreachable = remaining after step 3        # quota that cannot be spent at all
```

Back-filling is what implements the guiding principle: every window earlier than strictly necessary gets 0, so the user keeps first claim on everything until it is about to expire. `extra_quota_to_spend_units = 0` is the normal case and the tick ends there in milliseconds. `unreachable > 0` is reported as unavoidable waste (`../CONSIDERATIONS.md` §6), not acted upon.

**The two limits on a ceiling are never multiplied.** `max_spend_units_by_quota` answers *how much the meter will still give*; `max_spend_units_by_time` answers *how much can be burned before the window ends*. They are different questions with different units, and an earlier version of this design combined them — prorating the quota by the window's duration and then also subtracting what the window had used. That double-counts, and it does so hardest in the window that matters most:

| Open window, 5% used | Prorated × subtracted | `min(quota, time)` |
|---|---|---|
| 5 h left | 0.85 | 0.85 |
| **2.5 h left** | **0.40** | **0.85** |
| 1 h left | 0.13 | 0.85 |
| 30 min left | 0.04 | 0.50 ← the clock, correctly |

The middle row is the whole point: a window half elapsed with 5% used has 0.85 genuinely free in it, and the old formula offered 0.40 — **it skipped more than half the room in the window we most want to spend in**, precisely because postponement means we always arrive at a window late. Duration limits how *fast* quota can be burned, not how much of it exists.

**`BOT_BURN_UNITS_PER_HOUR` is load-bearing here, and it is not yet measured.** At 1.0 unit/h (`../07_pipeline/DESIGN.md` §11) `max_spend_units_by_time` binds only on remaining windows shorter than 54 minutes, so in practice the ceiling is `max_spend_units_by_quota` and the clock matters only right before a reset. If the real burn rate is lower, the stage promises a window more than can be burned in it — and the failure mode is that the amount simply is not spent and the next tick re-plans, which is **underspend, not collision**. That is the safe direction, which is why a placeholder is acceptable until P4 measures the rate (§7).

**The one unit conversion in the stage.** `remaining` comes from the 7-day meter, which reads in percent of the *week*, while remaining windows are measured in units of a 5-hour *window*. The bridge is the weekly capacity of `../CLAUDE_AND_CODEX.md` §1 — 8.85 windows for Claude, 6.2 for Codex:

```text
remaining = (100 − week_used_pct) / 100 × WEEK_CAPACITY_UNITS[agent]
```

This is the only place in the system where percent is multiplied by anything, and it is why the two agents need separate constants despite sharing every line of the arithmetic.

**What makes the stage refuse to decide.** It needs a weekly level and a reset in the future, and nothing else. Without both it returns `extra_quota_to_spend_units: 0` with a `reason`, and never a figure derived from a guess:

| Case | Result |
|---|---|
| No meter reading at all | `no meter reading` |
| `week_used_pct` or `week_end_ts` missing | `no usable weekly reading` |
| `week_end_ts` already in the past | `no usable weekly reading` — a stale reading, not an empty week. Treating a past reset as "the week is over" would compute the surplus against a period that has already rolled, and spend quota the user still has |
| Prediction file present but unreadable | Margin is a full window, so every ceiling is 0 (`../02_prediction/DESIGN.md` §5); `method` is `fail-safe` |
| The latest prediction is older than `ARTIFACT_MAX_AGE_SECONDS` | The same: `method` is `stale-prediction`. Checked **here**, not in the pipeline — the pipeline writes the prediction itself, so it can never catch a stale one. An explicit `--prediction PATH` is exempt: pointing at a file is taken as meaning it |
| No prediction file at all | Margin is `MIN_HUMAN_RESERVE_PCT`, which is P1 |

The last two differ deliberately: *absent* is the designed state until P2 lands, *corrupt* is a failure, and a failure must fail safe.

Nothing in the arithmetic assumes a weekday or an hour, so a Codex reset at 04:13 on a Sunday is handled identically.

The chain assumes a window always exists when one is needed. That assumption is made true by a separate stage, not by this one (`../04_start_windows/DESIGN.md`).

## 3. Output

The two numbers the executor is ultimately launched with, `extra_quota_to_spend_units` and `spend_by_ts`, plus the chain they came from. This is a real artifact, taken on 2026-10-04 with the chain trimmed:

```json
{"computed_ts": 1791136361, "method": "min-reserve", "config_hash": "1f3806aa",
 "agents": {
   "claude": {"verdict": "spend_now",
              "extra_quota_to_spend_units": 0.85,
              "spend_by_ts": 1791153300,
              "already_lost_units": 1.3025,
              "week": {"used_pct": 35, "end_ts": 1791226800, "left_units": 5.7525},
              "current_window": {"is_window_open": true, "end_ts": 1791153600, "used_pct": 5},
              "remaining_windows": [
                {"start_ts": 1791136361, "end_ts": 1791153600, "is_open_now": true,
                 "human_reserve_pct": 10, "max_spend_units_by_quota": 0.85,
                 "max_spend_units_by_time": 4.71, "limited_by": "quota",
                 "max_spend_units": 0.85, "planned_units": 0.85},
                {"start_ts": 1791153900, "end_ts": 1791171900, "is_open_now": false,
                 "human_reserve_pct": 10, "max_spend_units_by_quota": 0.9,
                 "max_spend_units_by_time": 5.0, "limited_by": "quota",
                 "max_spend_units": 0.9, "planned_units": 0.9}]},
   "codex":  {"verdict": "wait",
              "extra_quota_to_spend_units": 0.0, "spend_by_ts": null,
              "already_lost_units": 0.0,
              "week": {"used_pct": 9, "end_ts": 1791617040, "left_units": 5.642},
              "current_window": {"is_window_open": false, "end_ts": null, "used_pct": 0},
              "remaining_windows": []}}}
```

`extra_quota_to_spend_units` is in units and `spend_by_ts` is an epoch second. The plan stage clamps the amount to the room left in the window and copies both into the mandate the supervisor reads, so `extra_quota_to_spend_units` here is the budget's view before that clamp. `spend_by_ts` is `null` exactly when `extra_quota_to_spend_units` is 0.

`method` is carried through from the prediction that set the margins, so an artifact says which forecaster decided; `min-reserve` means there was no prediction at all. `verdict` exists so a 0 never has to be interpreted: a quiet night and an unreadable meter both produce `extra_quota_to_spend_units: 0`, and only one of them is a decision.

`remaining_windows` is not called *future* windows: entry `[0]` is normally the window already running, and `is_open_now` is true for it and for no other (`../VOCABULARY.md` §5).

**Each window carries its own working** — `human_reserve_pct`, `max_spend_units_by_quota`, `max_spend_units_by_time` and which of the two `limited_by` — so "why was this window's ceiling 0.85?" is answerable from the file, with no rerun and no reimplementation of §2. That matters beyond convenience: the notebook that visualises a decision (`../../notebook/README.md`) displays these fields rather than recomputing them, because a second copy of the formula is the copy a human would end up trusting.

`aqm budget` prints exactly the table a human would draw (`--json` gives the same content for a script). The same real tick as above:

```text
claude · now Sun 19:52 · week resets Mon 21:00 (in 25h07m) · remaining 5.75 units

  window                     ceiling   planned   left
  [Sun 19:52 – Mon 00:40]     0.85      0.85   4.90
  [Mon 00:45 – Mon 05:45]     0.90      0.90   4.00
  [Mon 05:50 – Mon 10:50]     0.90      0.90   3.10
  [Mon 10:55 – Mon 15:55]     0.90      0.90   2.20
  [Mon 16:00 – Mon 21:00]     0.90      0.90   1.30     (split by the reset)

  extra_quota_to_spend_units 0.85 · deadline Mon 00:35 · unreachable 1.30 · decision: spend before the reset
```

**That table is the project's premise, measured.** Every window to the reset is full, and 1.30 units — about $41 — still cannot be placed anywhere: Claude's week holds more quota than the windows left can absorb, which is exactly the waste this system exists to recover. Codex at the same instant has 133 hours to its reset, so its whole plan sits on Friday and `extra_quota_to_spend_units` is 0 — the normal case, and the one that costs nothing.

Note the first window: the window is 5% used with 4h48m left, and the ceiling is 0.85 — the quota actually free in it. Under the prorated formula it would have been 0.77 and falling fast, for no reason connected to anything real.

## 4. On the forecast

The budget stage uses the prediction in exactly one place — the margin of step 2 — and never asks for a forecast beyond the current window: back-filling already leaves every earlier window to the user, so reserving weekly quota against a predicted week would spend later than necessary and waste more (`PREVIOUS_IDEAS.md` §4.5).

With today's `recent-rate-v1` the margin is `MIN_HUMAN_RESERVE_PCT` everywhere except the current window; with a real predictor the same formula tightens the ceiling where the user is likely to work and relaxes it where they are not, with no other change.

**The concentration problem, and why it is not fixed here.** Back-filling piles the plan into the remaining windows immediately before the reset, which on a Claude week is Monday daytime — the user's own hours. The fix is *lead time and burn rate*, not lower ceilings: start as late as the machine can still finish, and burn hard. Lowering ceilings would convert a bounded 5-hour collision into an unbounded 7-day shortage, which is the trade this project refuses (`PREVIOUS_IDEAS.md` §4.5). The lead-time rule lives in `../05_planning/DESIGN.md` §1.

## 5. Testing

`../../test/test_budget.sh`, **40 assertions**, hermetic. Every expected number is hand-computed in a comment beside the assertion rather than copied from the implementation — that is what makes the file an acceptance test for P1 and not a snapshot of whatever the code happens to do.

What it pins, beyond the arithmetic of §2: the chain stops at the reset and never crosses it; the chain of a whole period is **34 remaining windows**, the structural bound of §6; the open window is the only window that loses what it has already used; a projected window loses nothing, because nothing has been spent in a window that does not exist; `spend_by_ts` is `null` exactly when `extra_quota_to_spend_units` is 0; each `reason` of §2; the prediction changes the current window's ceiling **and nothing else**; and `--at` writes nothing into the live tree while a live run writes the artifact, swaps the pointer and leaves no `.tmp` behind.

Nothing in this stage acts, and nothing in it can: it has no subprocess, no network and one write.

## 6. Speed

This stage runs on **every** tick, and it is the cheapest of the three: **pure arithmetic over at most 34 remaining windows, with no I/O beyond two small files.**

The bound is structural. A 7-day period is 168 hours and a window is one 5-hour window plus `WINDOW_GAP_SECONDS`, so the chain of step 1 can never hold more than **34 remaining windows** — and the loops of steps 2 and 3 are single passes over it. There is no search, no iteration to convergence and no simulation: the back-fill is one reversed pass.

| Work | Cost **[measured 2026-10-04]** |
| --- | --- |
| Read the latest prediction and the meter tail | **2.16 ms** for both agents, and it is almost all this |
| Project, ceiling, back-fill | ≤ 34 iterations × 3 passes — too small to measure |
| Write the artifact and swap the `latest` symlink | **0.56 ms**, ~4 KB, atomic |

No network, no subprocess, no database scan — everything it needs is the remaining weekly percentage, the two reset times and one prediction number.

**One process per tick, confirmed by this stage.** `aqm budget` costs **41 ms** of wall clock and `aqm ingest` costs 41 ms, of which 39 ms is a Python interpreter starting in both cases; the two stages' *work* together is **5.8 ms**. Running them as two processes therefore costs 82 ms against 45 ms for one, so `aqm pipeline` imports and calls the stages rather than shelling out to them, and the CLI verbs exist for inspection and backtests (`../01_ingestion/DESIGN.md` §8).

**The trap, twice now.** Both stages' first implementation was dominated by reading more of a CSV than it needed: ingestion type-converted a whole tail to recover one integer, and this stage read the *entire* meter file on every call because it passed a resolved `now` into `meter_now()` unconditionally, which silenced the tail fast path — 24,140 cells converted per tick, 5.0 ms instead of 2.2 ms. The fix is the sufficiency check ingestion already uses: tail first, and read the whole file only when the tail provably does not reach back far enough, which happens only for a `--at` backtest weeks in the past.

**`extra_quota_to_spend_units = 0` is the normal case and the tick ends there**, which is what makes the common path free: the pipeline stops before planning and before spawning anything (`../07_pipeline/DESIGN.md` §2). On the great majority of the 288 ticks a day, the whole chain of ingest → predict → budget is the entire cost of running this system.

## 7. Open points

- **`BOT_BURN_UNITS_PER_HOUR` is assumed, not measured.** Step 2's `max_spend_units_by_time` uses 1.0 unit/h because no worker has ever run (`../07_pipeline/DESIGN.md` §11). At that value the term binds only below 54 minutes, so an error in it is invisible most of the time and decisive in the last hour before a reset — where all the spending happens. **P4 must replace it with the measured median from `state/burnrate.jsonl`** (`../06_execution/DESIGN.md`), and until it does, `already_lost_units` in the last window is the figure to distrust. A rate set too high costs an underspend, never a collision, which is why the placeholder is tolerable.
- **~~`MIN_HUMAN_RESERVE_PCT` is smaller than `GUARD_PCT`~~ — settled on 2026-10-05.** It was 10% against a `GUARD_PCT` of 15%, so the budget planned a window up to 90% used while the executor terminated workers above 85% used: the budget's own target lay inside the region where the guard kills the work doing it, and the last 5 points of every full ceiling were unspendable by construction. Raising `MIN_HUMAN_RESERVE_PCT` to **25%** on independent evidence (`../02_prediction/DESIGN.md` §8 — it is the only protection the user has on 73.6% of ticks) satisfies `MIN_HUMAN_RESERVE_PCT >= GUARD_PCT` and the contradiction is gone. The better long-run answer is still for the guard to fire on *predicted human* room rather than total room, and that belongs with P4.
- **`WEEK_CAPACITY_UNITS` is a measured ratio, not a published one** (8.85 and 6.2, `../CLAUDE_AND_CODEX.md` §1). Both come from five weeks of one account, and Codex's is the weaker of the two. An error here scales `week_left_units` and therefore `already_lost_units` directly, so the figure to watch in P6 is whether the weekly meter actually reaches 100% when `week_left_units` reaches 0.
- ~~Prediction staleness is not checked here.~~ **Resolved 2026-10-04:** it had to be, because the pipeline writes the prediction itself and so can never catch a stale one. The check lives in this stage, on the auto-resolved latest only (§2); an explicit `--prediction PATH` is taken as meant.
