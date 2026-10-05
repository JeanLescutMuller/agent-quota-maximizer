#!/usr/bin/python3
"""aqm -- agent-quota-maximizer.

`ingest` turns the raw meter logs written by agent-usage-tracker into one tidy
5-minute table per agent (../design/01_ingestion/DESIGN.md). `budget` decides how
much may be spent and by when (../design/03_budgeting/DESIGN.md).

One file, and every stage is a function in it, because a tick costs 39 ms of
interpreter startup against 4 ms of work: stages share a process or the startup is
paid again for nothing (../design/01_ingestion/DESIGN.md 8).

Python 3.9, standard library only, no network, no subprocess.
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import sys
import time

AGENTS = ("claude", "codex")
SLOT_SECONDS = 300          # seconds; ROLLUP_BUCKET
FIVE_HOURS = 18000
TAIL_START = int(os.environ.get("AQM_TAIL_BYTES", 65536))  # READ_TAIL_KB: sized
                      # for a normal tick and doubled when short, so a small
                      # value only costs doublings -- which is what tests exploit
FAKE_TOLERANCE = 180  # Codex fake idle countdown: |reset - now - span| below this
SMALL_TAIL = 4096     # bytes: enough for ~21 slot rows (105 min) or ~28 meter
                      # readings. Queries that want only the recent past start
                      # here instead of at TAIL_START, because typing a 64 KB tail
                      # to answer "the last 30 minutes" was the single most
                      # expensive thing in a tick (../design/02_prediction/DESIGN.md 7)

Reading = collections.namedtuple(
    "Reading", "agent at window_used_pct week_used_pct window_end_ts week_end_ts source agent_session_id")


def usage_dir() -> pathlib.Path:
    """Where agent-usage-tracker writes the raw meter logs we read."""
    return pathlib.Path(os.environ.get(
        "AQM_USAGE_DATA", "~/opt/agent-usage-tracker")).expanduser()


def home() -> pathlib.Path:
    return pathlib.Path(os.environ.get(
        "AQM_HOME", "~/opt/agent-quota-maximizer")).expanduser()


def data_dir(agent: str) -> pathlib.Path:
    """`data/<agent>/` -- ingested history, one directory per agent, mirroring
    the layout agent-usage-tracker uses for its own sources."""
    return home() / "data" / agent


def state_dir() -> pathlib.Path:
    """`state/` -- what the pipeline mutates as it runs: our own run records,
    locks, latest-artifact pointers. Distinct from `data/`, which is history and
    is rebuildable from the raw logs at any time."""
    return home() / "state"


# --------------------------------------------------------------------------
# parameters and configuration
# --------------------------------------------------------------------------
#
# One table, ../design/07_pipeline/DESIGN.md 11, which owns the values and the
# reasoning. `config.json` may override any of them; `load_config()` applies the
# overrides into P once, at startup, so no function has to be handed a config.

P = {
    # Measured, not chosen: replaying this engine over the 8-day held-out period in
    # `../design/02_prediction/lab/live_replay.py`, the forecast is exactly 0 on 73.6%
    # of ticks, and on those the human's demand to the window's end has a 90.9th
    # percentile of 26%. So on three ticks in four this floor is the *only* protection
    # the user has, and 10 was far too low: 25 costs 9.1% less under the cost model in
    # `../design/02_prediction/lab/README.md`. It also settles the GUARD_PCT conflict,
    # since 25 >= 15 (`../design/03_budgeting/DESIGN.md` 9).
    "MIN_HUMAN_RESERVE_PCT": 25.0,          # pct of a window never offered to the bot
    "WINDOW_GAP_SECONDS": 300,              # assumed gap between consecutive windows
    "DEADLINE_MARGIN_SECONDS": 300,         # safety before a window's end
    "BOT_BURN_UNITS_PER_HOUR": 1.0,         # assumed, until P4 measures one
    "BURN_LOOKBACK_MINUTES": 30,            # how far back the recent human rate looks
    "MIN_RATE_DENOMINATOR_MINUTES": 10,     # floor on the window-average divisor
    # 1.5 is also measured rather than assumed: the same replay tried 2, 3 and 5 (the
    # value a simplified bench preferred) and every one of them cost more, because this
    # engine's rate is already the larger of two arms.
    "SAFETY_MULTIPLIER": 1.5,               # turns an average rate into a p95
    "ACTIVE_HUMAN_BURN_RATE": 0.15,         # pct of a window per minute, when the floor fires
    "READING_MAX_AGE_SECONDS": 600,         # s: freshness required of a meter reading
    "ARTIFACT_MAX_AGE_SECONDS": 600,           # s: freshness required of an input artifact
    "ARTIFACT_RETENTION_DAYS": 14,
    "LOG_MAX_MB": 20,
    "TICK_INTERVAL_SECONDS": 300,           # s: the scheduler period
    "WEEK_CAPACITY_UNITS": {"claude": 8.85, "codex": 6.2},   # 5h windows per 7-day period
}

CONFIG = {"enabled": True, "disabled_reason": None, "agents": list(AGENTS)}


def config_path() -> pathlib.Path:
    return home() / "config.json"


def load_config() -> dict:
    """Read `config.json` and fold its parameters into P.

    A missing file is the designed state of a fresh checkout, so the defaults
    stand. A file that exists but does not parse is a **hard error**: it means
    someone edited it and got it wrong, and running on silently-ignored
    configuration is how a safety limit disappears without anyone noticing
    (../design/07_pipeline/DESIGN.md 3)."""
    CONFIG.update({"enabled": True, "disabled_reason": None, "agents": list(AGENTS)})
    path = config_path()
    if not path.exists():
        return CONFIG
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("config error: %s: %s" % (path, exc))
    if not isinstance(raw, dict):
        raise ValueError("config error: %s is not an object" % path)
    for key, value in (raw.get("parameters") or {}).items():
        if key not in P:
            raise ValueError("config error: unknown parameter %r" % key)
        if not isinstance(value, type(P[key])) and not (
                isinstance(value, (int, float)) and isinstance(P[key], (int, float))):
            raise ValueError("config error: %s should be a %s"
                             % (key, type(P[key]).__name__))
        P[key] = value
    CONFIG.update({k: v for k, v in raw.items() if k != "parameters"})
    return CONFIG


def config_hash() -> str:
    """Identifies the parameters a past decision was taken under. Without it, a
    config edit is the one change that makes an old artifact unexplainable."""
    return hashlib.sha256(
        json.dumps(P, sort_keys=True).encode()).hexdigest()[:8]


def agents() -> tuple:
    """The configured agents, in a stable order. An agent named in config that we
    have no reader for is a config error, not something to skip quietly."""
    chosen = CONFIG.get("agents") or list(AGENTS)
    unknown = [a for a in chosen if a not in AGENTS]
    if unknown:
        raise ValueError("config error: unknown agents %s" % unknown)
    return tuple(a for a in AGENTS if a in chosen)


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


def read_all(path: pathlib.Path) -> list:
    if not path.exists():
        return []
    rows = []
    with path.open("rb") as handle:
        for line in handle:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


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
# state files: CSV, because these are read by hand and in a notebook
# --------------------------------------------------------------------------
#
# Every row leads with `observed_dt`, a local-time rendering of the row's own
# timestamp, followed by that timestamp as epoch seconds. The epoch value is what
# everything computes on (`../design/07_pipeline/DESIGN.md` 7 keeps internals in
# UTC epoch seconds); the rendering exists so the file can be read without a tool,
# and carries its UTC offset so a DST change cannot make it ambiguous.
#
# CSV rather than JSONL because this stage's whole job is to normalise several raw
# shapes into one, so the output schema is fixed and the per-row key names were
# pure overhead. The raw inputs stay JSONL: they are not ours, and they really are
# heterogeneous.

METER_COLUMNS = ["observed_dt", "observed_ts", "window_used_pct", "week_used_pct",
                 "window_end_dt", "window_end_ts", "week_end_dt", "week_end_ts",
                 "source", "agent_session_id"]

SLOT_COLUMNS = ["slot_start_dt", "slot_id",
                  "window_end_dt", "window_id",
                  "window_used_pct", "week_used_pct", "slot_window_used_pct", "slot_week_used_pct",
                  "slot_window_bot_pct", "slot_window_human_pct", "slot_window_human_pct_lo", "slot_window_human_pct_hi",
                  "attribution", "slot_bot_usd", "slot_human_usd", "window_age_minutes",
                  "is_window_maxed", "is_window_open", "slot_n_readings", "was_machine_awake",
                  "n_bot_workers", "was_human_active"]

# CSV loses types, so they are declared once, here. Columns absent from these maps
# are display-only and come back as the string in the file.
METER_TYPES = {"observed_ts": int, "window_used_pct": float, "week_used_pct": float,
               "window_end_ts": int, "week_end_ts": int}
SLOT_TYPES = {"window_used_pct": float, "week_used_pct": float,
                "slot_window_used_pct": float, "slot_week_used_pct": float,
                "slot_window_bot_pct": float, "slot_window_human_pct": float,
                "slot_window_human_pct_lo": float, "slot_window_human_pct_hi": float,
                "slot_bot_usd": float, "slot_human_usd": float, "window_age_minutes": int,
                "is_window_maxed": bool, "is_window_open": bool, "was_machine_awake": bool,
                "slot_n_readings": int, "n_bot_workers": int, "was_human_active": int}


def shown(epoch) -> str:
    """Local time with its UTC offset, e.g. `2026-10-04 14:20:00 +0200`.

    Built from an aware UTC datetime and converted, not from a naive
    `fromtimestamp`: a naive value renders `%z` as nothing, which would drop the
    offset and make an hour in late October ambiguous."""
    if epoch is None:
        return ""
    local = dt.datetime.fromtimestamp(epoch, dt.timezone.utc).astimezone()
    return local.strftime("%Y-%m-%d %H:%M:%S %z")


def cell(value) -> str:
    """None is an empty field, booleans are words, and a whole float loses its
    trailing .0 so the file stays readable. Types are restored on read."""
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def typed(row: dict, types: dict) -> dict:
    out = {}
    for key, raw in row.items():
        if raw == "" or raw is None:
            out[key] = None
        elif key in types:
            out[key] = (raw == "true") if types[key] is bool else types[key](raw)
        else:
            out[key] = raw
    return out


def meter_path(agent) -> pathlib.Path:
    return data_dir(agent) / "meter.csv"


def slots_path(agent) -> pathlib.Path:
    return data_dir(agent) / "slots.csv"


def append(path: pathlib.Path, columns, rows) -> None:
    """Append whole lines, writing the header when the file is new. A row whose
    keys do not match the columns raises rather than silently shifting every
    field after it."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="") as handle:
        # lineterminator="\n": csv.writer defaults to RFC-4180 CRLF, which puts a
        # stray \r on the last field of every `cut`, `awk` or `column` pipeline --
        # and these files exist to be read by hand on a Unix machine (DESIGN.md 7).
        writer = csv.writer(handle, lineterminator="\n")
        if fresh:
            writer.writerow(columns)
        for row in rows:
            if set(row) != set(columns):
                raise ValueError("row does not match the schema: %s"
                                 % sorted(set(row) ^ set(columns)))
            writer.writerow([cell(row[name]) for name in columns])


