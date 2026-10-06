# TODO 02 — P2b, the code half: make `release/aqm/` match the 2026-10-06 design

**Status:** ready to execute, but **§5 has two questions for the user** that should be answered first. **Written:** 2026-10-06, against `a951ecf`.

**One agent takes this file and does the whole thing.** It changes `release/aqm/` and five test files, so two agents in here at once will conflict on every one of them.

> **This is half of P2b, on purpose.** `design/DESIGN_v2.md` §6 defines P2b as *"Move to the VM (§8.1) … **then** the §8 changes in code"*, with acceptance *"`aqm pipeline` ticks on the VM from its own meter readings; the Mac runs nothing scheduled for this project."* This file is only the second clause — §1's six code changes. The VM half (installing Codex there, a meter reader for both agents, cloning the repos, a systemd user timer replacing the LaunchAgent) is **deployment on a second machine and needs the user's explicit authorisation**, which has never been given for this project — `release/install.sh` has not been run even on the Mac.
>
> The two halves are independent and this one is safe to do first: every change below is machine-independent and testable on the Mac today, and doing it first means the VM gets correct code rather than code that has to be fixed remotely. One caveat — **the VM starts with an empty `data/`**, so the measured `week_capacity_units` of §2.B will have no complete weeks there and will fall back to its seed for two weeks. Worth knowing before anyone reads a low amount on the VM as a bug.

---

## 0. Why this exists, and what to check first

On 2026-10-06 another session reviewed stages 1–3 by replaying the **whole decision** over the 42 recorded days (`lab/07_pipeline/pipeline_replay.py`, or `notebook/pipeline_replay.py` if TODO 01 has not run yet) and rewrote the design. Its own commit message says: *"The code catches up in P2b."* That is this file.

`design/DESIGN_v2.md` §8 is the index of what changed. This file is the delta between it and `release/aqm/`.

```bash
cd ~/dev/agent-quota-maximizer
git status --porcelain                    # must be empty
git log --oneline -1 -- release/ test/    # expect c5bd27a — if later, someone started P2b
bash test/run.sh | tail -1                # must pass BEFORE you start
grep -c MIN_HUMAN_RESERVE_PCT release/aqm/core.py   # expect 1 — the param table is still as described
```

**Stop and ask the user** if the tree is dirty, if the suite is red before you begin, or if something has already landed in `release/`. **Do not push** — the repo is public and there are already several unpushed commits by two authors.

**TODO 01 (the `lab/` move) is independent of this.** Either order works. If 01 has already run, read every `notebook/…` path in this file as `lab/…`.

---

## 1. The gap, in one table

| # | Design says | Code does | Owns the rule |
|---|---|---|---|
| **A** | The week meter never decreases | filters on the **window** percent only; the week rides along unchecked | `01_ingestion/DESIGN.md` §5 |
| **B** | `week_capacity_units` is **measured** | `P["WEEK_CAPACITY_UNITS"] = {claude: 8.85, codex: 6.2}`, a constant | `03_budgeting/DESIGN.md` §2 |
| **C** | The window outliving the weekly reset gets only its **share before the reset** | gets the full 75% | `03_budgeting/DESIGN.md` §2 |
| **D** | No forecast: the reserve is the constant 25% | `max_spend_parts` takes `p95` and does `max(floor, p95)` | `02_prediction/DESIGN.md` §9 |
| **E** | Stage 2 leaves the tick | `pipeline()` runs `predict` every tick and budget reads the artifact | `02_prediction/DESIGN.md` §9 |
| **F** | Five parameters parked, two removed | all still live in `P` | `07_pipeline/DESIGN.md` §11 |

Each is independently committable and independently verifiable. **Do them in the order A → F**, because B's measurement is only correct once A is fixed, and C/D change the same function.

---

## 2. The work

### A. Ingestion: the week meter obeys "never decreases"

