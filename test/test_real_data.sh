#!/bin/bash
# Against the real recorded history, read-only. Skipped when agent-usage-tracker
# is not deployed, so the suite still passes on a bare machine.
#
# This is the file that catches what fixtures cannot: it reconciles the rebuilt
# table against an independently measured figure, and holds the per-tick cost to
# its documented budget.
# ../design/01_ingestion/DESIGN.md 8, 11
set -uo pipefail
source "$(dirname "$0")/harness.sh"

REAL="${AQM_REAL_DATA:-$HOME/opt/agent-usage-tracker}"

if [ ! -f "$REAL/data/claude/account.jsonl" ]; then
    echo "  - skipped: no recorded history at $REAL"
    exit 0
fi

setup
# Read the real logs, write state into the throwaway dir.
export AQM_USAGE_DATA="$REAL"

section "backfill over the recorded history"

start_ms=$(("$($PY -c 'import time; print(int(time.time()*1000))')"))
ingest --backfill
elapsed=$(( "$($PY -c 'import time; print(int(time.time()*1000))')" - start_ms ))

claude_rows="$(meter claude window_used_pct | wc -w | tr -d ' ')"
codex_rows="$(meter codex window_used_pct | wc -w | tr -d ' ')"
echo "  - ${elapsed} ms, $claude_rows Claude and $codex_rows Codex meter rows"

is "the backfill finishes inside 10 s" "$([ "$elapsed" -lt 10000 ] && echo yes)" "yes"
is "Claude readings survive the drop_stale_readings" \
   "$([ "$claude_rows" -gt 100 ] && echo yes)" "yes"
is "Codex readings survive the drop_stale_readings" \
   "$([ "$codex_rows" -gt 20 ] && echo yes)" "yes"

section "reconciliation"

# Deltas must sum to the sum of per-window peaks, per agent. Any mistake in the
# window key, the drop_stale_readings or the slot walk breaks this identity.
#
# Both sides are evaluated against the same horizon: the last slot the tick
# closed. Readings taken inside the slot still in progress are in the meter but
# not yet in any slot row, so counting them would make the identity fail by a
# few points for a correct implementation.
for agent in claude codex; do
    verdict="$("$PY" - "$AQM_HOME/data/$agent" <<'EOF'
import collections, csv, sys
agent_dir = sys.argv[1]
delta, horizon = 0.0, 0
with open("%s/slots.csv" % agent_dir, newline="") as handle:
    for row in csv.DictReader(handle):
        delta += float(row["slot_window_used_pct"])
        horizon = max(horizon, int(row["slot_id"].split("_")[1]))
peaks = collections.defaultdict(float)
with open("%s/meter.csv" % agent_dir, newline="") as handle:
    for row in csv.DictReader(handle):
        if row["window_end_ts"] and int(row["observed_ts"]) < horizon:
            key = int(row["window_end_ts"])
            peaks[key] = max(peaks[key], float(row["window_used_pct"]))
print("%.0f %.0f %d" % (delta, sum(peaks.values()), len(peaks)))
EOF
)"
    read -r delta peaks windows <<< "$verdict"
    is "$agent: deltas sum to the window peaks ($delta)" "$delta" "$peaks"
    echo "  - $agent: $windows windows"
done

# 46 Claude windows over 40.8 days is 1.13 a day, which adhoc_quotas_analysis
# measured independently as 35 over 30 days. A count far above it means the
# window key is wrong -- which is exactly how the 1-second reset drift surfaced.
per_day="$("$PY" - "$AQM_HOME/data/claude" <<'EOF'
import csv, sys
agent_dir = sys.argv[1]
resets, first, last = set(), None, None
with open("%s/meter.csv" % agent_dir, newline="") as handle:
    for row in csv.DictReader(handle):
        if row["window_end_ts"]:
            resets.add(int(row["window_end_ts"]))
        at = int(row["observed_ts"])
        first = at if first is None else first
        last = at
print("%.2f" % (len(resets) / ((last - first) / 86400.0)))
EOF
)"
echo "  - Claude windows per day: $per_day (independently measured: 1.17)"
is "the window count is plausible, so the key is not splitting windows" \
   "$("$PY" -c "print('yes' if 0.8 <= $per_day <= 1.6 else 'no')")" "yes"

section "cost of a tick"

# Three ticks in a row: the first may close a slot, the rest should be no-ops.
for _ in 1 2 3; do ingest; done
tick_ms="$("$PY" - "$AQM" <<'EOF'
import subprocess, sys, time
best = None
for _ in range(5):
    started = time.perf_counter()
    subprocess.run([sys.executable, sys.argv[1], "ingest"], check=True,
                   stdout=subprocess.DEVNULL)
    ms = (time.perf_counter() - started) * 1000
    best = ms if best is None else min(best, ms)
print("%.0f" % best)
EOF
)"
echo "  - ${tick_ms} ms wall clock per tick, interpreter startup included"
is "a tick stays far inside its one-second budget" \
   "$([ "$tick_ms" -lt 300 ] && echo yes)" "yes"

section "budgeting against the real meter"

# The same conservation identity as the fixture test, but over whatever chain the
# live meter happens to produce -- including a Codex period over 130 h long, which
# no fixture would think to build.
for agent in claude codex; do
    verdict="$("$PY" - "$REPO/release" "$agent" <<'EOF'
import sys
sys.path.insert(0, sys.argv[1])
import aqm
a = aqm.budget()["agents"][sys.argv[2]]
if not a["remaining_windows"]:
    print("skip %s" % a["verdict"]); raise SystemExit
placed = sum(c["planned_units"] for c in a["remaining_windows"]) + a["already_lost_units"]
print("%.3f %.3f %d %s" % (placed, a["week"]["left_units"], len(a["remaining_windows"]),
                           a["extra_quota_to_spend_units"] <= a["remaining_windows"][0]["max_spend_units"] + 1e-9))
EOF
)"
    if [ "${verdict%% *}" = "skip" ]; then
        echo "  - skipped $agent: ${verdict#skip }"; continue
    fi
    read -r placed remaining windows clamped <<< "$verdict"
    is "$agent: planned + unreachable = remaining ($placed)" "$placed" "$remaining"
    is "$agent: the chain respects the 34-window bound ($windows)" \
       "$([ "$windows" -le 34 ] && echo yes)" "yes"
    is "$agent: extra_quota_to_spend_units never exceeds the current window's ceiling" "$clamped" "True"
done

# Both stages in one process, which is how the pipeline will run them: the wall
# clock is dominated by interpreter startup either way, so paying it twice would
# double a tick for nothing (../design/01_ingestion/DESIGN.md 8).
both_ms="$("$PY" - "$REPO/release" <<'EOF'
import statistics, sys, time
sys.path.insert(0, sys.argv[1])
import aqm
samples = []
for _ in range(10):
    started = time.perf_counter()
    aqm.ingest(); aqm.budget()
    samples.append((time.perf_counter() - started) * 1000)
print("%.1f" % statistics.median(samples))
EOF
)"
echo "  - ingest + budget in one process: ${both_ms} ms of work"
is "the two stages together stay well inside a tick" \
   "$("$PY" -c "print('yes' if $both_ms < 100 else 'no')")" "yes"

section "a tick adds nothing when nothing happened"

before="$(wc -l < "$AQM_HOME/data/claude/slots.csv")"
ingest
after="$(wc -l < "$AQM_HOME/data/claude/slots.csv")"
is "an idle tick appends no slot rows" "$((after - before))" "0"

teardown
finish