def read_csv(path: pathlib.Path, types: dict) -> list:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return [typed(row, types) for row in csv.DictReader(handle)]


def tail_csv(path: pathlib.Path, types: dict, n: int = TAIL_START) -> list:
    """The last rows of a growing CSV. The header is read separately so the tail
    can start mid-row: a partial first line is dropped, and the header is skipped
    if the window reaches back over it."""
    if not path.exists():
        return []
    size = path.stat().st_size
    with path.open("rb") as handle:
        raw_header = handle.readline()
        if not raw_header.strip():
            return []
        handle.seek(max(len(raw_header), size - n))
        blob = handle.read().decode("utf-8", "replace")
    if size - n > len(raw_header):
        blob = blob.partition("\n")[2]
    columns = next(csv.reader([raw_header.decode().strip()]))
    rows = []
    for parsed in csv.reader(blob.splitlines()):
        if len(parsed) != len(columns) or parsed[0] == columns[0]:
            continue                            # partial row, or the header
        rows.append(typed(dict(zip(columns, parsed)), types))
    return rows


def load_meter(agent):
    """Returns (readings, watermark, drop_stale_readings state) -- all derived from the file
    itself, so there is no cursor that can go stale. Tailed, not read whole: the
    watermark, the open window's maximum and the slot primer are all recent, so
    per-tick cost stays flat as the history grows."""
    readings, best, watermark = [], {}, 0
    for row in tail_csv(meter_path(agent), METER_TYPES):
        r = Reading(agent, row["observed_ts"], row["window_used_pct"], row["week_used_pct"],
                    row["window_end_ts"], row["week_end_ts"], row["source"],
                    row["agent_session_id"])
        readings.append(r)
        watermark = max(watermark, r.at)
        if r.window_end_ts is None:
            best["window_believed_closed"] = True
        else:
            best["window_believed_closed"] = False
            best["last_window_end_ts"] = r.window_end_ts
            best[r.window_end_ts] = max(best.get(r.window_end_ts, -1), r.window_used_pct)
    return readings, watermark, best


