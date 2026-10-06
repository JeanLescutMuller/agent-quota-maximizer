# Vocabulary

**Every name used by this project, what it measures, and in which unit.** Agreed with
the user on 2026-10-05, replacing the first generation of names (`organic`, `extra`,
`bucket`, `chunk`, `room`, `envelope`, `tripwire`, `p95_pct`, `amount_due`), which were
ambiguous about *when* and *percent of what*.

Two rules keep it that way:

- **One concept, one word, project-wide.** Renaming a concept is a breaking change, and
  the CSV header tests exist to make it one.
- **No name without a unit and a span.** If a new field cannot be named with the grammar
  below, the grammar is wrong and gets extended here first.

## 1. The grammar

```
  <span>_<meter>_<who>_<unit>

  span    slot_       during one 5-minute slot
          (none)      cumulative, as of now
  meter   window_     of the 5-hour meter        1 window = 1 unit ~ $32 Claude / $20.5 Codex
          week_       of the 7-day meter         1 week ~ 8.85 units Claude / 6.2 Codex
  who     used_       total: human + bot
          human_      the user's own interactive work
          bot_        this project's background work
          (none)      not attributable to either
  unit    pct         0-100, of the meter named by <meter>
          units       1 unit = one whole 5-hour meter
          usd         dollars
          burn_rate   percent of the 5-hour meter per minute
          _minutes _hours _seconds
          _ts         epoch seconds -- what code computes on
          _dt         the same instant as local text with offset -- what a human reads
  bool    is_ / was_ / has_ prefix
  pred.   predicted_   a forecast, not a measurement -- the only prefix that says
                       the number describes the future rather than the past
```

`_minutes`, not `_min`, so it never reads as "minimum". `_ts`/`_dt` rather than `_at`,
whose meaning used to drift between the two.

### The three spans, in one picture

```
  week      |<-------------------------- 7 days --------------------------->|
  window    |<-- 5h -->| |<-- 5h -->| |<-- 5h -->|  ...  gaps where none runs
  slot      |||||||||||||||||||||||||||||||||||||||||||  5 minutes each, always present
```

`used_pct` on its own is always *percent of the 5-hour meter*. The weekly meter is the
exception and is always marked `week_`.

### The two burn rates are not the same unit

| Name | Unit | Why |
|---|---|---|
| `human_avg_burn_rate` | % of the 5-hour meter **per minute** | how fast the user is working |
| `BOT_BURN_UNITS_PER_HOUR` | **units per hour** | our own capacity; spelled out so it is never confused with the above |

## 2. `data/<agent>/meter.csv` — one row per surviving reading

| Was | Name | What it is | Example |
|---|---|---|---|
| `datetime` | `observed_dt` | when the reading was taken, for human eyes | `2026-10-05 14:20:03 +0200` |
| `observed_at` | `observed_ts` | the same instant, epoch seconds | `1791289203` |
| `five_pct` | `window_used_pct` | % of the 5-hour meter used so far in the open window. Empty if none open | `31` |
| `seven_pct` | `week_used_pct` | % of the 7-day meter used so far this week | `19` |
| `five_reset_at` | `window_end_dt` | when the 5-hour meter zeroes, human | `2026-10-05 17:30:00 +0200` |
| `five_reset` | `window_end_ts` | the same, epoch. Empty = no window open | `1791300600` |
| `seven_reset_at` | `week_end_dt` | when the weekly meter zeroes, human | `2026-10-09 06:00:00 +0200` |
| `seven_reset` | `week_end_ts` | the same, epoch | `1791612000` |
| `source` | `source` | which upstream file the reading came from | `statusline` |
| `by_session` | `agent_session_id` | the Claude/Codex session that produced it | `bc36b32e-...` |

## 3. `data/<agent>/slots.csv` — one row per 5 minutes, always

Was `buckets.csv`.

