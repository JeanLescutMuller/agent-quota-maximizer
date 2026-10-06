# 02 — Prediction (`aqm predict`)

> **Parked on 2026-10-06 — kept, not deleted.** The design no longer forecasts: a fixed rule protects the user instead (`../DESIGN_v2.md` §8.2), because replaying the whole decision showed the forecast changes no outcome while the bot burns at 0.5 unit/h or more. This stage comes back only if the bot turns out slower than that (§9). Everything below describes the stage as built, and the code still runs it until P2b removes it from the tick.

Stage 2 answers one question per agent: **how much human demand is still coming before the current 5-hour window ends?** It answers with a quantile rather than a flag, so the rest of the system is written against a real forecast from day one and needs no change when the forecast gets better.

| | |
|---|---|
| Command | `aqm predict [--at <iso>] [--out PATH]` |
| Reads | `data/<agent>/slots.csv` (`../01_ingestion/DESIGN.md` §6.2) |
| Writes | `artifacts/predictions/<date>/<time>.json` |
| Acts | No |
| Consumed by | Budgeting (`../03_budgeting/DESIGN.md` §2, the margin) and planning (`../05_planning/DESIGN.md` §2, the room clamp) |
| **Runtime** | **Milliseconds**, every tick (§7) |

**Built 2026-10-04**; 27 assertions in `../../test/test_predict.sh`. Today's implementation is deliberately trivial (§2). The eight candidate forecasting methods, their drawbacks and how to evaluate them are in `PREVIOUS_IDEAS.md`; nothing there is built yet.

## 1. Output

The file has **two halves, and only the first is an interface** (`../VOCABULARY.md` §4). The contract is three fields, engine-independent; everything this engine computed on the way there sits under `internals`, where no other stage may read it. A later engine — a model, say — will not have a 30-minute mean to report, and `based_on` is vocabulary only `recent-rate-v1` can speak.

```json
{"computed_ts": 1790170589, "method": "recent-rate-v1", "config_hash": "9f2c…",
 "agents": {
   "claude": {"window_end_ts": 1790172600,
              "predicted_p95_human_usage_pct": 42.0,
              "internals": {
                "based_on": "last_30_min",
                "effective_burn_rate": 0.21,
                "last_human_slot_ts": 1790170500,
                "last_30_min": {"human_avg_burn_rate": 0.21, "slot_n_readings": 3},
                "current_window": {"is_window_open": true, "start_ts": 1790154600,
                                   "window_age_minutes": 266, "remaining_minutes": 34,
                                   "window_used_pct": 31, "human_avg_burn_rate": 0.19}}},
   "codex":  {"window_end_ts": null,
              "predicted_p95_human_usage_pct": 0.0,
              "internals": {"based_on": "nothing", "effective_burn_rate": 0.0,
                            "last_human_slot_ts": 1789561500,
                            "last_30_min": {"human_avg_burn_rate": 0.0, "slot_n_readings": 0},
                            "current_window": {"is_window_open": false}}}}}
```

### 1.1 The contract

**The stage answers exactly one question: how much of the 5-hour meter will the human burn between now and the end of the current window.** Cumulative, to the window's end, always. There is no horizon parameter, no cap and no special case — if no window is open, the answer is for a projected one starting now.

| Field | Why a downstream stage needs it |
|---|---|
| `predicted_p95_human_usage_pct` | the answer, in percent of the 5-hour meter, clamped to 100 |
| `window_end_ts` | which window the answer is *about*, so budgeting can refuse one for a window that has since reset |

**What the stage predicts and how it predicts it are separate concerns.** The specification above is fixed; the choice of estimator — which rate, over what history, damped how — is the engine's, lives in §2, and is reported only in `internals`. An engine that is poor over a long remaining window is an accuracy problem to fix in §2, never a reason to shrink what the contract promises.

`budget` reads those and nothing else, and `test_predict.sh` proves it: blanking `internals` must leave the decision byte-identical.

