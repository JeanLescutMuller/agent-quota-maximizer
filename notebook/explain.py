"""Helpers for explain_budget.ipynb: load the data, replay a decision, draw it.

This is ad-hoc, run-by-hand tooling, so it lives in the source repo and runs from
here even though it reads the live `~/opt` state (`~/AGENTS.md`). Unlike
`release/aqm.py` it may use pandas and matplotlib: nothing scheduled imports it.

**It never recomputes anything `aqm.py` computes.** Every number the notebook shows
comes back from `aqm.predict()` / `aqm.budget()` or from the CSVs they read. A
helper that re-derived a ceiling or a rate here would eventually disagree with the
real one, and the copy is the one a human would be reading.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import tempfile

import matplotlib
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

REPO = pathlib.Path(__file__).resolve().parent.parent

# matplotlib formats dates in rcParams["timezone"], which is UTC by default, while
# every timestamp here is local. Left alone, a 13:53 peak is labelled 11:53 and
# nothing on the axis lines up with the clock on the wall -- which is exactly how a
# reset "looked missing" when it was in the data all along.
LOCAL = dt.datetime.now().astimezone().tzinfo
matplotlib.rcParams["timezone"] = str(LOCAL)
UNIT_USD = {"claude": 32.0, "codex": 20.5}      # for display only, never arithmetic

aqm = None          # set by connect()


# ---------------------------------------------------------------- loading

def connect(home=None, usage=None, refresh=False):
    """Import `aqm` from release/ and point it at a data tree.

    `refresh=True` runs `aqm.ingest()`, which appends new readings to
    `data/<agent>/`. That is safe to do from here -- it is the same append-only,
    idempotent operation the scheduler performs, and `data/` is explicitly
    rebuildable (`design/01_ingestion/DESIGN.md` 6.0). Nothing else in this module
    writes: `predict()` and `budget()` are pure functions, and only the CLI and
    `pipeline()` write artifacts.
    """
    global aqm
    import sys
    if home:
        os.environ["AQM_HOME"] = str(pathlib.Path(home).expanduser())
    if usage:
        os.environ["AQM_USAGE_DATA"] = str(pathlib.Path(usage).expanduser())
    sys.path.insert(0, str(REPO / "release"))
    import aqm as module
    aqm = module
    aqm.load_config()
    report = aqm.ingest() if refresh else None
    print("reading   %s" % aqm.home())
    print("raw logs  %s" % aqm.usage_dir())
    print("agents    %s · config_hash %s" % (", ".join(aqm.agents()), aqm.config_hash()))
    if report:
        print("refreshed %s" % json.dumps(report))
    for agent in aqm.agents():
        m = aqm.meter_now(agent)
        print("  %-7s last reading %s%s" % (
            agent, aqm.shown(m["observed_ts"]) if m else "none",
            "" if not m else "  (%.0f min old)" % (m["age"] / 60)))
    return aqm


def moment(value=None) -> int:
    """`None` -> now; an ISO string or an epoch int -> that instant."""
    return int(dt.datetime.now().timestamp()) if value is None else aqm.when(str(value))


def decide(at=None) -> tuple:
    """(prediction, budget) as of `at`, chained exactly as the pipeline chains them.

    The prediction is written to a temporary file and passed to `budget` by path.
    That matters for any `at` in the past: the auto-resolved `state/latest` is
    freshness-checked and a past moment would fail it, so the budget would come
    back `stale-prediction` with every ceiling at 0. An explicit path is exempt
    (`design/03_budgeting/DESIGN.md` 2), which is what makes a backtest possible.
    """
    at = moment(at)
    prediction = aqm.predict(at=at)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(prediction, handle)
        path = handle.name
    try:
        return prediction, aqm.budget(at=at, prediction=path)
    finally:
        os.unlink(path)


def when(series):
    """Epoch seconds -> local-time datetimes, for an axis."""
    return pd.to_datetime(pd.Series(series), unit="s", utc=True).dt.tz_convert(
        LOCAL)


# ---------------------------------------------------------------- the inputs

def meter_frame(agent, since, until) -> pd.DataFrame:
    """Rows of `data/<agent>/meter.csv` in [since, until] -- the audit trail of
    every reading that survived the drop_stale_readings."""
    rows = aqm.read_csv(aqm.meter_path(agent), aqm.METER_TYPES)
    frame = pd.DataFrame([r for r in rows
                          if since <= r["observed_ts"] <= until])
    if frame.empty:
        return frame
    frame["at"] = when(frame["observed_ts"])
    return frame


def slot_frame(agent, since, until) -> pd.DataFrame:
    """Rows of `data/<agent>/slots.csv` overlapping [since, until], with the
    slot's start and end split out of `slot_id`."""
    rows = aqm.read_csv(aqm.slots_path(agent), aqm.SLOT_TYPES)
    out = []
    for row in rows:
        b0, b1 = (int(x) for x in row["slot_id"].split("_"))
        if b1 > since and b0 <= until:
            out.append(dict(row, b0=b0, b1=b1))
    frame = pd.DataFrame(out)
    if frame.empty:
        return frame
    frame["at"] = when(frame["b0"])
    return frame


