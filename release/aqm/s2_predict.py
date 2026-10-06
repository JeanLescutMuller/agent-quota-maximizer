"""Stage 2 -- ../../design/02_prediction/DESIGN.md.

How much human demand is still coming before this window ends. It may read `history`
and `core`; it must never import `s1_ingest` or `s3_budget`, because its only contract
with them is the artifact.
"""
from __future__ import annotations

import time

from .core import FIVE_HOURS, P, agents, config_hash
from .history import meter_now, recent_slots
from .io import clock


# --------------------------------------------------------------------------
# stage 2: prediction -- how much human demand is still coming
# --------------------------------------------------------------------------
#
# ../design/02_prediction/DESIGN.md. `recent-rate-v1`: the measured recent past,
# extrapolated, with nothing learned. The value of the stage today is its *shape* --
# a quantile in percent of the current window -- so a real forecaster can replace
# the three lines of arithmetic without any other stage changing.

def predict_agent(agent: str, now: int) -> dict:
    """One agent's forecast. Raises nothing: an unreadable source becomes a full
    window, which is the fail-safe direction (DESIGN.md 5)."""
    try:
        m = meter_now(agent, now)
        opened = (m["window_end_ts"] - FIVE_HOURS) if (m and m["window_end_ts"]) else None
        since = now - P["BURN_LOOKBACK_MINUTES"] * 60
        rows = recent_slots(agent, min(since, opened) if opened else since, now)
    except (OSError, ValueError):
        return {"window_end_ts": None,
                "predicted_p95_human_usage_pct": 100.0,
                "internals": {"based_on": "unreadable"}}

    measured = span = window_total = 0.0
    readings = was_human_active = 0
    last_human = None
    for row in rows:
        b0, b1 = (int(x) for x in row["slot_id"].split("_"))
        value = row["slot_window_human_pct"]
        if value is None:                 # censored: the upper bound is the
            value = row["slot_window_human_pct_hi"]  # conservative choice here
        if b1 > since:                    # the recent mean
            measured += value
            span += (b1 - b0) / 60.0
            readings += row["slot_n_readings"] or 0
            was_human_active += row["was_human_active"] or 0
        if opened is not None and b1 > opened:
            # b1 > opened, not b0 >= opened: the slot a window opens inside
            # starts before the window does, and excluding it dropped the first
            # minutes of every window from its own average. Nothing is
            # double-counted, because the 5-hour meter cannot move while no
            # 5-hour window is running.
            window_total += value
        if value:
            last_human = b0
    recent_rate = measured / span if span else 0.0
    rate = recent_rate

    # S1-window: the same human movement averaged over the WHOLE open window.
    #
    # The 30-minute mean alone reads an ordinary pause as an absence. Measured on
    # 2026-10-04: the meter sat at 31% mid-window, the user went on to burn the
    # remaining 69% within two hours, and at 11:50 and 12:00 this stage reported
    # 0 %/min with signal `None` -- "genuinely idle" -- which would have offered
    # that window to background work. A window's own average cannot say that:
    # while a window is filling it is never zero. The two are combined with max(),
    # so the recent mean still reacts to a burst within one slot and the window
    # average sets a floor under it.
    #
    # The divisor is floored at MIN_RATE_DENOMINATOR_MINUTES, not at
    # BURN_LOOKBACK_MINUTES: a 15-minute-old window in which the user had already
    # burned 10% reported 0.33 %/min against a true 0.67, because 10 was divided by
    # 30. Halving the measured pace understates the reserve, which is the dangerous
    # direction. A small floor still stops a 2-minute-old window reading 0.5 %/min
    # off its first quantised percent.
    window_rate = 0.0
    if opened is not None:
        window_rate = window_total / max((now - opened) / 60.0,
                                        P["MIN_RATE_DENOMINATOR_MINUTES"])
    based_on = "last_30_min" if rate > 0 else "nothing"
    if window_rate > rate:
        rate, based_on = window_rate, "current_window"

    # S2: a confirmed request in a session that is not ours. It says *that* the
    # user is active, not how much, so it raises the rate to a floor and never
    # sets it -- the meter quantises at 1%, so a user who has just started shows
    # nothing in S1 for a minute or two. S3 (telemetry query_source) is not wired
    # yet: ingestion does not read the telemetry rows (DESIGN.md 3).
    if was_human_active and rate < P["ACTIVE_HUMAN_BURN_RATE"]:
        rate, based_on = P["ACTIVE_HUMAN_BURN_RATE"], "human_active_floor"

    end = m["window_end_ts"] if m and m["window_end_ts"] and m["window_end_ts"] > now else None
    # To the END OF THE WINDOW, always. The stage's specification is a single
    # number -- cumulative human usage between now and this window's end -- and it
    # has no horizon parameter, no cap and no special case. How well the estimate
    # holds over a long remaining window is a property of the *engine* and belongs
    # in `internals`, not in the contract (DESIGN.md 1.1).
    minutes = max(0, ((end if end else now + FIVE_HOURS) - now) / 60.0)
    p95 = min(100.0, rate * minutes * P["SAFETY_MULTIPLIER"])
    # Two halves, and the split is load-bearing (VOCABULARY.md 4): everything a
    # downstream stage may read is at the top, and everything that is an artefact of
    # *this* engine's arithmetic is under `internals`. A later engine will not have a
    # 30-minute mean to report, and `based_on` is vocabulary only this one can speak.
    return {"window_end_ts": end,
            "predicted_p95_human_usage_pct": round(p95, 4),
            "internals": {
                "based_on": based_on,
                "effective_burn_rate": round(rate, 5),
                "last_human_slot_ts": last_human,
                "last_30_min": {"human_avg_burn_rate": round(recent_rate, 5),
                                "slot_n_readings": readings},
                "current_window": {
                    "is_window_open": opened is not None,
                    "start_ts": opened,
                    "window_age_minutes": int((now - opened) // 60) if opened else None,
                    "remaining_minutes": int((end - now) // 60) if end else None,
                    "window_used_pct": m["window_used_pct"] if m else None,
                    "human_avg_burn_rate": round(window_rate, 5)}}}
def predict(at: "int | None" = None) -> dict:
    now = int(time.time()) if at is None else at
    return {"computed_ts": now, "method": "recent-rate-v1",
            "config_hash": config_hash(),
            "agents": {agent: predict_agent(agent, now) for agent in agents()}}
def predict_table(report: dict) -> str:
    lines = []
    for agent, a in report["agents"].items():
        x = a.get("internals") or {}
        lines.append(
            "%-7s p95 %5.1f%% of the window, to %s · based on %s"
            % (agent, a["predicted_p95_human_usage_pct"],
               clock(a["window_end_ts"]) if a["window_end_ts"] else "no window",
               x.get("based_on", "?")))
    return "\n".join(lines)
