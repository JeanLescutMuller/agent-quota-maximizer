# 03 — Budgeting (`aqm budget`)

Stage 3 answers **how much may be spent, and by when.** It is the stage that implements the project's guiding principle: spend only what cannot survive until the reset, and spend it as late as possible.

| | |
|---|---|
| Command | `aqm budget [--at <iso>] [--prediction PATH] [--out PATH]` |
| Reads | `state/buckets.jsonl`, the latest prediction (`../02_prediction/DESIGN.md` §1) |
| Writes | `artifacts/budgets/<date>/<time>.json` |
| Acts | No |
| Consumed by | Planning (`../05_planning/DESIGN.md`) |
| **Runtime** | **Milliseconds**, every tick (§6) |

It does **not** decide what to run — that is planning — and it does not decide *when to start*, only the deadline to finish by. The policies that were considered and rejected before this one, including the nightly top-up and the availability-weighted ceilings, are in `PREVIOUS_IDEAS.md`; this design is Policy D there.

## 1. Definitions used here

| Term | Definition |
|---|---|
| Unit | The quota of one full 5-hour window = 100% of the 5-hour meter |
| Chunk | A window, or the part of a window on one side of the 7-day reset |
| Ceiling | The most extra work a chunk may hold, after the user's predicted demand |
| Surplus | Remaining weekly quota that the chunks still to come cannot absorb |

## 2. The algorithm

```text
1. PROJECT the chain of chunks from now to the 7-day reset:
     first chunk = the open window (its real end), or a hypothetical window starting now
     next chunks = 5h windows separated by WINDOW_GAP
     the chunk containing the reset is split; only the part before the reset is planned

2. CEILING of each chunk — the room extra work may take, after the user's predicted demand
   (a full window is 1 unit, so a ceiling of 0.9 is what `MIN_MARGIN = 0.1` leaves):
     margin(chunk)  = max(MIN_MARGIN, prediction.p95 for that chunk)     # no per-chunk forecast yet
                                                                          # → future chunks use MIN_MARGIN
     ceiling(chunk) = (1 − margin(chunk)) × (chunk duration / 5h)
     current chunk: ceiling −= what is already used in this window

3. BACK-FILL from the last chunk before the reset, backwards:
     remaining = remaining weekly quota (units)
     for chunk in reversed(chunks):
         planned[chunk] = max(0, min(ceiling[chunk], remaining))
         remaining     -= planned[chunk]

4. OUTPUT: amount_due = planned[current chunk]
           deadline   = end(current chunk) − DEADLINE_MARGIN
           unreachable = remaining after step 3        # quota that cannot be spent at all
```

Back-filling is what implements the guiding principle: every chunk earlier than strictly necessary gets 0, so the user keeps first claim on everything until it is about to expire. `amount_due = 0` is the normal case and the tick ends there in milliseconds. `unreachable > 0` is reported as unavoidable waste (`../CONSIDERATIONS.md` §6), not acted upon.

Nothing in the arithmetic assumes a weekday or an hour, so a Codex reset at 04:13 on a Sunday is handled identically.

The chain assumes a window always exists when one is needed. That assumption is made true by a separate stage, not by this one (`../04_start_windows/DESIGN.md`).

## 3. Output

The two numbers the executor is ultimately launched with, `amount_due` and `deadline`, plus the chain they came from:

```json
{"computed_at": 1790170589, "config_hash": "9f2c…",
 "agents": {
   "claude": {"amount_due": 0.30, "deadline": 1790188200, "unreachable": 0.0,
              "window": {"end": 1790188500, "used": 0.12}, "remaining_week": 2.70,
              "chunks": [{"start": 1790170589, "end": 1790172900, "ceiling": 0.40, "planned": 0.00},
                         {"start": 1790173200, "end": 1790191200, "ceiling": 0.90, "planned": 0.30}]},
   "codex":  {"amount_due": 0.00, "deadline": null, "unreachable": 0.0,
              "window": {"end": null, "used": 0.0}, "remaining_week": 6.10, "chunks": []}}}
```

`amount_due` is in units and `deadline` is an epoch second. The plan stage clamps the amount to the room left in the window and copies both into the mandate the supervisor reads, so `amount_due` here is the budget's view before that clamp. `deadline` is `null` exactly when `amount_due` is 0.

`aqm budget` prints exactly the table a human would draw (`--json` gives the same content for a script):

```text
claude · now 2026-09-20 22:17 · week resets Mon 21:00 (in 22h43m) · remaining 2.70 units

  chunk                     ceiling   planned   left
  [now       – Sun 21:35]      0.40      0.00   2.70
  [Sun 21:40 – Mon 02:40]      0.90      0.00   2.70
  [Mon 02:45 – Mon 07:45]      0.90      0.30   2.40
  [Mon 07:50 – Mon 12:50]      0.90      0.90   1.50
  [Mon 12:55 – Mon 17:55]      0.90      0.90   0.60
  [Mon 18:00 – Mon 21:00]      0.54      0.60   0.00     (split by the reset)

  amount_due 0.00 · deadline Mon 07:40 · decision: nothing due
```

## 4. On the forecast

The budget stage uses the prediction in exactly one place — the margin of step 2 — and never asks for a forecast beyond the current window: back-filling already leaves every earlier chunk to the user, so reserving weekly quota against a predicted week would spend later than necessary and waste more (`PREVIOUS_IDEAS.md` §4.5).

With today's `recent-rate-v1` the margin is `MIN_MARGIN` everywhere except the current window; with a real predictor the same formula tightens the ceiling where the user is likely to work and relaxes it where they are not, with no other change.

**The concentration problem, and why it is not fixed here.** Back-filling piles the plan into the chunks immediately before the reset, which on a Claude week is Monday daytime — the user's own hours. The fix is *lead time and burn rate*, not lower ceilings: start as late as the machine can still finish, and burn hard. Lowering ceilings would convert a bounded 5-hour collision into an unbounded 7-day shortage, which is the trade this project refuses (`PREVIOUS_IDEAS.md` §4.5). The lead-time rule lives in `../05_planning/DESIGN.md` §1.

## 5. Testing

Unit tests on chunk chains, the reset split, ceilings, back-fill and `unreachable`. Acceptance for P1: the table above matches a hand-computed example, and nothing acts.

## 6. Speed

This stage runs on **every** tick, and it is the cheapest of the three: **pure arithmetic over at most 34 chunks, with no I/O beyond two small files.**

The bound is structural. A 7-day period is 168 hours and a chunk is one 5-hour window plus `WINDOW_GAP`, so the chain of step 1 can never hold more than **34 chunks** — and the loops of steps 2 and 3 are single passes over it. There is no search, no iteration to convergence and no simulation: the back-fill is one reversed pass.

| Work | Cost |
| --- | --- |
| Read the latest prediction and the meter | Two small reads; the prediction is ~1 KB |
| Project, ceiling, back-fill | ≤ 34 iterations × 3 passes |
| Write the artifact and swap the `latest` symlink | ~2 KB, atomic |

No network, no subprocess, no database scan — everything it needs is the remaining weekly percentage, the two reset times and one prediction number.

**`amount_due = 0` is the normal case and the tick ends there**, which is what makes the common path free: the pipeline stops before planning and before spawning anything (`../07_pipeline/DESIGN.md` §2). On the great majority of the 288 ticks a day, the whole chain of ingest → predict → budget is the entire cost of running this system.
