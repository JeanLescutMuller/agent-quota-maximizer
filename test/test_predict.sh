#!/bin/bash
# Stage 2: the rate, the active-human floor, and the two
# directions the stage is allowed to be wrong in. Every expected number is
# hand-computed in its comment.
# ../design/02_prediction/DESIGN.md 1, 2, 3, 5
set -uo pipefail
source "$(dirname "$0")/harness.sh"

NOW=1790000400
HOUR=3600

# The contract (one number, plus which window it is about) and, separately, this
# engine's internals.
# Nothing downstream may read the latter: VOCABULARY.md 4.2.
A="aqm.predict(at=$NOW)['agents']['claude']"
eff()      { api "$A['internals']['effective_burn_rate']"; }
p95()      { api "$A['predicted_p95_human_usage_pct']"; }
pend()     { api "$A['window_end_ts']"; }
rate()     { api "$A['internals']['last_30_min']['human_avg_burn_rate']"; }
wrate()    { api "$A['internals']['current_window']['human_avg_burn_rate']"; }
based_on() { api "$A['internals']['based_on']"; }

section "what a zero forecast actually means"

setup
# Truly idle: no window open and nothing moved. This is the only shape that may
# predict exactly zero.
week $((NOW - HOUR)) 0 none 20 $((NOW + 86400))
ingest --now "$NOW"
is "no window and no movement means no predicted demand" "$(p95)" "0.0"
is "the rate is zero, not absent" "$(rate)" "0.0"
is "and nothing was based on" "$(based_on)" "nothing"
teardown

setup
# A window is open but nothing has been spent in it. Still zero: there is no
# evidence of a user, only of a window.
week $((NOW - HOUR)) 0 $((NOW + 2 * HOUR)) 20 $((NOW + 86400))
ingest --now "$NOW"
is "an open but unused window predicts nothing either" "$(p95)" "0.0"
is "the window end is carried through for the consumer" "$(pend)" "$((NOW + 2 * HOUR))"
teardown

section "a quiet half-hour inside a busy window is not idleness"

setup
# The regression this exists for, taken from real data of 2026-10-04: the meter sat
# at 31% mid-window while the 30-minute mean read 0 %/min, and the stage reported
# `signal: null` -- for a user who went on to burn the remaining 69% within two
# hours. A pause is not an absence.
#
# Window opened at NOW-3h, filled to 30% by NOW-2h, then nothing for two hours.
#   recent 30-min mean    = 0           <- would have said "idle"
#   window-to-date        = 30 pct / 180 min = 0.1667 %/min
#   p95 = 0.1667 x 120 x 1.5 = 30
week $((NOW - 3 * HOUR)) 0 $((NOW + 2 * HOUR)) 20 $((NOW + 86400))
week $((NOW - 2 * HOUR)) 30 $((NOW + 2 * HOUR)) 20 $((NOW + 86400))
ingest --now "$NOW"
is "a window that has plainly been used is never predicted at zero" "$(p95)" "30.0"
is "the rate comes from the window's own average" "$(wrate)" "0.16667"
is "and says which measurement won" "$(based_on)" "current_window"
is "the recent mean is still reported beside it" \
   "$(api "aqm.predict(at=$NOW)['agents']['claude']['internals']['current_window']['human_avg_burn_rate']")" \
   "0.16667"
teardown

setup
# max(), not a replacement: a burst in the last BURN_LOOKBACK_MINUTES must still dominate the
# window's slower average, or the stage would stop reacting within a window.
#   window-to-date = 36 / 180 = 0.2 ;  recent = (36-30)/30 = 0.2 ... so push harder:
#   NOW-10min jumps 30 -> 48, recent = 18/30 = 0.6 %/min, window = 48/180 = 0.2667
week $((NOW - 3 * HOUR)) 0 $((NOW + 2 * HOUR)) 20 $((NOW + 86400))
week $((NOW - 2 * HOUR)) 30 $((NOW + 2 * HOUR)) 20 $((NOW + 86400))
week $((NOW - 600)) 48 $((NOW + 2 * HOUR)) 20 $((NOW + 86400))
ingest --now "$NOW"
is "a burst still beats the window average" "$(rate)" "0.6"
is "and is attributed to the recent measurement" "$(based_on)" "last_30_min"
teardown

setup
# A brand-new window must not read its first percent as a huge rate: the divisor
# never falls below BURN_LOOKBACK_MINUTES, so 1% two minutes in is 1/30, not 1/2.
# An earlier window first, so the slot grid spans the whole BURN_LOOKBACK_MINUTES --
# otherwise the recent mean divides by the few minutes of history that exist.
week $((NOW - HOUR)) 40 $((NOW - 180)) 20 $((NOW + 86400))
# then a window opened at NOW-120, which therefore ends at NOW + 18000 - 120
week $((NOW - 120)) 1 $((NOW + 17880)) 20 $((NOW + 86400))
ingest --now "$NOW"
is "a two-minute-old window is damped, not amplified" "$(wrate)" "0.1"
is "so the combined rate is the honest one, not 0.5" "$(rate)" "0.03333"
teardown

