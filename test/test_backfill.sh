#!/bin/bash
# The one-off backfill and the API later stages consume.
# ../design/01_ingestion/DESIGN.md 2, 11
set -uo pipefail
source "$(dirname "$0")/harness.sh"

section "backfill"

setup
push "$BASE" 5
push $((BASE + 400)) 9
ingest --backfill --now $((BASE + 1200))
first_meter="$(meter claude window_used_pct)"; first_buckets="$(rows claude slot_window_used_pct)"
ingest --backfill --now $((BASE + 1200))
is "a second backfill is a no-op, not a doubling" \
   "$(meter claude window_used_pct)|$(rows claude slot_window_used_pct)" "$first_meter|$first_buckets"
teardown

setup
push "$BASE" 5
ingest --now $((BASE + 300))
push $((BASE + 310)) 9
ingest --backfill --now $((BASE + 600))
is "backfill rebuilds from the raw logs rather than appending to old state" \
   "$(meter claude window_used_pct)" "5 9"
is "and the table it produces covers the whole span" "$(count claude)" "2"
teardown

setup
push "$BASE" 5
ingest --backfill --now $((BASE + 600))
rm -f "$AQM_HOME/data/claude/slots.csv" "$AQM_HOME/data/claude/meter.csv"
ingest --backfill --now $((BASE + 600))
is "derived state is disposable: deleting it and rebuilding is the recovery path" \
   "$(meter claude window_used_pct)" "5"
teardown

section "sum of deltas reconciles with the window peaks"

setup
# Three readings in one window, then a second window. Deltas must sum to the sum
# of per-window peaks: 30 + 12 = 42. This is the check that caught the window-key
# bug on real data.
push "$BASE" 10
push $((BASE + 310)) 22
push $((BASE + 620)) 30
push $((BASE + 930)) 12 $((RESET + 18000))
ingest --backfill --now $((BASE + 1500))
total="$(rows claude slot_window_used_pct | tr ' ' '+' | sed 's/+$//' | bc)"
is "deltas sum to the sum of window peaks" "$total" "42"
teardown

section "what later stages read"

setup
push $((RESET - 1200)) 30
ingest --now $((RESET - 900))
is "meter_now reports the level"      "$(api 'aqm.meter_now("claude")["window_used_pct"]')" "30.0"
is "meter_now reports the reset"      "$(api 'aqm.meter_now("claude")["window_end_ts"]')" "$RESET"
is "window_state says the window is open" "$(api 'aqm.window_state("claude")["open"]')" "true"
is "window_state carries the end"     "$(api 'aqm.window_state("claude")["end"]')" "$RESET"
teardown

setup
is "meter_now is None with no data, rather than a fabricated zero" \
   "$(api 'aqm.meter_now("claude")')" "null"
is "window_state then reports no window" \
   "$(api 'aqm.window_state("claude")["open"]')" "false"
is "burn_rate is zero rather than an error" \
   "$(api 'aqm.burn_rate("claude", 30)')" "0.0"
teardown

setup
push "$BASE" 0 none
ingest --now $((BASE + 300))
is "a reading with no window leaves window_state closed" \
   "$(api 'aqm.window_state("claude")["open"]')" "false"
teardown

finish
