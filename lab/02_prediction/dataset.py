"""The shared bench every candidate forecaster is measured on.

One module, imported by every notebook in this folder, so that no two approaches can
accidentally be scored on different rows, a different split or a different metric.
Nothing here is imported by `release/`: this is investigation, and the package stays
standard-library only (`../../design/07_pipeline/DESIGN.md` §1).

What is being predicted is fixed by the stage's specification (`../../design/02_prediction/DESIGN.md` §1.1):
**the p95 of human burn, in percent of a 5-hour meter, between now and the end of the
current window.** The window contributes exactly one thing, `N`, the minutes it
happens to have left, so every model here answers at five values of `N` and nothing
in the feature set knows about windows at all.

## The split

Three parts, in calendar order, never at random:

    |<---------- train 60% --------->|<-- validation 20% -->|<--- test 20% --->|
                                   ^^^                    ^^^
                                 embargo                embargo

- **train** fits a model.
- **validation** chooses between models and tunes them. Look at it as often as
  you like.
- **test** is read **once**, by `05_comparison.ipynb`, after every approach has
  frozen its configuration. An approach notebook that scores itself on test has
  destroyed the only honest number in the study.

Two details make the split real rather than decorative:

**Random splitting would leak.** Anchors sit 5 minutes apart and each one's horizon
covers up to 5 hours, so a random holdout puts almost every test anchor's own hours
into training. Splitting by calendar time is the only defence.

**Each boundary carries an embargo of `max(HORIZONS)` minutes.** Without it, an
anchor at the end of train has a target that reaches into validation, which is the
same leak in miniature.

## The cost

Accuracy is not the objective; money is. Two unit costs, both per **percent of a
5-hour meter**, and every approach is tuned to minimise their sum:

    COST_WASTED  $0.32   quota the bot was refused although the human never came.
                         Exactly 1% of a $32 unit -- a certain, measurable loss, and
                         the loss this project exists to remove.

    COST_STOLEN  $3.20   quota the human turned out to want and the bot was allowed
                         to take. Ten times worse, because the project's premise is
                         that the user has first claim: we would rather waste nine
                         points than take one. It is also roughly the gap between
                         subscription and API pricing for the same work, which is
                         what the user would actually pay to get back what was taken.

The 10:1 ratio is a judgement, so `05_comparison.ipynb` sweeps it from 1:1 to 100:1
and shows where the ranking changes. If the ranking is stable across that range, the
judgement did not matter; if it flips, the number deserves an argument.

**This is why the quantile is no longer a requirement.** Minimising
`C_stolen x shortfall + C_wasted x excess` over a predicted reserve is exactly
quantile regression at

    q = C_stolen / (C_stolen + C_wasted) = 3.20 / 3.52 = 0.909

so the cost ratio *derives* the quantile rather than us picking 0.95 and hoping. Each
approach is still free to find a different operating point on validation -- a model
whose errors are skewed may do better away from the theoretical optimum -- but 0.909
is where the theory says to look.

## The metrics

`pinball` at 0.95 is the statistically correct loss for a quantile, and `coverage`
(the share of actuals at or below the prediction, which should be 95) says whether a
model is biased low. But both can rank a model well that is useless here, so two
project-specific costs are reported beside them, in percent of a 5-hour meter per
anchor:

- **stolen** — quota the human turned out to want and the bot was allowed to take.
  This is the cost the project must not pay: it is the user queueing behind a bot.
- **wasted** — quota the bot was refused although the human never came. This is the
  cost the project exists to eliminate.

A model that predicts 100 everywhere steals nothing and is worthless. A model that
predicts 0 everywhere wastes nothing and is dangerous. The reserve the budget stage
actually applies is `max(prediction, MIN_HUMAN_RESERVE_PCT)`, so both costs are
measured through that floor rather than against the raw prediction.
"""
import datetime as dt
import math
import pathlib
import sys

import numpy as np
import pandas as pd

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "release"))
import aqm                                                  # noqa: E402

LOCAL = dt.datetime.now().astimezone().tzinfo
HORIZONS = [30, 60, 120, 180, 300]          # minutes ahead to forecast
LAGS = [5, 15, 30, 60, 120, 240, 480, 1440, 2880]   # minutes of history as features
QUANTILE = 0.95                             # reported for continuity; not the objective
COST_WASTED = 0.32                          # USD per percent of a 5-hour meter
COST_STOLEN = 3.20                          # USD per percent -- 10x, the user has first claim
UNIT_USD = 32.0                             # one whole 5-hour meter, Claude Pro
FRACTIONS = (0.60, 0.20, 0.20)              # train, validation, test
P_FLOOR = aqm.P["MIN_HUMAN_RESERVE_PCT"]   # the reserve the budget stage applies anyway
RESULTS = pathlib.Path(__file__).resolve().parent / "results"


