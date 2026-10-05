#!/bin/bash
# The strongest check in the suite: ingesting tick by tick must produce the same
# table as one backfill over the same span. It is what caught the rebuild-every-
# tick bug and the two disagreeing carry-forward rules; neither was visible to a
# fixture test.
# ../design/01_ingestion/DESIGN.md 4, 6.2
set -uo pipefail
source "$(dirname "$0")/harness.sh"

section "tick by tick equals one backfill"

setup
# A day of synthetic history: a window filling, a gap with no window, a second
# window, stale re-pushes throughout.
at=$BASE
for step in 0 1 2 3 4 5 6 7 8 9; do
    push $((at + step * 300 + 10)) $((step * 7)) "$RESET"
    push $((at + step * 300 + 60)) $((step * 7)) "$RESET"      # a repeat
    [ "$step" -gt 0 ] && push $((at + step * 300 + 120)) $(((step - 1) * 7)) "$RESET"
    codex_row $((at + step * 300 + 30)) $((step * 3)) $((RESET + 600))
done
for step in 10 11 12; do
    push $((at + step * 300 + 10)) 0 none
    codex_row $((at + step * 300 + 30)) 0 $((at + step * 300 + 30 + 18000))
done
for step in 13 14 15; do
    push $((at + step * 300 + 10)) $(((step - 12) * 5)) $((RESET + 18000))
    codex_row $((at + step * 300 + 30)) $(((step - 12) * 4)) $((RESET + 18600))
done
END=$((BASE + 16 * 300))

cp -R "$AQM_USAGE_DATA" "$TMP/usage-copy"

# Reference: one backfill to the end.
ingest --backfill --now "$END"
snapshot() {            # every agent's table, keyed by agent and slot
    "$PY" - "$AQM_HOME/data" "$1" <<'EOF'
import csv, json, os, sys
root, out = sys.argv[1], sys.argv[2]
table = {}
for agent in sorted(os.listdir(root)):
    path = os.path.join(root, agent, "slots.csv")
    if not os.path.exists(path):
        continue
    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            table[agent + "|" + row["slot_id"]] = row
json.dump(table, open(out, "w"))
print(len(table))
EOF
}
ref_rows="$(snapshot "$TMP/ref.json")"

# Replay: backfill a third of the way, then tick every 5 minutes to the same end.
rm -rf "$AQM_HOME"
MID=$((BASE + 5 * 300))
ingest --backfill --now "$MID"
# macOS `seq` renders epochs in scientific notation, so step with arithmetic.
t=$((MID + 300))
while [ "$t" -le "$END" ]; do ingest --now "$t"; t=$((t + 300)); done
rep_rows="$(snapshot "$TMP/rep.json")"

is "both paths produce the same number of rows" "$rep_rows" "$ref_rows"

# Compare every field but `was_machine_awake`, which cannot match by construction: a live
# tick proves the machine was awake, a backfill can only infer it from row
# density, so backfill is deliberately the conservative one.
verdict="$("$PY" - "$TMP/ref.json" "$TMP/rep.json" <<'EOF'
import json, sys
ref = json.load(open(sys.argv[1]))
rep = json.load(open(sys.argv[2]))
same_keys = set(ref) == set(rep)
fields = [f for f in next(iter(ref.values())) if f != "was_machine_awake"]
differing = [k for k in set(ref) & set(rep)
             if any(ref[k][f] != rep[k][f] for f in fields)]
only_conservative = all(rep[k]["was_machine_awake"] == "true" or ref[k]["was_machine_awake"] != "true"
                        for k in set(ref) & set(rep))
print("%s %d %s" % (same_keys, len(differing), only_conservative))
if differing:
    k = differing[0]
    print({f: (ref[k][f], rep[k][f]) for f in fields if ref[k][f] != rep[k][f]},
          file=sys.stderr)
EOF
)"
read -r same_keys differing conservative <<< "$verdict"
is "both paths cover the same slots" "$same_keys" "True"
is "no row differs in any field but observed" "$differing" "0"
is "observed never claims less in a live tick than a backfill does" \
   "$conservative" "True"
teardown

section "a long sleep loses nothing"

setup
# Two days of readings arrive while nothing is running, then one tick catches up.
for day in 0 1; do
    for hour in 0 6 12 18; do
        push $((BASE + day * 86400 + hour * 3600)) $((hour / 6 + 1)) \
            $((RESET + day * 86400))
    done
done
export AQM_TAIL_BYTES=512     # far too small, so the doubling path must run
ingest --now $((BASE + 2 * 86400))
unset AQM_TAIL_BYTES
is "every reading survives a catch-up tick" \
   "$(meter claude window_used_pct | wc -w | tr -d ' ')" "8"
is "and the grid is filled across the whole gap" \
   "$(count claude)" "576"
teardown

finish
