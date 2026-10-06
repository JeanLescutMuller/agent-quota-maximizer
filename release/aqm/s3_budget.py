"""Stage 3 -- ../../design/03_budgeting/DESIGN.md.

How much may be spent and by when. It reads stage 2's **artifact**, never
`s2_predict` -- the two-field contract (`window_end_ts`,
`predicted_p95_human_usage_pct`) is the whole interface, and `internals` is invisible
here. `test/test_imports.sh` enforces that this module does not import `s2_predict`.
"""
from __future__ import annotations

import json
import pathlib
import time

from .core import FIVE_HOURS, P, agents, config_hash
from .history import meter_now
from .io import latest_artifact
from .plumbing import fresh_enough


# --------------------------------------------------------------------------
# stage 3: budgeting -- how much may be spent, and by when
# --------------------------------------------------------------------------
#
# ../design/03_budgeting/DESIGN.md. Everything here is arithmetic over at most 34
# windows: a 7-day period holds no more than that many 5-hour windows, and each of
# the three steps is one pass over the chain.

def remaining_windows(now: int, window: dict, week_end_ts: int) -> list:
    """Step 1 -- every 5-hour window between `now` and the 7-day reset.

    Entry [0] is the window already running, or a hypothetical one starting now; the
    rest are 5-hour windows separated by WINDOW_GAP_SECONDS. The one holding the
    weekly reset is cut at it, because quota does not cross a reset. A window is
    assumed to exist whenever one is needed, which stage 4 is what makes true.

    Called `remaining_windows`, not `future_windows`: [0] is usually not in the
    future (VOCABULARY.md 5)."""
    end = window["end"] if window["open"] and window["end"] > now else now + FIVE_HOURS
    start, out = now, []
    while start < week_end_ts:
        out.append({"start_ts": start, "end_ts": min(end, week_end_ts),
                    "is_open_now": not out and window["open"]})
        start = end + P["WINDOW_GAP_SECONDS"]
        end = start + FIVE_HOURS
    return out
def max_spend(window: dict, used: float, p95: "float | None") -> float:
    """Step 2 -- the most extra work this window may hold.

    Two limits, independent, and the smaller one wins:

      max_spend_units_by_quota  what the window's meter will still give, after the margin
      max_spend_units_by_time   what can actually be burned in the time the window lasts

    They are *not* multiplied. Prorating the quota by the window's duration and then
    also subtracting what the window has used double-counts: a window half elapsed
    with 5% used scored 0.9 x 0.5 - 0.05 = 0.40 against the 0.85 genuinely free in
    it, skipping more than half the room in the window we most want to spend in.
    Duration limits how fast quota can be burned, not how much of it exists.

    `p95` is the predicted human demand for the window in percent of a window, and
    only the current one ever has a forecast; everywhere else the margin is
    MIN_HUMAN_RESERVE_PCT, the stand-in for a predictor that does not exist yet. DESIGN.md 2."""
    return max_spend_parts(window, used, p95)["max_spend_units"]
def max_spend_parts(window: dict, used: float, p95: "float | None") -> dict:
    """The same arithmetic with its working shown, and what every window of the
    artifact carries. "Why was this window's ceiling 0.16?" is then answerable from
    the file alone, and the notebook can display the two limits without
    reimplementing them -- a second copy of this formula would eventually disagree
    with this one, and the copy is the one a human would be reading."""
    floor = P["MIN_HUMAN_RESERVE_PCT"]
    reserve_pct = floor if p95 is None else max(floor, p95)
    used_pct = used if window["is_open_now"] else 0.0
    max_spend_units_by_quota = (100.0 - reserve_pct - used_pct) / 100.0
    max_spend_units_by_time = ((window["end_ts"] - window["start_ts"]) / 3600.0
                               * P["BOT_BURN_UNITS_PER_HOUR"])
    return {"human_reserve_pct": round(reserve_pct, 4),
            "max_spend_units_by_quota": round(max_spend_units_by_quota, 4),
            "max_spend_units_by_time": round(max_spend_units_by_time, 4),
            "limited_by": "quota" if max_spend_units_by_quota <= max_spend_units_by_time else "time",
            "max_spend_units": round(max(0.0, min(max_spend_units_by_quota, max_spend_units_by_time)), 4)}