`release/aqm/s1_ingest.py`, `drop_stale_readings()`. It keeps a reading when the **window** percent reaches a new high and carries the week percent along unchecked. The two Claude sources disagree on the week by one point, the poller always ahead (`CONSIDERATIONS.md` §22), so the week level goes 21 → 20 → 21 and every round trip counts another point of movement: `slot_week_used_pct` summed to **111% for a week whose peak was 73%**, and the latest reading — which budgeting reads — can be a point low.

Apply the rule already used for the window: **the week level kept is the running maximum per `week_end_ts`, and a reading below it moves nothing.** `best` is the dict that already carries per-window state across ticks; add the per-week maximum to it the same way.

**Watch out:** `best` is persisted between ticks, so whatever key shape you choose has to survive a round trip through its storage. The window maximum is keyed by the raw `window_end_ts` integer; follow that.

Verify:

```bash
./release/aqm-cli ingest --backfill          # ~2 s, rebuilds data/ from raw history
bash test/run.sh | tail -1
```

Then confirm the double-count is gone — it should come out near the week's peak rather than 111%:

```bash
/usr/bin/python3 -c "
import csv, collections, pathlib
d = pathlib.Path.home()/'opt/agent-quota-maximizer/data/claude'
rows = list(csv.DictReader((d/'slots.csv').open()))
print('summed slot_week_used_pct:', round(sum(float(r['slot_week_used_pct'] or 0) for r in rows), 1))
m = [r for r in csv.DictReader((d/'meter.csv').open()) if r['week_used_pct']]
print('sum of per-week peaks:   ', round(sum(
    max(float(x['week_used_pct']) for x in m if x['week_end_ts']==k)
    for k in {r['week_end_ts'] for r in m if r['week_end_ts']}), 1))"
```

`test_real_data.sh` asserts the same invariant for the **window** meter; consider adding the weekly twin there.

### B. Budget: `week_capacity_units` measured, not constant

`release/aqm/s3_budget.py:110` is the only use:

```python
remaining = (100.0 - m["week_used_pct"]) / 100.0 * P["WEEK_CAPACITY_UNITS"][agent]
```

Replace with the measurement from `03_budgeting/DESIGN.md` §2:

```
week_capacity_units = sum(slot_window_used_pct) over the last two complete weeks
                      ─────────────────────────
                      sum(peak week_used_pct) of those same weeks       × 100
```

**Put the function in `release/aqm/history.py`, not in `s3_budget.py`.** It is a question asked of the ingested table, which is exactly what that module is for, and `s3_budget` is already allowed to import `history` (`test_imports.sh`). Putting it in `s3_budget` would work but puts a second kind of thing in a stage module.

**Read §4.1 before writing this — the formula cannot be computed as the design states it.**

Measured here on 2026-10-06, as a target to reproduce:

| Week ending | Peak week % | Window movement | Implied units/week |
|---|---|---|---|
| 2026-08-31 | 88% | 888 | 10.09 |
| 2026-09-14 | 27% | 247 | 9.15 |
| 2026-09-21 | 56% | 460 | 8.21 |
| 2026-10-05 | 73% | 548 | **7.51** |

The design quotes 8.2, 7.3 and 7.6 for the last three, so expect the same ballpark but **not** identical figures — the numbers above come from an approximate slot→week join (§4.1) and the design's come from `pipeline_replay.py`. Reproducing the design's exact values is not the acceptance criterion; agreeing with `pipeline_replay.py` on the same input is.

Also required by the design: **until two complete weeks exist for an agent, the last measured value stands.** So the constant does not disappear entirely — it becomes a seed. Decide where the "last measured" value lives (`state/` is the natural home; it is not rebuildable, which is correct for this) and say so in `VOCABULARY.md`.

### C. Budget: the window that outlives the weekly reset

`release/aqm/s3_budget.py`, `max_spend_parts()`. Today:

```python
max_spend_units_by_quota = (100.0 - reserve_pct - used_pct) / 100.0
```

Design (`03_budgeting/DESIGN.md` §2, step 2):

