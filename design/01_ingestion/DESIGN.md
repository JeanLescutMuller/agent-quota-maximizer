# 01 — Ingestion (`aqm ingest`)

Stage 1 of the pipeline. It turns the raw quota feeds into **one tidy 5-minute table per agent**, updated incrementally at every tick. It is the whole system's interface to reality: no later stage reads a raw file.

| | |
|---|---|
| Command | `aqm ingest [--backfill]` |
| Reads | The two quota logs, plus file **mtimes** for a liveness tripwire (§3) |
| Writes | `data/<agent>/meter.csv`, `data/<agent>/slots.csv` — one directory per agent (§6) |
| Acts | No |
| Consumed by | Every later stage, through the three functions of §2 |
| **Runtime** | **Well under a second**, every tick, whatever the history holds (§8) |

Where each field comes from, in which unit, and every trap in the raw data, is canonical in `~/dev/agent-usage-tracker/USAGE_DATA_SOURCES.md` (what exists upstream) and `~/dev/agent-usage-tracker/USAGE_DATA_REFERENCE.md` (what is captured, and where). This document does not restate them; it says what this stage *does* with it. Overview and stage map: `../DESIGN_v2.md`. Cross-cutting rules: `../07_pipeline/DESIGN.md`.

Facts marked **[verified]** were measured on this machine, on 2026-10-04 where a date is given in place, on 2026-09-29 otherwise.

## 1. Attribution: separating the user's spend from ours

Everything this system decides rests on one question: **how much of the quota the user spent, as opposed to us.** This section is the whole answer.

### 1.1 Per-session percent does not exist, and cannot

A meter row carries `observed_ts`, the percentages and the reset times, and **no session identifier** (`USAGE_DATA_SOURCES.md` §2.3). This is not an omission upstream could fix: the meter is one account-level number, so when two sessions spend concurrently there is **no per-session percentage to expose**. Asking for one is asking for a quantity that is not defined.

An earlier version of this design therefore parsed all 156 Claude `.jsonl` transcripts — 517 MB **[verified]** — to price every message and attribute it. That is not necessary, because **we know when our own work ran**: the executor launches it and records the interval.

```text
human movement = meter movement − movement during our own run intervals
```

| Question | Answered by | Cost |
|---|---|---|
| How much was spent in total? | The meter, after the drop_stale_readings | ~30 rows/day **[verified]** |
| Was any of it ours? | Our own run intervals, from `state/runs.jsonl` | Free — written anyway |
| Is the user active *right now*? | A new account row or telemetry row that is not ours (§3.1) | Already read; no extra I/O |
| What did our work cost in dollars? | One telemetry row per API request, tagged `query_source: "sdk"` (§1.3) | Written by Claude Code itself |

So this stage reads the **two quota logs** and **stats** the session files. It never parses a `.jsonl` transcript.

### 1.2 Three cases, and only one of them is hard

The subtraction above sounds lossy. It mostly is not, because **our runs and the user's work barely overlap.**

| Case | How often | Attribution | `attribution` field |
|---|---|---|---|
| No worker of ours was running | The **vast majority** of the 288 daily slots | `slot_window_human_pct = slot_window_used_pct` — nothing to subtract | `exact` |
| Our workers ran, no human signal | Our bursts, ~20 min at a time | `slot_window_bot_pct = slot_window_used_pct` | `exact` |
| Our workers ran **and** the user was active | Rare **by design** | §1.4 | `apportioned` or `censored` |

The third row is rare because the whole architecture exists to keep it empty: the late start, the room test, `GUARD_PCT`, and the fact that "blocking incidents" is already **the metric that must stay at zero** (`../07_pipeline/DESIGN.md` §8). Overlap is not a normal case to be modelled — it is an incident already being counted.

**Detecting which case applies needs no estimation.** `workers > 0` comes from our own run records; human activity during the interval comes from the liveness tripwire (§3) and from any human session's own push rows. Both are facts, not inferences.

### 1.3 Our own share is measurable exactly, not estimated

In the clean case the executor does not have to infer its spend. It reads the meter immediately **before** launching its first worker and immediately **after** the last one exits; with no human activity in between, that delta **is** our spend, in percent, measured. `spent_so_far` already works this way (`../06_execution/DESIGN.md` §3).

**Since 2026-09-30 the recorded data says which requests were ours, independently of our own bookkeeping.** Claude Code exports one OpenTelemetry event per API request, and `agent-usage-tracker` appends it to that session's file with `source: "claude_otel"` (`USAGE_DATA_REFERENCE.md` §9). Each event carries `cost_usd`, the token split, and a `query_source` that separates the two populations outright:

| `query_source` | Who | Our use for it |
|---|---|---|
| `sdk` | A headless `-p` run — ours | Our dollars, per request, with a timestamp |
| `repl_main_thread`, `generate_session_title`, `prompt_suggestion` | An interactive session — human | The user's dollars, per request |

Two independent marks therefore distinguish our work from the user's, and they agree: a headless run renders no status line, so it writes **no push row at all**, and its telemetry says `sdk`. Both were confirmed on the two headless runs in the recorded data — one telemetry row each, zero push rows **[verified 2026-10-04]**. This retires the assumption the earlier design rested on.

