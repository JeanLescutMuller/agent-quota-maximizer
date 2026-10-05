#!/bin/bash
# Stage 3: the window chain, the reset split, ceilings, the back-fill and
# `already_lost_units`. Every expected number below is hand-computed in its comment --
# that is the acceptance criterion for P1, so the arithmetic is written out rather
# than taken from the implementation.
# ../design/03_budgeting/DESIGN.md 2, 3, 5
set -uo pipefail
source "$(dirname "$0")/harness.sh"

# A minute-aligned `now`, so the reset rounding of ingestion is a no-op.
NOW=1790000400
HOUR=3600
DAY=86400
# A Claude week is 8.85 five-hour windows (design/CLAUDE_AND_CODEX.md 1).

# due <field>  -- one field of the claude budget, as of NOW
due() { api "aqm.budget(at=$NOW)[\"agents\"][\"claude\"][\"$1\"]"; }
# window_field <field> -- that field of every claude window, space separated
# week_field <field> -- one field of the claude week block
week_field() { api "aqm.budget(at=$NOW)[\"agents\"][\"claude\"][\"week\"][\"$1\"]"; }
window_field() {
    api "' '.join('%.4g' % c['$1'] for c in
         aqm.budget(at=$NOW)['agents']['claude']['remaining_windows'])"
}

section "the window chain"

setup
# A window open for 2 more hours, the week resetting in 24 h.
week "$NOW" 20 $((NOW + 2 * HOUR)) 60 $((NOW + DAY))
ingest --now $((NOW + 300))

# Chunks: the open window [now, +2h], then 5-hour windows separated by 5 min,
# until the one holding the reset is cut at it:
#   1 [+0h00, +2h00]   2 [+2h05, +7h05]   3 [+7h10, +12h10]
#   4 [+12h15, +17h15] 5 [+17h20, +22h20] 6 [+22h25, +24h00]  <- cut
is "the chain reaches the reset and stops" "$(api 'len(aqm.budget(at='$NOW')["agents"]["claude"]["remaining_windows"])')" "6"
is "the first window is the open window" \
   "$(api "aqm.budget(at=$NOW)['agents']['claude']['remaining_windows'][0]['is_open_now']")" "true"
is "and it ends when the window does" \
   "$(api "aqm.budget(at=$NOW)['agents']['claude']['remaining_windows'][0]['end_ts']")" "$((NOW + 2 * HOUR))"
is "the next window starts one WINDOW_GAP_SECONDS later" \
   "$(api "aqm.budget(at=$NOW)['agents']['claude']['remaining_windows'][1]['start_ts']")" \
   "$((NOW + 2 * HOUR + 300))"
is "the last window is cut at the weekly reset, not past it" \
   "$(api "aqm.budget(at=$NOW)['agents']['claude']['remaining_windows'][-1]['end_ts']")" "$((NOW + DAY))"

section "ceilings"

# ceiling = min(max_spend_units_by_quota, max_spend_units_by_time), the two never multiplied:
#   max_spend_units_by_quota = 1 - MIN_HUMAN_RESERVE_PCT - (used, open window only)
#   max_spend_units_by_time  = duration_hours x BOT_BURN_UNITS_PER_HOUR (1.0 unit/h)
#   1  quota (100 - 25 - 20)/100 = 0.55   time 2.00  -> 0.55
#   2-5      (100 - 25)/100   = 0.75        time 5.00  -> 0.75
#   6        0.75                           time 1.583 -> 0.75
is "every window is ceilinged, the open one net of what it has used" \
   "$(window_field max_spend_units)" "0.55 0.75 0.75 0.75 0.75 0.75"

section "the back-fill"

# remaining = (100 - 60)% x 8.85 = 3.54 units, filled from the last window back:
#   6: min(0.75, 3.54) = 0.75 -> 2.79
#   5: 0.75 -> 2.04    4: 0.75 -> 1.29    3: 0.75 -> 0.54
#   2: min(0.75, 0.54) = 0.54 -> 0
#   1: nothing left
is "the weekly remainder is read off the 7-day meter" "$(week_field left_units)" "3.54"
is "the plan is piled into the windows just before the reset" \
   "$(window_field planned_units)" "0 0.54 0.75 0.75 0.75 0.75"
is "so nothing is due now, which is the normal case" "$(due extra_quota_to_spend_units)" "0.0"
is "and no deadline is published when nothing is due" "$(due spend_by_ts)" "null"
is "all of it is reachable, so none is written off" "$(due already_lost_units)" "0.0"
teardown

section "the back-fill conserves quota"