```
max_spend_units_by_quota = ((100 − human_reserve_pct) × share_before_reset − window_used_pct) / 100
```

`share_before_reset` is 1 for every window except the one containing the weekly reset. A weekly reset does **not** reset the 5-hour window (`CONSIDERATIONS.md` §13), so filling that window to 75% just before the reset hands the user a window three-quarters full at the start of their new week. The replay says this one rule removed the collisions that appeared once the bot ran on an always-on machine.

`remaining_windows()` already cuts that window at the reset — `"end_ts": min(end, week_end_ts)` — so the share is the cut duration over the full 5 hours. Compute it there and carry it on the window dict, rather than recomputing it inside `max_spend_parts`.

Add `share_before_reset` to what each window carries in the artifact, and to `VOCABULARY.md` §5. The artifact exists so that *"why was this window's ceiling 0.16?"* is answerable from the file alone; a share applied invisibly breaks that.

**Hand-compute the expected number beside each new assertion**, as every other case in `test_budget.sh` does. A window starting one hour before the reset: `share = 1/5`, so `(75 × 0.2 − 0)/100 = 0.15`, not 0.75.

### D. Budget: drop the forecast

With stage 2 parked the reserve is a constant, which deletes a surprising amount:

| In `release/aqm/s3_budget.py` | |
|---|---|
| `max_spend(window, used, p95)` and `max_spend_parts(window, used, p95)` | drop the `p95` argument |
| `reserve_pct = floor if p95 is None else max(floor, p95)` | becomes `P["MIN_HUMAN_RESERVE_PCT"]` |
| `budget_agent(agent, now, p95)` | drop the argument |
| `budget(at=None, prediction=None)` | drop `prediction`, the `latest_artifact("predictions")` read, and the staleness check |
| `method` values `stale-prediction` and `fail-safe` | gone — there is no input artifact to be stale or unreadable. `03_budgeting/DESIGN.md` §2 says so explicitly |

`03_budgeting/DESIGN.md` §2's refusal table shrinks to two rows: `no meter reading` and `no usable weekly reading`. Keep both.

**`aqm predict` stays as a command** (`02_prediction/DESIGN.md` §9: *"What stays… `aqm predict` as a command for analysis"*). Do not delete `s2_predict.py`, its tests, or the bench. Only its place in the tick goes.

### E. Pipeline: stage 2 leaves the tick

`release/aqm/pipeline.py`:

- lines ~50–52: the `predict(at=now)` call and `write_artifact("predictions", …)`
- lines ~79–84: the metrics line's `predicted_p95_human_usage_pct` field

Removing a field from `metrics.jsonl` is a schema change for the only consumer that exists, `lab/03_budgeting/explain.py` (or `notebook/explain.py`) — check it and the notebook before assuming nothing reads it.

`housekeeping()` prunes `artifacts/predictions/`. Leave the pruning: the directory keeps its history and `aqm predict --out` can still write there.

Check `budget_table()` too — it may render a forecast column.

### F. Parameters

`release/aqm/core.py`, the `P` dict. **`GUARD_PCT` and `MAX_DAILY_UNITS` are not in the code at all** — they were design-only, for stages that do not exist, and `bf35b6b` already removed them from `07_pipeline/DESIGN.md` §11. Do not go looking for them. The one mention left is prose in a comment at `release/aqm/core.py:62`, which should now say the conflict is moot rather than settled.

| Parameter | Action |
|---|---|
| `WEEK_CAPACITY_UNITS` | → the measurement of **B**; keep a seed value, see §5.2 |
| `ARTIFACT_MAX_AGE_SECONDS` | remove — nothing reads an input artifact any more |
| `SAFETY_MULTIPLIER`, `BURN_LOOKBACK_MINUTES`, `MIN_RATE_DENOMINATOR_MINUTES`, `ACTIVE_HUMAN_BURN_RATE` | **keep** — `aqm predict` still uses all four. They are parked, not removed. Say so in the comment beside each, pointing at `02_prediction/DESIGN.md` §9 |
| `HUMAN_IDLE_MINUTES` (30) | the design's §11 adds it, but **its only consumer is stage 5, which does not exist**. See §5.1 |
| `TICK_INTERVAL_SECONDS` | pre-existing: declared and never read; the 5 minutes live in the plist. Either wire it or delete it, and say which |