| Was | Name | What it is | Example |
|---|---|---|---|
| `datetime` | `slot_start_dt` | the slot's start, human | `2026-10-05 14:20:00 +0200` |
| `bucket_id` | `slot_id` | the slot's start and end, epoch | `1791289200_1791289500` |
| `window_ends_at` | `window_end_dt` | when the window this slot sits in resets | `2026-10-05 17:30:00 +0200` |
| `fivehours_window_id` | `window_id` | which 5-hour window this slot belongs to | `1791282600_1791300600` |
| `five_pct` | `window_used_pct` | % of the 5-hour meter used **so far this window**, at the slot's end | `31` |
| `seven_pct` | `week_used_pct` | % of the weekly meter used **so far this week** | `19` |
| `five_delta` | `slot_window_used_pct` | how much the 5-hour meter moved **during this slot** | `2` |
| `seven_delta` | `slot_week_used_pct` | how much the weekly meter moved during this slot | `0.23` |
| `extra_pct` | `slot_window_bot_pct` | of the above movement, the part **we** burned | `0` |
| `organic_pct` | `slot_window_human_pct` | of it, the part **the user** burned. Empty when the two cannot be told apart | `2` |
| `organic_pct_lo` | `slot_window_human_pct_lo` | lowest it could be, when empty | `0` |
| `organic_pct_hi` | `slot_window_human_pct_hi` | highest it could be | `2` |
| `attribution` | `attribution` | how the split was made: `exact` / `apportioned` / `censored` | `exact` |
| `our_usd` | `slot_bot_usd` | dollar value of `slot_window_bot_pct` | `0.0` |
| `organic_usd` | `slot_human_usd` | dollar value of `slot_window_human_pct` | `0.64` |
| `t_in_window` | `window_age_minutes` | minutes since this window opened | `108` |
| `window_capped` | `is_window_maxed` | the 5-hour meter reached 100%, so real usage above it is invisible | `false` |
| `no_window` | `is_window_open` | was a 5-hour window running at all. **Inverted** from `no_window` | `true` |
| `n_readings` | `slot_n_readings` | meter readings that landed in this slot and survived the stale-reading filter | `2` |
| `observed` | `was_machine_awake` | any reading arrived at all, so the Mac was on | `true` |
| `workers` | `n_bot_workers` | how many of our background jobs were running | `0` |
| `user_seen` | `was_human_active` | the user sent a prompt during this slot | `true` |

### `slot_n_readings` is about our sampling, not about anyone's usage

The 5-hour meter is one account-wide gauge; it cannot say who moved it. `slot_n_readings`
counts how many times we *read* that gauge — which is why it carries no `human_`/`bot_`
part. Who moved it is inferred separately, from `was_human_active` and `n_bot_workers`.

Its only job is to separate three situations that all report a burn rate of zero:

| `slot_n_readings` | `was_machine_awake` | What actually happened |
|---|---|---|
| 0 | `false` | the Mac was asleep: absence of data, not absence of the user |
| 0 | `true` | the Mac was on and the meter never moved: idle, or burning under 1% |
| >= 1 | `true` | the meter moved, and we measured how fast |

## 4. `artifacts/predictions/<date>/<time>.json`

> **Parked on 2026-10-06** with stage 2 (`DESIGN_v2.md` §8): no decision reads this file any more. Its names are kept for the day it comes back.

**The question this answers: how much of the 5-hour meter will the *user* still want
before it resets?**

The file has two halves, and the split is the point of this section:

```
  prediction stage                                budget stage
  +-------------------------+                     +-------------------+
  |  engine                 |                     |                   |
  |  recent-rate-v1 today,  |--- contract -------)|  reads ONLY       |
  |  ML or anything later   |    2 fields, stable |  the contract     |
  |                         |                     +-------------------+
  |  its own workings ------+--- internals -+
  +-------------------------+    free-form  |
                                            +---)  notebook . audit . never budget
```

**The contract is engine-independent.** Everything an engine computes on the way to the
answer is its own business and goes in `internals`, because the next engine will not have
the same intermediates. `based_on: "last_30_min"` is vocabulary only `recent-rate-v1` can
speak; a model would have nothing to put there.