# --------------------------------------------------------------------------
# the table
# --------------------------------------------------------------------------

def build(agent="claude") -> pd.DataFrame:
    """One row per 5-minute anchor: what was knowable then, and what the human
    actually went on to burn over each horizon."""
    rows = aqm.read_csv(aqm.slots_path(agent), aqm.SLOT_TYPES)
    start = np.array([int(r["slot_id"].split("_")[0]) for r in rows])
    # A censored slot's upper bound is the conservative reading, the same choice the
    # live stage makes (`../../design/01_ingestion/DESIGN.md` §6.3).
    burn = np.array([r["slot_window_human_pct_hi"] if r["slot_window_human_pct"] is None
                     else r["slot_window_human_pct"] for r in rows], dtype=float)
    active = np.array([1.0 if r["was_human_active"] else 0.0 for r in rows])
    awake = np.array([1.0 if r["was_machine_awake"] else 0.0 for r in rows])
    order = np.argsort(start)
    start, burn, active, awake = start[order], burn[order], active[order], awake[order]

    n = len(start)
    step = aqm.SLOT_SECONDS
    cum = {"burn": np.concatenate([[0.0], np.cumsum(burn)]),
           "active": np.concatenate([[0.0], np.cumsum(active)]),
           "awake": np.concatenate([[0.0], np.cumsum(awake)])}
    idx = np.arange(n)

    def span(lo, hi, which):
        c = cum[which]
        return c[np.clip(hi, 0, n)] - c[np.clip(lo, 0, n)]

    when = np.array([dt.datetime.fromtimestamp(t, LOCAL) for t in start])
    hour = np.array([w.hour + w.minute / 60.0 for w in when])
    feat = {"dow": np.array([w.isoweekday() for w in when], dtype=float)}
    # Three harmonics rather than one: the measured profile has a hard edge around
    # 08:00 that a single sine cannot bend around, and the shrinkage in a ridge or a
    # Gaussian prior is what decides how much of each harmonic survives -- which is
    # "smooth unless the data proves otherwise", expressed as a basis.
    for k in (1, 2, 3):
        feat["hour_sin%d" % k] = np.sin(2 * math.pi * k * hour / 24.0)
        feat["hour_cos%d" % k] = np.cos(2 * math.pi * k * hour / 24.0)
    feat["dow_sin"] = np.sin(2 * math.pi * feat["dow"] / 7.0)
    feat["dow_cos"] = np.cos(2 * math.pi * feat["dow"] / 7.0)
    feat["is_weekend"] = (feat["dow"] >= 6).astype(float)

    for lag in LAGS:
        k = lag * 60 // step
        feat["burn_%dm" % lag] = span(idx - k, idx, "burn")
        feat["active_%dm" % lag] = span(idx - k, idx, "active")
        feat["awake_%dm" % lag] = span(idx - k, idx, "awake")

    # Minutes since the human last burned anything: the most informative single
    # scalar in the rule-based engine, so every model is given it too.
    since, seen = np.empty(n), -1
    for i in range(n):
        if burn[i] > 0:
            seen = i
        since[i] = (i - seen) * step / 60.0 if seen >= 0 else 9999.0
    feat["minutes_since_burn"] = since

    frame = pd.DataFrame(feat)
    for N in HORIZONS:
        k = N * 60 // step
        y = span(idx, idx + k, "burn")
        y[idx + k > n] = np.nan      # not enough recorded future to score it
        frame["y%d" % N] = y
    frame["ts"] = start
    frame["when"] = when
    frame["hour"] = hour.astype(int)
    return frame


def features(frame) -> list:
    """The feature columns, in a fixed order, so an exported model's coefficients
    can be matched back to names."""
    return [c for c in frame.columns
            if not c.startswith("y") and c not in ("ts", "when", "hour")]


# --------------------------------------------------------------------------
# the split
# --------------------------------------------------------------------------