def meter_row(r: Reading) -> dict:
    return {"observed_dt": shown(r.at), "observed_ts": r.at,
            "window_used_pct": r.window_used_pct, "week_used_pct": r.week_used_pct,
            "window_end_dt": shown(r.window_end_ts), "window_end_ts": r.window_end_ts,
            "week_end_dt": shown(r.week_end_ts), "week_end_ts": r.week_end_ts,
            "source": r.source, "agent_session_id": r.agent_session_id}


def last_slot_end(agent) -> int:
    """Highest slot end already written for this agent.

    Reads the final line only. Rows are appended in time order and one file holds
    one agent, so the last line is the answer -- and parsing a whole tail here
    meant type-converting ~1,900 cells a tick to recover one integer. When both
    agents shared a file this could not be done, because a tail could land
    entirely inside one agent's rows and report zero for the other."""
    path = slots_path(agent)
    if not path.exists():
        return 0
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(max(0, size - 4096))
        lines = [l for l in handle.read().splitlines() if l.strip()]
    if len(lines) < 2:                      # header only, or empty
        return 0
    row = next(csv.reader([lines[-1].decode("utf-8", "replace")]))
    try:
        return int(row[SLOT_COLUMNS.index("slot_id")].split("_")[1])
    except (IndexError, ValueError):
        return 0


