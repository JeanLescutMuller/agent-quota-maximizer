#!/bin/bash
# Stage 1, reading the meter: the drop_stale_readings, the window key, the raw record shapes
# and the tail-and-watermark reader.
# ../design/01_ingestion/DESIGN.md 4, 5
set -uo pipefail
source "$(dirname "$0")/harness.sh"

section "the drop_stale_readings"

setup
# An idle renderer re-pushes a reading taken a day earlier, so line order lies.
push 1790170589 18
push 1790170600 0
ingest --now 1790171000
is "a stale re-pushed reading is dropped" "$(meter claude window_used_pct)" "18"
teardown

setup
push 1000 5; push 1100 5; push 1200 5; push 1300 9; push 1400 9
ingest --now 2000
is "only the first reading of each new maximum is kept" \
   "$(meter claude window_used_pct)" "5 9"
is "and it is dated by when it was observed" \
   "$(meter claude observed_ts)" "1000 1300"
teardown

setup
# A push row with no assistant entry to date it is written undated upstream.
emit claude '{"ts":1000,"source":"claude_statusline","observed_at":null,
"five_hour_pct":5,"seven_day_pct":5,"five_hour_resets_at":"1790784600",
"seven_day_resets_at":null,"observed_by_session":null}'
ingest --now 2000
is "an undated push row is ignored, never guessed at" "$(meter claude window_used_pct)" ""
teardown

section "window identity"

setup
# Push rows report the reset one second below the API's, which split 40 of 46
# real Claude windows in two before the key was rounded to the minute.
push 1000 5 $((RESET - 1))
push 1100 4 "$RESET"
ingest --now 2000
is "a reset drifting by 1s is one window, not two" "$(meter claude window_used_pct)" "5"
teardown

setup
push $((RESET - 600)) 20
ingest --now $((RESET - 300))
is "the window id is derived from the rounded reset" \
   "$(rows claude window_id | tr ' ' '\n' | grep -v null | tail -1)" \
   "$((RESET - 18000))_$RESET"
teardown

setup
# 0% with a reset exactly one span away is Codex's fake idle countdown.
codex_row 1000 0 $((1000 + 18000))
ingest --now 2000
is "Codex's fake countdown is recorded as no window" "$(meter codex window_end_ts)" "null"
is "and no slot has a window open" \
   "$(rows codex is_window_open | tr ' ' '\n' | sort -u | tr '\n' ' ' | xargs)" "false"
teardown

setup
codex_row 1000 7 $((1000 + 9000))
ingest --now 2000
is "a real Codex window is kept" "$(meter codex window_used_pct)" "7"
is "and its last slot has a window open" \
   "$(rows codex is_window_open | tr ' ' '\n' | tail -1)" "true"
teardown

section "raw record shapes"

# Rows are recognised by shape, never by their source string. Upstream renamed
# `codex` to `codex_app_server`, and an exact match turned that into zero readings
# for the agent, silently -- so both the old and a hypothetical future name parse.
setup
codex_row 1000 7 $((1000 + 9000)) codex
codex_row 1300 9 $((1000 + 9000)) codex_something_else_entirely
ingest --now 2000
is "a Codex row parses whatever its source is called" "$(meter codex window_used_pct)" "7 9"
teardown

setup
push 1000 5
poll 1200 9
ingest --now 2000
is "push and poller rows are told apart by shape, not by source" \
   "$(meter claude source)" "claude_statusline claude"
teardown

setup
# The failure that must never be silent: a file full of rows, none recognisable.
emit codex '{"ts":1000,"iso":"x","source":"codex_app_server","something_new":{}}'
"$PY" "$AQM" ingest --now 2000 >/dev/null 2>&1
is "an entirely unrecognisable file raises instead of reporting zero readings" "$?" "1"
teardown

setup
poll 1000 11 no_source
ingest --now 2000
is "a poller row predating the source key still parses" "$(meter claude window_used_pct)" "11"
teardown

setup
# data/ holds readings only since 2026-10-04; a failure row must not pass as one.
emit claude '{"ts":1000,"source":"claude","api":{},"error":{"kind":"http","status":429}}'
"$PY" "$AQM" ingest --now 2000 >/dev/null 2>&1
is "an error row in data/ is refused rather than read as a reading" "$?" "1"
teardown

setup
push 1000 5
printf '{"ts": 1100, "five_hour_pct": 9' >> "$AQM_USAGE_DATA/data/claude/account.jsonl"
ingest --now 2000
is "a half-written final line is skipped" "$(meter claude window_used_pct)" "5"
teardown

section "tail and watermark"

setup
# The one failure that would otherwise lose data silently: the tail does not
# reach back to the watermark, so it must double until it does.
for i in $(seq 0 399); do push $((1000 + i)) $((1 + i / 50)); done
export AQM_TAIL_BYTES=256
ingest --now 2000
unset AQM_TAIL_BYTES
is "a tail far shorter than the history doubles until sufficient" \
   "$(meter claude window_used_pct)" "1 2 3 4 5 6 7 8"
teardown

setup
push 1000 5
ingest --now 2000
push 1500 8
ingest --now 3000
is "the watermark stops rows being processed twice" "$(meter claude window_used_pct)" "5 8"
teardown

setup
push 1000 5
ingest --now 2000
push 900 3          # arrives late, below the watermark
ingest --now 3000
is "a row older than the watermark is not appended" "$(meter claude window_used_pct)" "5"
teardown

finish