def split(frame, verbose=True):
    """train, validation, test -- in calendar order, with an embargo at each seam."""
    ts = frame["ts"].values
    lo, hi = ts.min(), ts.max()
    cut1 = lo + (hi - lo) * FRACTIONS[0]
    cut2 = lo + (hi - lo) * (FRACTIONS[0] + FRACTIONS[1])
    embargo = max(HORIZONS) * 60

    train = frame[ts <= cut1 - embargo]
    validation = frame[(ts > cut1) & (ts <= cut2 - embargo)]
    test = frame[ts > cut2]
    if verbose:
        total = len(frame)
        print("%d anchors over %.1f days, embargo %d min at each seam"
              % (total, (hi - lo) / 86400, embargo // 60))
        for name, part in (("train", train), ("validation", validation), ("test", test)):
            print("  %-11s %5d rows  %s -> %s"
                  % (name, len(part), part["when"].iloc[0].strftime("%m-%d %H:%M"),
                     part["when"].iloc[-1].strftime("%m-%d %H:%M")))
        print("  dropped to the embargo: %d" % (total - len(train) - len(validation) - len(test)))
    return train.copy(), validation.copy(), test.copy()


# --------------------------------------------------------------------------
# the metrics
# --------------------------------------------------------------------------

def optimal_quantile(stolen=None, wasted=None) -> float:
    """The quantile that minimises the cost. Under linear asymmetric costs the
    cost-minimising reserve is the `q`-quantile of the demand distribution with
    `q = C_stolen / (C_stolen + C_wasted)`; this is the newsvendor result, and it is
    why the stage never needed a `SAFETY_MULTIPLIER`."""
    stolen = COST_STOLEN if stolen is None else stolen
    wasted = COST_WASTED if wasted is None else wasted
    return stolen / (stolen + wasted)


def cost(y, pred, floor=None, stolen=None, wasted=None) -> dict:
    """What a forecast costs, in dollars per anchor, through the reserve the budget
    stage really applies: `max(prediction, MIN_HUMAN_RESERVE_PCT)`.

    `per_anchor` is the ranking number: it uses every anchor, so it has the least
    variance. It is **not** a weekly bill -- anchors sit 5 minutes apart and their
    horizons overlap, so the same quota is counted many times. `weekly_usd` in
    `cost_weekly()` is the figure to quote at a human."""
    stolen_c = COST_STOLEN if stolen is None else stolen
    wasted_c = COST_WASTED if wasted is None else wasted
    floor = aqm.P["MIN_HUMAN_RESERVE_PCT"] if floor is None else floor
    y = np.asarray(y, dtype=float)
    pred = np.clip(np.asarray(pred, dtype=float), 0.0, 100.0)
    keep = ~np.isnan(y)
    y, pred = y[keep], pred[keep]
    reserve = np.maximum(pred, floor)
    short = np.maximum(y - reserve, 0.0)              # percent stolen from the human
    excess = np.maximum(reserve - np.maximum(y, floor), 0.0)   # percent wasted
    return {"per_anchor": float(np.mean(short * stolen_c + excess * wasted_c)),
            "stolen_pct": float(np.mean(short)), "wasted_pct": float(np.mean(excess)),
            "stolen_usd": float(np.mean(short) * stolen_c),
            "wasted_usd": float(np.mean(excess) * wasted_c),
            "n": int(len(y))}


def cost_weekly(frame, y, pred, N, floor=None, stolen=None, wasted=None) -> float:
    """The same cost on **non-overlapping** anchors only -- one every `N` minutes --
    so each percent of quota is counted once and the figure can be read as dollars a
    week. Comparisons should still be made on `per_anchor`, which uses all the rows."""
    stride = max(1, N * 60 // aqm.SLOT_SECONDS)
    sel = np.zeros(len(frame), dtype=bool)
    sel[::stride] = True
    c = cost(np.asarray(y)[sel], np.asarray(pred)[sel], floor, stolen, wasted)
    per_window = c["per_anchor"]
    return per_window * (7 * 24 * 60 / N)


def pinball(y, pred, q=QUANTILE):
    d = np.asarray(y) - np.asarray(pred)
    return float(np.mean(np.maximum(q * d, (q - 1) * d)))


def evaluate(name, y, pred, floor=None):
    """Every number a candidate is judged on, for one horizon. `cost_usd` is the
    objective; the rest is diagnosis."""
    if floor is None:
        floor = aqm.P["MIN_HUMAN_RESERVE_PCT"]
    y = np.asarray(y, dtype=float)
    pred = np.clip(np.asarray(pred, dtype=float), 0.0, 100.0)
    keep = ~np.isnan(y)
    y, pred = y[keep], pred[keep]
    reserve = np.maximum(pred, floor)
    return {
        "model": name,
        "n": int(len(y)),
        "pinball": pinball(y, pred),
        "coverage": float(np.mean(y <= pred) * 100),
        # the two costs the project actually pays, in percent of a window per anchor
        "stolen": float(np.mean(np.maximum(y - reserve, 0.0))),
        "wasted": float(np.mean(np.maximum(reserve - np.maximum(y, floor), 0.0))),
        "mean_pred": float(np.mean(pred)),
        "actual_p95": float(np.percentile(y, 95)) if len(y) else float("nan"),
        "cost_usd": float(np.mean(np.maximum(y - reserve, 0.0)) * COST_STOLEN
                          + np.mean(np.maximum(reserve - np.maximum(y, floor), 0.0))
                          * COST_WASTED),
    }


def report(rows, sort="cost_usd"):
    frame = pd.DataFrame(rows).sort_values(sort)
    return frame[["model", "n", "cost_usd", "stolen", "wasted", "coverage",
                  "pinball", "mean_pred", "actual_p95"]].round(3).to_string(index=False)


def tune(label, candidates, frame, make_pred, horizons=None, show=6):
    """Choose the cheapest configuration **per horizon**, on the frame given.

    Per horizon, not one setting for all of them: the cost-optimal reserve is the
    q-quantile of demand over the next `N` minutes, and that quantity grows with `N` --
    measured on validation it runs 1, 4, 8, 13, 22 percent across the five horizons.
    A single setting forced to cover all five is dominated by the short ones, where the
    `MIN_HUMAN_RESERVE_PCT` floor already pays the whole bill, and the result is the
    degenerate "reserve nothing". The live stage knows `N`, so it is free to carry one
    setting per horizon and interpolate between them.

    Returns `{N: (printable name, config)}` and the full table."""
    horizons = horizons or HORIZONS
    rows = []
    for name, config in candidates.items():
        for N in horizons:
            c = cost(frame["y%d" % N].values, make_pred(config, N))
            rows.append({"config": name, "N": N, "cost_usd": c["per_anchor"],
                         "stolen_usd": c["stolen_usd"], "wasted_usd": c["wasted_usd"]})
    table = pd.DataFrame(rows)
    chosen = {}
    for N in horizons:
        sub = table[table["N"] == N].sort_values("cost_usd")
        pick = sub.iloc[0]["config"]
        chosen[N] = (pick, candidates[pick])
        print("N = %-4d best: %-28s $%.4f   (worst $%.4f)"
              % (N, pick, sub.iloc[0]["cost_usd"], sub.iloc[-1]["cost_usd"]))
        if show:
            print(sub.head(show)[["config", "cost_usd", "stolen_usd", "wasted_usd"]]
                  .round(4).to_string(index=False))
            print()
    total = sum(table[(table["N"] == N) & (table["config"] == chosen[N][0])]
                ["cost_usd"].iloc[0] for N in horizons) / len(horizons)
    print("%s: mean cost across horizons on this frame = $%.4f per anchor" % (label, total))
    return chosen, table


def per_horizon(chosen) -> dict:
    """The frozen form: one config per horizon, keyed by `N` as a string."""
    return {str(N): cfg for N, (_, cfg) in chosen.items()}


# --------------------------------------------------------------------------
# the cells that matter, named once
# --------------------------------------------------------------------------

def state(frame):
    """`active` if the human burned in the last 30 min, `warm` if in the last 2 h,
    else `idle`. Three states, because the measured p95 over the next two hours is
    66 / 47 / 16 percent -- a real separation, not a guessed one."""
    return np.where(frame["burn_30m"].values > 0, "active",
                    np.where(frame["burn_120m"].values > 0, "warm", "idle"))


def band(frame):
    h = frame["hour"].values
    return np.where(h < 8, "night", np.where(h < 13, "morning",
                    np.where(h < 18, "afternoon", "evening")))


BANDS = ("night", "morning", "afternoon", "evening")
STATES = ("idle", "warm", "active")


def by_cell(frame, y, preds, N=300, min_n=10):
    """Per (state, band) accuracy. The aggregate can hide a model that is right on
    average and wrong in the two cells the project lives or dies by: `idle` at night,
    which is the quota we are trying to harvest, and `idle` in the morning, which is
    a user about to start work."""
    s, b = state(frame), band(frame)
    out = []
    for st in STATES:
        for bd in BANDS:
            m = (s == st) & (b == bd) & ~np.isnan(y)
            if m.sum() < min_n:
                continue
            row = {"state": st, "band": bd, "n": int(m.sum()),
                   "ACTUAL p95": round(float(np.percentile(y[m], 95)), 1)}
            for name, p in preds.items():
                row[name] = round(float(np.mean(np.clip(p, 0, 100)[m])), 1)
            out.append(row)
    return pd.DataFrame(out)


def save(name, config, validation_rows):
    """Freeze an approach: its chosen configuration and the validation numbers that
    chose it. `05_comparison.ipynb` refits from this on train+validation and is the
    only notebook that may touch test."""
    import json
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / ("%s.json" % name)
    path.write_text(json.dumps({"name": name, "config": config,
                                "validation": validation_rows}, indent=1) + "\n")
    print("froze %s -> %s" % (name, path.relative_to(REPO)))
    return path


def load_all():
    import json
    return [json.loads(p.read_text()) for p in sorted(RESULTS.glob("*.json"))]
