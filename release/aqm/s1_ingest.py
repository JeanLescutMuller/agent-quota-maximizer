"""Stage 1 -- ../../design/01_ingestion/DESIGN.md.

Raw account logs into two tidy tables: normalise three reset formats into one shape,
tail with a watermark, drop the stale readings, build the 5-minute slot grid.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import pathlib
import time

from .core import (FAKE_TOLERANCE,
    FIVE_HOURS,
    Reading,
    SLOT_SECONDS,
    TAIL_START,
    agents,
    usage_dir)
from .io import (METER_COLUMNS,
    SLOT_COLUMNS,
    append,
    bot_sessions,
    last_slot_end,
    load_meter,
    meter_path,
    meter_row,
    read_all,
    shown,
    slots_path)


# --------------------------------------------------------------------------
# normalising: three reset formats, two agents, one shape
# --------------------------------------------------------------------------

def window_end(value) -> "int | None":
    """Reset time -> epoch seconds, rounded to the minute.

    Rounding is not cosmetic. Both agents report one window's reset at two
    different instants -- Claude's push rows carry it one second below its API's
    -- so an unrounded value splits 40 of 46 real windows in two. The rounded
    value doubles as the window identifier, which is why this stays in seconds:
    the audit trail has to be readable by a human, and the three input formats
    (epoch int, epoch string, ISO) all collapse here. DESIGN.md 5."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        try:
            seconds = float(int(value))
        except (TypeError, ValueError):
            try:
                iso = str(value).replace("Z", "+00:00")
                seconds = dt.datetime.fromisoformat(iso).timestamp()
            except ValueError:
                return None
    return int(round(seconds / 60) * 60)
def normalise(agent: str, row: dict) -> "Reading | None":
    """Rows are recognised by their **shape**, never by their `source` string.

    Upstream renames sources: `codex` became `codex_app_server`, which an exact
    match turned into zero readings for that agent, silently. The shape is what
    actually carries the meaning, and `USAGE_DATA_REFERENCE.md` 5.6 says as much --
    there is no schema version, so a reader must sniff."""
    if row.get("error"):
        # data/ holds readings only since 2026-10-04; a failure row here would
        # otherwise be silently read as a reading.
        raise ValueError("error row in data/: errors belong in logs/")
    return _claude(row) if agent == "claude" else _codex(row)
def _claude(row: dict) -> "Reading | None":
    if "api" not in row:        # a status-line push row
        at = row.get("observed_at")       # upstream's name, not ours
        if at is None:          # undated push row: unusable, never guessed
            return None
        if "five_hour_pct" not in row:
            return None
        return Reading("claude", at, row.get("five_hour_pct"), row.get("seven_day_pct"),
                       window_end(row.get("five_hour_resets_at")),
                       window_end(row.get("seven_day_resets_at")),
                       "claude_statusline", row.get("observed_by_session"))
    api = row.get("api") or {}
    five, seven = api.get("five_hour") or {}, api.get("seven_day") or {}
    if five.get("utilization") is None:
        return None
    return Reading("claude", row["ts"], five.get("utilization"), seven.get("utilization"),
                   window_end(five.get("resets_at")), window_end(seven.get("resets_at")),
                   "claude", None)
def _codex(row: dict) -> "Reading | None":
    if "codex_rate_limits" not in row:
        return None             # plan_limit_history is retroactive, not a live reading
    limits = (row.get("codex_rate_limits") or {}).get("rateLimits") or {}
    primary, secondary = limits.get("primary") or {}, limits.get("secondary") or {}
    pct, reset = primary.get("usedPercent"), primary.get("resetsAt")
    if pct is None:
        return None
    span = (primary.get("windowDurationMins") or 300) * 60
    if pct == 0 and reset and abs(reset - row["ts"] - span) < FAKE_TOLERANCE:
        reset = None            # fake idle countdown: no window is open
    return Reading("codex", row["ts"], pct, secondary.get("usedPercent"),
                   window_end(reset), window_end(secondary.get("resetsAt")),
                   "codex", None)
# --------------------------------------------------------------------------
# reading: tail and watermark, no cursors
# --------------------------------------------------------------------------