`load_config()` raises on an unknown parameter, so every removal is also a config-compatibility change. `release/config.json.template` must not name a parameter that no longer exists.

---

## 3. Tests that will move

Counted on `a951ecf`, by how many lines mention prediction or a removed parameter:

| File | Lines | What to expect |
|---|---|---|
| `test_predict.sh` | 39 | Most should **survive** — `aqm predict` still works. The section proving budget ignores `internals` has to go, because budget no longer reads a prediction at all |
| `test_pipeline.sh` | 10 | The tick no longer has a `predict` stage; `stages` and the metrics line both change |
| `test_imports.sh` | 11 | Written in `c5bd27a`. The layering still holds — but once `pipeline.py` stops importing `s2_predict`, tighten its allowance rather than leaving a permission nothing uses |
| `test_budget.sh` | 8 | The hand-computed figures change for **C**, and the p95 cases go with **D** |
| `test_slots.sh` | 2 | Probably only prose |

Expect the count to **fall** below 256 as prediction cases are deleted, then rise as C and B gain cases. A falling number is correct here; say so in the commit so it does not read as lost coverage.

---

## 4. Two gaps in the design you will hit

### 4.1 The capacity formula cannot be computed as written

`03_budgeting/DESIGN.md` §2 says:

```
week_capacity_units = sum(slot_window_used_pct) / sum(peak week_used_pct) × 100
```

over "the last two complete weeks". But **`slots.csv` carries no week-end column**, so nothing in it says which 7-day period a slot belongs to:

```
slots.csv  slot_start_dt, slot_id, window_end_dt, window_id, window_used_pct,
           week_used_pct, slot_window_used_pct, slot_week_used_pct, …
                                                      └── no week_end_dt / week_id

meter.csv  …, window_end_dt, window_end_ts, week_end_dt, week_end_ts, …
                                             └── only here
```

Three ways out, in the order I would try them:

1. **Join to `meter.csv`** at read time: a slot's week is the week whose span contains it. No schema change, and `history.py` is where the join belongs. This is what the figures in §2.B came from, using the nearest reading — do it properly, by span.
2. **Add `week_id` to `slots.csv`**, mirroring `window_id`. Cleanest to read afterwards, but it is a CSV schema change: header tests, `VOCABULARY.md` §3, and a `--backfill`. The header tests exist to make this deliberate, so it is a decision, not a detail.
3. Group by calendar week. **Do not** — resets are not aligned to calendar weeks, and Codex's land at times like 04:13 on a Sunday.

Whichever you pick, **state it in `03_budgeting/DESIGN.md` §2** so the formula there stops being uncomputable.

### 4.2 Codex's `week_end_ts` is not stable

Grouping Codex readings by `week_end_ts` produces more groups than there are weeks, several with a 0% peak:

```
  2026-09-06   peak  0%     2026-09-15  peak 0%     2026-09-28  peak 0%
  2026-09-07   peak 46%     2026-09-15  peak 2%     2026-09-28  peak 2%
```

So "the last two complete weeks" needs a definition of *complete* that survives this, and a 0%-peak group is a division by zero waiting to happen. `01_ingestion/DESIGN.md` §5 already documents Codex's fake idle countdown (`FAKE_TOLERANCE`), which is probably the same root cause. The design acknowledges Codex's measurement is the weaker of the two but does not say what to do when it is unusable.

**Suggestion, not a decision:** require a minimum peak (the §2.B table used ≥ 20%) before a week counts as complete, and fall back to the last good measurement otherwise — which the design already mandates for the first two weeks anyway. Whatever you choose, write it down.