`predicted_p95_human_usage_pct` is human **demand in meter percent of the 5-hour window** — what the user would spend with no cap (`PREVIOUS_IDEAS.md` §1) — between `computed_ts` and `window_end_ts`. It is clamped to 100: a forecast is a share of one window, and a rate extrapolated past that says nothing more than "all of it".

`window_end_ts` is `null` when no window is open, and the answer then covers a projected 5-hour window starting now. `internals.based_on` records which rule answered: `last_30_min`, `current_window`, `human_active_floor`, `nothing` when the agent is genuinely idle, or `unreadable` when the source could not be read. **`unreadable` is the one value a consumer must not read as calm**, and it comes with `predicted_p95_human_usage_pct: 100.0` so that reading it wrongly still cannot spend anything.

**It is in percent, not dollars or units, because percent is the only unit a limit is enforced against** (`../CONSIDERATIONS.md` §5). The rate is read from the `slot_window_human_pct` column ingestion already computed (`../01_ingestion/DESIGN.md` §6.2); dollars appear in this stage only as a diagnostic, never in `predicted_p95_human_usage_pct`. `internals.last_30_min.slot_n_readings` is carried through because the two agents are not observed equally well — Codex yields about 5 meter readings a day against Claude's 23 **[verified]** — so a reader can tell a confident zero from an unobserved one. It counts reads of the **account-wide gauge**, not anyone's usage, which is why it carries no `human_`/`bot_` part (`../VOCABULARY.md` §3).

A better predictor may add a `remaining_windows` array (`[{start, end, p95}, …]`) covering future windows, which would let the budget stage tighten the ceiling of remaining windows where the user is likely to work (`../03_budgeting/DESIGN.md` §2). Consumers fall back to `MIN_HUMAN_RESERVE_PCT` for any window the file does not cover, so adding it changes nothing else.

## 2. Today's method (`recent-rate-v1`)

The measured recent past, extrapolated, with nothing learned:

```text
recent   = human movement over the last BURN_LOOKBACK_MINUTES        # percent of the 5-hour window per minute
window   = human movement over the whole open window / max(minutes open, BURN_LOOKBACK_MINUTES)
rate     = max(recent, window)                               # then floored by ACTIVE_HUMAN_BURN_RATE if the active-human floor fired
minutes  = minutes_to(window_end_ts)                          # always, no cap
predicted_p95_human_usage_pct  = rate × minutes × SAFETY_MULTIPLIER                  # clamped to 100
```

**Two measurements of the same thing, combined with `max`, because each one's blind spot is the other's strength.** The recent mean alone was the first implementation, and it has a failure that is not subtle — measured on 2026-10-04:

| Moment | Meter | Recent mean | p95 said | What actually happened |
|---|---|---|---|---|
| 11:50 | **31%** | 0.00 %/min | **0%, `based_on: nothing`** | the user burned the remaining **69%** within two hours |
| 12:00 | **31%** | 0.00 %/min | **0%, `based_on: nothing`** | " |

A twenty-minute pause — reading, thinking, waiting on a test — emptied a 30-minute mean, and the stage reported the window as idle and therefore available. **That is the precise shape of blocking the user**, and no multiplier on the rate would have fixed it, because the rate itself was zero.

A window's own average cannot say that: while a window is filling it is never zero. At those two moments it read 0.207 and 0.194 %/min, giving 37% and 35% — a sane forecast. The recent mean is kept because it is the one that reacts within a single slot to a burst, which the window average damps.

**The window average is divided by `max(window_age_minutes, MIN_RATE_DENOMINATOR_MINUTES)`, not by `max(…, BURN_LOOKBACK_MINUTES)`.** Flooring the divisor at 30 minutes halved the measured pace of every younger window: 10% burned in a 15-minute-old window read 0.33 %/min against a true 0.67, and understating the pace understates the reserve, which is the direction that costs the user their quota. A 10-minute floor still stops a 2-minute-old window reading 0.5 %/min off its first quantised percent — it reports 0.1. `max` takes whichever is currently saying more, and `internals.based_on` records which.