def read_tail(path: pathlib.Path, watermark: int, start: int = TAIL_START):
    """(rows newer than the watermark, slot starts of every row seen). Doubles
    the tail until it demonstrably reaches back past the watermark, so a long
    sleep cannot drop rows. The second value is liveness: a stale row that the
    watermark discards still proves the machine was awake in its slot."""
    if not path.exists():
        return [], frozenset()
    size = path.stat().st_size
    n = start
    while True:
        with path.open("rb") as handle:
            handle.seek(max(0, size - n))
            blob = handle.read()
        if n < size:
            blob = blob.partition(b"\n")[2]     # drop the partial first line
        rows, seen, oldest = [], set(), None
        for line in blob.splitlines():
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue                        # half-written final line
            at = row.get("observed_at") or row.get("ts")   # upstream names
            if at is None:
                continue
            oldest = at if oldest is None else min(oldest, at)
            seen.add(at // SLOT_SECONDS * SLOT_SECONDS)
            if at > watermark:
                rows.append(row)
        if n >= size or (oldest is not None and oldest <= watermark):
            return rows, frozenset(seen)
        n *= 2
# --------------------------------------------------------------------------
# drop_stale_readings: within a window the true percentage never decreases
# --------------------------------------------------------------------------

def drop_stale_readings(readings, best: dict) -> list:
    """Keep the first reading of each new running maximum, per window. `best`
    carries state across ticks and is mutated. DESIGN.md 5."""
    kept = []
    for r in sorted(readings, key=lambda x: x.at):
        if r.window_used_pct is None:
            continue
        if r.window_end_ts is None:
            # A missing reset is believed only once the last known window has
            # really expired. 65 of the 122 such rows in the recorded history
            # claimed "no window" while a window was demonstrably still running --
            # one of them four hours before its real end -- because the status line
            # renders before `rate_limits` arrive and upstream stores the row
            # anyway. Believing one costs the rest of that window: `window_used`
            # reads 0, and budgeting projects a hypothetical window on top of the
            # real one. DESIGN.md 5.
            if best.get("last_window_end_ts") and best["last_window_end_ts"] > r.at:
                continue
            if best.get("window_believed_closed"):
                continue                        # collapse a run of idle readings
            best["window_believed_closed"] = True
            kept.append(r)
            continue
        best["window_believed_closed"] = False
        best["last_window_end_ts"] = r.window_end_ts
        if best.get(r.window_end_ts, -1) >= r.window_used_pct:
            continue
        best[r.window_end_ts] = r.window_used_pct
        kept.append(r)
    return kept
# --------------------------------------------------------------------------
# the 5-minute table
# --------------------------------------------------------------------------

def build_slots(agent, readings, start, end, runs=(), ours=frozenset(),
                  prime=None, raw_at=frozenset(), was_human_active_at=None) -> list:
    # `was_machine_awake` answers "was the machine awake", which is what distinguishes a
    # real zero from a gap (DESIGN.md 6.3). Two things prove it: a raw row
    # timestamped in the slot, or the slot being the one this tick just
    # closed -- a tick running at all means the machine was up. A backfill has
    # only the first proof, so its `was_machine_awake` is the conservative version.
    """One row per 5-minute slot in [start, end). Increases are attributed to
    the slot in which they were *observed*: with ~20 readings a day the moment
    inside a gap is unknowable, and inventing one would be worse. `prime` is the
    last reading before `start`, so the first slot's delta is a delta."""
    readings = sorted((r for r in readings if r.at >= start), key=lambda r: r.at)
    was_human_active_at = was_human_active_at or {}
    index, seen = 0, {}
    level = seven_level = window = None
    if prime is not None:
        window = prime.window_end_ts
        level = prime.window_used_pct if window is not None else None
        if window is not None:
            seen[window] = prime.window_used_pct
        if prime.week_end_ts is not None and prime.week_used_pct is not None:
            seen[("week", prime.week_end_ts)] = prime.week_used_pct
            seven_level = prime.week_used_pct
    rows = []
    for b0 in range(start, end, SLOT_SECONDS):
        b1 = b0 + SLOT_SECONDS
        d5 = d7 = 0.0
        n = 0
        capped = False
        while index < len(readings) and readings[index].at < b1:
            r = readings[index]
            index += 1
            n += 1
            if r.window_end_ts is None:
                window = level = None       # a closed window's level is not current
            else:
                d5 += max(0.0, r.window_used_pct - seen.get(r.window_end_ts, 0.0))
                seen[r.window_end_ts] = r.window_used_pct
                window, level = r.window_end_ts, r.window_used_pct
                capped = capped or r.window_used_pct >= 100
            if r.week_end_ts is not None and r.week_used_pct is not None:
                key = ("week", r.week_end_ts)
                d7 += max(0.0, r.week_used_pct - seen.get(key, 0.0))
                seen[key] = r.week_used_pct
                seven_level = r.week_used_pct
        # `was_human_active` is counted from the RAW rows, before the drop_stale_readings. The
        # drop_stale_readings exists to drop readings that carry no new percentage -- and a
        # reading from a session that is not ours proves the user was there
        # whether or not the meter had ticked yet. Counting it after the drop_stale_readings
        # silently deleted the one case the presence floor exists for: a user who
        # has just started, whose first 1% has not landed (DESIGN.md 6.4).
        was_human_active = was_human_active_at.get(b0, 0)
        workers = sum(1 for s, e in runs if s < b1 and e > b0)
        rows.append(_slot_row(b0, b1, window, level, seven_level, d5, d7,
                                n, was_human_active, workers, capped, b0 in raw_at or b1 == end))
    return rows
def _slot_row(b0, b1, window, level, seven_level,
                d5, d7, n, was_human_active, workers, capped, observed) -> dict:
    if workers == 0:
        extra, human, lo, hi, how = 0.0, d5, d5, d5, "exact"
    elif was_human_active == 0:
        extra, human, lo, hi, how = d5, 0.0, 0.0, 0.0, "exact"
    else:
        # Overlap. Apportioning by measured dollars needs the session feed,
        # which P0 does not read; bounds are kept rather than a number invented.
        extra, human, lo, hi, how = None, None, 0.0, d5, "censored"
    end = window
    return {
        "slot_start_dt": shown(b0),
        "slot_id": "%d_%d" % (b0, b1),
        "window_end_dt": shown(end),
        "window_id": "%d_%d" % (end - FIVE_HOURS, end) if end else None,
        "window_used_pct": level,
        "week_used_pct": seven_level,
        "slot_window_used_pct": round(d5, 4),
        "slot_week_used_pct": round(d7, 4),
        "slot_window_bot_pct": extra,
        "slot_window_human_pct": human,
        "slot_window_human_pct_lo": round(lo, 4),
        "slot_window_human_pct_hi": round(hi, 4),
        "attribution": how,
        "slot_bot_usd": None,
        "slot_human_usd": None,
        "window_age_minutes": (b1 - (end - FIVE_HOURS)) // 60 if end else None,
        "is_window_maxed": capped,
        "is_window_open": window is not None,
        "slot_n_readings": n,
        "was_machine_awake": observed,
        "n_bot_workers": workers,
        "was_human_active": was_human_active,
    }
# --------------------------------------------------------------------------
# the stage
# --------------------------------------------------------------------------

def ingest(backfill: bool = False, now: "int | None" = None) -> dict:
    now = int(time.time()) if now is None else now
    horizon = now - now % SLOT_SECONDS               # only closed slots are written
    runs, ours = bot_sessions()
    report = {}

    if backfill:
        for agent in agents():
            meter_path(agent).unlink(missing_ok=True)
            slots_path(agent).unlink(missing_ok=True)

    for agent in agents():
        source = usage_dir() / "data" / agent / "account.jsonl"
        history, watermark, best = load_meter(agent)
        if backfill:
            raw = read_all(source)
            raw_at = frozenset((row.get("observed_ts") or row.get("ts") or 0)
                               // SLOT_SECONDS * SLOT_SECONDS for row in raw)
        else:
            raw, raw_at = read_tail(source, watermark)
        parsed = [normalise(agent, row) for row in raw]
        if raw and not any(r is not None for r in parsed):
            raise ValueError(
                "%s: %d rows read from %s and not one was recognisable -- the "
                "upstream shape has changed" % (agent, len(raw), source))
        # From every row read this tick, not only the fresh ones: a re-pushed
        # stale row still proves who was active in its slot.
        was_human_active_at = collections.Counter(
            r.at // SLOT_SECONDS * SLOT_SECONDS for r in parsed
            if r is not None and r.agent_session_id and r.agent_session_id not in ours)
        fresh = [r for r in parsed if r is not None and r.at > watermark]
        kept = drop_stale_readings(fresh, best)
        append(meter_path(agent), METER_COLUMNS, [meter_row(r) for r in kept])

        readings = history + kept
        start = last_slot_end(agent)
        if not start:
            if not readings:
                report[agent] = {"readings": 0, "slots": 0}
                continue
            first = min(r.at for r in readings)
            start = first - first % SLOT_SECONDS
        prior = [r for r in readings if r.at < start]
        mine = build_slots(agent, readings, start, horizon, runs, ours,
                             prime=prior[-1] if prior else None, raw_at=raw_at,
                             was_human_active_at=was_human_active_at)
        append(slots_path(agent), SLOT_COLUMNS, mine)
        report[agent] = {"readings": len(kept), "slots": len(mine),
                         "recent_windows": len([k for k in best if isinstance(k, int)])}
    return report
