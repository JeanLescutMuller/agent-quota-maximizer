# lab/02_prediction/ — the forecasting bench

One notebook per candidate forecaster in this folder, all measured on the same rows, the
same split and the same metrics, so the comparison means something.

```bash
/opt/anaconda3/bin/jupyter lab lab/02_prediction/
```

| File | What it is |
|---|---|
| `dataset.py` | **The bench.** Features, the three-way split, the metrics, the cell breakdown. Every notebook imports it and none of them redefines any of it |
| `results/*.json` | One per approach: the configuration it froze, and the validation numbers that chose it |
| `live_replay.py` | **Replays the real stage**, calling `aqm.predict_agent()` with `aqm.P` patched. Any change to `BURN_LOOKBACK_MINUTES` or `SAFETY_MULTIPLIER` must be justified here, not in a notebook: the notebooks tune a simplification that omits the window-average arm, so they will keep asking for a bigger multiplier than this engine needs (`../../design/02_prediction/DESIGN.md` §8) |

| Notebook | Approach |
|---|---|
| `00_constant_reserve.ipynb` | One tuned number, ignoring everything. The null hypothesis every approach must beat |
| `01_baseline_recent_rate.ipynb` | What the stage does today: one recent rate, extrapolated, times a multiplier |
| `02_state_clock_table.ipynb` | Empirical p95 per (state × clock band) — a lookup, no model |
| `03_sklearn_quantile.ipynb` | Gradient boosting with `loss="quantile"`, so the p95 is predicted directly |
| `04_hierarchical_bayes.ipynb` | Smooth priors over day-of-week and time-of-day, recent usage as the shift, residual spread as the quantile |
| `05_comparison.ipynb` | **The only notebook that reads the test set** |

## The protocol

```
  train 60%          validation 20%        test 20%
  fit here           choose here           read ONCE, in 05
       \                   |                    |
        \                  v                    v
         +----> an approach notebook -----> results/<name>.json
                freezes its config here        refit on train+validation,
                                               scored in 05_comparison
```

Three rules, and breaking any one of them invalidates the study:

1. **The split is by calendar time, never random.** Anchors sit 5 minutes apart and
   each target reaches up to 5 hours ahead, so a random holdout puts nearly every
   test anchor's own hours into training.
2. **Each seam carries a 300-minute embargo**, or the last anchors of a split predict
   into the next one.
3. **An approach notebook never scores itself on test.** It tunes on validation and
   calls `D.save(...)`. `05_comparison.ipynb` refits on train + validation and reads
   test once. If you tune after reading test, the number is gone and only new data
   brings it back.

## What is being predicted

Exactly what the stage promises (`../../design/02_prediction/DESIGN.md` §1.1): the p95 of human burn, in
percent of a 5-hour meter, from now to the end of the current window. The window
contributes one number, `N`, the minutes it has left — so each approach is fitted at
`N` ∈ {30, 60, 120, 180, 300} and nothing in the feature set knows a window exists.

## The objective is money, not accuracy

Two unit costs, per **percent of a 5-hour meter**:

| | $/pct | What it is |
|---|---|---|
| `COST_WASTED` | **0.32** | quota the bot was refused although the human never came. Exactly 1% of a $32 unit — a certain loss, and the loss this project exists to remove |
| `COST_STOLEN` | **3.20** | quota the human turned out to want and the bot took. **10×**, because the user has first claim: we would rather waste nine points than take one |

Every approach is tuned to minimise `C_stolen × shortfall + C_wasted × excess`,
measured through the reserve the budget stage really applies,
`max(prediction, MIN_HUMAN_RESERVE_PCT)`.

### The cost model derives the quantile

Minimising an asymmetric linear cost over a predicted quantity is the newsvendor
problem, whose solution is the `q`-quantile of demand with

```
  q = C_stolen / (C_stolen + C_wasted) = 3.20 / 3.52 = 0.909
```

So nobody has to choose 0.95 and hope. `00_constant_reserve.ipynb` confirms it
empirically: the cheapest constant reserve equals the measured q90.9 of demand at
every horizon, to the percentage point.

`pinball` and `coverage` are still printed, as diagnosis — but they do not decide
anything. `pinball` at 0.95 penalises under-prediction 19× more than over-prediction,
so it happily ranks a ruinously wasteful model first.

### Tuning is per horizon, and has to be

The cost-optimal reserve is the q-quantile of demand over the next `N` minutes, and
that grows with `N` — measured on validation it runs 1, 4, 8, 13, 22 percent across
the five horizons. One setting forced to cover all five is dominated by the short
ones, where the `MIN_HUMAN_RESERVE_PCT` floor already pays the whole bill, and the
answer degenerates to "reserve nothing". The live stage knows `N`, so it may carry one
setting per horizon and interpolate. `D.tune()` therefore returns one config per `N`,
and each approach freezes all five.

### Two costs, two numbers

`cost per anchor` is the ranking figure: it uses every row, so it has the least
variance, but it is **not** a weekly bill — anchors sit 5 minutes apart and their
horizons overlap, so the same quota is counted many times. `weekly_usd` subsamples
one anchor every `N` minutes so each percent is counted once, and that is the figure
to quote at a human.

## And the cells

`D.by_cell()` breaks accuracy down by (state × clock band). Two cells decide whether
a forecaster is usable at all:

- **`idle` / night** — the quota this project exists to harvest. A model that
  predicts high here defeats the purpose.
- **`idle` / morning** — a user about to start work. A model that predicts low here
  puts the bot in front of them.

They pull in opposite directions, and the aggregate metrics hide both.

## Nothing here ships as-is

`release/aqm/` is standard library only, so the tick cannot import sklearn
(`../../design/07_pipeline/DESIGN.md` §1). A winning approach has to leave as something
stdlib can evaluate:

| Approach | Exports to | Feasible in a tick |
|---|---|---|
| recent rate | two constants | trivially |
| state × clock | a dict of ~36 numbers | trivially |
| hierarchical Gaussian | ~38 coefficients + 3 σ per horizon, about 10 KB of JSON | yes |
| gradient boosting | hundreds of trees | only by evaluating trees in pure Python |

So a model that wins by a small margin and cannot be exported loses.
