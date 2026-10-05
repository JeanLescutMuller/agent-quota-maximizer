# test/

Every test for this project. Bash only; each file is independently runnable.

```bash
bash test/run.sh              # the whole suite
bash test/test_buckets.sh     # one file
```

| File | Covers |
|---|---|
| `harness.sh` | Shared helpers and fixtures. Sourced, never run |
| `test_readings.sh` | The drop_stale_readings, the window key, the raw record shapes, tail-and-watermark |
| `test_buckets.sh` | The 5-minute grid, deltas across ticks, the predictor's columns, the three attribution cases |
| `test_backfill.sh` | `--backfill` idempotence and disposability, and the API later stages call |
| `test_replay.sh` | **Tick-by-tick must equal one backfill.** The strongest check here |
| `test_predict.sh` | Stage 2: the rate, the persistence horizon, the S2 tripwire floor, and the two directions the forecast may fail in |
| `test_budget.sh` | Stage 3: the chain of remaining windows, the reset split, both ceiling limits, the back-fill and `already_lost_units` |
| `test_pipeline.sh` | The plumbing: config validation, the on/off switch, the lock, the staleness guard, housekeeping, metrics, exit codes |
| `test_real_data.sh` | The recorded history, read-only: reconciliation, the budget's conservation identity, and per-tick cost. Skips itself when `~/opt/agent-usage-tracker` is absent |

## Conventions

- **Hermetic.** Every test gets a throwaway `AQM_USAGE_DATA` and `AQM_HOME` under `mktemp -d`. Nothing writes to `~/opt`. `test_real_data.sh` reads the real logs but still writes its state to a temp dir.
- **No `jq`.** The only `jq` on this machine lives in a Conda prefix, and these tests must not depend on one. JSON is read with `/usr/bin/python3` through the `rows`, `meter` and `api` helpers.
- **No LLM is ever called.** Stages 4 and 6 will act and spend; their tests will use recorded fixtures and `--dry-run`, never a live agent. Worker prompts belong in `release/prompts/`, not here.
- **Determinism.** `aqm ingest --now <epoch>` fixes the clock, and `AQM_TAIL_BYTES` shrinks the tail so the doubling path can be exercised. `BASE` in the harness is slot-aligned (`BASE % 300 == 0`) because the grid assertions depend on it.
- **CSV out, JSONL in.** The state files this stage writes are CSV with a fixed header; the raw sources it reads stay JSONL. Percent columns are floats in memory, but a whole value is written without its trailing `.0`, so assertions read as plain integers. Types come from the declared column maps in `aqm.py`, never from guessing per value.
- **Rows are recognised by shape, not by `source`.** Upstream renamed `codex` to `codex_app_server` mid-development and an exact match silently produced zero Codex readings. `test_readings.sh` asserts that an unknown source still parses, and that a wholly unrecognisable file raises instead of reporting zero.

- **Every expected number is hand-computed in a comment beside its assertion**, in `test_budget.sh` and `test_predict.sh`. That is what makes them acceptance tests rather than snapshots of whatever the code currently does: a test whose expectation was copied from the output cannot fail for the right reason.
- **A test that cannot fail is worse than a missing one.** Three config assertions once passed while writing `config.json` to a directory that did not exist — the writes failed silently and an earlier `aqm` call had created the directory by the time the later ones ran. `setup()` now creates `$AQM_HOME`, and the lesson is in the harness as a comment.

## What is not covered yet

`apportioned` and `censored` attribution are unit-tested but have never been exercised against real data — all 21,856 real slots came out `exact`, because no worker has run yet. P4 is the first time those paths meet reality.

**S3** (the telemetry tripwire) is designed but not wired, so nothing tests it: ingestion does not read the telemetry rows yet (`../design/02_prediction/DESIGN.md` §3).

**The P2 acceptance criteria that need time** cannot be checked by this suite: a week of real ticks with zero errors, and whether the predicted p95 actually covers human demand 95% of the time. Those are read out of `logs/metrics.jsonl` in a notebook once the job has been running (`../design/07_pipeline/DESIGN.md` §8).