def rate_rows(agent, at) -> pd.DataFrame:
    """Exactly the rows `burn_rate()` sums, with the running total -- so the rate
    can be checked by eye against the number the stage reports."""
    at = moment(at)
    rows = aqm.recent_slots(agent, at - aqm.P["BURN_LOOKBACK_MINUTES"] * 60, at)
    out = []
    for row in rows:
        b0, b1 = (int(x) for x in row["slot_id"].split("_"))
        value = row["slot_window_human_pct"]
        out.append({
            "slot": aqm.shown(b0)[11:16],
            "window_used_pct": row["window_used_pct"],
            "slot_window_used_pct": row["slot_window_used_pct"],
            "slot_window_human_pct": row["slot_window_human_pct"],
            "used": value if value is not None else row["slot_window_human_pct_hi"],
            "censored": value is None,
            "attribution": row["attribution"],
            "slot_n_readings": row["slot_n_readings"],
            "was_human_active": row["was_human_active"],
            "n_bot_workers": row["n_bot_workers"],
            "minutes": (b1 - b0) / 60.0,
        })
    frame = pd.DataFrame(out)
    if not frame.empty:
        frame["cumulative"] = frame["used"].cumsum()
    return frame


# ---------------------------------------------------------------- the arithmetic

def predict_steps(agent, at, prediction) -> pd.DataFrame:
    """Stage 2's formula, one row per step, with the stage's own output as the last
    line so a mismatch would be visible rather than hidden."""
    at = moment(at)
    block = prediction["agents"][agent]
    x = block.get("internals") or {}
    horizon = block["window_end_ts"] or (at + aqm.FIVE_HOURS)
    minutes = max(0, (horizon - at) / 60.0)
    steps = [
        ("human movement in the last BURN_LOOKBACK_MINUTES",
         "%s %%/min" % (x.get("last_30_min") or {}).get("human_avg_burn_rate"),
         "based on %s · %d readings · BURN_LOOKBACK_MINUTES = %d min"
         % (x.get("based_on", "n/a"),
            (x.get("last_30_min") or {}).get("slot_n_readings", 0),
            aqm.P["BURN_LOOKBACK_MINUTES"])),
        ("effective burn rate", "%s %%/min" % x.get("effective_burn_rate"),
         "max(last_30_min, current_window), floored by the active-human floor"),
        ("x minutes to the window's end", "%d min" % minutes,
         "always the whole remaining window -- the stage has no horizon parameter"),
        ("x SAFETY_MULTIPLIER", "%s" % aqm.P["SAFETY_MULTIPLIER"],
         "the quantile multiplier on the measured rate"),
        ("= predicted_p95_human_usage_pct", "%.1f %%"
         % block["predicted_p95_human_usage_pct"],
         "clamped to 100: a forecast is a share of ONE window"),
        ("-> human_reserve_pct budgeting will use", "%.1f %%"
         % max(aqm.P["MIN_HUMAN_RESERVE_PCT"], block["predicted_p95_human_usage_pct"]),
         "max(MIN_HUMAN_RESERVE_PCT = %s, the p95)" % aqm.P["MIN_HUMAN_RESERVE_PCT"]),
    ]
    return pd.DataFrame(steps, columns=["step", "value", "where it comes from"])


