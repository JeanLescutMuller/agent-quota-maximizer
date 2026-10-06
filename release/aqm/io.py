"""Reading and writing our own files, and turning values into text.

The CSV column lists and their types, the append/read/tail primitives, the artifact
writer, and the four formatters (`shown`, `cell`, `clock`, `lasting`). Everything here
is about a value's *representation*, whether its destination is a CSV cell, a JSON
artifact or a terminal table; nothing here knows what any stage means.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import pathlib

from .core import Reading, TAIL_START, data_dir, home, state_dir


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

def clock(epoch) -> str:
    if epoch is None:
        return "--:--"
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).astimezone() \
             .strftime("%a %H:%M")
def lasting(seconds: int) -> str:
    return "%dh%02dm" % (seconds // 3600, seconds % 3600 // 60)
