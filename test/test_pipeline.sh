#!/bin/bash
# The plumbing: config, the on/off switch, the lock, the staleness guard, the
# artifact chain, housekeeping, metrics and exit codes.
# ../design/07_pipeline/DESIGN.md 1.2, 2, 4, 5, 6, 8
set -uo pipefail
source "$(dirname "$0")/harness.sh"

NOW=1790000400
HOUR=3600

# A reading fresh as of NOW, so the READING_MAX_AGE_SECONDS guard passes.
fresh_meter() {
    week $((NOW - 60)) 10 $((NOW + 2 * HOUR)) 50 $((NOW + 86400))
    ingest --now "$NOW"
}
# tick [args...] -- the pipeline as of NOW, JSON, with the exit code appended
tick() { aqm pipeline --now "$NOW" --json "$@"; }
field() { api "aqm.pipeline(now=$NOW)[\"$1\"]"; }

section "configuration"

setup
is "no config.json at all is the designed state, and the defaults stand" \
   "$(api 'aqm.load_config()["enabled"]')" "true"
is "and both agents are configured by default" \
   "$(api 'list(aqm.agents())')" '["claude", "codex"]'

echo '{ not json' > "$AQM_HOME/config.json"
is "a config that does not parse is a hard error, not a silent default" \
   "$(aqm pipeline --now "$NOW" 2>&1 >/dev/null | grep -c 'config error')" "1"
is "and the exit code says error" \
   "$(aqm pipeline --now "$NOW" >/dev/null 2>&1; echo $?)" "1"

echo '{"parameters": {"MIN_MARGN": 0.2}}' > "$AQM_HOME/config.json"
is "a misspelled parameter is refused rather than ignored" \
   "$(aqm pipeline --now "$NOW" 2>&1 >/dev/null | grep -c 'unknown parameter')" "1"

echo '{"parameters": {"MIN_HUMAN_RESERVE_PCT": "a lot"}}' > "$AQM_HOME/config.json"
is "and so is one of the wrong type" \
   "$(aqm pipeline --now "$NOW" 2>&1 >/dev/null | grep -c 'should be a float')" "1"

echo '{"parameters": {"MIN_HUMAN_RESERVE_PCT": 0.25}}' > "$AQM_HOME/config.json"
is "a valid override reaches the parameter table" \
   "$(api 'aqm.load_config() and aqm.P["MIN_HUMAN_RESERVE_PCT"]')" "0.25"
is "and changes the config hash, so an old artifact stays explainable" \
   "$(api 'aqm.config_hash() != (aqm.load_config() and aqm.config_hash())')" "true"
teardown

section "the on/off switch"

setup
fresh_meter
echo '{"enabled": false, "disabled_reason": "testing"}' > "$AQM_HOME/config.json"
is "disabled means no stage runs at all" "$(field stages)" "[]"
is "the decision says why" "$(field decision)" "disabled"
is "the reason is carried through from the config" "$(field reason)" "testing"
is "and a guard refusal is exit code 2" \
   "$(aqm pipeline --now "$NOW" >/dev/null 2>&1; echo $?)" "2"
is "nothing was written, not even an artifact" \
   "$([ -d "$AQM_HOME/artifacts" ] && echo yes || echo no)" "no"
teardown

section "a normal tick"

setup
fresh_meter
is "the three read-only stages run, in order" \
   "$(field stages)" '["ingest", "predict", "budget"]'
is "and the tick succeeds" \
   "$(aqm pipeline --now "$NOW" >/dev/null 2>&1; echo $?)" "0"

aqm pipeline --now "$NOW" >/dev/null
is "a prediction artifact is written" \
   "$(find "$AQM_HOME/artifacts/predictions" -name '*.json' | wc -l | tr -d ' ')" "1"
is "a budget artifact is written" \
   "$(find "$AQM_HOME/artifacts/budgets" -name '*.json' | wc -l | tr -d ' ')" "1"
is "both latest pointers resolve" \
   "$([ -e "$AQM_HOME/state/latest/prediction.json" ] && \
      [ -e "$AQM_HOME/state/latest/budget.json" ] && echo yes)" "yes"
is "the budget used this tick's prediction, not a default" \
   "$(api "aqm.latest_artifact('budgets')['method']")" "recent-rate-v1"
teardown

section "metrics: one line per agent per tick, even when nothing happens"