---

## 5. Questions for the user — ask before starting

### 5.1 Add `HUMAN_IDLE_MINUTES` now, or with stage 5?

`07_pipeline/DESIGN.md` §11 adds it, but its only consumer is `05_planning/DESIGN.md` §3 — stage 5, which does not exist. Adding it now creates a parameter that nothing reads, which is exactly the smell `TICK_INTERVAL_SECONDS` already is in this same dict.

**Recommend:** leave it in the design, add it to `P` in P5 with the code that uses it. But it is the user's call, since they may prefer the parameter table and `P` to match exactly.

### 5.2 Where does the last measured `week_capacity_units` live?

The design requires a fallback for an agent without two complete weeks. Options: a seed constant in `P` (visible, but then it is still a hard-coded 8.85 in the file), or `state/` (not rebuildable, which is the honest classification, but invisible until you look).

**Recommend:** `state/`, with the seed in `P` used only on a cold start, and both named in `VOCABULARY.md`. Flagging it because it decides whether `WEEK_CAPACITY_UNITS` disappears from `P` or merely changes meaning — and the user has been explicit about parameters meaning one thing.

---

## 6. Acceptance

```bash
cd ~/dev/agent-quota-maximizer
bash test/run.sh                       # green; count may be below 256 — see section 3
./release/aqm-cli ingest --backfill    # ~2 s
./release/aqm-cli budget               # the amount, and a reason when 0
./release/aqm-cli predict              # still works — parked, not deleted
./release/aqm-cli pipeline --json      # "stages" no longer contains "predict"
```

Then the two that matter most, because they are the reason for the change:

- **The weekly double-count is gone** (A) — the §2.A snippet.
- **The replay agrees with the code.** `pipeline_replay.py` reimplements budget in closed form, including the measured capacity and the reset share. After B and C the two should agree; before them they cannot. If they disagree, **the replay is not automatically right** — it was written to explore, the code is what runs. Find out which is wrong before changing either.

A `--at` comparison against the pre-P2b code is **not** a useful check here: the decisions are meant to change. Where `predict` is concerned it still is, since nothing about stage 2 changes.

---

## 7. Out of scope

| | Why |
|---|---|
| **Pushing** | several unpushed commits by two authors, and the repo is public |
| **Moving the bot to the VM** (`DESIGN_v2.md` §8.1) | **part of P2b, but not of this file** — see the note under the title. It is deployment on a second machine and needs the user's authorisation. Its own TODO |
| **Stages 4, 5, 6** | P3/P4/P5. The rule's parts 3 and 4 (just-in-time start, not while the user is active) belong to stage 5 and are not written here |
| **Deleting `s2_predict.py`, its tests, or `lab/02_prediction/`** | parked, not deleted (`02_prediction/DESIGN.md` §9) |
| **Dropping `slot_bot_usd` / `slot_human_usd`** | empty in all 22,586 rows, and §1.3–1.4 will not be built — but removing a column is a CSV schema change the design does not ask for. Leave them; raise it separately if it bothers you |
| **`test_real_data.sh` flakiness** | pre-existing: failed once on `planned + unreachable = remaining` at 8.407, reproduced on the pre-split monolith, now reads 8.496. It reads live data while ingestion may be writing. Its own TODO |

---

## 8. The commit

Several commits are better than one here — A, B, C+D, E, F each stand alone and each keeps the suite green. If you prefer one, say in the body which of A–F it contains.

End every message with:

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

Update `AGENTS.md`'s status line: it still says P0–P2 and names P3/P4 as next, with no mention of P2b.

`design/DESIGN_v2.md` §6 already has its P2b row — do **not** add one. Mark only the code clause done and leave the VM clause open, so the row stays honest about what is left. And `DESIGN_v2.md` §8 ends with *"**The code does not follow yet.** `release/aqm/` still runs stage 2 every tick and budget still reads its forecast"* — that is the sentence to delete when this is finished, and the single best check that it really is.