def window_frame(block) -> pd.DataFrame:
    """The window chain as the artifact carries it, including each ceiling's own
    working (`human_reserve_pct`, `max_spend_units_by_quota`, `max_spend_units_by_time`, `limited_by`) and the back-fill's
    running remainder read top to bottom."""
    rows, left = [], block["week"]["left_units"]
    for n, window in enumerate(block["remaining_windows"]):
        left -= window["planned_units"]
        rows.append({
            "#": n,
            "from": aqm.clock(window["start_ts"]),
            "to": aqm.clock(window["end_ts"]),
            "hours": round((window["end_ts"] - window["start_ts"]) / 3600.0, 2),
            "window": window["is_open_now"],
            "human_reserve_pct": window["human_reserve_pct"],
            "max_spend_units_by_quota": window["max_spend_units_by_quota"],
            "max_spend_units_by_time": window["max_spend_units_by_time"],
            "limited_by": window["limited_by"],
            "max_spend_units": window["max_spend_units"],
            "planned_units": window["planned_units"],
            "left": round(left, 4),
        })
    return pd.DataFrame(rows)


def decision_steps(agent, block) -> pd.DataFrame:
    """How the weekly percentage became `extra_quota_to_spend_units`, in the order it happens."""
    unit = UNIT_USD[agent]
    week = aqm.P["WEEK_CAPACITY_UNITS"][agent]
    rows = [
        ("7-day meter",
         "%s %%" % (None if block["week"]["left_units"] is None else
                    round(100 - block["week"]["left_units"] / week * 100, 2)),
         "the only account-level number there is"),
        ("week_left_units", "%.3f unit" % block["week"]["left_units"],
         "(100 - week_used_pct)/100 x WEEK_CAPACITY_UNITS = %.2f  (~$%.0f)"
         % (week, block["week"]["left_units"] * unit)),
        ("window used", "%.0f %%" % block["current_window"]["used_pct"],
         "subtracted from the open window's max_spend_units_by_quota only"),
        ("windows to the reset", "%d" % len(block["remaining_windows"]),
         "5h windows + WINDOW_GAP_SECONDS, the last one cut at the reset"),
        ("sum of ceilings", "%.3f unit"
         % sum(c["max_spend_units"] for c in block["remaining_windows"]),
         "the most the remaining windows could absorb"),
        ("extra_quota_to_spend_units", "%.3f unit  (~$%.0f)"
         % (block["extra_quota_to_spend_units"], block["extra_quota_to_spend_units"] * unit),
         "what the back-fill left in the CURRENT window"),
        ("spend_by_ts", aqm.clock(block["spend_by_ts"]),
         "window end - DEADLINE_MARGIN_SECONDS; null when nothing is due"),
        ("already_lost_units", "%.3f unit  (~$%.0f)"
         % (block["already_lost_units"], block["already_lost_units"] * unit),
         "no window can absorb it: this is the waste, measured"),
        ("decision", block["verdict"], "why, so a 0 never has to be interpreted"),
    ]
    return pd.DataFrame(rows, columns=["quantity", "value", "note"])


# ---------------------------------------------------------------- the pictures

