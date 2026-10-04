# 02 — Prediction (`aqm predict`)

Stage 2 answers one question per agent: **how much organic demand is still coming before the current 5-hour window ends?** It answers with a quantile rather than a flag, so the rest of the system is written against a real forecast from day one and needs no change when the forecast gets better.

| | |
|---|---|
| Command | `aqm predict [--at <iso>] [--out PATH]` |
| Reads | `state/buckets.jsonl` (`../01_ingestion/DESIGN.md` §6.2) |
| Writes | `artifacts/predictions/<date>/<time>.json` |
| Acts | No |
| Consumed by | Budgeting (`../03_budgeting/DESIGN.md` §2, the margin) and planning (`../05_planning/DESIGN.md` §2, the room clamp) |
| **Runtime** | **Milliseconds**, every tick (§7) |

Today's implementation is deliberately trivial (§2). The eight candidate forecasting methods, their drawbacks and how to evaluate them are in `PREVIOUS_IDEAS.md`; nothing there is built yet.

## 1. Output

One number is consumed by the rest of the system: the p95 of organic demand between now and the end of the current 5-hour window. Everything else in the file is there to explain that number to a human.

```json
{"computed_at": 1790170589, "method": "recent-rate-v1", "config_hash": "9f2c…",
 "agents": {
   "claude": {"window": {"end": 1790172600, "p95_pct": 42.0},
              "observed": {"organic_pct_per_min": 0.21, "last_organic_bucket": 1790170500,
                           "n_readings": 3, "signal": "S1"}},
   "codex":  {"window": {"end": null, "p95_pct": 0.0},
              "observed": {"organic_pct_per_min": 0.0, "last_organic_bucket": 1789561500,
                           "n_readings": 0, "signal": null}}}}
```

`p95_pct` is organic **demand in meter percent of the 5-hour window** — what the user would spend with no cap (`PREVIOUS_IDEAS.md` §1) — between `computed_at` and `window.end`.

**It is in percent, not dollars or units, because percent is the only unit a limit is enforced against** (`../CONSIDERATIONS.md` §5). The rate is read from the `organic_pct` column ingestion already computed (`../01_ingestion/DESIGN.md` §6.2); dollars appear in this stage only as a diagnostic, never in `p95_pct`. `n_readings` is carried through because the two agents are not observed equally well — Codex yields about 5 meter readings a day against Claude's 23 **[verified]** — so a consumer can tell a confident zero from an unobserved one.

A better predictor may add a `chunks` array (`[{start, end, p95}, …]`) covering future windows, which would let the budget stage tighten the ceiling of chunks where the user is likely to work (`../03_budgeting/DESIGN.md` §2). Consumers fall back to `MIN_MARGIN` for any chunk the file does not cover, so adding it changes nothing else.

## 2. Today's method (`recent-rate-v1`)

The measured recent past, extrapolated, with nothing learned:

```text
rate     = spend_rate(agent, RATE_WINDOW, organic)           # percent of the 5-hour window per minute
minutes  = min(minutes_to(window.end), PERSISTENCE_HOURS × 60)
p95_pct  = rate × minutes × PERSISTENCE_P95
```

`spend_rate` sums the `organic_pct` of the buckets in `RATE_WINDOW` and divides by their span — so the whole method is one sum over a few rows. Buckets whose `attribution` is `censored` contribute their `organic_pct_hi`, not a point value: the conservative direction is the one that predicts *more* organic demand and so leaves us less room (`../01_ingestion/DESIGN.md` §1.5).

It has one deliberate blind spot: **an idle user who is about to start** is predicted at ≈ 0. What protects that case today is not the forecast but `MIN_MARGIN` — the room the budget stage refuses to fill in any window. When a real predictor lands, that margin can shrink towards zero and the protection moves into the forecast where it belongs.

## 3. Signals behind the rate

| #   | Signal                                                              | Latency                               | Blind spot                                                     |
| --- | ------------------------------------------------------------------- | ------------------------------------- | -------------------------------------------------------------- |
| S1  | `organic_pct` summed over the last `RATE_WINDOW` (`../01_ingestion/DESIGN.md` §6.2) | Up to one bucket, and the meter quantises at 1% | Local and remote usage both, but only once the meter moves |
| S2  | `touched` — the mtime of a session file we did not create moved | Seconds | Local sessions only; says *that* the user is active, not how much |
| S3  | Claude telemetry rows with a non-`sdk` `query_source` in the current bucket | Seconds | Claude only, and only while the receiver is up |

**S1 is the measurement; S2 and S3 exist because S1 is late.** The meter moves in 1% steps, so a user who has just started shows nothing for a minute or two, while a file mtime moves immediately. When S2 or S3 fires, the rate is raised to at least `TRIPWIRE_RATE` — a floor, not an estimate, since neither signal carries a magnitude.

The statusline heartbeat is **not** used: it keeps ticking while idle sessions stay open overnight, so it would predict activity every night. S2 is the heartbeat's honest version, because a session file is appended to only when something actually happens in it.