setup
# A full week remaining with only 10 h of windows to put it in, so the back-fill
# both fills and overflows: every unit must end up either planned or unreachable.
week "$NOW" 0 none 0 $((NOW + 10 * HOUR))
ingest --now $((NOW + 300))
is "planned + unreachable = remaining, exactly" \
   "$(api "'%.4f' % (sum(c['planned_units'] for c in
        aqm.budget(at=$NOW)['agents']['claude']['remaining_windows'])
        + aqm.budget(at=$NOW)['agents']['claude']['already_lost_units'])")" \
   "$(api "'%.4f' % aqm.budget(at=$NOW)['agents']['claude']['week']['left_units']")"
is "and no window is planned above its ceiling" \
   "$(api "all(c['planned_units'] <= c['max_spend_units'] + 1e-9 for c in
        aqm.budget(at=$NOW)['agents']['claude']['remaining_windows'])")" "true"
teardown

section "--at selects the reading at or before the moment"

setup
# Three readings. A backtest asks what was known *then*, so a later reading must
# never leak into an earlier decision -- that is the whole validity of a backtest.
week "$NOW" 10 $((NOW + 2 * HOUR)) 40 $((NOW + DAY))
week $((NOW + HOUR)) 30 $((NOW + 2 * HOUR)) 60 $((NOW + DAY))
ingest --now $((NOW + 2 * HOUR))
is "a moment before any reading has nothing to decide on" \
   "$(api "aqm.meter_now('claude', $((NOW - 1)))")" "null"
is "a moment between two readings sees only the earlier one" \
   "$(api "aqm.meter_now('claude', $((NOW + HOUR - 1)))['week_used_pct']")" "40.0"
is "and the later moment sees the later one" \
   "$(api "aqm.meter_now('claude', $((NOW + HOUR)))['week_used_pct']")" "60.0"
is "age is measured against the moment asked about, not the wall clock" \
   "$(api "aqm.meter_now('claude', $((NOW + HOUR - 1)))['age']")" "3599"
teardown

section "when the reset is close, the current window is the plan"

setup
# The week resets in 1 h, so there is exactly one window and it is the cut one.
week "$NOW" 0 $((NOW + 2 * HOUR)) 50 $((NOW + HOUR))
ingest --now $((NOW + 300))
# quota (100 - 25 - 0)/100 = 0.75, time 1 h x 1.0 = 1.0  ->  0.75
# remaining = 50% x 8.85 = 4.425, of which only 0.75 fits anywhere.
is "one window is left" "$(api 'len(aqm.budget(at='$NOW')["agents"]["claude"]["remaining_windows"])')" "1"
is "and it is due now" "$(due extra_quota_to_spend_units)" "0.75"
is "with a deadline one DEADLINE_MARGIN_SECONDS before its end" \
   "$(due spend_by_ts)" "$((NOW + HOUR - 300))"
is "the rest cannot be spent by any window and is reported as such" \
   "$(due already_lost_units)" "3.675"
is "which is said plainly rather than left to be inferred" \
   "$(due verdict)" "spend_now"
teardown

section "no window open"

setup
# No window: the first window is a hypothetical one starting now and running 5 h,
# which stage 4 is what makes true. Full ceiling, nothing used.
week "$NOW" 0 none 50 $((NOW + 4 * HOUR))
ingest --now $((NOW + 300))
is "a window is assumed where one is needed" \
   "$(api "aqm.budget(at=$NOW)['agents']['claude']['remaining_windows'][0]['is_open_now']")" "false"
is "and it is cut at the reset like any other" \
   "$(api "aqm.budget(at=$NOW)['agents']['claude']['remaining_windows'][0]['end_ts']")" "$((NOW + 4 * HOUR))"
# quota (100 - 25)/100 = 0.75 (no window means nothing used), time 4 h x 1.0 = 4.0
is "nothing is subtracted for a window that does not exist" \
   "$(window_field max_spend_units)" "0.75"
teardown

section "a window too short to burn its quota"

setup
# The limit that is not the meter. 30 minutes before the reset, the window would
# still give 0.75, but at BOT_BURN_UNITS_PER_HOUR = 1.0 unit/h only 0.5 can be burned in the time
# left -- so the smaller of the two wins, and it is the clock, not the quota.
week "$NOW" 0 none 0 $((NOW + 1800))
ingest --now $((NOW + 300))
is "the burn rate caps the ceiling when the quota does not" \
   "$(window_field max_spend_units)" "0.5"
is "and the whole rest of the week is unreachable, because time ran out" \
   "$(due already_lost_units)" "8.35"
teardown

section "the structural bound on the chain"

setup
# A 7-day period holds 5-hour windows plus a 5-minute gap, so the chain can never
# be longer than ceil(604800 / 18300) = 34 windows. That bound is what makes this
# stage arithmetic rather than a search.
week "$NOW" 0 none 0 $((NOW + 7 * DAY))
ingest --now $((NOW + 300))
is "a whole period is at most 34 windows" \
   "$(api 'len(aqm.budget(at='$NOW')["agents"]["claude"]["remaining_windows"])')" "34"