setup
fresh_meter
aqm pipeline --now "$NOW" >/dev/null
aqm pipeline --now "$NOW" >/dev/null
is "two ticks over two agents is four lines" \
   "$(wc -l < "$AQM_HOME/logs/metrics.jsonl" | tr -d ' ')" "4"
is "and each carries the forecast beside the decision" \
   "$(grep -c '"predicted_p95_human_usage_pct"' "$AQM_HOME/logs/metrics.jsonl")" "4"
is "the pipeline log records the tick too" \
   "$(grep -c 'ingest,predict,budget' "$AQM_HOME/logs/pipeline.log")" "2"
teardown

section "the staleness guard"

setup
# Something is due -- the weekly reset is an hour away, so the one window left holds
# 0.9 -- but the reading it would be spent against is two hours old. The meter may
# have moved since without us seeing it, so that agent is refused.
week $((NOW - 2 * HOUR)) 10 $((NOW + 2 * HOUR)) 50 $((NOW + HOUR))
ingest --now "$NOW"
is "a stale meter stops the agent before anything can act" "$(field decision)" "stale"
is "naming the agents whose readings are old" "$(field stale_agents)" '["claude"]'
is "and dropping them from what is due" "$(field due)" "{}"
is "which is a guard refusal, exit code 2" \
   "$(aqm pipeline --now "$NOW" >/dev/null 2>&1; echo $?)" "2"
is "but the read-only stages still ran, so the record is complete" \
   "$(field stages)" '["ingest", "predict", "budget"]'
teardown

setup
# An agent with no reading at all is a different thing from a stale one, and it
# must not stop the other agent's tick: a machine with no Codex still budgets
# Claude. Budgeting already answers it with `no meter reading`.
fresh_meter
is "an agent that has never been read is named, not treated as stale" \
   "$(field no_reading)" '["codex"]'
is "and the tick proceeds normally" "$(field decision)" "nothing due"
is "with codex reported as unreadable rather than idle" \
   "$(api "aqm.budget(at=$NOW)['agents']['codex']['verdict']")" "no_meter_reading"
teardown

section "the lock"

setup
fresh_meter
is "a tick runs when the lock is free" "$(field decision)" "nothing due"
# Hold the lock from another process, exactly as an overrunning tick would.
"$PY" - "$AQM_HOME" <<'EOF' &
import fcntl, os, pathlib, sys, time
path = pathlib.Path(sys.argv[1]) / "state" / "locks" / "pipeline.lock"
path.parent.mkdir(parents=True, exist_ok=True)
handle = path.open("a+")
fcntl.flock(handle, fcntl.LOCK_EX)
print("held", flush=True)
time.sleep(5)
EOF
holder=$!
sleep 1
is "a second tick skips rather than piling up" "$(field decision)" "busy"
is "and reports it as locked, exit code 3" \
   "$(aqm pipeline --now "$NOW" >/dev/null 2>&1; echo $?)" "3"
kill "$holder" 2>/dev/null; wait "$holder" 2>/dev/null
is "once the holder dies the lock is free again, with no reclaim step" \
   "$(field decision)" "nothing due"
teardown

section "housekeeping"

setup
fresh_meter
# Artifacts from well before the retention horizon, in the date-sharded layout.
mkdir -p "$AQM_HOME/artifacts/budgets/2020-01-01" \
         "$AQM_HOME/artifacts/predictions/2020-01-02"
touch "$AQM_HOME/artifacts/budgets/2020-01-01/120000.json" \
      "$AQM_HOME/artifacts/predictions/2020-01-02/120000.json"
is "old artifact days are pruned, a directory at a time" \
   "$(api "aqm.pipeline(now=$NOW)['housekeeping']['pruned_days']")" "2"
is "and today's are kept" \
   "$(find "$AQM_HOME/artifacts" -name '*.json' | wc -l | tr -d ' ')" "2"

# Rotation triggers on size, so shrink the threshold rather than writing 20 MB.
echo '{"parameters": {"LOG_MAX_MB": 0}}' > "$AQM_HOME/config.json"
aqm pipeline --now "$NOW" >/dev/null
is "an oversized log is rotated aside, not deleted" \
   "$([ -f "$AQM_HOME/logs/metrics.jsonl.1" ] && echo yes)" "yes"
teardown

section "a tick is cheap enough to run 288 times a day"

setup
fresh_meter
ms="$(api "round(aqm.pipeline(now=$NOW)['ms'])")"
is "the whole chain is well inside its one-second budget (${ms} ms)" \
   "$("$PY" -c "print('yes' if $ms < 500 else 'no')")" "yes"
teardown

finish