def fill_from_the_end(windows: list, remaining: float) -> float:
    """Step 3 -- fill from the last window backwards, and return what no window could
    take. Filling backwards *is* the guiding principle: every window earlier than
    strictly necessary gets 0, so the user keeps first claim on all of it until it
    is about to expire."""
    for window in reversed(windows):
        window["planned_units"] = round(max(0.0, min(window["max_spend_units"], remaining)), 4)
        remaining -= window["planned_units"]
    return round(max(0.0, remaining), 4)
def _nothing(verdict: str) -> dict:
    return {"verdict": verdict,
            "extra_quota_to_spend_units": 0.0, "spend_by_ts": None,
            "already_lost_units": 0.0,
            "week": {"used_pct": None, "end_ts": None, "left_units": None},
            "current_window": {"is_window_open": False, "end_ts": None, "used_pct": 0.0},
            "remaining_windows": []}
def budget_agent(agent: str, now: int, p95: "float | None") -> dict:
    m = meter_now(agent, now)
    if m is None:
        return _nothing("no_meter_reading")
    if m["week_used_pct"] is None or m["week_end_ts"] is None or m["week_end_ts"] <= now:
        # Without a weekly level and a reset in the future there is no surplus to
        # compute, and guessing one is the only way this stage can spend quota the
        # user still wanted.
        return _nothing("no_usable_week_reading")
    # The window comes off the same reading, not from window_state(): that would
    # tail the meter a second time, which doubled the cost of the whole stage.
    window = {"open": m["window_end_ts"] is not None, "end": m["window_end_ts"]}
    used_pct = (m["window_used_pct"] or 0.0) if window["open"] else 0.0
    remaining = (100.0 - m["week_used_pct"]) / 100.0 * P["WEEK_CAPACITY_UNITS"][agent]
    windows = remaining_windows(now, window, m["week_end_ts"])
    for n, w in enumerate(windows):
        # update(), not just the cap: each window carries its own working, so a past
        # decision explains itself without a rerun (max_spend_parts()).
        w.update(max_spend_parts(w, used_pct, p95 if n == 0 else None))
    lost = fill_from_the_end(windows, remaining)
    due = windows[0]["planned_units"] if windows else 0.0
    return {"verdict": "wait" if not due else "spend_now",
            "extra_quota_to_spend_units": due,
            "spend_by_ts": (windows[0]["end_ts"] - P["DEADLINE_MARGIN_SECONDS"]
                            if due else None),
            "already_lost_units": lost,
            "week": {"used_pct": m["week_used_pct"],
                     "end_ts": m["week_end_ts"],
                     "left_units": round(remaining, 4)},
            "current_window": {"is_window_open": window["open"],
                               "end_ts": window["end"],
                               "used_pct": round(used_pct, 4)},
            "remaining_windows": windows}
def budget(at: "int | None" = None, prediction=None) -> dict:
    """Stage 3. `prediction` is a path; without one the margin is MIN_HUMAN_RESERVE_PCT
    everywhere, which is exactly P1. A prediction that is present but unreadable
    sets the margin to a full window, so nothing runs -- the fail-safe direction
    (../design/02_prediction/DESIGN.md 5), and so does one that is too old to
    decide on. The freshness check lives here rather than in the pipeline because
    the pipeline writes the prediction itself and so can never catch a stale one."""
    now = int(time.time()) if at is None else at
    method, p95 = "min-reserve", {}
    source = None
    if prediction is None:
        # Only the auto-resolved latest is freshness-checked. An explicit path is
        # someone pointing at a fixture, a past artifact or an experimental
        # predictor on purpose, and second-guessing that would make the flag
        # useless (../design/07_pipeline/DESIGN.md 1.1).
        source = latest_artifact("predictions")
        if source is not None and not fresh_enough(source, now):
            method, p95 = "stale-prediction", {a: 100.0 for a in agents()}
            source = None
    else:
        try:
            source = json.loads(pathlib.Path(prediction).expanduser().read_text())
        except (OSError, ValueError):
            method, p95 = "fail-safe", {a: 100.0 for a in agents()}
    if source:
        method = source.get("method", "unknown")
        for agent, block in (source.get("agents") or {}).items():
            # The contract, never `internals`: VOCABULARY.md 4.2.
            p95[agent] = block.get("predicted_p95_human_usage_pct")
    return {"computed_ts": now, "method": method, "config_hash": config_hash(),
            "agents": {agent: budget_agent(agent, now, p95.get(agent))
                       for agent in agents()}}
