# lab/

Ad-hoc analysis, run by hand. Not deployed, not scheduled. One folder per stage it studies, numbered like `design/`, so `lab/02_prediction/` and `design/02_prediction/` pair up by eye.

**The one rule: `release/` never imports `lab/`; `lab/` may import `release/`.** That is why this is the one place in the project allowed to use pandas, matplotlib and sklearn: `release/aqm/` is standard library only, so the scheduled job cannot break when a Conda environment moves (`../design/07_pipeline/DESIGN.md` §1). `test/test_imports.sh` enforces both halves.

| Folder | The question it answers |
|---|---|
| [`02_prediction/`](02_prediction/README.md) | Which forecaster predicts human demand best? Five candidates, one notebook each, measured on one shared bench (`dataset.py`). Stage 2 is parked since 2026-10-06; the bench is kept |
| `03_budgeting/` | Where does one budget decision come from? |
| `07_pipeline/` | What happens when the whole decision runs over the recorded history? |

| File | What it is |
|---|---|
| `03_budgeting/explain_budget.ipynb` | **Where a budget decision comes from.** One decision taken apart: every measurement, intermediate value and step of arithmetic, in the order the pipeline computes it |
| `03_budgeting/explain.py` | The loading and plotting helpers the notebook imports |
| `07_pipeline/pipeline_replay.py` | **Replays the whole decision over the recorded history** — budget, a just-in-time start, a bot burning quota — and counts waste left at each reset and the times the bot made the user wait. Every figure behind `../design/DESIGN_v2.md` §8 comes from it. Standard library only, about 10 s |

```bash
cd ~/dev/agent-quota-maximizer
/usr/bin/python3 lab/07_pipeline/pipeline_replay.py            # stdlib only
/opt/anaconda3/bin/jupyter lab lab/03_budgeting/explain_budget.ipynb
/opt/anaconda3/bin/jupyter lab lab/02_prediction/               # the forecasting bench
/opt/anaconda3/bin/python lab/02_prediction/live_replay.py      # the real stage, replayed
```

Notebooks import their helpers from their own folder, so open them from there (Jupyter does this by default).

## `explain_budget.ipynb`: using it

Two knobs, both in the first code cell:

```python
AT      = None       # None = now; or "2026-10-03T21:30"; or an epoch int
AGENT   = "claude"   # or "codex"
REFRESH = True       # append any new readings first
```

Set `AT` to any past moment and the whole notebook recomputes as of then, reading only data that existed at that time. That is the same `--at` mechanism the backtest uses, and it is why §6 can replay two days of decisions in one cell.

## Two rules `03_budgeting/` keeps

**It never recomputes what `release/aqm/` computes.** Every number comes back from `aqm.predict()` / `aqm.budget()` or out of the CSVs they read. A notebook that re-derived a ceiling or a rate would eventually disagree with the real one, and the notebook is the copy a human would be reading. This is why each window in the budget artifact carries its own `human_reserve_pct`, `max_spend_units_by_quota`, `max_spend_units_by_time` and `limited_by` (`../design/03_budgeting/DESIGN.md` §3) — the decomposition is produced once, by the code that decides, and merely displayed here.

**It writes nothing except ingested history.** `predict()` and `budget()` are pure functions; only the CLI and `pipeline()` write artifacts. `REFRESH=True` calls `aqm.ingest()`, which appends to `data/<agent>/` — the same append-only, idempotent operation the scheduler performs, on a directory that is explicitly rebuildable (`../design/01_ingestion/DESIGN.md` §6.0). Nothing here touches `state/`, `artifacts/` or `config.json`.

A past moment is read through `aqm.predict(at=…)` and then handed to `aqm.budget(prediction=<path>)` **by path**. That detail matters: the auto-resolved `state/latest/prediction.json` is freshness-checked, so a backtest at a past moment would otherwise come back `stale-prediction` with every ceiling at zero. An explicit path is exempt, which is what makes a backtest possible at all.

## What its §6 is for

A single tick cannot tell you whether the system behaves; the sweep can. Replaying 48 hours at 30-minute steps on the recorded history answers three questions at once:

- does `extra_quota_to_spend_units` appear only as the reset approaches, and stay at 0 before?
- does `already_lost_units` grow while we watch — quota becoming unsaveable in real time?
- did the **S2** tripwire fire *before* the meter moved, rather than after?

The first real run of it showed all three, including S2 firing 27 minutes before the meter caught up. It also showed something the design had not said out loud: because the forecast closes the current window while the user is active, **being busy now converts quota into `already_lost_units` later**. That is the correct trade — the user has first claim — but it is the mechanism by which an active week still wastes quota, and it is visible here rather than in any single tick.