teardown

section "nothing to rescue, and nothing to read"

setup
week "$NOW" 0 none 100 $((NOW + DAY))
ingest --now $((NOW + 300))
is "a spent week plans nothing" "$(due extra_quota_to_spend_units)" "0.0"
is "and writes nothing off either" "$(due already_lost_units)" "0.0"
teardown

setup
is "with no reading at all, nothing is due" "$(due extra_quota_to_spend_units)" "0.0"
is "and the reason says why, rather than looking like a decision" \
   "$(due verdict)" "no_meter_reading"
is "codex is budgeted independently and is just as quiet" \
   "$(api "aqm.budget(at=$NOW)['agents']['codex']['extra_quota_to_spend_units']")" "0.0"
teardown

setup
# A weekly reset already in the past means the meter is stale, not that the week
# is empty: computing a surplus from it would spend quota the user still has.
week "$NOW" 0 none 50 $((NOW - DAY))
ingest --now $((NOW + 300))
is "a weekly reset in the past is refused rather than used" \
   "$(due verdict)" "no_usable_week_reading"
teardown

section "the prediction, used in exactly one place"

setup
week "$NOW" 0 $((NOW + 2 * HOUR)) 50 $((NOW + HOUR))
ingest --now $((NOW + 300))
is "with no prediction the reserve is MIN_HUMAN_RESERVE_PCT" "$(due extra_quota_to_spend_units)" "0.75"

cat > "$TMP/p.json" <<EOF
{"computed_ts": $NOW, "method": "recent-rate-v1",
 "agents": {"claude": {"window_end_ts": $((NOW + 2 * HOUR)),
                       "predicted_p95_human_usage_pct": 50.0}}}
EOF
# reserve = max(10, 50) = 50 pct  ->  quota (100 - 50 - 0)/100 = 0.5, under time 1.0
is "a forecast of human demand tightens the current ceiling and nothing else" \
   "$(api "aqm.budget(at=$NOW, prediction='$TMP/p.json')['agents']['claude']['extra_quota_to_spend_units']")" \
   "0.5"
is "and the artifact records which method decided" \
   "$(api "aqm.budget(at=$NOW, prediction='$TMP/p.json')['method']")" "recent-rate-v1"

is "an unreadable prediction fails safe to a full window, so nothing runs" \
   "$(api "aqm.budget(at=$NOW, prediction='$TMP/missing.json')['agents']['claude']['extra_quota_to_spend_units']")" \
   "0.0"
is "and says it fell back rather than reporting a real method" \
   "$(api "aqm.budget(at=$NOW, prediction='$TMP/missing.json')['method']")" "fail-safe"
teardown

section "artifacts"

setup
week "$NOW" 0 $((NOW + 2 * HOUR)) 50 $((NOW + HOUR))
ingest --now $((NOW + 300))

aqm budget --at "$NOW" >/dev/null
is "--at writes nothing into the live tree" \
   "$([ -d "$AQM_HOME/artifacts" ] && echo yes || echo no)" "no"

aqm budget --at "$NOW" --out "$TMP/one.json" >/dev/null
is "--out writes where it is told" "$([ -f "$TMP/one.json" ] && echo yes)" "yes"
is "and still moves no pointer" \
   "$([ -e "$AQM_HOME/state/latest/budget.json" ] && echo yes || echo no)" "no"

aqm budget >/dev/null
is "a live run writes a dated, timed artifact" \
   "$(find "$AQM_HOME/artifacts/budgets" -name '*.json' | wc -l | tr -d ' ')" "1"
is "and the latest pointer resolves to it" \
   "$(dirname "$(dirname "$($PY -c "import os,sys; print(os.path.realpath(sys.argv[1]))" \
        "$AQM_HOME/state/latest/budget.json")")" | xargs basename)" "budgets"
is "no temporary file is left behind" \
   "$(find "$AQM_HOME" -name '*.tmp' | wc -l | tr -d ' ')" "0"
teardown

section "the table a human reads"

setup
week "$NOW" 0 $((NOW + 2 * HOUR)) 50 $((NOW + HOUR))
ingest --now $((NOW + 300))
table="$(aqm budget --at "$NOW")"
is "it leads with the week left" \
   "$(printf '%s' "$table" | grep -c '4.42 units left')" "1"
is "it names the split window" \
   "$(printf '%s' "$table" | grep -c "split by the week's end")" "1"
is "and ends on the decision" \
   "$(printf '%s' "$table" | grep -c 'extra_quota_to_spend_units 0.75')" "1"
teardown

finish