def bot_sessions():
    runs, ours = [], set()
    for row in read_all(state_dir() / "runs.jsonl"):
        if row.get("started_at") and row.get("ended_at"):
            runs.append((row["started_at"], row["ended_at"]))
        if row.get("session_id"):
            ours.add(row["session_id"])
    return runs, frozenset(ours)


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


# --------------------------------------------------------------------------
# artifacts: immutable, one per stage per tick
# --------------------------------------------------------------------------

def write_artifact(kind: str, payload: dict, at: int, out=None) -> pathlib.Path:
    """`artifacts/<kind>/<date>/<time>.json`, plus the `state/latest` pointer.

    Written to a temporary name and renamed, so a supervisor reading the previous
    artifact never sees a half-written one. With `out` the artifact goes there and
    no pointer moves: that is what keeps a backtest out of the live tree
    (../design/07_pipeline/DESIGN.md 5)."""
    if out is not None:
        path = pathlib.Path(out).expanduser()
    else:
        local = dt.datetime.fromtimestamp(at, dt.timezone.utc).astimezone()
        path = (home() / "artifacts" / kind / local.strftime("%Y-%m-%d")
                / (local.strftime("%H%M%S") + ".json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)
    if out is None:
        link = state_dir() / "latest" / (kind[:-1] + ".json")
        link.parent.mkdir(parents=True, exist_ok=True)
        staging = link.with_name(link.name + ".tmp")
        staging.unlink(missing_ok=True)
        staging.symlink_to(path)
        os.replace(staging, link)
    return path


def latest_artifact(kind: str) -> "dict | None":
    link = state_dir() / "latest" / (kind[:-1] + ".json")
    if not link.exists():
        return None
    return json.loads(link.read_text())


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


# --------------------------------------------------------------------------
# plumbing: locks, logs, housekeeping
# --------------------------------------------------------------------------
#
# ../design/07_pipeline/DESIGN.md 4, 5, 8.

def acquire(name: str):
    """A non-blocking `flock`. Returns the open file on success -- keep it alive for
    as long as the lock is wanted -- or None if someone else holds it.

    There is no stale-lock reclaim and none is needed: `flock` is held by an open
    file descriptor, so the kernel releases it when the holder dies, however it
    dies. The PID is written only so a human can see who is in there."""
    path = state_dir() / "locks" / (name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write("%d\n" % os.getpid())
    handle.flush()
    return handle


def log(line: str) -> None:
    path = home() / "logs" / "pipeline.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write("%s %s\n" % (shown(int(time.time())), line))


def metric(row: dict) -> None:
    """One line per tick per agent, in `logs/metrics.jsonl`. This is the file the
    notebook reads, and **a gap in it is how a silently dead job is noticed** --
    which is why it is appended even on a tick that decides nothing."""
    path = home() / "logs" / "metrics.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def rotate(path: pathlib.Path) -> None:
    if path.exists() and path.stat().st_size > P["LOG_MAX_MB"] * 1024 * 1024:
        path.replace(path.with_name(path.name + ".1"))


def housekeeping(now: int) -> dict:
    """Rotate the logs and prune old artifacts. Date-sharded directories make the
    pruning a comparison of names and an unlink of a directory, with nothing to
    list and no path that grows without bound."""
    for name in ("pipeline.log", "metrics.jsonl", "executor.log"):
        rotate(home() / "logs" / name)
    cutoff = (dt.datetime.fromtimestamp(now, dt.timezone.utc).astimezone()
              - dt.timedelta(days=P["ARTIFACT_RETENTION_DAYS"])).strftime("%Y-%m-%d")
    pruned = 0
    root = home() / "artifacts"
    for kind in sorted(root.glob("*")) if root.exists() else []:
        for day in sorted(kind.glob("*")):
            if day.is_dir() and day.name < cutoff:
                for item in day.iterdir():
                    item.unlink()
                day.rmdir()
                pruned += 1
    return {"pruned_days": pruned}


def fresh_enough(report: dict, now: int) -> bool:
    """Is this artifact close enough in time to `now` to be decided on?

    The case that matters is a stage having failed on the previous tick: without
    this, the next stage decides confidently on data from an hour ago. The distance
    is absolute, because for a `--at` backtest an artifact from *after* the moment
    being replayed is just as wrong as a stale one -- it would leak the future into
    a past decision, which is the one error a backtest must not make."""
    if not report:
        return False
    return abs(now - report.get("computed_ts", 0)) <= P["ARTIFACT_MAX_AGE_SECONDS"]


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


# --------------------------------------------------------------------------

def clock(epoch) -> str:
    if epoch is None:
        return "--:--"
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).astimezone() \
             .strftime("%a %H:%M")


def lasting(seconds: int) -> str:
    return "%dh%02dm" % (seconds // 3600, seconds % 3600 // 60)


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


#   0 success · 1 error · 2 refused by a guard · 3 locked
#   (../design/07_pipeline/DESIGN.md 1.2)
REFUSED = {"disabled": 2, "stale": 2}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="aqm")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    one = sub.add_parser("ingest", help="read the meter logs into the 5-minute table")
    one.add_argument("--backfill", action="store_true",
                     help="rebuild from the whole history (a few seconds)")
    one.add_argument("--now", type=int, default=None,
                     help="treat this epoch as the current time (tests, replay)")
    one.add_argument("--json", action="store_true")

    two = sub.add_parser("predict", help="how much the user will still want")
    three = sub.add_parser("budget", help="how much may be spent, and by when")
    for p in (two, three):
        p.add_argument("--at", default=None,
                       help="decide as of this moment (epoch or ISO); never writes")
        p.add_argument("--out", default=None, help="write the artifact here instead")
        p.add_argument("--json", action="store_true")
    three.add_argument("--prediction", default=None,
                       help="a prediction artifact (default: state/latest)")

    run = sub.add_parser("pipeline", help="every stage in order: the scheduled job")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--now", type=int, default=None)
    run.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    started = time.perf_counter()
    try:
        load_config()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.command == "ingest":
        report = ingest(backfill=args.backfill, now=args.now)
        elapsed = (time.perf_counter() - started) * 1000
        if args.json:
            print(json.dumps({"stage": "ingest", "ms": round(elapsed, 2),
                              "agents": report}))
        else:
            for agent, r in report.items():
                print("%-7s %4d new readings  %5d new slots  %s windows in the tail"
                      % (agent, r["readings"], r["slots"], r.get("recent_windows", "?")))
            print("%.1f ms" % elapsed)
        return 0

    if args.command == "pipeline":
        report = pipeline(dry_run=args.dry_run, now=args.now)
        if args.json:
            print(json.dumps(report, sort_keys=True))
        else:
            print("%s · %s" % (" → ".join(report["stages"]) or "none",
                               report["decision"]))
            if report.get("budget"):
                print(budget_table(report["budget"]))
        return REFUSED.get(report["decision"], 3 if report["decision"] == "busy" else 0)

    # `predict` and `budget`: `--at` is the backtest knob and must not touch the
    # live tree, so it writes only where it is told to (07_pipeline/DESIGN.md 1.2).
    at = when(args.at)
    if args.command == "predict":
        report, kind, table = predict(at=at), "predictions", predict_table
    else:
        report, kind, table = (budget(at=at, prediction=args.prediction),
                               "budgets", budget_table)
    elapsed = (time.perf_counter() - started) * 1000
    if args.out is not None or at is None:
        write_artifact(kind, report, report["computed_ts"], out=args.out)
    if args.json:
        print(json.dumps(dict(report, ms=round(elapsed, 2)), sort_keys=True))
    else:
        print(table(report))
        print("%.1f ms" % elapsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
