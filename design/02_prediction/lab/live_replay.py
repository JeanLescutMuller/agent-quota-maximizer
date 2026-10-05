"""Replay the **real** stage, not a stand-in, over the bench's test period.

`01_baseline_recent_rate.ipynb` tunes a simplification: one look-back, extrapolated,
times a multiplier. The live stage does more than that --

    rate = max(burn over BURN_LOOKBACK_MINUTES, burn over the whole open window)
    rate = max(rate, ACTIVE_HUMAN_BURN_RATE)  if the human was active this slot
    p95  = rate x minutes to the window's end x SAFETY_MULTIPLIER,  clamped to 100

-- so the notebook's chosen constants are **not** this engine's constants, and plugging
them in blind would reserve more than was ever measured. This script closes that gap by
calling `aqm.predict_agent()` itself, through the same tail reads a tick performs, with
`aqm.P` patched per candidate.

The target has to match what the live stage actually claims, which is not a fixed
horizon: it is the human's burn between `now` and **this window's** end, whatever that
happens to be. So the horizon is read per anchor from the slot grid rather than chosen.

    /opt/anaconda3/bin/python design/02_prediction/lab/live_replay.py
"""
import sys
import pathlib

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dataset as D                                         # noqa: E402

import aqm                                                  # noqa: E402  (dataset puts release/ on the path)


def targets(agent="claude"):
    """Per anchor: minutes to this window's end, and the human burn over exactly that
    span. Anchors whose window end is not recorded, or that run past the data, are
    dropped rather than guessed."""
    rows = aqm.read_csv(aqm.slots_path(agent), aqm.SLOT_TYPES)
    start = np.array([int(r["slot_id"].split("_")[0]) for r in rows])
    burn = np.array([r["slot_window_human_pct_hi"] if r["slot_window_human_pct"] is None
                     else r["slot_window_human_pct"] for r in rows], dtype=float)
    order = np.argsort(start)
    start, burn, rows = start[order], burn[order], [rows[i] for i in order]
    cum = np.concatenate([[0.0], np.cumsum(burn)])
    step = aqm.SLOT_SECONDS

    out = {}
    for i, t in enumerate(start):
        wid = rows[i]["window_id"]
        # No window open: the stage forecasts a projected 5-hour one starting now.
        end = int(wid.split("_")[1]) if wid else t + aqm.FIVE_HOURS
        if end <= t:
            continue
        j = i + int((end - t) // step)      # the slot index at the window's end
        if j > len(start):
            continue
        out[t] = ((end - t) / 60.0, float(cum[min(j, len(start))] - cum[i]))
    return out


def live_p95(agent, now, lookback, multiplier, floor_rate=None):
    """`predict_agent` under patched parameters -- the real code path, so the window
    arm and the active-human floor are both in play."""
    saved = dict(aqm.P)
    aqm.P["BURN_LOOKBACK_MINUTES"] = lookback
    aqm.P["SAFETY_MULTIPLIER"] = multiplier
    if floor_rate is not None:
        aqm.P["ACTIVE_HUMAN_BURN_RATE"] = floor_rate
    try:
        return aqm.predict_agent(agent, now)["predicted_p95_human_usage_pct"]
    finally:
        aqm.P.clear()
        aqm.P.update(saved)


def main(agent="claude"):
    frame = D.build(agent)
    _, _, test = D.split(frame)
    tgt = targets(agent)
    anchors = [int(t) for t in test["ts"].values if int(t) in tgt]
    print("\nreplaying the live stage on %d test anchors" % len(anchors))
    print("horizon is each anchor's own distance to its window end, not a fixed N\n")

    CANDIDATES = [
        ("live today      L=30 x1.5", 30, 1.5),
        ("notebook's pick L=30 x5.0", 30, 5.0),
        ("              L=30 x2.0", 30, 2.0),
        ("              L=30 x3.0", 30, 3.0),
        ("              L=60 x2.0", 60, 2.0),
        ("              L=60 x3.0", 60, 3.0),
        ("              L=120 x2.0", 120, 2.0),
    ]
    rows = []
    for label, L, m in CANDIDATES:
        pred = np.array([live_p95(agent, t, L, m) for t in anchors])
        y = np.array([tgt[t][1] for t in anchors])
        c = D.cost(y, pred)
        rows.append({"setting": label, "cost_usd": round(c["per_anchor"], 3),
                     "stolen_usd": round(c["stolen_usd"], 3),
                     "wasted_usd": round(c["wasted_usd"], 3),
                     "stolen_pct": round(c["stolen_pct"], 3),
                     "wasted_pct": round(c["wasted_pct"], 3),
                     "mean_pred": round(float(np.mean(np.clip(pred, 0, 100))), 1)})
    import pandas as pd
    table = pd.DataFrame(rows).sort_values("cost_usd")
    print(table.to_string(index=False))
    base = table[table["setting"].str.contains("today")]["cost_usd"].iloc[0]
    print("\nchange against the live engine as it stands:")
    for _, r in table.iterrows():
        print("  %-26s %+6.1f%%" % (r["setting"].strip(),
                                    (r["cost_usd"] - base) / base * 100))
    y = np.array([tgt[t][1] for t in anchors])
    mins = np.array([tgt[t][0] for t in anchors])
    print("\nfor context: horizon %.0f-%.0f min (median %.0f), actual demand"
          " mean %.1f%%, q90.9 %.1f%%"
          % (mins.min(), mins.max(), np.median(mins), y.mean(),
             np.percentile(y, D.optimal_quantile() * 100)))
    return table


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "claude")