## 4. Telling our own work apart

Classification is done by ingestion (`../01_ingestion/DESIGN.md` §6) and this stage only consumes `kind`. The two mechanisms:

| Agent  | Mechanism                                                                                                                                            |
| ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| Claude | Three marks, agreeing: the executor records the run interval **before** launching; our workers' telemetry rows carry `query_source: "sdk"`; and a headless run writes **no** status-line push row at all **[verified 2026-10-04]** (`../01_ingestion/DESIGN.md` §1.3) |
| Codex  | `codex exec -C <scratch dir>` **[verified]**; `session_meta` carries that `cwd`, and the executor records the rollout file that appears. No telemetry and no USD on any channel, so Codex has only the interval mark |

The Claude marks matter because they are **independent of our own bookkeeping**: a worker that dies before reporting still leaves its requests tagged `sdk` in the recorded data. Codex has no such cross-check, which is one more reason its attribution stays at the interval level.

Anything unclassifiable counts as organic — the conservative direction, which makes the system step aside rather than spend.

## 5. Failure modes

| Case                                                | Behaviour                                                                           |
| --------------------------------------------------- | ----------------------------------------------------------------------------------- |
| Sources unreadable                                  | `p95` is set to the full window, so nothing runs (fail safe)                        |
| The user works on another machine or in Codex cloud | Invisible locally; S3 catches it, and the meter movement shrinks the surplus anyway |
| A session is open but idle                          | No spend, so no predicted demand                                                    |
| The telemetry receiver is down                      | S3 goes quiet. S1 and S2 are unaffected, so the floor is lost but the measurement is not |
| Codex is active, so its poller is skipping          | The feed is thinnest exactly when demand is highest (`../01_ingestion/DESIGN.md` §5). `n_readings` falls, and a consumer must read a low count as *unobserved*, not as idle |
| Clock jumps backwards                               | Negative ages clamp to 0                                                            |
| A huge single message just landed                   | The rate stays high for `RATE_WINDOW`, which is the intent                          |

## 6. Backtesting and replacement

`--at <iso>` recomputes as of a past moment, reading only data that existed then; with `--at` the stage prints its result and writes a file only when `--out` is given, so a backtest never touches live state (`../07_pipeline/DESIGN.md` §1.2).

A real predictor is a drop-in: it writes the same artifact as §1, optionally with per-chunk values. The trigger for building one is P6 showing that `MIN_MARGIN` and static quiet hours are what limit the result (`../DESIGN_v2.md` §5). Calibration is measured continuously — the share of windows where actual organic demand exceeded the predicted p95 should be ≤ 5% (`../07_pipeline/DESIGN.md` §8).

**Codex has an independent ground truth, and it is the only sub-percent number in the system.** `agent-usage-tracker` fetches ChatGPT's `plan_limit_history` daily: the final usage of each *finished* window in basis points, with the window's real start and end (`USAGE_DATA_REFERENCE.md` §3.2). It is useless live — it lags to the start of the UTC day — but for a backtest it is exactly what is missing, because it scores two things at once:

| What it scores | Against |
|---|---|
| Whether the predictor was right | The window's true final usage, at 0.01% instead of 1% |
| Whether **ingestion's reconstruction** was right | The same number, rebuilt from 1%-quantised readings on a feed that skips while sessions are active |

The second is the more valuable, and it has no Claude equivalent. Only 4 fetches exist so far **[verified 2026-10-04]**, so this is a method to build when the history is deep enough, not a check to run now.

## 7. Speed

Like ingestion, this stage runs on **every** tick whether or not anything is due, so it is built to cost nothing: **a tail read of a few dozen lines and a 1 KB write, in milliseconds.**

| Work | Cost |
| --- | --- |
| `spend_rate(agent, RATE_WINDOW, organic)` per agent | Tail `state/buckets.jsonl` far enough back to cover `RATE_WINDOW` — at 2 rows per tick, a 30-minute window is **12 lines** |
| `window_state(agent)` per agent | The last row for that agent, from the same tail |
| The formula of §2 | Two multiplications |
| Write the artifact and swap the `latest` symlink | ~1 KB, atomic |

One tail read serves both calls, because ingestion appends to `buckets.jsonl` in time order and has just written this tick's rows. No index is needed and none exists: the file is JSONL precisely so that "the last N minutes" is a seek to the end (`../01_ingestion/DESIGN.md` §7).

No network, no subprocess, no model call — the word "prediction" here means arithmetic on rows already in `state/buckets.jsonl`, nothing more. The stage never re-reads a raw source; ingestion has already done that.

**This is a constraint on any future predictor, not just on today's.** A replacement may fit whatever it likes offline, but the per-tick path must stay in the same budget: load parameters, evaluate, write. Anything that needs to scan the full history, or to re-fit at every tick, belongs in a separate offline job that writes its parameters to a file this stage reads. The candidate methods of `PREVIOUS_IDEAS.md` are all cheap to evaluate once fitted, which is why the shape of §1 does not change.