Dividing by at least `BURN_LOOKBACK_MINUTES` is what stops a two-minute-old window reading its first percent as 0.5 %/min.

**It is human movement in both cases, not `window_used_pct`.** Using the raw meter level would feed our own spend back in as predicted user demand — spend, predict the spend as demand, then refuse to spend — which is the one loop this system must never close.

`burn_rate` sums the `slot_window_human_pct` of the slots in `BURN_LOOKBACK_MINUTES` and divides by their span — so the whole method is one sum over a few rows. Slots whose `attribution` is `censored` contribute their `slot_window_human_pct_hi`, not a point value: the conservative direction is the one that predicts *more* human demand and so leaves us less room (`../01_ingestion/DESIGN.md` §1.5).

It has one deliberate blind spot left: **an idle user who is about to start**, in a window that has not been used yet, is predicted at ≈ 0. What protects that case today is not the forecast but `MIN_HUMAN_RESERVE_PCT` — the room the budget stage refuses to fill in any window. When a real predictor lands, that margin can shrink towards zero and the protection moves into the forecast where it belongs.

## 3. Signals behind the rate

| #   | Signal                                                              | Latency                               | Blind spot                                                     |
| --- | ------------------------------------------------------------------- | ------------------------------------- | -------------------------------------------------------------- |
| `last_30_min` | `slot_window_human_pct` summed over the last `BURN_LOOKBACK_MINUTES` (`../01_ingestion/DESIGN.md` §6.2) | Up to one slot, and the meter quantises at 1% | Reads an ordinary pause as an absence — see §2 |
| `current_window` | the same, over the whole open window | Up to one slot | Says nothing about a window that has not been used yet, and lags a burst |
| `human_active_floor` | `was_human_active` — a confirmed request in a session that is not ours, from `../01_ingestion/DESIGN.md` §3.1 | Seconds | Says *that* the user is active, not how much |
| telemetry (unwired) | Claude telemetry rows with a non-`sdk` `query_source` in the current slot | Seconds | Claude only, and only while the receiver is up |

**The two burn rates are the measurement; the active-human floor exists because they are late.** The meter moves in 1% steps, so a user who has just started shows nothing for a minute or two. When the floor fires, the rate is raised to at least `ACTIVE_HUMAN_BURN_RATE` — a floor, not an estimate, since neither signal carries a magnitude. `ACTIVE_HUMAN_BURN_RATE` is in **percent of a window per minute**, like everything else this stage computes in (`../07_pipeline/DESIGN.md` §11).

**The floor only works because `was_human_active` is counted before the drop_stale_readings, and that had to be fixed to make it work at all.** The drop_stale_readings's job is to drop readings carrying no new percentage — which is exactly what a reading from a freshly-started user looks like. Counting `was_human_active` after it deleted the one case the floor exists for, silently: the signal was present in the data, dropped in ingestion, and the stage read a legitimate-looking zero. `was_human_active` is now taken from the raw rows (`../01_ingestion/DESIGN.md` §6.2).

**The telemetry floor is designed but not wired.** Ingestion does not read the telemetry rows yet, so today the floor is raised by `was_human_active` alone. The consequence is Claude-specific and bounded: a user active in a *remote* session that pushes no status-line row is invisible until the meter moves. `internals.based_on` names which one fired, so the artifact never hides it.

**Neither a heartbeat nor a file mtime is used, and both for the same reason:** they move without anyone doing anything. The statusline heartbeat ticks while idle sessions stay open overnight; transcript mtimes move in bursts with no new content at all (`../CONSIDERATIONS.md` §5). A signal that can fire on an idle machine would predict demand every night, which is the one conclusion that costs us the whole window.

## 4. Telling our own work apart

Classification is done by ingestion (`../01_ingestion/DESIGN.md` §6) and this stage only consumes `kind`. The two mechanisms:

