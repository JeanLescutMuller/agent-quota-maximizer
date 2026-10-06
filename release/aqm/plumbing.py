"""Locks, logging, metrics and housekeeping -- ../../design/07_pipeline/DESIGN.md 4, 5, 8.

No stage logic: this is what makes a scheduled tick survivable.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import pathlib
import time

from .core import P, home, state_dir
from .io import shown


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