The value of this is not that it replaces the meter — it cannot, see §1.1 — but that **our dollar figure no longer depends on us reporting it honestly**. A worker that crashes before returning its result object still leaves its requests in the telemetry. It also calibrates the fallback of §1.4: over any clean interval both the list-price dollars and the meter delta are known, so their ratio is observable rather than assumed.

**Telemetry is a measurement, not an authority.** Claude Code buffers no events on disk, so a receiver that was down loses them silently, and sessions started before the telemetry keys were set send none (`USAGE_DATA_REFERENCE.md` §9). A missing telemetry row therefore means "unknown", never "no spend" — which is why percent still comes only from the meter, and why a slot with no usable dollar figure is marked `censored` rather than `exact` (§1.4).

### 1.4 When they overlap: apportion by measured dollars, and keep the bounds

An overlapping interval's meter delta cannot be split by any account-level reading. But both parties' **dollar** spend over that interval is separately measurable:

| Party | Source of its USD | Resolution |
|---|---|---|
| Us | Telemetry rows with `query_source: "sdk"`, summed over the interval; the worker's own `total_cost_usd` as a cross-check | **Per request**, exactly timestamped |
| The user | Telemetry rows with any other `query_source`; failing that, `session_cost_usd` deltas on push rows, which are **human-only by construction** since headless runs write none **[verified]** | Per request, or per render |

Both figures are read from the same files, by `query_source`, so the split needs no join against our own records. Per-request granularity also means an interval boundary no longer has to fall between two renders: a slot's share is the sum of the requests whose event time falls inside it.

So the delta is apportioned by measured share rather than guessed:

```text
slot_window_human_pct ≈ slot_window_used_pct × slot_human_usd / (slot_human_usd + slot_bot_usd)
```

**The residual error is in the ratio, not in the attribution.** Dollars are list-price and the meter weights cache reads far below list, so a cache-heavy session and a cache-light one convert differently. That is a bounded error on a rare case, and §1.3's calibration narrows it.

**The bounds are kept regardless.** `slot_window_human_pct` in such a slot is known to lie in `[0, slot_window_used_pct]`, and the slot is marked `apportioned`. If either dollar figure is missing, no number is invented: the slot is marked `censored` and carries the bounds only. That is the same treatment as `is_window_maxed` (§6.3) — **an interval-censored observation, never a fabricated point value.**

### 1.5 Two rules that follow, and must not be broken

- **Do not drop contaminated slots from a training set.** Our work runs when the user is *absent*, so silently excluding those slots removes low-human periods and would inflate predicted demand. Mark them and let the model downweight them.
- **When something forces a single number, pick the direction that makes us spend less.** Ambiguous movement counts as **human** for the prediction (raising predicted demand, shrinking room) and as **ours** for the executor's running total (`../06_execution/DESIGN.md` §3). Both are the same principle: assume the worse for us.

### 1.6 What this stage needs from upstream

`agent-usage-tracker` owns the feeds (split out of `agent-statusline` on 2026-09-30), and **this layout has been in place since 2026-09-30** (`USAGE_DATA_REFERENCE.md` §1). Two scopes, never mixed in one file:

| Scope | File | Carries | Never carries |
|---|---|---|---|
| **Account** | `data/<agent>/account.jsonl` | Meter percent, reset times, `observed_ts`, `source` | Per-session cost or token totals |
| **Session** | `data/claude/<session-id>.jsonl` | Tokens, USD, model, cache statistics, `observed_ts`, `source` | **Percent — under any name** |

