#!/usr/bin/python3
"""Replay the whole decision -- budget, a late start, a bot burning quota -- over the
recorded human usage, and measure what the user actually cares about: waste left at
each weekly reset, and how often the bot made the user wait at a full 5-hour window.

    /usr/bin/python3 notebook/pipeline_replay.py            # every table, ~20 s

Why this exists next to `design/02_prediction/lab/`: the lab scores a forecaster on its
own, on every tick. This scores the *pipeline*, so a forecast only counts where it
changes what the bot does. Written for the 2026-10-05 review of stages 1-3.

The model, deliberately small:

  human   recorded `slot_window_human_pct` per 5-minute slot (no bot has ever run, so
          all of it is human). Re-windowed: a window opens at the first demand -- human,
          bot or the window starter -- and lasts 5 h. Demand above a full window is
          blocked, not deferred.
  week    7-day periods chained from the first recorded reset; WEEK_CAPACITY_UNITS
          converts window percent into week units.
  budget  `release/aqm/s3_budget.py`, re-implemented in closed form: spend now =
          max(0, units left in the week - what the later windows can take), capped at
          what this window can take. Forecast = recent-rate-v1 (max of the 30-minute
          and whole-window human rates, active-human floor, x1.5, to the window's end).
  bot     acts only on ticks where the Mac is awake; starts when the time left in the
          window is <= amount / burn x 1.5 + 5 min ("late"), burns BURN units/h,
          never fills a window past 100 - GUARD.
  awake   proxy: any raw row (push, poll, poll error, Codex) within +-10 minutes.
          Checked against `pmset -g log` over its 7 days: 60% awake against a true 48%,
          and it never misses a waking minute -- so "real sleep" is the optimistic case.

Caveats: 6 weeks of one person; blocked demand is lost rather than postponed; the bot
re-plans every tick instead of freezing a mandate; BOT_BURN_UNITS_PER_HOUR is assumed.
"""
import collections
import csv
import datetime as dt
import glob
import json
import os

AQM = os.path.expanduser(os.environ.get("AQM_HOME", "~/opt/agent-quota-maximizer"))
USAGE = os.path.expanduser(os.environ.get("AQM_USAGE_DATA", "~/opt/agent-usage-tracker"))
SLOT, WIN = 300, 60            # seconds per slot, slots per 5-hour window
CAPACITY = {"claude": 8.85, "codex": 6.2}


# --------------------------------------------------------------------------- data

def load_slots(agent):
    out = []
    with open(f"{AQM}/data/{agent}/slots.csv") as handle:
        for r in csv.DictReader(handle):
            human = r["slot_window_human_pct"] or r["slot_window_human_pct_hi"] or "0"
            out.append({"t": int(r["slot_id"].split("_")[0]), "human": float(human),
                        "active": int(r["was_human_active"] or 0),
                        "window_pct": float(r["slot_window_used_pct"] or 0),
                        "week_pct": float(r["slot_week_used_pct"] or 0)})
    return sorted(out, key=lambda s: s["t"])


def first_week_end(agent):
    with open(f"{AQM}/data/{agent}/meter.csv") as handle:
        return next(int(r["week_end_ts"]) for r in csv.DictReader(handle) if r["week_end_ts"])