def plot_meter(agent, at, prediction, hours=12, ax=None):
    """The 5-hour meter over the recent past, its window boundaries, and the
    forecast drawn on top of the window still open."""
    at = moment(at)
    since = at - hours * 3600
    meter = meter_frame(agent, since, at)
    slots = slot_frame(agent, since, at)
    if ax is None:
        _, ax = plt.subplots(figsize=(13, 4))

    if not slots.empty:
        # One line per window, never one line across windows. Drawn as a single
        # series, the jump from 100% (window ending) to 0% (window starting) looks
        # like a decline rather than a reset, and two windows' percentages are not
        # the same quantity: each is a share of its own window.
        windowed = slots[slots["window_used_pct"].notna()]
        first = True
        for window_id, part in windowed.groupby("window_id", sort=False):
            ax.step(part["at"], part["window_used_pct"], where="post", color="#1f77b4",
                    lw=1.6, label="window_used_pct (meter)" if first else None)
            start = part["at"].iloc[0]
            ax.axvspan(start, part["at"].iloc[-1], color="#1f77b4", alpha=.05)
            ax.annotate("window opens", (start, 100), rotation=90, fontsize=6,
                        color="#2ca02c", va="top", ha="right")
            ax.axvline(start, color="#2ca02c", ls=":", lw=1)
            first = False
        gaps = slots[~slots["is_window_open"]]
        if not gaps.empty:
            ax.plot(gaps["at"], [0] * len(gaps), "|", color="#bbb", ms=8,
                    label="no window open (window_used_pct is empty, not 0)")
        active = slots[slots["was_human_active"] > 0]
        if not active.empty:
            ax.plot(active["at"], [-3] * len(active), "v", color="#d62728", ms=5,
                    label="was_human_active: the user was there")
    if not meter.empty:
        ax.plot(meter["at"], meter["window_used_pct"], "o", color="#1f77b4", ms=4,
                label="readings kept by the drop_stale_readings")

    block = prediction["agents"][agent]
    now = when([at])[0]
    ax.axvline(now, color="k", lw=1.2)
    ax.annotate("now", (now, ax.get_ylim()[1]), fontsize=8, ha="right")
    end = block["window_end_ts"]
    if end:
        level = (meter["window_used_pct"].iloc[-1] if not meter.empty else 0)
        ax.annotate("", xy=(when([end])[0], level + block["predicted_p95_human_usage_pct"]),
                    xytext=(now, level),
                    arrowprops=dict(arrowstyle="->", color="#ff7f0e", lw=2))
        ax.plot([], [], color="#ff7f0e", lw=2,
                label="predicted human p95 (%.0f%%)" % block["predicted_p95_human_usage_pct"])
        ax.axvline(when([end])[0], color="#2ca02c", ls="--", lw=1.2,
                   label="window ends")
    ax.set_title("%s — the 5-hour meter, last %d h" % (agent, hours))
    ax.set_ylabel("% of the 5-hour window")
    ax.set_ylim(-6, 105)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %H:%M", tz=LOCAL))
    ax.legend(fontsize=8, loc="upper left", ncol=2)
    ax.grid(alpha=.25)
    return ax


def plot_windows(agent, block, ax=None):
    """The window chain: each window's ceiling as an outline and what the back-fill
    planned into it as a fill. Back-filling from the reset backwards is visible as
    the plan piling up on the right."""
    if ax is None:
        _, ax = plt.subplots(figsize=(13, 3.4))
    if not block["remaining_windows"]:
        ax.text(.5, .5, block["verdict"], ha="center", transform=ax.transAxes)
        return ax
    for n, window in enumerate(block["remaining_windows"]):
        start, end = when([window["start_ts"]])[0], when([window["end_ts"]])[0]
        width = end - start
        ax.barh(0, width, left=start, height=.62, color="none",
                edgecolor="#999", lw=1)
        if window["max_spend_units"]:
            share = window["planned_units"] / window["max_spend_units"] if window["max_spend_units"] else 0
            ax.barh(0, width * share, left=start, height=.62,
                    color="#ff7f0e" if n == 0 else "#1f77b4", alpha=.85)
        ax.text(start + width / 2, .42, "%.2f" % window["max_spend_units"], ha="center",
                fontsize=7, color="#555")
        if window["planned_units"]:
            ax.text(start + width / 2, -.44, "%.2f" % window["planned_units"],
                    ha="center", fontsize=7, weight="bold")
    ax.axvline(when([block["week"]["end_ts"]])[0], color="#d62728", lw=2,
               label="7-day reset")
    ax.axvline(when([block["remaining_windows"][0]["start_ts"]])[0], color="k", lw=1.2,
               label="now")
    if block["spend_by_ts"]:
        ax.axvline(when([block["spend_by_ts"]])[0], color="#ff7f0e", ls="--",
                   label="spend_by_ts")
    ax.set_yticks([])
    ax.set_ylim(-.8, .8)
    ax.set_title("%s — windows to the reset.  outline = ceiling (top number), "
                 "fill = planned (bottom).  extra_quota_to_spend_units %.2f, unreachable %.2f"
                 % (agent, block["extra_quota_to_spend_units"], block["already_lost_units"]), fontsize=10)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %H:%M", tz=LOCAL))
    ax.legend(fontsize=8, loc="upper left")
    return ax