section "a measured rate is extrapolated"

setup
# 12 points of human movement over the 30 minutes before NOW:
#   rate    = 12 pct / 30 min = 0.4 pct/min
#   minutes = 120, the time to the window's end
#   p95     = 0.4 x 120 x 1.5 = 72
week $((NOW - 1800)) 0 $((NOW + 2 * HOUR)) 20 $((NOW + 86400))
week $((NOW - 600)) 12 $((NOW + 2 * HOUR)) 20 $((NOW + 86400))
ingest --now "$NOW"
is "the rate is human movement over the span measured" "$(rate)" "0.4"
is "and p95 is that rate held to the window end, times SAFETY_MULTIPLIER" \
   "$(p95)" "72.0"
is "which is last_30_min, the measurement" "$(based_on)" "last_30_min"
teardown

section "the horizon is the shorter of the window and the persistence limit"

setup
# The window ends in 30 min, so only 30 minutes of demand can still land in it:
#   p95 = 0.4 x 30 x 1.5 = 18
week $((NOW - 1800)) 0 $((NOW + 1800)) 20 $((NOW + 86400))
week $((NOW - 600)) 12 $((NOW + 1800)) 20 $((NOW + 86400))
ingest --now "$NOW"
is "a window about to end bounds the forecast" "$(p95)" "18.0"
teardown

setup
# The window filled and then closed. There is no window to bound the forecast, so
# a projected 5-hour one is assumed, so the forecast covers all 300 of its minutes:
# 0.4 %/min x 300 x 1.5 = 180, and the only bound left is the clamp to 100. There is
# no horizon parameter to bound it instead -- the stage always answers for the whole
# window. (Movement with no window open is not a case that can exist: the 5-hour
# meter only moves while a 5-hour window is running.)
# The window really ends at NOW-300, and only then does the null reset arrive --
# a null while the window is still open is upstream noise and is ignored
# (../design/01_ingestion/DESIGN.md 5).
week $((NOW - 1800)) 0 $((NOW - 300)) 20 $((NOW + 86400))
week $((NOW - 900)) 12 $((NOW - 300)) 20 $((NOW + 86400))
week $((NOW - 240)) 0 none 20 $((NOW + 86400))
ingest --now "$NOW"
is "with no window, a projected 5-hour one is forecast whole, so the clamp bounds it" "$(p95)" "100.0"
is "and the end is reported as absent rather than invented" "$(pend)" "null"
teardown

section "p95 cannot exceed the window it is a share of"

setup
# 60 points in 30 min = 2 pct/min -> 2 x 120 x 1.5 = 360, which is meaningless:
# a forecast is a share of one window, so it clamps at 100.
week $((NOW - 1800)) 0 $((NOW + 2 * HOUR)) 20 $((NOW + 86400))
week $((NOW - 600)) 60 $((NOW + 2 * HOUR)) 20 $((NOW + 86400))
ingest --now "$NOW"
is "an extreme rate clamps instead of promising more than a window holds" \
   "$(p95)" "100.0"
teardown

section "the tripwire: the user is here, but the meter has not moved yet"

setup
# A reading from a session that is not ours, in a slot with no movement. S1 sees
# nothing because the meter quantises at 1%; S2 says the user is present.
#   rate = ACTIVE_HUMAN_BURN_RATE = 0.15  ->  p95 = 0.15 x 120 x 1.5 = 27
week $((NOW - 1800)) 0 $((NOW + 2 * HOUR)) 20 $((NOW + 86400))
week $((NOW - 600)) 0 $((NOW + 2 * HOUR)) 20 $((NOW + 86400)) someone-else
ingest --now "$NOW"
is "a confirmed request raises the rate to a floor" "$(eff)" "0.15"
is "which is the human_active_floor, not a measurement" "$(based_on)" "human_active_floor"
is "and the floor is extrapolated like any rate" "$(p95)" "27.0"
teardown

setup
# The floor never lowers a rate that is already higher: it is a floor, not an
# estimate, and S1 remains the measurement when S1 is the larger.
week $((NOW - 1800)) 0 $((NOW + 2 * HOUR)) 20 $((NOW + 86400))
week $((NOW - 600)) 12 $((NOW + 2 * HOUR)) 20 $((NOW + 86400)) someone-else
ingest --now "$NOW"
is "a measured rate above the floor is kept" "$(rate)" "0.4"
is "and stays attributed to S1" "$(based_on)" "last_30_min"
teardown

section "our own work is not predicted demand"