def awake_slots():
    """Slot starts during which the Mac was (probably) awake: any raw row within 10 min."""
    files = [f"{USAGE}/data/claude/account.jsonl", f"{USAGE}/data/codex/account.jsonl"]
    files += glob.glob(f"{USAGE}/logs/*poll-errors*.jsonl") + glob.glob(f"{USAGE}/data/_archive/*pre-rename")
    seen = set()
    for path in files:
        with open(path, "rb") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                at = row.get("observed_at") or row.get("ts")
                if isinstance(at, (int, float)):
                    seen.add(int(at) // SLOT * SLOT)
    return {t + d for t in seen for d in (-600, -300, 0, 300, 600)}


# --------------------------------------------------------------------------- the replay

def replay(agent, *, bot=True, floor=25.0, guard=15.0, forecast=True, awake=None, burn=1.0,
           start="late", idle_slots=6, prorate_straddle=False):
    S, cap = SLOTS[agent], CAPACITY[agent]
    week_end = first_week_end(agent)
    while week_end - 7 * 86400 > S[0]["t"]:
        week_end -= 7 * 86400
    week_used = 0.0                                   # units
    win_end, win_used, win_human, win_open = None, 0.0, 0.0, None
    recent = collections.deque(maxlen=max(6, idle_slots))
    weeks, waited, bot_in = [], {}, collections.Counter()
    n = collections.Counter()
    for s in S:
        t = s["t"]
        if t >= week_end:
            weeks.append(max(0.0, cap - week_used))
            week_used, week_end = 0.0, week_end + 7 * 86400
        if win_end is not None and t >= win_end:
            win_end = None
        is_awake = awake is None or t in awake
        if bot and is_awake and win_end is None:      # stage 4: keep a window open
            win_end, win_used, win_human, win_open = t + WIN * SLOT, 0.0, 0.0, t
        spend = 0.0
        if bot and is_awake:
            p95 = None
            if forecast:
                rate = sum(list(recent)[-6:]) / 30.0
                if win_end:
                    rate = max(rate, win_human / max((t - win_open) / 60.0, 10))
                if s["active"] and rate < 0.15:
                    rate = 0.15
                p95 = min(100.0, rate * ((win_end - t) / 60.0 if win_end else 300.0) * 1.5)
            reserve0 = floor if p95 is None else max(floor, p95)
            end0 = win_end or t + WIN * SLOT
            chain, st, en = [], t, end0
            while st < week_end:
                e = min(en, week_end)
                quota = (100 - (reserve0 if not chain else floor) - (win_used if not chain and win_end else 0)) / 100
                if prorate_straddle and en > week_end:
                    # the 5-hour window outlives the weekly reset: the bot may only fill the
                    # share of it that lies before the reset, so the new week starts with room
                    before = (week_end - (en - WIN * SLOT)) / (WIN * SLOT)
                    quota = min(quota, (100 - floor) * before / 100 - (win_used if not chain and win_end else 0) / 100)
                chain.append(max(0.0, min(quota, (e - st) / 3600.0 * burn)))
                st, en = en + 300, en + 300 + WIN * SLOT
            surplus = (1 - week_used / cap) * cap - sum(chain[1:])
            due = max(0.0, min(chain[0] if chain else 0.0, surplus))
            n["ticks"] += 1
            n["due"] += surplus > 0
            n["forecast_binds"] += surplus > 0 and p95 is not None and p95 > floor and chain[0] < surplus
            mins_left = (min(end0, week_end) - t) / 60.0    # a reset cuts the window short
            late = mins_left <= due / burn * 60 * 1.5 + 5
            idle = not s["active"] and sum(list(recent)[-idle_slots:]) == 0
            when = {"late": late, "asap": True, "idle": idle, "late_and_idle": late and idle}[start]
            if due > 0.005 and when and mins_left > 5:
                if win_end is None:
                    win_end, win_used, win_human, win_open = t + WIN * SLOT, 0.0, 0.0, t
                spend = min(burn * 100 / 12, due * 100, max(0.0, 100 - guard - win_used),
                            max(0.0, (cap - week_used) * 100))
        if spend:
            win_used += spend; week_used += spend / 100; n["bot"] += spend / 100
            bot_in[win_end] += spend
        d = s["human"]
        if d > 0 and win_end is None:
            win_end, win_used, win_human, win_open = t + WIN * SLOT, 0.0, 0.0, t
        if d > 0:
            ok = min(d, max(0.0, 100 - win_used), max(0.0, (cap - week_used) * 100))
            if d - ok > 0 and bot_in[win_end] > 0 and win_end not in waited:
                waited[win_end] = (win_end - t) / 3600.0      # the user waits until the reset
            win_used += ok; week_used += ok / 100; win_human += ok
        recent.append(d)
    weeks = weeks[1:]                                 # the first week is partial
    return {"waste": sum(weeks) / len(weeks), "bot": n["bot"], "waits": sorted(waited.values()), **n}


def line(agent, label, r, base):
    w = r["waits"]
    return ("  %-6s %-40s waste %4.0f%% of today's · bot %4.2f u/wk · collisions %d, waited %4.1f h"
            % (agent, label, r["waste"] / base * 100, r["bot"] / 6, len(w), sum(w)))


def main():
    global SLOTS
    SLOTS = {a: load_slots(a) for a in CAPACITY}
    awake = awake_slots()
    base = {a: replay(a, bot=False)["waste"] for a in CAPACITY}
    print("waste with no bot (units per week):", {a: round(v, 2) for a, v in base.items()})

    print("\n1. How often does the forecast change the amount?")
    for a in CAPACITY:
        r = replay(a)
        print("  %-6s %d ticks · the week forces spending now on %.1f%% · the forecast lowers that amount on %.2f%%"
              % (a, r["ticks"], r["due"] / r["ticks"] * 100, r["forecast_binds"] / r["ticks"] * 100))

    print("\n2. Machine availability x start rule (floor 25, guard 15, burn 1.0)")
    for a in CAPACITY:
        for sleep in ("always awake", "real sleep"):
            for start in ("late", "idle", "asap"):
                r = replay(a, awake=awake if sleep == "real sleep" else None, start=start,
                           idle_slots=12)
                print(line(a, "%s, start %s" % (sleep, start), r, base[a]))

    print("\n3. Forecast on/off, across assumed burn rates (late start, floor 25)")
    for a in CAPACITY:
        for burn in (0.3, 0.5, 1.0):
            for sleep in ("always awake", "real sleep"):
                for fc in (True, False):
                    r = replay(a, burn=burn, forecast=fc, awake=awake if sleep == "real sleep" else None)
                    print(line(a, "burn %.1f, %s, forecast %s" % (burn, sleep, "on" if fc else "off"), r, base[a]))

    print("\n4. Floor (what the budget plans per window) vs guard (where the executor stops)")
    for a in CAPACITY:
        for floor, guard in ((10, 15), (10, 10), (25, 15), (25, 25), (40, 40)):
            print(line(a, "floor %d, guard %d" % (floor, guard), replay(a, floor=floor, guard=guard), base[a]))

    print("\n5. Window units per week = sum(slot_window_used_pct) / the week's PEAK week_used_pct")
    print("   (the budget assumes 8.85 / 6.2; not sum(slot_week_used_pct), which double-counts")
    print("   every 1-point disagreement between the status line and the poller)")
    for a in CAPACITY:
        with open(f"{AQM}/data/{a}/meter.csv") as handle:
            meter = sorted(((int(r["observed_ts"]), int(r["week_end_ts"]), float(r["week_used_pct"]))
                            for r in csv.DictReader(handle) if r["week_end_ts"] and r["week_used_pct"]))
        peak, window_pct, i, week = collections.defaultdict(float), collections.defaultdict(float), 0, None
        for s in SLOTS[a]:
            while i < len(meter) and meter[i][0] < s["t"] + SLOT:
                week = meter[i][1]; peak[week] = max(peak[week], meter[i][2]); i += 1
            if week:
                window_pct[week] += s["window_pct"]
        print("  %-6s %s" % (a, "  ".join("%s %.2f" % (dt.datetime.fromtimestamp(k).strftime("%m-%d"), window_pct[k] / peak[k])
                                          for k in sorted(peak) if peak[k] >= 15)))

    print("\n6. Is the human active right now? Human use over the next N minutes, by state")
    print("   (active = moved the meter or sent a prompt in the last 30 minutes)")
    for a in CAPACITY:
        v = [s["human"] for s in SLOTS[a]]
        prompt = [s["active"] for s in SLOTS[a]]
        active = [sum(v[max(0, i - 5):i + 1]) > 0 or sum(prompt[max(0, i - 5):i + 1]) > 0
                  for i in range(len(v))]
        print("  %-6s active on %.0f%% of slots" % (a, sum(active) / len(active) * 100))
        for minutes in (60, 90, 180, 300):
            k = minutes // 5
            ahead = [sum(v[i + 1:i + 1 + k]) for i in range(len(v) - k)]
            on = [x for x, act in zip(ahead, active) if act]
            off = [x for x, act in zip(ahead, active) if not act]
            print("    next %3d min: uses >25%% of a window in %3.0f%% of cases if active, %3.0f%% if not"
                  % (minutes, sum(x > 25 for x in on) / len(on) * 100, sum(x > 25 for x in off) / len(off) * 100))

    print("\n7. On an always-on machine: forecast vs the rule, across burn rates")
    print("   rule = late start + 25% reserve + the window that outlives the weekly reset")
    print("          only filled in proportion to its part before the reset")
    for a in CAPACITY:
        for burn in (0.25, 0.5, 1.0):
            for label, kw in (("forecast (today)", dict(forecast=True)),
                              ("rule, no forecast", dict(forecast=False, prorate_straddle=True)),
                              ("rule + not while active", dict(forecast=False, prorate_straddle=True,
                                                               start="late_and_idle", idle_slots=6))):
                print(line(a, "burn %.2f u/h, %s" % (burn, label), replay(a, burn=burn, **kw), base[a]))


if __name__ == "__main__":
    main()