```json
{
  "computed_ts": 1791289500,
  "method": "recent-rate-v1",
  "config_hash": "a1b2c3d4",
  "agents": {
    "claude": {
      "window_end_ts": 1791300600,
      "predicted_p95_human_usage_pct": 37,

      "internals": {
        "based_on": "last_30_min",
        "last_human_slot_ts": 1791288900,
        "last_30_min":    { "human_avg_burn_rate": 0.40, "n_readings": 6 },
        "current_window": { "is_open": true, "start_ts": 1791282600,
                            "age_minutes": 108, "remaining_minutes": 62,
                            "used_pct": 31,
                            "human_avg_burn_rate": 0.29, "n_readings": 22 }
      }
    }
  }
}
```

### 4.1 The contract

| Field | What it is | Why budget needs it |
|---|---|---|
| `predicted_p95_human_usage_pct` | worst case, % of the 5-hour meter the user will still burn. 95th percentile, not the average | it is the answer |
| `window_end_ts` | which window the forecast is about. Empty = none open, so it covers a hypothetical one starting now | lets budget refuse a forecast for a window that has since reset, which the freshness check alone does not catch |

Plus `computed_ts`, `method` and `config_hash` at the top level.

**The contract is two fields, and that is deliberate.** The stage answers one question --
cumulative human usage to the end of the current window -- so there is nothing to say
about a horizon: it is always the window's end. How accurately a given engine answers
over a long remaining window is a §2 problem, reported in `internals` and never a reason
to narrow the promise.

### 4.2 `internals` — engine-private, never load-bearing

Free-form, and its shape is whatever `method` happens to produce. Three rules keep it
from becoming a second interface:

| Rule | Enforced by |
|---|---|
| `budget` never reads `internals` | a test that blanks it and asserts the decision is byte-identical |
| tests never assert on its contents, only that it exists | review |
| the notebook may read it, but must survive a missing key | it degrades to "not available for this engine" |

What `recent-rate-v1` puts there:

| Field | What it is |
|---|---|
| `based_on` | which of its rules produced the answer, below |
| `last_human_slot_ts` | start of the last slot the user burned anything in. **Empty means "nothing within the look-back"**, not "never": we cannot see further back than we looked |
| `last_30_min.human_avg_burn_rate` | the user's pace over `BURN_LOOKBACK_MINUTES`. Crosses a window boundary on purpose -- pace is a property of the person, not of a meter, and both windows' percentages are percentages of a same-sized meter |
| `last_30_min.n_readings` | readings behind that figure |
| `current_window.*` | the open window's own facts, all empty when `is_open` is false. Re-derivable from `slots.csv`; kept so a past decision explains itself without a rerun |
| `current_window.human_avg_burn_rate` | the same pace averaged over the whole window -- the floor that stops an ordinary pause from reading as an absence |

```
   last_30_min.human_avg_burn_rate       0.40 --+
                                                +--) max()  --)  rate 0.40
   current_window.human_avg_burn_rate    0.29 --+                     |
                                                                      |
   both about zero but was_human_active:  rate = the floor 0.15 ------+
                                                                      |
                                                                      v
        rate  x  minutes to the window's end  x  SAFETY_MULTIPLIER
        0.40  x  62                           x  1.5     =  37

                                     = predicted_p95_human_usage_pct
```

| `based_on` | Meaning | `predicted_p95_human_usage_pct` |
|---|---|---|
| `last_30_min` | the user's recent pace was the higher of the two | rate x minutes x multiplier |
| `current_window` | the window's own average was higher: a pause mid-session, not an absence | same, with that rate |
| `human_active_floor` | both rates about zero, but the user sent a prompt this slot and the meter has not ticked yet | the floor rate |
| `nothing` | no sign of the user anywhere in the look-back | `0` |
| `unreadable` | the data could not be read | `100` -- fail safe, spend nothing |

## 5. `artifacts/budgets/<date>/<time>.json`

**The question this answers: how many units may the bot spend right now, and by when?**