setup
# Movement inside one of our own run intervals is extra, not human, so it must
# not come back as a forecast of the user -- that is the loop this system must
# never close: spend, predict the spend as demand, then refuse to spend.
runs $((NOW - 1800)) "$NOW" ours-1
week $((NOW - 1800)) 0 $((NOW + 2 * HOUR)) 20 $((NOW + 86400))
week $((NOW - 600)) 12 $((NOW + 2 * HOUR)) 20 $((NOW + 86400))
ingest --now "$NOW"
is "our own movement does not become predicted human demand" "$(rate)" "0.0"
is "so nothing is reserved against it" "$(p95)" "0.0"
teardown

section "failing safe"

setup
is "with no data at all, the forecast is zero rather than an error" "$(p95)" "0.0"
is "and codex is predicted independently" \
   "$(api "aqm.predict(at=$NOW)['agents']['codex']['predicted_p95_human_usage_pct']")" "0.0"
teardown

setup
week $((NOW - 600)) 12 $((NOW + 2 * HOUR)) 20 $((NOW + 86400))
ingest --now "$NOW"
rm -f "$AQM_HOME/data/claude/slots.csv"
mkdir -p "$AQM_HOME/data/claude/slots.csv"   # a path that exists and cannot be read
is "an unreadable source predicts a full window, so nothing can run" \
   "$(p95)" "100.0"
is "and says so instead of reporting a rate it does not have" \
   "$(based_on)" "unreadable"
teardown

section "what budgeting does with it"

setup
# The whole point of the stage: a forecast of human demand closes the current
# window to extra work. margin = max(0.1, 72/100) = 0.72, and the window is 0.12
# used, so max_spend_units_by_quota = 1 - 0.72 - 0.12 = 0.16.
week $((NOW - 1800)) 0 $((NOW + 2 * HOUR)) 20 $((NOW + 86400))
week $((NOW - 600)) 12 $((NOW + 2 * HOUR)) 20 $((NOW + 86400))
ingest --now "$NOW"
aqm predict --at "$NOW" --out "$TMP/p.json" >/dev/null
is "the forecast tightens the current window to the room it really leaves" \
   "$(api "'%.2f' % aqm.budget(at=$NOW, prediction='$TMP/p.json')['agents']['claude']['remaining_windows'][0]['max_spend_units']")" \
   "0.16"
is "and the method is recorded in the budget artifact" \
   "$(api "aqm.budget(at=$NOW, prediction='$TMP/p.json')['method']")" "recent-rate-v1"
teardown

section "a stale prediction is not used"

setup
week $((NOW - 600)) 12 $((NOW + 2 * HOUR)) 20 $((NOW + 86400))
ingest --now "$NOW"
aqm predict --at "$NOW" --out "$TMP/old.json" >/dev/null
mkdir -p "$AQM_HOME/state/latest"
ln -sfn "$TMP/old.json" "$AQM_HOME/state/latest/prediction.json"
# ARTIFACT_MAX_AGE_SECONDS is 10 min; this one is an hour old by the time budgeting runs.
is "a prediction older than ARTIFACT_MAX_AGE_SECONDS fails safe to a full window" \
   "$(api "aqm.budget(at=$((NOW + HOUR)))['agents']['claude']['remaining_windows'][0]['max_spend_units']")" "0.0"
is "and the budget says the forecast was stale, not that nothing was due" \
   "$(api "aqm.budget(at=$((NOW + HOUR)))['method']")" "stale-prediction"
is "while a fresh one is used normally" \
   "$(api "aqm.budget(at=$NOW)['method']")" "recent-rate-v1"
teardown

section "the contract and the engine's internals are separate"

setup
# VOCABULARY.md 4.2: budget reads the contract and nothing else, so that a later
# engine -- a model, say -- can replace recent-rate-v1 without touching stage 3.
# Blanking `internals` must leave the decision byte-identical. If this ever fails,
# something downstream has started depending on how the forecast was reached.
week $((NOW - 2 * HOUR)) 30 $((NOW + 2 * HOUR)) 20 $((NOW + 86400))
ingest --now "$NOW"
aqm predict --at "$NOW" --out "$TMP/full.json" >/dev/null
"$PY" - "$TMP/full.json" "$TMP/bare.json" <<'EOF'
import json, sys
report = json.load(open(sys.argv[1]))
for block in report["agents"].values():
    block["internals"] = {}
json.dump(report, open(sys.argv[2], "w"))
EOF
is "a forecast stripped of its internals decides exactly the same"    "$(api "json.dumps(aqm.budget(at=$NOW, prediction='$TMP/bare.json')['agents'], sort_keys=True)")"    "$(api "json.dumps(aqm.budget(at=$NOW, prediction='$TMP/full.json')['agents'], sort_keys=True)")"
is "and the contract carries the three fields it promises"    "$(api "' '.join(sorted(k for k in aqm.predict(at=$NOW)['agents']['claude'] if k != 'internals'))")"    "predicted_p95_human_usage_pct window_end_ts"
teardown

finish
