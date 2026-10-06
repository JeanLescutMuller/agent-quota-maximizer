"""Constants, paths, and the parameter table.

Imported by every other module and importing none of them, so there is nowhere for a
cycle to form. `P` and `CONFIG` are **mutated, never rebound** -- `load_config` does
`P[key] = value` -- which is what lets every module share the one dict and what lets a
notebook patch `aqm.P` and have the change reach the stage it is testing.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import pathlib


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
