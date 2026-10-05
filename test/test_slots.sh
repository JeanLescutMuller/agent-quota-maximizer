#!/bin/bash
# Stage 1, the 5-minute table: the grid, deltas across ticks, the columns a
# predictor cannot do without, and the three attribution cases.
# ../design/01_ingestion/DESIGN.md 1.2, 6.2, 6.3
set -uo pipefail
source "$(dirname "$0")/harness.sh"

section "the schema a human reads"

setup
push "$BASE" 5
ingest --now $((BASE + 300))
# The header is an interface: these files are read by hand and by the notebook, and
# `touched` was renamed to `was_human_active` on 2026-10-04 because "touched" never said by
# whom. Pinning the line means the next rename has to be deliberate -- and that an
# older data/ is noticed rather than silently misread, since a column rename makes
# every existing row unreadable until `--backfill` rebuilds it.
is "the slot columns are exactly these, in this order" "$(header claude slots)" \
   "slot_start_dt,slot_id,window_end_dt,window_id,window_used_pct,week_used_pct,slot_window_used_pct,slot_week_used_pct,slot_window_bot_pct,slot_window_human_pct,slot_window_human_pct_lo,slot_window_human_pct_hi,attribution,slot_bot_usd,slot_human_usd,window_age_minutes,is_window_maxed,is_window_open,slot_n_readings,was_machine_awake,n_bot_workers,was_human_active"
is "and the local rendering comes first, the epoch second" \
   "$(header claude slots | cut -d, -f1,2)" "slot_start_dt,slot_id"
is "the meter columns likewise" "$(header claude meter | cut -d, -f1,2,3)" \
   "observed_dt,observed_ts,window_used_pct"
is "and there is no agent column: the directory carries it" \
   "$(header claude slots | tr ',' '\n' | grep -cx agent)" "0"
teardown

section "the grid"

setup
push "$BASE" 5
ingest --now $((BASE + 6 * 300))
is "one row per slot, including the quiet ones" "$(count claude)" "6"
is "quiet slots carry no readings rather than being absent" \
   "$(rows claude slot_n_readings)" "1 0 0 0 0 0"
is "rows are in time order" \
   "$(rows claude slot_id | tr ' ' '\n' | cut -d_ -f1 | sort -c && echo sorted)" "sorted"
is "both agents share the one grid file" "$(count codex)" "0"
teardown

setup
push $((BASE + 10)) 20
ingest --now $((BASE + 400))
is "a slot still in progress is not written" "$(count claude)" "1"
teardown

# Percent columns are floats in memory, but a whole value is written without its
# trailing .0, so the file stays readable. Types are restored on read from the
# declared column types, not guessed per value.
section "deltas across ticks"

setup
# The bug a naive second tick introduces: counting the level again as a delta.
push $((BASE + 10)) 20
ingest --now $((BASE + 300))
push $((BASE + 310)) 26
ingest --now $((BASE + 600))
is "the second slot holds a delta, not the level" "$(rows claude slot_window_used_pct)" "20 6"
is "and the level is carried, not re-derived" "$(rows claude window_used_pct)" "20 26"
teardown

setup
push "$BASE" 10
push $((BASE + 310)) 10
ingest --now $((BASE + 900))
is "a repeated reading moves the meter by nothing" "$(rows claude slot_window_used_pct)" "10 0 0"
teardown

section "columns a predictor cannot do without"

setup
push "$BASE" 100
ingest --now $((BASE + 300))
is "reaching the cap is flagged as censoring" "$(rows claude is_window_maxed)" "true"
teardown

setup
# A closed window's percentage is not the current one: carrying 21 forward would
# let a later stage compute room against a window that has ended.
#
# The window must really have ended for the no-window reading to be believed: a
# null reset arriving while the window is still open is upstream noise, not a
# closure (../design/01_ingestion/DESIGN.md 5). So the window ends at BASE+300 and
# the null arrives after it.
push "$BASE" 21 $((BASE + 300))
push $((BASE + 310)) 0 none
ingest --now $((BASE + 900))
is "window_used_pct is the level while a window is open" \
   "$(rows claude window_used_pct | cut -d' ' -f1)" "21"
is "and becomes null once none is" "$(rows claude window_used_pct | cut -d' ' -f2)" "null"
is "and stays null in later slots" "$(rows claude window_used_pct | cut -d' ' -f3)" "null"
is "which is what is_window_open reports" "$(rows claude is_window_open | cut -d' ' -f3)" "false"
teardown

setup
# The same shape, but the window had NOT ended: 65 of 122 such rows in the real
# history were this, claiming "no window" up to four hours before the real end.
# Believing one would read window_used as 0 for the rest of a window we are in.
push "$BASE" 21 $((BASE + 7200))
push $((BASE + 310)) 0 none
ingest --now $((BASE + 900))
is "a null reset inside an open window is ignored, not believed" \
   "$(rows claude is_window_open | cut -d' ' -f3)" "true"
is "so the level is still the window's" "$(rows claude window_used_pct | cut -d' ' -f3)" "21"
is "and it never reached the meter file" "$(meter claude window_end_ts | wc -w | tr -d ' ')" "1"
teardown

setup
push "$BASE" 21
ingest --now $((BASE + 300))
push $((BASE + 310)) 5            # stale: below the running maximum
push $((BASE + 320)) 21           # a repeat
ingest --now $((BASE + 600))
is "both new rows are stale, so none survives the drop_stale_readings" \
   "$(rows claude slot_n_readings)" "1 0"
is "but the machine was plainly awake, and observed says so" \
   "$(rows claude was_machine_awake)" "true true"
teardown

section "attribution"

setup
push "$BASE" 12
ingest --now $((BASE + 300))
is "with no worker of ours, all movement is the user's" "$(rows claude slot_window_human_pct)" "12"
is "and none is ours" "$(rows claude slot_window_bot_pct)" "0"
is "which is exact, not estimated" "$(rows claude attribution)" "exact"
is "so the bounds collapse onto the value" \
   "$(rows claude slot_window_human_pct_lo) $(rows claude slot_window_human_pct_hi)" "12 12"
teardown

setup
runs "$BASE" $((BASE + 300)) ours-1
push $((BASE + 10)) 12
ingest --now $((BASE + 300))
is "our worker running with the user absent means all movement is ours" \
   "$(rows claude slot_window_bot_pct)" "12"
is "and none is the user's" "$(rows claude slot_window_human_pct)" "0"
is "still exact: both facts are measured" "$(rows claude attribution)" "exact"
teardown

setup
runs "$BASE" $((BASE + 300)) ours-1
push $((BASE + 10)) 12 "$RESET" theirs-1
ingest --now $((BASE + 300))
is "an overlap is censored rather than guessed" "$(rows claude attribution)" "censored"
is "no human number is invented" "$(rows claude slot_window_human_pct)" "null"
is "the bounds are kept instead" \
   "$(rows claude slot_window_human_pct_lo) $(rows claude slot_window_human_pct_hi)" "0 12"
teardown

setup
runs "$BASE" $((BASE + 300)) ours-1
push $((BASE + 10)) 12 "$RESET" ours-1
ingest --now $((BASE + 300))
is "our own session does not trip the human wire" "$(rows claude attribution)" "exact"
is "so the movement is still attributed to us" "$(rows claude slot_window_bot_pct)" "12"
teardown

finish