| Agent  | Mechanism                                                                                                                                            |
| ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| Claude | Three marks, agreeing: the executor records the run interval **before** launching; our workers' telemetry rows carry `query_source: "sdk"`; and a headless run writes **no** status-line push row at all **[verified 2026-10-04]** (`../01_ingestion/DESIGN.md` §1.3) |
| Codex  | `codex exec -C <scratch dir>` **[verified]**; `session_meta` carries that `cwd`, and the executor records the rollout file that appears. No telemetry and no USD on any channel, so Codex has only the interval mark |

The Claude marks matter because they are **independent of our own bookkeeping**: a worker that dies before reporting still leaves its requests tagged `sdk` in the recorded data. Codex has no such cross-check, which is one more reason its attribution stays at the interval level.

Anything unclassifiable counts as human — the conservative direction, which makes the system step aside rather than spend.

## 5. Failure modes

| Case                                                | Behaviour                                                                           |
| --------------------------------------------------- | ----------------------------------------------------------------------------------- |
| Sources unreadable                                  | `p95` is set to the full window, so nothing runs (fail safe)                        |
| The user works on another machine or in Codex cloud | Invisible locally; the telemetry floor catches it, and the meter movement shrinks the surplus anyway |
| A session is open but idle                          | No spend, so no predicted demand                                                    |
| The telemetry receiver is down                      | the telemetry floor goes quiet. `last_30_min` and the active-human floor are unaffected, so the floor is lost but the measurement is not |
| Codex is active, so its poller is skipping          | The feed is thinnest exactly when demand is highest (`../01_ingestion/DESIGN.md` §5). `slot_n_readings` falls, and a consumer must read a low count as *unobserved*, not as idle |
| Clock jumps backwards                               | Negative ages clamp to 0                                                            |
| A huge single message just landed                   | The rate stays high for `BURN_LOOKBACK_MINUTES`, which is the intent                          |

## 6. Backtesting and replacement

`--at <iso>` recomputes as of a past moment, reading only data that existed then; with `--at` the stage prints its result and writes a file only when `--out` is given, so a backtest never touches live state (`../07_pipeline/DESIGN.md` §1.2).

A real predictor is a drop-in: it writes the same artifact as §1, optionally with per-window values. The trigger for building one is P6 showing that `MIN_HUMAN_RESERVE_PCT` and static quiet hours are what limit the result (`../DESIGN_v2.md` §5). Calibration is measured continuously — the share of windows where actual human demand exceeded the predicted p95 should be ≤ 5% (`../07_pipeline/DESIGN.md` §8).

**Codex has an independent ground truth, and it is the only sub-percent number in the system.** `agent-usage-tracker` fetches ChatGPT's `plan_limit_history` daily: the final usage of each *finished* window in basis points, with the window's real start and end (`USAGE_DATA_REFERENCE.md` §3.2). It is useless live — it lags to the start of the UTC day — but for a backtest it is exactly what is missing, because it scores two things at once:

| What it scores | Against |
|---|---|
| Whether the predictor was right | The window's true final usage, at 0.01% instead of 1% |
| Whether **ingestion's reconstruction** was right | The same number, rebuilt from 1%-quantised readings on a feed that skips while sessions are active |

The second is the more valuable, and it has no Claude equivalent. Only 4 fetches exist so far **[verified 2026-10-04]**, so this is a method to build when the history is deep enough, not a check to run now.

## 7. Speed

Like ingestion, this stage runs on **every** tick whether or not anything is due, so it is built to cost nothing: **a tail read of a few dozen lines and a 1 KB write, in milliseconds.**

| Work | Cost **[measured 2026-10-04]** |
| --- | --- |
| The recent slots and the latest reading, per agent | **0.78 ms** for both agents, and it is all of this |
| The formula of §2 | Two multiplications — too small to measure |
| Write the artifact and swap the `latest` symlink | 0.56 ms, ~1 KB, atomic |

One tail read serves both calls, because ingestion appends to `slots.csv` in time order and has just written this tick's rows. No index is needed and none exists: the file is a flat append-only text table precisely so that "the last N minutes" is a seek to the end (`../01_ingestion/DESIGN.md` §7).