A session file holds rows from two writers, told apart by `source`: `claude_statusline` (one per render, cumulative session cost) and `claude_otel` (one per API request, that request's cost). Both are useful, and §1.4 prefers the second.

An account row may keep the id of the session that *observed* it, named so it cannot be misread as attribution (`observed_by_session`). That is provenance, and a free liveness signal; it is not a statement about whose usage the reading represents. Since `account.jsonl` shares its directory with generated files, any glob over `data/<agent>/*.jsonl` excludes it explicitly.

**The prohibition is the important half.** A `five_hour_pct` field inside a per-session file would be read by somebody, sooner or later, as "this session's quota usage" — a quantity that does not exist (§1.1). Keeping percent out of session-scoped files makes that error unrepresentable rather than merely discouraged.

Why one file per session rather than session rows interleaved in the account log:

- **The scope boundary becomes structural.** The attribution bug this whole section guards against is a scope confusion; a directory layout that mirrors the scopes prevents it by construction.
- **Our own sessions are identifiable by filename.** The executor generates its worker UUIDs, so it knows exactly which files are ours without parsing anything.
- **A session's history is self-contained**, so reading one session costs one small file rather than a scan of an interleaved log, and mtime-filtering the directory is the same 0.3 ms trick this stage already uses (§3).
- **Heterogeneous row shapes stop colliding.** The account log has already carried two incompatible formats at once (`USAGE_DATA_REFERENCE.md` §5.6); separate scopes mean a schema change to one cannot corrupt reads of the other.
- **It duplicates nothing that is reliable.** Per-session cost does persist in the transcripts' `cost-state` records, but those appear in only 71 of 159 transcripts with an unverified trigger (`USAGE_DATA_SOURCES.md` §3). The status-line feed is written on every render, so it is the timelier and more complete of the two.

Correlation between the two scopes is a **time join** on `observed_ts` — which is exactly what §1.4 does — so no field needs to be repeated across them.

Codex has no equivalent per-render channel, so its session scope can only be filled from its own session `.jsonl` files, and it gets no USD at all (`USAGE_DATA_SOURCES.md` §4.1). Codex attribution therefore stays at §1.2's interval level.

## 2. Outputs

```text
meter_now(agent)                  -> pct, resets, freshness
window_state(agent)               -> open? end? used%?
burn_rate(agent, minutes, kind)  -> percent/min, kind ∈ {human, extra}
```

All quota arithmetic downstream is done in **meter percent**; dollars are for display only, and are never a budget (`USAGE_DATA_SOURCES.md` §1).

## 3. What is read, and how

| Source | Read how | Why |
|---|---|---|
| `data/claude/account.jsonl` | Tail (§4) | The meter, and the Claude liveness tripwire (§3.1) |
| `data/codex/account.jsonl` | Tail (§4) | The meter |
| `data/claude/<session-id>.jsonl` | mtime filter, then tail the ones that moved | Per-request and per-session USD for the overlap case (§1.4), and the second Claude tripwire (§3.1). **Only read when a slot overlaps one of our runs, or when a tripwire candidate must be confirmed** — on an ordinary tick these files are not opened. The glob must exclude `account.jsonl`, which shares the directory (`USAGE_DATA_REFERENCE.md` §5.7) |
| `~/.codex/sessions/**/*.jsonl` | `stat` to find candidates, then **tail the few that moved** (§3.1) | Codex liveness tripwire — Codex has no other channel |
| `state/runs.jsonl` | Read whole (tiny) | Our own intervals, and which session ids are ours |

**No `~/.claude/projects/` transcript is read or stated at all.** An earlier version of this design used their mtimes as the liveness tripwire; §3.1 explains why that was wrong.

### 3.1 The liveness tripwire, and why it is not a file mtime

The tripwire exists because the meter quantises at 1%: a user who has just started spending shows no meter movement for a minute or two, and the budget must not treat that silence as room.

**File mtime cannot supply that signal.** Measured over all 168 Claude transcripts on this machine **[verified 2026-10-04]**, an mtime is later than the last real message by more than an hour in **45 files and by more than a day in 24** — and five files in five different project directories were stamped within one 3-second burst while their last conversational entry was 12 to 19 days old. Something rewrites or touches them with no user involved (`../CONSIDERATIONS.md` §5). Separately, 115 of the 168 end in a `system` bookkeeping entry rather than a message, so even the last *entry* is not the last interaction.

A tripwire built on mtime would therefore fire in correlated bursts across many projects at once — which is indistinguishable from the user suddenly becoming very active everywhere, the exact condition that makes this system stand down. **It would not merely be noisy; it would be wrong in the direction that wastes the quota we exist to spend.**

The replacement uses data this stage already reads, and differs per agent:

| Agent | Tripwire | Why it is sound |
|---|---|---|
| **Claude** | A row in `account.jsonl` whose `observed_ts` falls in the slot and whose `observed_by_session` is **not one of ours**; or a telemetry row in a session file whose `query_source` is not `sdk` | Since 2026-10-04 a push row is appended only when the reading actually changed, so a new row means a **new API response** — the user sent something. Nothing writes one on a timer |
| **Codex** | `stat` the session files to find which moved, then **tail only those** and require a turn or `token_count` event timestamped inside the slot | Codex has no account-side per-session channel, so the file is the only source. The stat is a pre-filter that produces *candidates*; the tail is what decides |

**The rule this establishes: mtime may select candidates, never confirm them.** It is a cheap way to avoid opening 168 files; it is not evidence that anything happened. The confirmation always comes from a timestamped record of an actual request.

Our own sessions are excluded by id in both cases — the executor generates its worker UUIDs, so `state/runs.jsonl` already knows them (§1.3). Without that exclusion the tripwire would fire on our own work and the system would stand down from itself.

## 4. Incremental reading: tail and watermark

Since 2026-10-04 `data/` contains **readings only** — failed poll attempts go to `logs/<agent>-poll-errors.jsonl` instead (`USAGE_DATA_REFERENCE.md` §1). That removed the largest filter this stage used to apply: Claude poller rows fell from 17,279 to 6,842, all of them now real readings. **Nothing here inspects an `error` field.**

No byte-offset cursors, no stored file state. Each tick:

```text
watermark = max(observed_ts) already stored for this agent

read the last N bytes of the log            # N starts at TAIL_BYTES
drop the first partial line
parse the rest
if oldest parsed observed_ts > watermark:   # the tail did not reach back far enough
    N *= 2 and retry (up to the whole file)
keep records with observed_ts > watermark
sort by observed_ts, dedupe, apply the drop_stale_readings (§5), assign to slots (§6)
```

**The watermark is derived from the data itself**, so there is no cursor that can go stale. Rotation, truncation, an inode change, a rebuilt store — none of them exist as failure modes. Compare with a stored `(inode, size, offset)`, every one of which is a way to silently skip or re-read data.

**The sufficiency check is the one thing that makes it safe.** If the machine sleeps for two days, a fixed tail may not reach back to the watermark and rows would vanish silently. Comparing the oldest parsed record against the watermark catches exactly that, and doubling is self-correcting.

Sizing, re-measured on 2026-10-04 **[verified]** — and the earlier figure in this section was wrong by nearly 5×, which is why the sufficiency check matters more than the constant:

| | Busiest recorded day | Quiet day |
|---|---|---|
| `claude/account.jsonl` | **229 KB/h** | 14 KB/h |
| `codex/account.jsonl` | **61 KB/h** (~3.6 KB a row) | 2.3 KB/h |

**`TAIL_BYTES` is 64 KB**, and sizing it for the worst case instead would be a mistake. Measured on the real files **[verified 2026-10-04]**:

| Starting tail | Claude rows parsed | Cost |
|---|---|---|
| **64 KB** | 126 | **0.51 ms** |
| 256 KB | 343 | 1.86 ms |
| 4 MB | 12,488 | **42.28 ms** |

A normal tick has ~18 KB of new bytes at the busiest recorded rate, so 64 KB covers it with 3× headroom and needs **zero doublings**. Starting at 4 MB would spend 42 ms parsing rows already stored, every tick, for ever — about 40× the cost of the entire rest of the stage (§8).

**The doubling path is the safety net; the starting size is tuned for the common case.** Measured end to end, including the doublings:

| Behind by | Doublings | Ingestion work |
|---|---|---|
| 5 min (normal) | 0 | **1.05 ms** |
| 1 hour | 1 | 2.19 ms |
| 8 hours (overnight) | 2 | 8.85 ms |
| 2 days | 5 | 67.6 ms |

For the large-tail cases there is a cheap refinement, not needed yet: scanning for `"observed_ts":` at the byte level and comparing numerically costs 7.4 ms on a 4 MB tail against 42 ms to `json.loads` it, so the doubling path can skip parsing rows below the watermark entirely.

Row density should also fall sharply: since 2026-10-04 the push path appends a row only when the reading differs from that session's previous one (`USAGE_DATA_REFERENCE.md` §6). The growth table above is the *pre-dedup* peak, so the constant is sized against the worst recorded case rather than the hoped-for one.

**`--backfill`** reads both logs whole — 51 MB and 17 MB today — and is run once at install (§11). Nothing in the steady-state path should be reasoned about from those numbers.

## 5. The drop_stale_readings

**Within one window the true percentage never decreases, so only the first reading of each new running maximum is kept; everything at or below it is stale or duplicate.** Full statement of the rule, the evidence and the trap it fixes: `USAGE_DATA_REFERENCE.md` §5.2.

Three points specific to this stage.

**It is applied per window, and the window key is not simply `resets_at`.** The reset timestamp is stable while a window runs, so it does identify the window — but only after two corrections, both measured on 2026-10-04:

| Correction | Why | Measured **[verified 2026-10-04]** |
|---|---|---|
| Drop Codex's fake idle countdown first (`used_percent == 0`, `resets_at ≈ now + span`) | Its reset time moves with every tick, so every row looks like a new window | **3,136 of 4,687** Codex rows — **67%** |
| Round the reset time to the nearest minute before using it as a key | Both agents report the same window's reset at two different instants | **Claude: 40 of 46 windows are split in two without it.** Its push rows carry a reset one second *below* the API's (`…599199` vs `…599200`), so exact keying reports 92 windows where there are 46. Codex drifts the same way |

**Both agents need both corrections — this was previously assumed to be a Codex-only problem, and that was wrong.** Claude additionally reports the reset in two formats (an epoch-seconds *string* on push rows, an ISO timestamp with sub-second precision on poller rows), so normalising to a minute-rounded integer is required regardless.

**A missing reset is not a closed window, and more than half of them are not.** A status-line row rendered before `rate_limits` arrive carries `five_hour_pct: 0` and a null reset, and upstream stores it like any other row. Taken at face value it closes a window that is still running:

| | Claude, recorded history **[verified 2026-10-04]** |
|---|---|
| Rows with no reset | 122 |
| …of which the known window had **not** yet expired | **65 — more than half** |
| Worst case | a window declared closed **4 hours** before its real end |

So a null reset is believed only once the last known reset has actually passed; before that the row carries no window information and is dropped. The cost of believing one is not cosmetic: `window_used` reads 0 for the rest of that window, budgeting projects a *hypothetical* window on top of the real one, and the forecast loses its horizon. Applying the rule moved **4,197 Claude slots** (36% of the table) out of `is_window_open`, which is why the share of elapsed time with no window open is 45% and not the 76% an earlier count reported.

A useful sanity check falls out: 46 windows over 40.8 days is 1.13 a day, which matches the independent figure from `adhoc_quotas_analysis` (35 windows over 30 days). A window count far above that means the key is wrong.

**It is what makes everything else small.** Re-measured over the full 40.7 days of recorded history **[verified 2026-10-04]**:

| Agent | Usable rows | After the drop_stale_readings | Compression | Real 5-hour windows |
|---|---|---|---|---|
| Claude | 167,451 | **835** (20.5/day) | 201× | 46 |
| Codex | 1,551 (after dropping 3,136 fake) | **153** (4.4/day) | 10× | 39 |

**Codex is the sparser feed, by an order of magnitude**, and it is sparsest exactly when usage is highest, because its poller deliberately skips while a session file is fresh (`USAGE_DATA_REFERENCE.md` §5.5). Any confidence weighting must come from `slot_n_readings` (§6.2) rather than from an assumption that both agents are observed equally well.

A dropped fake countdown is recorded as `is_window_open`, which is what the window starter reads (`../04_start_windows/DESIGN.md`).

## 6. The prepared table

Two files, both JSONL, both append-only.

### 6.0 One directory per agent

```text
~/opt/agent-quota-maximizer/
├── data/                  ingested history: rebuildable, per agent
│   ├── claude/{meter.csv, slots.csv}
│   └── codex/{meter.csv, slots.csv}
└── state/                 what the pipeline mutates: runs, locks, latest pointers
```

The split is not cosmetic. **`data/` is history and is disposable** — `--backfill` regenerates it from the raw logs in under two seconds (§11) — while `state/` holds things that cannot be recomputed, such as the record of what we ourselves ran. Mixing them would make "delete it and rebuild" an unsafe instruction. It also matches `agent-usage-tracker`, whose sources this stage reads from `data/<agent>/`, so one layout covers both ends of the pipe.

Separating the agents removed a real defect rather than just tidying up. With both in one file, the per-agent high-water mark came from a tail read, and a tail could land entirely inside one agent's rows and report **zero** for the other — which rebuilt the whole table on every tick, at 188 ms instead of 6 ms **[verified 2026-10-04]**. The fix at the time was to interleave the two agents by time on write so the tail was guaranteed to carry both; one file per agent makes the problem impossible instead, and that interleaving is gone.

The `agent` column is gone with it: the directory carries it, and a column that merely repeats the path is one more thing that can disagree with it.

### 6.1 `data/<agent>/meter.csv` — the audit trail

One line per surviving meter reading after the drop_stale_readings: `observed_ts`, `window_used_pct`, `week_used_pct`, `window_end_ts`, `week_end_ts`, `source`. About **23 rows/day for Claude and 5 for Codex**, under 1 MB/year for both **[verified 2026-10-04]**. It exists so any decision can be traced back to the readings it was taken from.

### 6.2 `data/<agent>/slots.csv` — what every consumer actually reads

One row per agent per 5-minute slot, appended at each tick. **The tick cadence and the predictor's grid are the same thing**, so the regular time series a forecaster needs falls out of the schedule for free.

| Column | Type | Meaning |
|---|---|---|
| `observed_dt` | str | Slot start in local time with its UTC offset — a rendering of `slot_id`'s first half, there so the file reads without a tool |
| `slot_id` | str | `"{ts_start}_{ts_end}"`, epoch seconds, 5 min apart |
| `window_id` | str | `"{ts_start}_{ts_end}"` of the 5-hour window; `ts_end` is `window_end_ts`, `ts_start` is `ts_end − 18000` |
| `window_used_pct`, `week_used_pct` | num | Meter at slot end, carried forward between readings. **`null` while no window is open** — a closed window's level is not the current one, and carrying it would let a later stage compute room against a window that has ended |
| `slot_window_used_pct`, `slot_week_used_pct` | num | Movement during the slot — the raw signal |
| `slot_window_bot_pct` | num | Our share of `slot_window_used_pct` (§1.2–§1.4) |
| `slot_window_human_pct` | num | `slot_window_used_pct − slot_window_bot_pct` — **the prediction target** |
| `slot_window_human_pct_lo`, `slot_window_human_pct_hi` | num | Bounds on `slot_window_human_pct`; equal to it when `attribution` is `exact` (§1.4) |
| `attribution` | str | `exact` \| `apportioned` \| `censored` — how `slot_window_human_pct` was obtained (§1.2) |
| `slot_bot_usd`, `slot_human_usd` | num | List-price dollars in the slot, split by `query_source` (§1.3). The apportionment inputs, kept so the USD→percent ratio can be calibrated afterwards rather than only used. `null` when no telemetry covered the slot — **which is not zero** |
| `window_age_minutes` | int | Minutes since the 5-hour window opened |
| `is_window_maxed` | bool | The window reached 100% — **censoring flag** |
| `is_window_open` | bool | No window was open during the slot |
| `slot_n_readings` | int | Meter readings surviving the drop_stale_readings — a confidence weight |
| `was_machine_awake` | bool | **The machine was awake.** True when a raw row is timestamped in the slot, or when this slot is the one the tick just closed. Distinguishes a real zero from a gap (§6.3) without relying on the row being new: a re-pushed stale reading is discarded as data but still proves liveness |
| `n_bot_workers` | int | Our workers running during the slot |
| `was_human_active` | int | Confirmed human activity events in the slot, not ours — a new Claude account row, a non-`sdk` telemetry row, or a Codex turn event found by tailing a candidate file (§3.1). **Never a bare mtime change.** Counted from the **raw** rows, before the drop_stale_readings: see below |

288 rows/day per agent. Measured at **230 bytes a row, so ~2.4 MB for the recorded 40 days and ~21 MB/year** **[verified 2026-10-04]** — three times the original estimate, which assumed fewer columns. Calendar features (hour of week, weekday) are **derived from `slot_id`, never stored**; sharding by month is the obvious compaction if it matters (§12).

**A delta is attributed to the slot in which it was *observed*, not spread over the gap.** With ~20 readings a day the moment inside a gap is unknowable, and interpolating would invent precision. The reconciliation test is that `sum(slot_window_used_pct)` equals the sum of per-window peaks: measured **2163 = 2163** on Claude and **549 = 549** on Codex **[verified 2026-10-04]**.

### 6.3 Three columns exist for the predictor, and dropping them corrupts it

- **`is_window_maxed`** marks censored observations. 5 of 35 Claude windows reached 100% **[verified]**; in those, demand was *truncated*, not satisfied. A model that treats them as ordinary observations learns that the user wants exactly the cap.
- **`is_window_open`** distinguishes "no window was open" from "the user wanted nothing". They look identical in the percentages and mean opposite things.
- **A missing `slot_id` means the machine was asleep, which is not zero demand.** No tick runs while the Mac sleeps, so no row is written, and the gap in the sequence *is* the signal. Any loader must reindex onto a full grid and mark the gaps explicitly (`../02_prediction/PREVIOUS_IDEAS.md` §2) — otherwise the model learns that every night the user wants nothing, which is the one conclusion the data cannot support.

### 6.4 `was_human_active` is counted before the drop_stale_readings, and that is not an implementation detail

The drop_stale_readings (§5) drops every reading that carries no new percentage. A reading from a user who has just started carries no new percentage — their first 1% has not landed yet. So counting `was_human_active` from the enveloped readings deleted **exactly** the case the liveness signal exists for, and did it silently: the evidence was in the raw data, discarded here, and stage 2 read a legitimate-looking zero.

`was_human_active` is therefore counted from the raw rows of the tick, keyed by slot, before any filtering:

| Counted from | A foreign session's reading with no new percentage | Consequence |
|---|---|---|
| Enveloped readings | Dropped | The tripwire can only fire once the meter has already moved — i.e. once S1 would have seen it anyway, which makes the signal worthless |
| **Raw rows** | **Kept** | The tripwire fires in the minute or two before the meter catches up, which is its entire purpose (`../02_prediction/DESIGN.md` §3) |

This is the same reasoning that put `was_machine_awake` on the raw rows rather than the enveloped ones (§6.3), and the two now share it: **the drop_stale_readings answers "how much", the raw rows answer "was anyone there", and the second question must never be asked of a filter built for the first.**

It also silently improved attribution. A slot where our worker ran, the meter moved, and a stale foreign reading existed used to be classified `exact, all ours`; it is now `censored`, which is correct — the user *was* active, and the old answer would have credited their spend to us.

## 7. Why CSV

Two decisions, taken in order. **SQLite was dropped** because at this scale indexes buy nothing:

| | Rows/day | Per year |
|---|---|---|
| `meter-<agent>.csv` | 23 (Claude), 5 (Codex) **[verified]** | < 1 MB |
| `slots.csv` | 576 | ~23 MB |

Every query is "the last N minutes" (a tail read) or "scan the month" (a backtest). Both are trivial at this size, and a text file gives three things the database actively blocked: a one-line read in the notebook, `tail -f` while debugging, and **no binary corruption mode**.

**Then JSONL was dropped for CSV**, because this stage's entire job is to turn several raw shapes into one: once the output schema is fixed, repeating the key names on all 21,856 rows is pure overhead. Measured on the recorded history, the same table is **2.6 MB as CSV against 9.6 MB as JSONL** — 3.7× smaller — and a human can read it in `less`, `column -t` or a spreadsheet without a tool. The raw inputs stay JSONL: they are not ours, they carry several incompatible record shapes at once, and a fixed column list would be a lie about them.

What CSV costs, and how each cost is paid:

| Cost | Paid by |
|---|---|
| Types are lost — every cell reads back as a string | One declared map per file (`METER_TYPES`, `SLOT_TYPES`). Declared once, not guessed per value |
| A missing value is indistinguishable from an empty one | `None` is written as an empty field and read back as `None`; no column uses `""` as a real value |
| A column added in the middle silently shifts every later field | Appending a row whose keys do not match the header **raises** |
| A tail read can start mid-row and has no header | The header is read separately from the front of the file, and a partial first line is dropped (`tail_csv`) |

**Every row leads with `observed_dt`, then that same instant as epoch seconds.** The epoch value is what everything computes on; the rendering exists so the file can be read without a tool. It is local time **with its UTC offset** (`2026-10-04 14:20:00 +0200`), which keeps an hour in late October unambiguous — and is why it is built from an aware UTC datetime rather than a naive `fromtimestamp`, whose `%z` renders as nothing.

That last point is the strongest. Because `--backfill` regenerates everything from the raw logs in seconds, **these files are disposable**: "corrupted state" stops being a failure mode and becomes `rm` plus a rebuild. The idempotence that a `PRIMARY KEY` used to provide is already provided by the watermark and the in-memory dedupe, which have to run anyway.

Writes are `O_APPEND` of whole lines, so a crash truncates at most one partial line, which the next read discards — the same rule the raw sources need.

**Not aggregated further.** Rolling the slots up before writing would save perhaps 20% on top of the drop_stale_readings's 175× and would cost the ability to re-derive anything at finer resolution. The raw-after-drop_stale_readings rows are already small enough to keep.

## 8. Speed: this runs 288 times a day

This stage runs on **every** tick, whether or not anything is due, so its cost is paid 288 times a day for ever. The budget was **well under one second**. The implementation sits three orders of magnitude inside it, and the interesting part is *where* the time goes **[verified 2026-10-04, `release/aqm.py`]**:

| | Median |
|---|---|
| `/usr/bin/python3 -c pass` — the floor, unavoidable | **21 ms** |
| …plus the stdlib imports (`csv`, `json`, `observed_dt`, `argparse`) | 34 ms |
| …plus importing `aqm` and parsing arguments | 41 ms |
| …plus **the entire ingestion of both agents** | **43 ms** |

**The work is 3.8 ms; the other 39 ms is a Python process starting.** That single fact decides more about this stage's shape than any algorithm in it:

- **One process per tick, not one per stage.** Running `ingest` and `budget` as separate commands pays the 39 ms twice, for ~6 ms of work: **measured, 41 ms each as separate processes against 45 ms for both in one** (`../03_budgeting/DESIGN.md` §6). The scheduled path runs every stage **in one process**: the CLI verbs remain for testing and backtests (`../07_pipeline/DESIGN.md` §1.1), but `aqm pipeline` imports and calls them rather than shelling out.
- **Optimising the work further is pointless.** At 3.8 ms against a 21 ms floor, halving it again would change the tick by 4%. The remaining cost is one 64 KB tail of each meter file, typed on read; it is left alone deliberately.

Work on a normal tick, in full: tail two account logs at 64 KB each and filter by watermark (§4); `stat` the Codex session files for tripwire candidates and tail only those that moved (§3.1); sort, drop_stale_readings, assign to slots; append one row per agent plus any surviving meter rows, about 1 KB. No Claude transcript is opened or stated at all.

**Degradation is gentle, which is what makes the doubling path safe** (§4):

| Behind by | Work |
|---|---|
| 5 min (normal) | 3.8 ms |
| 1 hour | 5.5 ms |
| 8 hours (overnight) | 7.3 ms |
| 2 days | 17.7 ms |

Daily total: **43 ms × 288 = about 18 seconds of wall clock a day**, a duty cycle of 0.02%.

One measured trap, since the fix is not obvious from reading the code. Recovering the last slot written used to parse a whole 64 KB tail through the typed reader — **~1,900 cell conversions to extract one integer**, which was two thirds of the entire tick. It now reads the final line only, which is correct precisely because rows are appended in time order and one file holds one agent (§6.0). That change alone took the work from 10.6 ms to 3.8 ms. **The same trap caught stage 3**, in a different disguise — reading more of a CSV than the answer needs is this codebase's characteristic performance bug, and the tail-plus-sufficiency-check of §4 is its standing answer (`../03_budgeting/DESIGN.md` §6).

Four rules keep it there, and none is an optimisation to add later:

- **No network calls, ever.** Every source is a local file `agent-usage-tracker` has already written. This stage never asks an API for the meter, so it cannot be slow, rate-limited, or itself a cause of spend.
- **No subprocesses.** `/usr/bin/python3` and the standard library.
- **No Claude `.jsonl` transcript is opened, or even stated.** The 517 MB of transcripts are not a source for this stage at all (§3.1). Codex session files are stated, and a tail is read only for the few that moved.
- **Session files are not opened on an ordinary tick.** They are only read for a slot that overlaps one of our own runs (§3), so the one feed that grows per render stays off the common path.
- **Work is proportional to new bytes, not to history.** The watermark means a month of accumulated data costs the same as an empty one — verified: 1.05 ms against a 51 MB log.
- **One process per tick, not one per stage.** See the table above; this is a hard rule, not a tuning option.

If a tick ever exceeds `TICK_INTERVAL_SECONDS`, the pipeline lock makes the next one exit rather than pile up (`../07_pipeline/DESIGN.md` §2), so slowness degrades into skipped ticks instead of a queue — a safety net, not a budget.

## 9. Failure modes

| Case | Behaviour |
|---|---|
| A log is rotated, truncated or replaced | The watermark is unaffected; the tail is re-read and records at or below the watermark are dropped |
| The tail does not reach the watermark | Detected and doubled (§4) — the one case that would otherwise lose data silently |
| A line is half-written | Not consumed until its newline arrives |
| Readings arrive out of order or re-pushed stale | Keyed by `observed_ts` and filtered by the drop_stale_readings (§5) |
| Codex reports its fake idle countdown | Dropped, recorded as `is_window_open` (§5) |
| A poller fails | Common, but **no longer visible here**: since 2026-10-04 a failed attempt goes to `logs/<agent>-poll-errors.jsonl`, and `data/` holds readings only (`USAGE_DATA_REFERENCE.md` §1). This stage never filters on `error`. The meter simply goes stale; the pipeline refuses to act on a reading older than `READING_MAX_AGE_SECONDS` (`../07_pipeline/DESIGN.md` §2) |
| Clock jumps backwards | Negative ages clamp to 0 |
| Machine asleep | No tick, so no slot row; the gap is the signal (§6.3) |
| Something rewrites transcripts or session files with no user involved | Cannot fire the tripwire: mtime only nominates a candidate, and a candidate is dropped unless a tail shows a request timestamped inside the slot (§3.1) |
| Our own worker's activity | Excluded by session id before the tripwire is evaluated (§3.1), so the system never stands down from itself |
| A state file is corrupted | Deleted and rebuilt by `--backfill` in seconds (§7) |

## 10. Testing

Every test lives in `../../test/`, is written in Bash and calls no agent (`../../test/README.md`). Determinism comes from two knobs on the implementation: `aqm ingest --now <epoch>` fixes the clock, and `AQM_TAIL_BYTES` shrinks the starting tail so the doubling path can be forced.

Golden tests on fixtures: the stale-row case, the Codex fake countdown, the older Claude record format, a truncated final line, a reset drifting by one second, and **a watermark further back than `TAIL_BYTES`** (the doubling path).

**The strongest test is not a fixture.** Ingesting tick by tick must produce the same table as one backfill over the same span, field for field — the check that caught both of the bugs this stage shipped with in draft: a rebuild of the whole table on every tick, and two disagreeing rules for carrying `window_used_pct` forward. The single exception is `was_machine_awake`, which cannot match by construction, since a live tick proves the machine was awake while a backfill can only infer it from row density; the test asserts the difference only ever runs in that direction.

Against the recorded history, two invariants are checked rather than golden values, so they survive the data growing: `sum(slot_window_used_pct)` equals the sum of per-window peaks evaluated at the same horizon, and the Claude window count stays near **1.13 a day**, which `adhoc_quotas_analysis` measured independently. The second is what surfaced the reset drift of §5.

Tripwire tests, from the measured failure (§3.1): a session file whose mtime moved while its contents did not must leave `was_human_active` at 0; a file whose tail gains a turn event inside the slot must raise it; and one of our own session ids must be ignored in both directions.

Attribution tests, one per row of §1.2: a slot with no workers, a slot with workers and no human signal, and an overlapping slot — the last asserting that `slot_window_human_pct` lies within `[slot_window_human_pct_lo, slot_window_human_pct_hi]`, that `attribution` is `apportioned`, and that a missing dollar figure downgrades it to `censored` rather than inventing a value. Acceptance for P0: replays the recorded month without error, and the meter totals match `quota_model.py`.

## 11. The one-off backfill (`--backfill`)

Run once at install, and again whenever the derived files are thrown away. **It is the same code with different input bounds, not a second implementation** — a separate backfill script would drift from the live one, and the backfill is the only thing that ever exercises the drop_stale_readings against the full history.

| | Measured **[verified 2026-10-04]** |
|---|---|
| Input | `claude/account.jsonl` 51.3 MB / 167,520 rows, `codex/account.jsonl` 17.0 MB / 4,693 rows |
| Output | ~990 meter rows, 85 window keys (46 Claude + 39 Codex) |
| Wall clock | **0.73 s**, single-threaded, standard library |

Because it is this cheap, the derived files are **disposable**: "corrupted state" is `rm` plus a rerun, not a recovery procedure (§7). Three properties it must hold:

- **Idempotent.** A second run produces byte-identical output.
- **Reconciled against an independent source.** Totals must match `adhoc_quotas_analysis/quota_model.py`, and the Claude window count must land near **1.13/day**. That check is what caught the reset-drift bug of §5 — a count far above it means the window key is wrong, and nothing else in the pipeline would have noticed.
- **Gaps stay gaps.** Slots with no tick are never written as zeros (§6.3).

Its real product is not the state files but the **training set**: 46 Claude and 39 Codex windows with their shapes, which is what any predictor beyond the current rate extrapolation has to be fitted on (`../02_prediction/DESIGN.md` §6).

## 12. Open points

- **Fractional percent.** Not available live for either agent: the upstream quota is whole-percent (`USAGE_DATA_REFERENCE.md` §5.1). The only sub-percent source is Codex's retroactive `plan_limit_history` — final usage per finished window in basis points, with the window's *real* start and end (`USAGE_DATA_REFERENCE.md` §3.2). It is useless live (it lags to the start of the UTC day, and only 4 fetches exist so far **[verified]**), but it is **ground truth for backtesting**: a Codex window's true final usage at 0.01% resolution, against which this stage's reconstruction from 1%-quantised readings can be scored. `../02_prediction/DESIGN.md` §6 is where that belongs.
- **Telemetry coverage is new and thin.** Only 66 telemetry rows exist, across 3 sessions, over the first 4 days **[verified 2026-10-04]** — sessions started before the keys were set send none. §1.4's per-request apportionment is therefore designed against a feed whose steady-state completeness is not yet observed. It degrades to `censored`, so this delays precision rather than blocking P0.
- **Codex has no session scope at all**, and no USD on any channel. Its per-thread tokens stay in `~/.codex/sessions/` (`USAGE_DATA_SOURCES.md` §4.1), which this stage does not read. Codex attribution therefore stays at §1.2's interval level permanently, not just in the MVP.
- **The USD→percent ratio** of §1.4 is calibrated from clean intervals (§1.3) and inherits the cache-weighting variance. How stable it is *within* one window is unmeasured; if it turns out tight, `apportioned` slots are nearly as good as `exact` ones.
- **Codex's account-wide `dailyUsageBuckets`** is the only signal that sees cloud, IDE and app usage. Too coarse to plan with, but it can quantify the blind spot — currently unused.
- **What moves transcript mtimes is not identified.** A 30-minute `agent-session-manager` reindex is the obvious suspect, but its log reports reading only, so the 09:17 burst is unexplained **[verified 2026-10-04]**. It does not need to be explained for this design to be safe — §3.1 no longer trusts mtime — but it is worth knowing, because the same sweeper touches the Codex session files this stage still stats.
- **Retention.** `slots.csv` grows ~23 MB/year. No compaction policy is decided; sharding by month would make pruning a file unlink.