def plot_backfill(agent, block, ax=None):
    """The back-fill as a waterfall, in the order it actually runs: from the last
    window before the reset, backwards, until the weekly remainder is used up. What
    is left at the end is `already_lost_units`."""
    if ax is None:
        _, ax = plt.subplots(figsize=(13, 3.4))
    if not block["remaining_windows"]:
        ax.text(.5, .5, block["verdict"], ha="center", transform=ax.transAxes)
        return ax
    remaining = block["week"]["left_units"]
    labels, bottoms, heights, colors = ["remaining\nweek"], [0], [remaining], ["#999"]
    for n, window in reversed(list(enumerate(block["remaining_windows"]))):
        labels.append("#%d\n%s" % (n, aqm.clock(window["start_ts"])[4:]))
        bottoms.append(remaining - window["planned_units"])
        heights.append(window["planned_units"])
        colors.append("#ff7f0e" if n == 0 else "#1f77b4")
        remaining -= window["planned_units"]
    labels.append("already_lost_units")
    bottoms.append(0)
    heights.append(remaining)
    colors.append("#d62728")
    ax.bar(range(len(labels)), heights, bottom=bottoms, color=colors, width=.72)
    for i, (b, h) in enumerate(zip(bottoms, heights)):
        if h > 0.005:
            ax.text(i, b + h / 2, "%.2f" % h, ha="center", va="center",
                    fontsize=7, color="white", weight="bold")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, fontsize=7)
    ax.set_ylabel("unit")
    ax.set_title("%s — the back-fill, in the order it runs: last window first. "
                 "Orange is the current window = extra_quota_to_spend_units." % agent, fontsize=10)
    ax.grid(alpha=.25, axis="y")
    return ax


def sweep(hours=48, step_minutes=30, until=None) -> pd.DataFrame:
    """Replay the decision at regular moments in the past.

    This is the view a single tick cannot give: whether `extra_quota_to_spend_units` appears when
    it should, how `already_lost_units` grows as the reset approaches, and whether the
    forecast tracked the user's actual activity.
    """
    end = moment(until)
    moments = range(end - hours * 3600, end + 1, step_minutes * 60)
    rows = []
    for at in moments:
        prediction, decision = decide(at)
        for agent in aqm.agents():
            b, p = decision["agents"][agent], prediction["agents"][agent]
            rows.append({
                "at": at, "agent": agent,
                "predicted_p95_human_usage_pct": p["predicted_p95_human_usage_pct"],
                "rate": ((p.get("internals") or {}).get("effective_burn_rate")),
                "based_on": (p.get("internals") or {}).get("based_on"),
                "window_used_pct": b["current_window"]["used_pct"],
                "week_left_units": b["week"]["left_units"],
                "extra_quota_to_spend_units": b["extra_quota_to_spend_units"],
                "already_lost_units": b["already_lost_units"],
                "remaining_windows": len(b["remaining_windows"]),
                "verdict": b["verdict"],
            })
    frame = pd.DataFrame(rows)
    frame["when"] = when(frame["at"])
    return frame


def plot_sweep(frame, agent):
    """Four stacked panels over the swept period, sharing one time axis."""
    one = frame[frame["agent"] == agent]
    fig, axes = plt.subplots(4, 1, figsize=(13, 9), sharex=True)
    axes[0].plot(one["when"], one["week_left_units"], color="#999")
    axes[0].set_ylabel("remaining\nweek (unit)")
    axes[0].set_title("%s — how the decision evolved" % agent)

    axes[1].plot(one["when"], one["predicted_p95_human_usage_pct"], color="#ff7f0e",
                 label="predicted human p95 (%)")
    tripped = one[one["based_on"] == "human_active_floor"]
    if not tripped.empty:
        axes[1].plot(tripped["when"], tripped["predicted_p95_human_usage_pct"], "o", ms=3,
                     color="#d62728", label="active-human floor fired")
    axes[1].axhline(aqm.P["MIN_HUMAN_RESERVE_PCT"] * 100, color="#bbb", ls=":",
                    label="MIN_HUMAN_RESERVE_PCT floor")
    axes[1].set_ylabel("p95 (%)")
    axes[1].legend(fontsize=8)

    axes[2].fill_between(one["when"], one["extra_quota_to_spend_units"], color="#1f77b4", alpha=.8)
    axes[2].set_ylabel("extra_quota_to_spend_units\n(unit)")

    axes[3].fill_between(one["when"], one["already_lost_units"], color="#d62728", alpha=.6)
    axes[3].set_ylabel("unreachable\n(unit)")
    axes[3].xaxis.set_major_formatter(mdates.DateFormatter("%a %H:%M", tz=LOCAL))
    for ax in axes:
        ax.grid(alpha=.25)
    fig.tight_layout()
    return fig