**The tail must be sized for the question, and getting that wrong made this the most expensive stage in the pipeline.** Reading the default 64 KB tail typed ~300 slot rows — about 6,600 cell conversions — to find the **six** rows a 30-minute `BURN_LOOKBACK_MINUTES` actually covers, and cost **10.9 ms**, more than ingestion. A 4 KB start covers ~21 rows (105 minutes), needs zero doublings in the normal case, and widens by doubling when it does not reach back far enough. That one change took the stage from 10.9 ms to 0.78 ms, and the whole pipeline from 29 ms of work to 7.3 ms.

This is the **third** time in this codebase that reading more CSV than the answer needed turned out to be the dominant cost, after the two in `../01_ingestion/DESIGN.md` §8. The standing rule: a query that wants the recent past starts from a small tail and doubles, and never from the tail sized for ingestion's watermark.

No network, no subprocess, no model call — the word "prediction" here means arithmetic on rows already in `data/<agent>/slots.csv`, nothing more. The stage never re-reads a raw source; ingestion has already done that.

**This is a constraint on any future predictor, not just on today's.** A replacement may fit whatever it likes offline, but the per-tick path must stay in the same budget: load parameters, evaluate, write. Anything that needs to scan the full history, or to re-fit at every tick, belongs in a separate offline job that writes its parameters to a file this stage reads. The candidate methods of `PREVIOUS_IDEAS.md` are all cheap to evaluate once fitted, which is why the shape of §1 does not change.

## 8. What the forecasting study established (2026-10-05)

Five candidate engines were built and compared on 42 days of recorded history, one
notebook each in `../../lab/02_prediction/`, all measured on the same rows, the same three-way split
and the same objective (`../../lab/02_prediction/README.md`). **The conclusion was to change nothing in
this stage** — and that is a result, not an absence of one.

### The objective was money, which derives the quantile

Two unit costs per percent of a 5-hour meter: `wasted` **$0.32** (exactly 1% of a $32
unit) and `stolen` **$3.20**, ten times worse because the user has first claim. The
cost-minimising reserve is then the newsvendor quantile

    q = C_stolen / (C_stolen + C_wasted) = 0.909

so nobody has to choose 0.95 and hope. It was confirmed empirically: the cheapest
constant reserve equals the measured q90.9 of demand at every horizon, to the point.

### The ranking, on the held-out test period

| Approach | cost | vs today |
|---|---|---|
| empirical quantile per (state x clock band) | 6.96 | −1.1% |
| **this stage, unchanged** | **7.03** | — |
| gradient boosting, `loss="quantile"` | 7.06 | +0.4% |
| hierarchical Gaussian (ridge + residual spread) | 7.52 | +6.9% |
| best constant reserve | 9.62 | +36.8% |
| **do nothing — reserve the whole window** | **27.76** | **+295%** |

**The four real approaches sit within 1.5% of each other, and all of them beat doing
nothing by about 4x.** Going from "no project" to *any* forecaster captures three
quarters of the available value; choosing between forecasters moves one and a half
percent. The ranking also inverts with the cost ratio — gradient boosting wins at 1:1
and 3:1, the lookup table at 10:1, this stage at 30:1, and **doing nothing wins at
100:1** — so the price assumption decides the winner, not the modelling.

### Why the notebook's constants were not shipped

`01_baseline_recent_rate.ipynb` tunes a *simplification* of this stage: one look-back,
extrapolated, times a multiplier. It preferred `multiplier = 5.0`. But this stage takes
`max(recent rate, window-average rate)` and then the active-human floor, so its rate is
already the larger of two arms. `../../lab/02_prediction/live_replay.py` calls `predict_agent()` itself over
the same test period, with `aqm.P` patched, and found:

| | cost | vs today |
|---|---|---|
| **`BURN_LOOKBACK_MINUTES` 30, `SAFETY_MULTIPLIER` 1.5 — today** | **16.553** | **best** |
| 30, 2.0 | 16.640 | +0.5% |
| 60, 2.0 | 16.666 | +0.7% |
| 30, 3.0 | 16.978 | +2.6% |
| 30, 5.0 — the notebook's pick | 17.654 | **+6.7%** |

Both constants are therefore **measured optima, not guesses**, and raising either makes
this engine worse. Any future tuning of them has to go through `live_replay.py`, because
a bench that omits the window arm will keep asking for a bigger multiplier.

### The one number the study did move

The replay also showed where the real error is: **this stage predicts exactly 0 on
73.6% of ticks**, and on those the human's demand to the window's end has a 90.9th
percentile of **26%**. On three ticks in four the forecast contributes nothing and
`MIN_HUMAN_RESERVE_PCT` is the user's only protection — and it was 10. Sweeping it with
the waste baseline held fixed gives a clean interior optimum at **25%**, 9.1% cheaper,
which is why that parameter changed and nothing here did
(`../07_pipeline/DESIGN.md` §11).

A scalar multiplier could not have fixed this: the stage's error is in its *shape*, not
its level. It predicts 0 in every idle cell (where measured demand reaches 87%) and
clamps to 100 in the active ones, so scaling the rate moves only the few cases in
between. That is also why none of the learned models helped much — they correct the
shape a little, and the shape error lives in the idle cells.

### What would actually help, and why it was not built

The dominant error is **non-stationarity**, not model class. Every engine learned
"idle means quiet" from August and September; in the held-out fortnight `idle`/morning
demand had a p95 of 87%. 42 days of one person is a single sample of a year, cells run
from n=18 to n=763, and the test set is now spent. The bench is checked in so that
re-running it on a fresh test period costs one command — that is the next step here,
not a sixth model.

## 9. Parked on 2026-10-06, and what would bring it back

**Why it is parked.** The study of §8 scored forecasters on their own, against every tick. Scoring the whole decision instead — budget, a just-in-time start, a bot burning quota, replayed over the 42 recorded days (`../../lab/07_pipeline/pipeline_replay.py`) — showed where a forecast can matter at all:

| | Claude | Codex |
|---|---|---|
| Ticks where the week forces the bot to spend in the current window | 24% | 21% |
| …of which the forecast lowers the amount | **3.1%** of all ticks | **0.1%** |
| Outcome at a bot burn rate of 0.5 unit/h or more, forecast against the rule of `../DESIGN_v2.md` §8.2 | the same: no collision either way | the rule does better (0 collisions against 1 at 1 unit/h) |

The reason is the just-in-time start. The bot only spends in the last hour or two of a window, and in the last hour the user exceeds the 25% reserve in 2% of cases when idle and 19% when active (`../CONSIDERATIONS.md` §21) — and the rule does not start while they are active. The forecast's job is already done by the start time, the reserve, and that observation.

**What would bring it back: a slow bot.** The rule's protection shrinks as the bot's lead time grows. At 0.25 unit/h, 0.75 unit takes about 4½ hours to burn, and the replay gives 3 collisions and 5.4 hours of waiting on Claude without a forecast, against 2 and 1.1 hours with today's (`../DESIGN_v2.md` §8.2). So:

```
P4 measures the bot's burn rate, per agent, with 1, 2 and 3 parallel workers
   >= 0.5 unit/h   the rule stands; this stage stays parked
   <  0.5 unit/h   more workers first; if still below, this stage comes back,
                   with the same contract (§1) and the bench of §8 to choose its engine
```

What a returning forecast would need to predict is no longer "the rest of the current window from now", but the user's demand **over the bot's lead time**, starting from a moment when they are idle — the case where the study of §8 found today's engine weakest (`idle`/morning).

**What stays.** The bench (`../../lab/02_prediction/`, its dataset and its notebooks), `aqm predict` as a command for analysis, and this document. What leaves the tick: the prediction artifact, its staleness check in budget, and the five parameters listed in `../07_pipeline/DESIGN.md` §11.

