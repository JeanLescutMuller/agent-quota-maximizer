# 01 — Ingestion (`aqm ingest`)

Stage 1 of the pipeline. It turns the raw quota feeds into **one tidy 5-minute table per agent**, updated incrementally at every tick. It is the whole system's interface to reality: no later stage reads a raw file.

| | |
|---|---|
| Command | `aqm ingest [--backfill]` |
| Reads | The two quota logs, plus file **mtimes** for a liveness tripwire (§3) |
| Writes | `state/meter-<agent>.jsonl`, `state/buckets.jsonl` |
| Acts | No |
| Consumed by | Every later stage, through the three functions of §2 |
| **Runtime** | **Well under a second**, every tick, whatever the history holds (§8) |

Where each field comes from, in which unit, and every trap in the raw data, is canonical in `~/dev/agent-usage-tracker/USAGE_DATA_SOURCES.md` (what exists upstream) and `~/dev/agent-usage-tracker/USAGE_DATA_REFERENCE.md` (what is captured, and where). This document does not restate them; it says what this stage *does* with it. Overview and stage map: `../DESIGN_v2.md`. Cross-cutting rules: `../07_pipeline/DESIGN.md`.

Facts marked **[verified]** were measured on this machine, on 2026-10-04 where a date is given in place, on 2026-09-29 otherwise.

## 1. Attribution: separating the user's spend from ours

Everything this system decides rests on one question: **how much of the quota the user spent, as opposed to us.** This section is the whole answer.

### 1.1 Per-session percent does not exist, and cannot

A meter row carries `observed_at`, the percentages and the reset times, and **no session identifier** (`USAGE_DATA_SOURCES.md` §2.3). This is not an omission upstream could fix: the meter is one account-level number, so when two sessions spend concurrently there is **no per-session percentage to expose**. Asking for one is asking for a quantity that is not defined.

An earlier version of this design therefore parsed all 156 Claude `.jsonl` transcripts — 517 MB **[verified]** — to price every message and attribute it. That is not necessary, because **we know when our own work ran**: the executor launches it and records the interval.

```text
organic movement = meter movement − movement during our own run intervals
```

| Question | Answered by | Cost |
|---|---|---|
| How much was spent in total? | The meter, after the envelope | ~30 rows/day **[verified]** |
| Was any of it ours? | Our own run intervals, from `state/runs.jsonl` | Free — written anyway |
| Is the user active *right now*? | **mtime** of the `.jsonl` transcripts, never their contents | 0.3 ms for all 222 files **[verified]** |
| What did our work cost in dollars? | One telemetry row per API request, tagged `query_source: "sdk"` (§1.3) | Written by Claude Code itself |

So this stage reads the **two quota logs** and **stats** the session files. It never parses a `.jsonl` transcript.

### 1.2 Three cases, and only one of them is hard

The subtraction above sounds lossy. It mostly is not, because **our runs and the user's work barely overlap.**

| Case | How often | Attribution | `attribution` field |
|---|---|---|---|
| No worker of ours was running | The **vast majority** of the 288 daily buckets | `organic_pct = five_delta` — nothing to subtract | `exact` |
| Our workers ran, no organic signal | Our bursts, ~20 min at a time | `extra_pct = five_delta` | `exact` |
| Our workers ran **and** the user was active | Rare **by design** | §1.4 | `apportioned` or `censored` |

The third row is rare because the whole architecture exists to keep it empty: the late start, the room test, `GUARD_PCT`, and the fact that "blocking incidents" is already **the metric that must stay at zero** (`../07_pipeline/DESIGN.md` §8). Overlap is not a normal case to be modelled — it is an incident already being counted.

**Detecting which case applies needs no estimation.** `workers > 0` comes from our own run records; organic activity during the interval comes from the liveness tripwire (§3) and from any organic session's own push rows. Both are facts, not inferences.

### 1.3 Our own share is measurable exactly, not estimated

In the clean case the executor does not have to infer its spend. It reads the meter immediately **before** launching its first worker and immediately **after** the last one exits; with no organic activity in between, that delta **is** our spend, in percent, measured. `spent_so_far` already works this way (`../06_execution/DESIGN.md` §3).

**Since 2026-09-30 the recorded data says which requests were ours, independently of our own bookkeeping.** Claude Code exports one OpenTelemetry event per API request, and `agent-usage-tracker` appends it to that session's file with `source: "claude_otel"` (`USAGE_DATA_REFERENCE.md` §9). Each event carries `cost_usd`, the token split, and a `query_source` that separates the two populations outright:

| `query_source` | Who | Our use for it |
|---|---|---|
| `sdk` | A headless `-p` run — ours | Our dollars, per request, with a timestamp |
| `repl_main_thread`, `generate_session_title`, `prompt_suggestion` | An interactive session — organic | The user's dollars, per request |

Two independent marks therefore distinguish our work from the user's, and they agree: a headless run renders no status line, so it writes **no push row at all**, and its telemetry says `sdk`. Both were confirmed on the two headless runs in the recorded data — one telemetry row each, zero push rows **[verified 2026-10-04]**. This retires the assumption the earlier design rested on.

The value of this is not that it replaces the meter — it cannot, see §1.1 — but that **our dollar figure no longer depends on us reporting it honestly**. A worker that crashes before returning its result object still leaves its requests in the telemetry. It also calibrates the fallback of §1.4: over any clean interval both the list-price dollars and the meter delta are known, so their ratio is observable rather than assumed.

**Telemetry is a measurement, not an authority.** Claude Code buffers no events on disk, so a receiver that was down loses them silently, and sessions started before the telemetry keys were set send none (`USAGE_DATA_REFERENCE.md` §9). A missing telemetry row therefore means "unknown", never "no spend" — which is why percent still comes only from the meter, and why a bucket with no usable dollar figure is marked `censored` rather than `exact` (§1.4).

### 1.4 When they overlap: apportion by measured dollars, and keep the bounds

An overlapping interval's meter delta cannot be split by any account-level reading. But both parties' **dollar** spend over that interval is separately measurable:

| Party | Source of its USD | Resolution |
|---|---|---|
| Us | Telemetry rows with `query_source: "sdk"`, summed over the interval; the worker's own `total_cost_usd` as a cross-check | **Per request**, exactly timestamped |
| The user | Telemetry rows with any other `query_source`; failing that, `session_cost_usd` deltas on push rows, which are **organic-only by construction** since headless runs write none **[verified]** | Per request, or per render |

Both figures are read from the same files, by `query_source`, so the split needs no join against our own records. Per-request granularity also means an interval boundary no longer has to fall between two renders: a bucket's share is the sum of the requests whose event time falls inside it.

So the delta is apportioned by measured share rather than guessed:

```text
organic_pct ≈ five_delta × organic_usd / (organic_usd + our_usd)
```

**The residual error is in the ratio, not in the attribution.** Dollars are list-price and the meter weights cache reads far below list, so a cache-heavy session and a cache-light one convert differently. That is a bounded error on a rare case, and §1.3's calibration narrows it.

**The bounds are kept regardless.** `organic_pct` in such a bucket is known to lie in `[0, five_delta]`, and the bucket is marked `apportioned`. If either dollar figure is missing, no number is invented: the bucket is marked `censored` and carries the bounds only. That is the same treatment as `window_capped` (§6.3) — **an interval-censored observation, never a fabricated point value.**

### 1.5 Two rules that follow, and must not be broken

- **Do not drop contaminated buckets from a training set.** Our work runs when the user is *absent*, so silently excluding those buckets removes low-organic periods and would inflate predicted demand. Mark them and let the model downweight them.
- **When something forces a single number, pick the direction that makes us spend less.** Ambiguous movement counts as **organic** for the prediction (raising predicted demand, shrinking room) and as **ours** for the executor's running total (`../06_execution/DESIGN.md` §3). Both are the same principle: assume the worse for us.

### 1.6 What this stage needs from upstream

`agent-usage-tracker` owns the feeds (split out of `agent-statusline` on 2026-09-30), and **this layout has been in place since 2026-09-30** (`USAGE_DATA_REFERENCE.md` §1). Two scopes, never mixed in one file:

| Scope | File | Carries | Never carries |
|---|---|---|---|
| **Account** | `data/<agent>/account.jsonl` | Meter percent, reset times, `observed_at`, `source` | Per-session cost or token totals |
| **Session** | `data/claude/<session-id>.jsonl` | Tokens, USD, model, cache statistics, `observed_at`, `source` | **Percent — under any name** |