```json
{
  "computed_ts": 1791289500,
  "config_hash": "a1b2c3d4",
  "agents": {
    "claude": {
      "verdict": "spend_now",
      "extra_quota_to_spend_units": 0.67,
      "spend_by_ts": 1791300300,
      "already_lost_units": 2.54,

      "week": {
        "used_pct": 19,
        "end_ts": 1791612000,
        "left_units": 4.4
      },

      "current_window": {
        "is_open": true,
        "end_ts": 1791300600,
        "used_pct": 31
      },

      "remaining_windows": [
        {
          "start_ts": 1791289500,
          "end_ts": 1791300600,
          "is_open_now": true,
          "human_reserve_pct": 42,
          "quota_limit_units": 0.27,
          "time_limit_units": 0.68,
          "limited_by": "quota",
          "max_spend_units": 0.27,
          "planned_units": 0.27
        }
      ]
    }
  }
}
```

| Was | Field | What it is |
|---|---|---|
| `reason` | `verdict` | why this amount |
| `amount_due` | `extra_quota_to_spend_units` | **the answer: spend this many units now.** `0.9` is about 90% of a 5-hour meter, about $29 |
| `deadline` | `spend_by_ts` | spend it before this instant or lose it |
| `unreachable` | `already_lost_units` | units of this week that **no remaining window can absorb**. Gone, and the number this project exists to shrink |
| `remaining_week` | `week.left_units` | units still unspent this week, the user's and ours together |
| `window.used` | `current_window.used_pct` | the level, in percent like everywhere else — no more 0-1 fraction in the artifact |
| `chunks` | `remaining_windows` | every 5-hour window between now and the week's end. **Not `future_windows`**: entry `[0]` is usually the window already running |
| `chunk.start` / `.end` | `start_ts` / `end_ts` | that window's bounds |
| `chunk.window` | `is_open_now` | this is the real window running now, rather than a projected one stage 4 must still open. Two invariants: it equals `current_window.is_open` on entry `[0]`, and at most one entry is ever true |
| `chunk.margin` | `human_reserve_pct` | % of that window held back for the user: `max(MIN_HUMAN_RESERVE_PCT, predicted_p95_human_usage_pct)` |
| `chunk.quota_room` | `max_spend_units_by_quota` | what the meter will still give: `(100 − human_reserve_pct − used_pct) / 100` |
| `chunk.time_room` | `max_spend_units_by_time` | what can physically be burned: `hours × BOT_BURN_UNITS_PER_HOUR`. The suffix is `units` because that is the unit; `by_time` says where the bound comes from |
| `chunk.bound_by` | `limited_by` | which of the two binds: `quota` or `time` |
| `chunk.ceiling` | `max_spend_units` | `min()` of the two above — the real cap. They are **never multiplied** (`03_budgeting/DESIGN.md` §2) |
| `chunk.planned` | `planned_units` | what the backwards fill allocated to it |

`verdict`: `spend_now` · `wait` · `blocked_stale_reading` · `no_meter_reading` ·
`no_usable_week_reading`.

### Why both branches are published, not just the answer

```
  human_reserve_pct --+
  used_pct -----------+--) max_spend_units_by_quota  0.27 --+
                      |                                     +--) max_spend_units 0.27
  end_ts - start_ts --+--) max_spend_units_by_time   0.68 --+     limited_by: quota
  BOT_BURN_UNITS_PER_HOUR
```

Unlike the prediction's `internals`, these are **not** engine internals: they are the
decision itself, and all four are needed downstream or for audit.

| | |
|---|---|
| The notebook must not reimplement the formula | it reads these fields instead of redoing the arithmetic. A second copy would eventually disagree, and the copy is the one a human reads (`../lab/README.md`) |
| `limited_by` is meaningless without them | it answers *"was the bot short of quota, or short of time?"* |
| Audit | *"why 0.27 units at 03:12 last Tuesday?"* must be answerable from one file, with no rerun |
| It is the measurement that validates `BOT_BURN_UNITS_PER_HOUR`, currently a guess | if `limited_by` says `time` most of the time, our own burn rate is the bottleneck rather than the user's quota -- the number P4 has to measure |

