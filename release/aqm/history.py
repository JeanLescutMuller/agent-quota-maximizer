"""Questions asked of the ingested table.

`io` knows how to read a row; this knows which rows answer "what does the meter say
now", "how fast has the human been burning". Stages 2 and 3 read the history through
here rather than touching a CSV.
"""
from __future__ import annotations

import time

from .core import FIVE_HOURS, P, SMALL_TAIL
from .io import METER_TYPES, SLOT_TYPES, meter_path, read_csv, slots_path, tail_csv


# --------------------------------------------------------------------------
# what later stages consume
# --------------------------------------------------------------------------

def meter_now(agent, now: "int | None" = None) -> "dict | None":
    """The freshest reading at or before `now`, which defaults to the present.

    Tailed, then checked for sufficiency, exactly as ingestion reads the raw logs
    (DESIGN.md 4): if the tail's oldest row is already newer than `now`, the moment
    asked about predates the tail and the whole file is read instead. That only
    happens for a `--at` backtest weeks in the past; on the hot path `now` is the
    present, the tail always suffices, and nothing beyond it is parsed. Passing a
    resolved `now` unconditionally is what made this read the whole file every
    tick."""
    at = int(time.time()) if now is None else now
    rows = tail_csv(meter_path(agent), METER_TYPES, SMALL_TAIL)
    if rows and rows[0]["observed_ts"] > at:
        rows = read_csv(meter_path(agent), METER_TYPES)
    rows = [row for row in rows if row["observed_ts"] <= at]
    if not rows:
        return None
    r = rows[-1]
    return {"window_used_pct": r["window_used_pct"], "week_used_pct": r["week_used_pct"],
            "window_end_ts": r["window_end_ts"], "week_end_ts": r["week_end_ts"],
            "observed_ts": r["observed_ts"], "age": at - r["observed_ts"],
            "source": r["source"]}
def window_state(agent, now: "int | None" = None) -> dict:
    m = meter_now(agent, now)
    at = int(time.time()) if now is None else now
    if not m or m["window_end_ts"] is None:
        return {"open": False, "end": None, "used": None, "window_age_minutes": None}
    return {"open": True, "end": m["window_end_ts"], "used": m["window_used_pct"],
            "window_age_minutes": (at - (m["window_end_ts"] - FIVE_HOURS)) // 60}
def recent_slots(agent, since: int, now: int) -> list:
    """Slot rows overlapping [since, now].

    Tailed small and doubled until it demonstrably reaches back past `since`, the
    same shape as read_tail() (../design/01_ingestion/DESIGN.md 4). Starting at
    TAIL_START instead cost ~6,600 cell conversions per tick to find the six rows
    of a 30-minute window -- the third time in this codebase that reading more CSV
    than the answer needs turned out to be the dominant cost."""
    path = slots_path(agent)
    size = path.stat().st_size if path.exists() else 0
    n = SMALL_TAIL
    while True:
        rows = tail_csv(path, SLOT_TYPES, n)
        reaches = not rows or int(rows[0]["slot_id"].split("_")[0]) <= since
        if reaches or n >= size:
            break
        n *= 2
    out = []
    for row in rows:
        b0, b1 = (int(x) for x in row["slot_id"].split("_"))
        if b1 > since and b0 <= now:
            out.append(row)
    return out
def burn_rate(agent, minutes=None, kind="human", now=None) -> float:
    """Percent of the 5-hour window per minute. Censored slots contribute
    their upper bound: the conservative direction predicts more human demand."""
    at = int(time.time()) if now is None else now
    minutes = P["BURN_LOOKBACK_MINUTES"] if minutes is None else minutes
    field = "human" if kind == "human" else "extra"
    total = span = 0.0
    for row in recent_slots(agent, at - minutes * 60, at):
        b0, b1 = (int(x) for x in row["slot_id"].split("_"))
        value = row.get(field + "_pct")
        if value is None:
            value = row["slot_window_human_pct_hi"] if field == "human" else row["slot_window_used_pct"]
        total += value
        span += (b1 - b0) / 60.0
    return total / span if span else 0.0
