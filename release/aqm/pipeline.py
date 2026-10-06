"""Every built stage in one process -- ../../design/07_pipeline/DESIGN.md 2.

The only module that may import several stages, because running them in order is
precisely its job.
"""
from __future__ import annotations

import datetime as dt
import time

from .core import CONFIG, P, agents, load_config
from .history import meter_now
from .io import clock, lasting, write_artifact
from .plumbing import acquire, housekeeping, log, metric
from .s1_ingest import ingest
from .s2_predict import predict
from .s3_budget import budget


# --------------------------------------------------------------------------
# the pipeline
# --------------------------------------------------------------------------

def pipeline(dry_run: bool = False, now: "int | None" = None) -> dict:
    """The one command the LaunchAgent runs. Stages in order, in **one process**:
    at 39 ms of interpreter startup against 6 ms of work, shelling out to each
    stage would multiply the cost of a tick by the number of stages
    (../design/01_ingestion/DESIGN.md 8).

    Stages 4-6 do not exist yet, so a tick today always ends at `budget` and
    nothing can act. The guards between the stages are built now regardless,
    because they are what makes it safe to add an acting stage later."""
    at = int(time.time()) if now is None else now
    started = time.perf_counter()
    result = {"computed_ts": at, "dry_run": dry_run, "stages": [], "decision": None}

    load_config()                                   # raises on a bad config
    if not CONFIG.get("enabled"):
        result["decision"] = "disabled"
        result["reason"] = CONFIG.get("disabled_reason")
        return result

    held = acquire("pipeline")
    if held is None:
        result["decision"] = "busy"                 # a tick overran; skip, never queue
        return result
    try:
        result["ingest"] = ingest(now=now)
        result["stages"].append("ingest")
        prediction = predict(at=now)
        write_artifact("predictions", prediction, at)
        result["stages"].append("predict")

        decision = budget(at=now)
        write_artifact("budgets", decision, at)
        result["stages"].append("budget")
        result["budget"] = decision

        result["housekeeping"] = housekeeping(at)

        # The meter may have moved without us seeing it, so an agent about to be
        # spent on must have a fresh reading. The guard is **per agent**, not a
        # global exit: the two agents are budgeted independently, and an agent that
        # has never been read at all (no Codex on this machine, say) is not a stale
        # meter -- budgeting already answers that one with `no meter reading`.
        due = {a: b["extra_quota_to_spend_units"] for a, b in decision["agents"].items()
               if b["extra_quota_to_spend_units"] > 0}
        ages = {a: (meter_now(a, now) or {}).get("age") for a in agents()}
        result["no_reading"] = sorted(a for a, age in ages.items() if age is None)
        stale = sorted(a for a in due
                       if ages[a] is None or ages[a] > P["READING_MAX_AGE_SECONDS"])
        for agent in stale:
            due.pop(agent)                  # refused: not spendable on this reading
        result["stale_agents"] = stale
        result["due"] = due
        result["decision"] = ("stale" if stale else
                              "nothing due" if not due else "not built")
        for agent, b in decision["agents"].items():
            p = prediction["agents"].get(agent, {})
            metric({"at": at, "agent": agent, "extra_quota_to_spend_units": b["extra_quota_to_spend_units"],
                    "spend_by_ts": b["spend_by_ts"], "already_lost_units": b["already_lost_units"],
                    "week_left_units": b["week"]["left_units"],
                    "window_used_pct": b["current_window"]["used_pct"],
                    "predicted_p95_human_usage_pct": p.get("predicted_p95_human_usage_pct"),
                    "based_on": (p.get("internals") or {}).get("based_on"),
                    "decision": result["decision"], "verdict": b["verdict"],
                    "dry_run": dry_run})
    finally:
        held.close()
    result["ms"] = round((time.perf_counter() - started) * 1000, 2)
    log("%s · %s · %.0f ms" % (",".join(result["stages"]), result["decision"],
                               result["ms"]))
    return result
def budget_table(report: dict) -> str:
    """The table a human would draw, which is also the acceptance test for P1."""
    now = report["computed_ts"]
    lines = []
    for agent, a in report["agents"].items():
        if not a["remaining_windows"]:
            lines.append("%s · %s" % (agent, a["verdict"]))
            continue
        reset = a["week"]["end_ts"]
        lines.append("%s · now %s · week ends %s (in %s) · %.2f units left"
                     % (agent, clock(now), clock(reset), lasting(reset - now),
                        a["week"]["left_units"]))
        lines.append("")
        lines.append("  window                   max_spend   planned   left")
        left = a["week"]["left_units"]
        for w in a["remaining_windows"]:
            left -= w["planned_units"]
            split = "     (split by the week's end)" if w["end_ts"] == reset else ""
            lines.append("  [%-9s – %-9s] %8.2f  %8.2f %6.2f%s"
                         % (clock(w["start_ts"]), clock(w["end_ts"]),
                            w["max_spend_units"], w["planned_units"], left, split))
        lines.append("")
        lines.append("  extra_quota_to_spend_units %.2f · spend by %s · already lost %.2f · %s"
                     % (a["extra_quota_to_spend_units"], clock(a["spend_by_ts"]),
                        a["already_lost_units"], a["verdict"]))
        lines.append("")
    return "\n".join(lines)
def when(value) -> "int | None":
    """`--at`: an epoch second or an ISO timestamp, which is what a backtest loop
    and a human respectively find natural to pass."""
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    moment = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return int(moment.timestamp())