The hours behind `max_spend_units_by_time` are deliberately **not** a field:
`end_ts - start_ts` gives them with no policy in between.

## 6. Parameters

`07_pipeline/DESIGN.md` §11 owns the values and their justification; this table owns the
names.

| Was | Name | What it is | Value |
|---|---|---|---|
| `MIN_MARGIN` | `MIN_HUMAN_RESERVE_PCT` | never fill a window past this; held for the user even when they look idle. Measured, not chosen (`02_prediction/DESIGN.md` §8). The only reserve since 2026-10-06 | `25` |
| — | `HUMAN_IDLE_MINUTES` | **added 2026-10-06**: nothing starts while the user moved the meter or sent a prompt within this many minutes (`05_planning/DESIGN.md` §3) | `30` |
| `WINDOW_GAP` | `WINDOW_GAP_SECONDS` | assumed gap between consecutive 5-hour windows | `300` |
| `DEADLINE_MARGIN` | `DEADLINE_MARGIN_SECONDS` | stop this long before a window's reset | `300` |
| `BURST_RATE` | `BOT_BURN_UNITS_PER_HOUR` | how fast our background work can burn quota. **Assumed, not yet measured** | `1.0` |
| `RATE_WINDOW` | `BURN_LOOKBACK_MINUTES` | how far back `last_30_min` looks. **Parked with stage 2** | `30` |
| `PERSISTENCE_HOURS`, `PERSISTENCE_P95` | `SAFETY_MULTIPLIER` | turns an average rate into a 95th percentile. **Parked with stage 2** | `1.5` |
| `TRIPWIRE_RATE` | `ACTIVE_HUMAN_BURN_RATE` | assumed pace when the user is clearly working but the meter has not ticked. **Parked with stage 2** | `0.15` |
| `READING_MAX_AGE` | `READING_MAX_AGE_SECONDS` | a meter reading older than this refuses the spend | `600` |
| `MAX_INPUT_AGE` | `ARTIFACT_MAX_AGE_SECONDS` | a forecast older than this is not trusted. **Parked with stage 2** | `600` |
| `TICK_INTERVAL` | `TICK_INTERVAL_SECONDS` | the LaunchAgent period | `300` |
| `WEEK_UNITS`, `WEEK_CAPACITY_UNITS` | `week_capacity_units` | how many 5-hour meters fit in a week, per agent. **Measured since 2026-10-06**, not configured, hence lower case (`03_budgeting/DESIGN.md` §2) | claude 7.6 on 2026-10-05 |
| `ARTIFACT_RETENTION_DAYS`, `LOG_MAX_MB` | unchanged | housekeeping | `14`, `20` |

## 7. Functions

| Was | Name | Why |
|---|---|---|
| `envelope()` | `drop_stale_readings()` | it discards any reading lower than one already seen in the window |
| `window_reset()` | `window_end()` | parses a window's end out of a raw row |
| `build_buckets()` | `build_slots()` | "slot" everywhere |
| `recent_buckets()` | `recent_slots()` | |
| `last_bucket_end()` | `last_slot_end()` | |
| `buckets_path()` | `slots_path()` | |
| `spend_rate()` | `burn_rate()` | returns a burn rate, in % of the 5-hour meter per minute |
| `ceiling()` / `ceiling_parts()` | `max_spend()` / `max_spend_parts()` | matches `max_spend_units` |
| `chain()` | `remaining_windows()` | matches `remaining_windows` |
| `back_fill()` | `fill_from_the_end()` | says what it does: the last window first |
| `our_sessions()` | `bot_sessions()` | human/bot everywhere |

## 8. Words removed from the vocabulary

| Retired | Say instead |
|---|---|
| **envelope** | *the stale-reading filter* |
| **tripwire** | *the active-human floor* |
| **chunk** | *future window* |
| **room** | *limit* |
| **organic** | *human* |
| **extra** (as a noun for our usage) | *bot* |
| **bucket** | *slot* |
| **touch** / **touched** | *`was_human_active`* |
| **p95** on its own | *`predicted_p95_human_usage_pct`* — always say what it is a p95 of |
