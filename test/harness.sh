#!/bin/bash
# Shared helpers for test/test_*.sh. Sourced, never run directly.
#
# Every test gets its own throwaway data/ and state/ via AQM_USAGE_DATA and
# AQM_HOME, so nothing touches ~/opt. CSV and JSON are read with /usr/bin/python3
# than jq: the only jq on this machine lives in a Conda prefix, and these tests
# must not depend on one.

PY=/usr/bin/python3
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AQM="$REPO/release/aqm.py"

GREEN='\033[0;32m'; RED='\033[0;31m'; DIM='\033[2m'; NC='\033[0m'
PASS=0; FAIL=0

# A slot-aligned epoch: BASE % 300 == 0, so grid assertions mean what they say.
BASE=1790000100
# A round 5-hour window end, used as a reset time.
RESET=1790784600

setup() {
    TMP="$(mktemp -d)"
    export AQM_USAGE_DATA="$TMP/usage"      # the raw logs we read
    export AQM_HOME="$TMP/home"             # our own data/ and state/
    mkdir -p "$AQM_USAGE_DATA/data/claude" "$AQM_USAGE_DATA/data/codex"
    # $AQM_HOME too, so a test can drop a config.json in before anything has run.
    # Without it those writes failed silently and the assertions that followed
    # passed only because an earlier `aqm` call had created the directory.
    mkdir -p "$AQM_HOME"
    unset AQM_TAIL_BYTES
}

teardown() { [ -n "${TMP:-}" ] && rm -rf "$TMP"; }

ok()   { PASS=$((PASS+1)); echo -e "  ${GREEN}✓${NC} $1"; }
bad()  { FAIL=$((FAIL+1)); echo -e "  ${RED}✗${NC} $1"; }

# is <description> <actual> <expected>
is() {
    if [ "$2" = "$3" ]; then ok "$1"
    else bad "$1"; echo -e "      ${DIM}expected: $3${NC}"; echo -e "      ${DIM}actual:   $2${NC}"; fi
}

section() { echo; echo "--- $1"; }

finish() {
    echo
    if [ "$FAIL" -eq 0 ]; then echo -e "${GREEN}$PASS passed${NC}"; exit 0
    else echo -e "${RED}$FAIL failed, $PASS passed${NC}"; exit 1; fi
}

# ---------------------------------------------------------------- fixtures

# push <observed_ts> <window_used_pct> [reset|none] [session]
# A Claude status-line row. `none` for the reset means no window is open.
push() {
    local at="$1" five="$2" reset="${3:-$RESET}" session="${4:-}"
    local r="\"$reset\"" s="null"
    [ "$reset" = "none" ] && r="null"
    [ -n "$session" ] && s="\"$session\""
    emit claude "{\"ts\":$at,\"iso\":\"x\",\"source\":\"claude_statusline\",\
\"observed_at\":$at,\"five_hour_pct\":$five,\"seven_day_pct\":5,\
\"five_hour_resets_at\":$r,\"seven_day_resets_at\":\"1791226800\",\
\"observed_by_session\":$s}"
}

# week <observed_ts> <window_used_pct> <window_end_ts|none> <week_used_pct> <week_end_ts> [session]
# A status-line row with the *weekly* meter under control too. `push` fixes the
# weekly fields because ingestion barely looks at them; budgeting is driven by
# them, so it needs its own fixture. Reset times are rounded to the minute on the
# way in, so pass minute-aligned values. A `session` that is not one of ours is
# what trips the S2 tripwire.
week() {
    local r="\"$3\"" s="null"
    [ "$3" = "none" ] && r="null"
    [ -n "${6:-}" ] && s="\"$6\""
    emit claude "{\"ts\":$1,\"iso\":\"x\",\"source\":\"claude_statusline\",\
\"observed_at\":$1,\"five_hour_pct\":$2,\"seven_day_pct\":$4,\
\"five_hour_resets_at\":$r,\"seven_day_resets_at\":\"$5\",\
\"observed_by_session\":$s}"
}

# poll <ts> <window_used_pct> [no_source]
# A Claude API poller row; `no_source` drops the source key, the pre-2026-08-30 shape.
poll() {
    local ts="$1" five="$2" src="\"source\":\"claude\","
    [ "${3:-}" = "no_source" ] && src=""
    emit claude "{\"ts\":$ts,\"iso\":\"x\",$src\
\"api\":{\"five_hour\":{\"utilization\":$five,\
\"resets_at\":\"2026-09-30T13:30:00.165686+00:00\"},\
\"seven_day\":{\"utilization\":5,\"resets_at\":\"2026-09-30T13:30:00.165686+00:00\"}},\
\"api_headers\":{}}"
}

# codex_row <ts> <used_percent> <resets_at_epoch> [source]
# Default source is the current upstream name; pass another to prove that rows are
# recognised by shape rather than by that string.
codex_row() {
    emit codex "{\"ts\":$1,\"iso\":\"x\",\"source\":\"${4:-codex_app_server}\",\
\"codex_rate_limits\":{\"rateLimits\":{\
\"primary\":{\"usedPercent\":$2,\"windowDurationMins\":300,\"resetsAt\":$3},\
\"secondary\":{\"usedPercent\":2,\"windowDurationMins\":10080,\"resetsAt\":$(($3+600000))}}},\
\"codex_usage\":{}}"
}

emit() { printf '%s\n' "$2" >> "$AQM_USAGE_DATA/data/$1/account.jsonl"; }

# runs <started_at> <ended_at> <session_id>  -- one of our own worker intervals
runs() {
    mkdir -p "$AQM_HOME/state"
    printf '{"started_at":%s,"ended_at":%s,"session_id":"%s"}\n' "$1" "$2" "$3" \
        >> "$AQM_HOME/state/runs.jsonl"
}

# ---------------------------------------------------------------- running

# ingest [--backfill] --now <epoch>
ingest() { "$PY" "$AQM" ingest "$@" >/dev/null; }

# aqm <args...> -- the CLI, with stdout kept
aqm() { "$PY" "$AQM" "$@"; }

# ---------------------------------------------------------------- reading

# column <file> <field> -- that field of every row, space separated.
# Empty cells print as "null" so an assertion can name them.
column_of() {
    "$PY" - "$1" "$2" <<'EOF'
import csv, sys
path, field = sys.argv[1], sys.argv[2]
try:
    handle = open(path, newline="")
except OSError:
    print(""); raise SystemExit
with handle:
    print(" ".join(r[field] or "null" for r in csv.DictReader(handle)))
EOF
}

# rows <agent> <field>   -- a field of data/<agent>/slots.csv
rows()  { column_of "$AQM_HOME/data/$1/slots.csv" "$2"; }
# meter <agent> <field>  -- a field of data/<agent>/meter.csv
meter() { column_of "$AQM_HOME/data/$1/meter.csv" "$2"; }

# count <agent> -- number of slot rows for that agent
count() { rows "$1" slot_id | wc -w | tr -d ' '; }

# header <agent> <file> -- the column line, for schema assertions
header() { head -1 "$AQM_HOME/data/$1/$2.csv"; }

# api <expression> -- evaluate one expression against the aqm module
api() {
    ( cd "$REPO/release" && "$PY" -c "
import json, sys
sys.path.insert(0, '.')
import aqm
value = $1
print(json.dumps(value) if not isinstance(value, str) else value)
" )
}