A session file holds rows from two writers, told apart by `source`: `claude_statusline` (one per render, cumulative session cost) and `claude_otel` (one per API request, that request's cost). Both are useful, and §1.4 prefers the second.

An account row may keep the id of the session that *observed* it, named so it cannot be misread as attribution (`observed_by_session`). That is provenance, and a free liveness signal; it is not a statement about whose usage the reading represents. Since `account.jsonl` shares its directory with generated files, any glob over `data/<agent>/*.jsonl` excludes it explicitly.

**The prohibition is the important half.** A `five_hour_pct` field inside a per-session file would be read by somebody, sooner or later, as "this session's quota usage" — a quantity that does not exist (§1.1). Keeping percent out of session-scoped files makes that error unrepresentable rather than merely discouraged.

Why one file per session rather than session rows interleaved in the account log:

- **The scope boundary becomes structural.** The attribution bug this whole section guards against is a scope confusion; a directory layout that mirrors the scopes prevents it by construction.
- **Our own sessions are identifiable by filename.** The executor generates its worker UUIDs, so it knows exactly which files are ours without parsing anything.
- **A session's history is self-contained**, so reading one session costs one small file rather than a scan of an interleaved log, and mtime-filtering the directory is the same 0.3 ms trick this stage already uses (§3).
- **Heterogeneous row shapes stop colliding.** The account log has already carried two incompatible formats at once (`USAGE_DATA_REFERENCE.md` §5.6); separate scopes mean a schema change to one cannot corrupt reads of the other.
- **It duplicates nothing that is reliable.** Per-session cost does persist in the transcripts' `cost-state` records, but those appear in only 71 of 159 transcripts with an unverified trigger (`USAGE_DATA_SOURCES.md` §3). The status-line feed is written on every render, so it is the timelier and more complete of the two.

Correlation between the two scopes is a **time join** on `observed_at` — which is exactly what §1.4 does — so no field needs to be repeated across them.

Codex has no equivalent per-render channel, so its session scope can only be filled from its own session `.jsonl` files, and it gets no USD at all (`USAGE_DATA_SOURCES.md` §4.1). Codex attribution therefore stays at §1.2's interval level.

## 2. Outputs

```text
meter_now(agent)                  -> pct, resets, freshness
window_state(agent)               -> open? end? used%?
spend_rate(agent, minutes, kind)  -> percent/min, kind ∈ {organic, extra}
```

All quota arithmetic downstream is done in **meter percent**; dollars are for display only, and are never a budget (`USAGE_DATA_SOURCES.md` §1).

## 3. What is read, and how

| Source | Read how | Why |
|---|---|---|
| `data/claude/account.jsonl` | Tail (§4) | The meter |
| `data/codex/account.jsonl` | Tail (§4) | The meter |
| `~/.claude/projects/*/*.jsonl` transcripts | **`stat` only** — mtime, never contents | Liveness tripwire |
| `~/.codex/sessions/**/*.jsonl` session files | **`stat` only** | Liveness tripwire |
| `state/runs.jsonl` | Read whole (tiny) | Our own intervals |
| `data/claude/<session-id>.jsonl` | mtime filter, then tail the touched ones | Per-request and per-session USD for the overlap case (§1.4). **Only read when a bucket overlaps one of our runs** — on an ordinary tick these files are not opened at all. The glob must exclude `account.jsonl`, which shares the directory (`USAGE_DATA_REFERENCE.md` §5.7) |

The tripwire exists because the meter quantises at 1%: a user who has just started shows no movement for a minute or two, but their `.jsonl` transcript file's mtime moves immediately. It feeds signal S2 of `../02_prediction/DESIGN.md` §3 and costs 0.3 ms.

## 4. Incremental reading: tail and watermark

No byte-offset cursors, no stored file state. Each tick:

```text
watermark = max(observed_at) already stored for this agent

read the last N bytes of the log            # N starts at TAIL_BYTES
drop the first partial line
parse the rest
if oldest parsed observed_at > watermark:   # the tail did not reach back far enough
    N *= 2 and retry (up to the whole file)
keep records with observed_at > watermark
sort by observed_at, dedupe, apply the envelope (§5), assign to buckets (§6)
```

**The watermark is derived from the data itself**, so there is no cursor that can go stale. Rotation, truncation, an inode change, a rebuilt store — none of them exist as failure modes. Compare with a stored `(inode, size, offset)`, every one of which is a way to silently skip or re-read data.

**The sufficiency check is the one thing that makes it safe.** If the machine sleeps for two days, a fixed tail may not reach back to the watermark and rows would vanish silently. Comparing the oldest parsed record against the watermark catches exactly that, and doubling is self-correcting.

Sizing, re-measured on 2026-10-04 **[verified]** — and the earlier figure in this section was wrong by nearly 5×, which is why the sufficiency check matters more than the constant:

| | Busiest recorded day | Quiet day |
|---|---|---|
| `claude/account.jsonl` | **212 KB/h** (16,170 rows) | 14 KB/h |
| `codex/account.jsonl` | **37 KB/h** (221 rows, ~3.3 KB each) | 2.3 KB/h |

`TAIL_BYTES` of 1 MB covers under **5 hours** of a busy Claude day, so an overnight sleep would reach the doubling path routinely. **`TAIL_BYTES` is therefore 4 MB** (≈19 h at the observed peak), which keeps the common case a single read without making the uncommon one unsafe.

Both numbers should fall sharply: since 2026-10-04 the push path appends a row only when the reading differs from that session's previous one, which cut push row density by about 99% upstream (`USAGE_DATA_REFERENCE.md` §6). The table above is deliberately the *pre-dedup* peak, so the constant stays sized for the worst recorded case rather than the hoped-for one.

**`--backfill`** reads both logs whole — 54 MB and 17 MB today, a few seconds — and is run once at install. Nothing in the steady-state path should be reasoned about from those numbers.

## 5. The envelope

**Within one window the true percentage never decreases, so only the first reading of each new running maximum is kept; everything at or below it is stale or duplicate.** Full statement of the rule, the evidence and the trap it fixes: `USAGE_DATA_REFERENCE.md` §5.2.

Three points specific to this stage.

**It is applied per window, and the window key is not simply `resets_at`.** The reset timestamp is stable while a window runs, so it does identify the window — but only after two corrections, both measured on 2026-10-04:

| Correction | Why | Measured |
|---|---|---|
| Drop Codex's fake idle countdown first (`used_percent == 0`, `resets_at ≈ now + span`) | Its reset time moves with every tick, so every row looks like a new window | **3,136 of 4,666** successful Codex poll rows **[verified]** |
| Round the reset time to the nearest minute before using it as a key | Codex returns an epoch integer that drifts by a second between readings of one window | 2 such pairs in 35 days; keyed naively, 50 real windows become 3,178 **[verified]** |

Claude needs neither correction but gets both, because the reset arrives in two formats — an epoch-seconds *string* on push rows, an ISO timestamp on poller rows — so normalising to a rounded epoch integer is required anyway.

**It is what makes everything else small.** Re-measured over the full 40.7 days of recorded history **[verified 2026-10-04]**:

| Agent | Usable rows | After the envelope | Compression | Real 5-hour windows |
|---|---|---|---|---|
| Claude | 167,250 | **954** (23/day) | 175× | 93 |
| Codex | 1,530 (after dropping 3,136 fake) | **162** (4.6/day) | 9.4× | 50 |

**Codex is the sparser feed, by an order of magnitude**, and it is sparsest exactly when usage is highest, because its poller deliberately skips while a session file is fresh (`USAGE_DATA_REFERENCE.md` §5.5). Any confidence weighting must come from `n_readings` (§6.2) rather than from an assumption that both agents are observed equally well.

A dropped fake countdown is recorded as `no_window`, which is what the window starter reads (`../04_start_windows/DESIGN.md`).

## 6. The prepared table

Two files, both JSONL, both append-only.

### 6.1 `state/meter-<agent>.jsonl` — the audit trail

One line per surviving meter reading after the envelope: `observed_at`, `five_pct`, `seven_pct`, `five_reset`, `seven_reset`, `source`. About **23 rows/day for Claude and 5 for Codex**, under 1 MB/year for both **[verified 2026-10-04]**. It exists so any decision can be traced back to the readings it was taken from.

### 6.2 `state/buckets.jsonl` — what every consumer actually reads

One row per agent per 5-minute bucket, appended at each tick. **The tick cadence and the predictor's grid are the same thing**, so the regular time series a forecaster needs falls out of the schedule for free.

| Column | Type | Meaning |
|---|---|---|
| `agent` | str | `claude` \| `codex` |
| `bucket_id` | str | `"{ts_start}_{ts_end}"`, epoch seconds, 5 min apart |
| `fivehours_window_id` | str | `"{ts_start}_{ts_end}"` of the 5-hour window; `ts_end` is `five_reset`, `ts_start` is `ts_end − 18000` |
| `five_pct`, `seven_pct` | num | Meter at bucket end |
| `five_delta`, `seven_delta` | num | Movement during the bucket — the raw signal |
| `extra_pct` | num | Our share of `five_delta` (§1.2–§1.4) |
| `organic_pct` | num | `five_delta − extra_pct` — **the prediction target** |
| `organic_pct_lo`, `organic_pct_hi` | num | Bounds on `organic_pct`; equal to it when `attribution` is `exact` (§1.4) |
| `attribution` | str | `exact` \| `apportioned` \| `censored` — how `organic_pct` was obtained (§1.2) |
| `our_usd`, `organic_usd` | num | List-price dollars in the bucket, split by `query_source` (§1.3). The apportionment inputs, kept so the USD→percent ratio can be calibrated afterwards rather than only used. `null` when no telemetry covered the bucket — **which is not zero** |
| `t_in_window` | int | Minutes since the 5-hour window opened |
| `window_capped` | bool | The window reached 100% — **censoring flag** |
| `no_window` | bool | No window was open during the bucket |
| `n_readings` | int | Meter readings in the bucket — a confidence weight |
| `workers` | int | Our workers running during the bucket |
| `touched` | int | Session files whose mtime moved — the liveness tripwire |

576 rows/day for both agents, ~30 MB/year. Calendar features (hour of week, weekday) are **derived in the notebook from `bucket_id`, never stored**.

### 6.3 Three columns exist for the predictor, and dropping them corrupts it

- **`window_capped`** marks censored observations. 5 of 35 Claude windows reached 100% **[verified]**; in those, demand was *truncated*, not satisfied. A model that treats them as ordinary observations learns that the user wants exactly the cap.
- **`no_window`** distinguishes "no window was open" from "the user wanted nothing". They look identical in the percentages and mean opposite things.
- **A missing `bucket_id` means the machine was asleep, which is not zero demand.** No tick runs while the Mac sleeps, so no row is written, and the gap in the sequence *is* the signal. Any loader must reindex onto a full grid and mark the gaps explicitly (`../02_prediction/PREVIOUS_IDEAS.md` §2) — otherwise the model learns that every night the user wants nothing, which is the one conclusion the data cannot support.

## 7. Why JSONL rather than SQLite

An earlier version used SQLite. At this scale, indexes buy nothing:

| | Rows/day | Per year |
|---|---|---|
| `meter-<agent>.jsonl` | 23 (Claude), 5 (Codex) **[verified]** | < 1 MB |
| `buckets.jsonl` | 576 | ~30 MB |

Every query is "the last N minutes" (a tail read) or "scan the month" (a backtest). Both are trivial at this size, and JSONL gives three things the database actively blocked: `pd.read_json(lines=True)` in the notebook, `tail -f` while debugging, and **no binary corruption mode**.

That last point is the strongest. Because `--backfill` regenerates everything from the raw logs in seconds, **these files are disposable**: "corrupted state" stops being a failure mode and becomes `rm` plus a rebuild. The idempotence that a `PRIMARY KEY` used to provide is already provided by the watermark and the in-memory dedupe, which have to run anyway.

Writes are `O_APPEND` of whole lines, so a crash truncates at most one partial line, which the next read discards — the same rule the raw sources need.

**Not aggregated further.** Rolling the buckets up before writing would save perhaps 20% on top of the envelope's 175× and would cost the ability to re-derive anything at finer resolution. The raw-after-envelope rows are already small enough to keep.

## 8. Speed: this runs 288 times a day

This stage runs on **every** tick, whether or not anything is due, so its cost is paid 288 times a day for ever. The budget is **well under one second of wall clock and a few hundred kilobytes of I/O**.

| Work on a normal tick | Typical |
| --- | --- |
| `stat` 222 session files for the tripwire **[verified count]** | **0.3 ms** **[verified]** |
| Tail two quota logs | ~18 KB and ~3 KB of new bytes on the busiest recorded day, well under 1 KB on a quiet one **[verified 2026-10-04]** |
| Sort, dedupe, envelope | Tens of records |
| Append two rows to `buckets.jsonl`, plus any surviving meter rows | ~1 KB |

Four rules keep it there, and none is an optimisation to add later:

- **No network calls, ever.** Every source is a local file `agent-usage-tracker` has already written. This stage never asks an API for the meter, so it cannot be slow, rate-limited, or itself a cause of spend.
- **No subprocesses.** `/usr/bin/python3` and the standard library.
- **No `.jsonl` transcript is ever opened.** The 517 MB of transcripts are `stat`ed, never read (§1).
- **Session files are not opened on an ordinary tick.** They are only read for a bucket that overlaps one of our own runs (§3), so the one feed that grows per render stays off the common path.
- **Work is proportional to new bytes, not to history.** The watermark means a month of accumulated data costs the same as an empty one.

If a tick ever exceeds `TICK_INTERVAL`, the pipeline lock makes the next one exit rather than pile up (`../07_pipeline/DESIGN.md` §2), so slowness degrades into skipped ticks instead of a queue — a safety net, not a budget.

## 9. Failure modes

| Case | Behaviour |
|---|---|
| A log is rotated, truncated or replaced | The watermark is unaffected; the tail is re-read and records at or below the watermark are dropped |
| The tail does not reach the watermark | Detected and doubled (§4) — the one case that would otherwise lose data silently |
| A line is half-written | Not consumed until its newline arrives |
| Readings arrive out of order or re-pushed stale | Keyed by `observed_at` and filtered by the envelope (§5) |
| Codex reports its fake idle countdown | Dropped, recorded as `no_window` (§5) |
| A poller fails | Common — 66% of Claude poller rows carry an error (SSL, HTTP 429, missing token) **[verified]**. The meter simply goes stale; the pipeline refuses to act on a reading older than `READING_MAX_AGE` (`../07_pipeline/DESIGN.md` §2) |
| Clock jumps backwards | Negative ages clamp to 0 |
| Machine asleep | No tick, so no bucket row; the gap is the signal (§6.3) |
| A state file is corrupted | Deleted and rebuilt by `--backfill` in seconds (§7) |

## 10. Testing

Golden tests on fixtures: the stale-row case, the Codex fake countdown, the older Claude record format, a truncated final line, and **a watermark further back than `TAIL_BYTES`** (the doubling path).

Attribution tests, one per row of §1.2: a bucket with no workers, a bucket with workers and no organic signal, and an overlapping bucket — the last asserting that `organic_pct` lies within `[organic_pct_lo, organic_pct_hi]`, that `attribution` is `apportioned`, and that a missing dollar figure downgrades it to `censored` rather than inventing a value. Acceptance for P0: replays the recorded month without error, and the meter totals match `quota_model.py`.

## 11. Open points

- **Fractional percent.** Not available live for either agent: the upstream quota is whole-percent (`USAGE_DATA_REFERENCE.md` §5.1). The only sub-percent source is Codex's retroactive `plan_limit_history` — final usage per finished window in basis points, with the window's *real* start and end (`USAGE_DATA_REFERENCE.md` §3.2). It is useless live (it lags to the start of the UTC day, and only 4 fetches exist so far **[verified]**), but it is **ground truth for backtesting**: a Codex window's true final usage at 0.01% resolution, against which this stage's reconstruction from 1%-quantised readings can be scored. `../02_prediction/DESIGN.md` §6 is where that belongs.
- **Telemetry coverage is new and thin.** Only 66 telemetry rows exist, across 3 sessions, over the first 4 days **[verified 2026-10-04]** — sessions started before the keys were set send none. §1.4's per-request apportionment is therefore designed against a feed whose steady-state completeness is not yet observed. It degrades to `censored`, so this delays precision rather than blocking P0.
- **Codex has no session scope at all**, and no USD on any channel. Its per-thread tokens stay in `~/.codex/sessions/` (`USAGE_DATA_SOURCES.md` §4.1), which this stage does not read. Codex attribution therefore stays at §1.2's interval level permanently, not just in the MVP.
- **The USD→percent ratio** of §1.4 is calibrated from clean intervals (§1.3) and inherits the cache-weighting variance. How stable it is *within* one window is unmeasured; if it turns out tight, `apportioned` buckets are nearly as good as `exact` ones.
- **Codex's account-wide `dailyUsageBuckets`** is the only signal that sees cloud, IDE and app usage. Too coarse to plan with, but it can quantify the blind spot — currently unused.
- **Retention.** `buckets.jsonl` grows ~30 MB/year. No compaction policy is decided; sharding by month would make pruning a file unlink.
