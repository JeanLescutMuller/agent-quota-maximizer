#!/bin/bash
# Runs every test/test_*.sh and aggregates the result.
#   bash test/run.sh                 everything
#   bash test/test_slots.sh          one file, independently runnable
#
# Hermetic except test_real_data.sh, which reads the recorded history read-only
# and skips itself when agent-usage-tracker is not deployed. Nothing writes
# outside a temp dir.
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
GREEN='\033[0;32m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'

pass=0; fail=0; failed=()

for file in "$DIR"/test_*.sh; do
    name="$(basename "$file")"
    echo -e "${BOLD}== $name${NC}"
    output="$(bash "$file" 2>&1)"; status=$?
    printf '%s\n' "$output"
    pass=$((pass + $(printf '%s' "$output" | grep -c '✓' || true)))
    fail=$((fail + $(printf '%s' "$output" | grep -c '✗' || true)))
    [ "$status" -eq 0 ] || failed+=("$name")
    echo
done

if [ "$fail" -eq 0 ] && [ "${#failed[@]}" -eq 0 ]; then
    echo -e "${GREEN}${BOLD}all $pass assertions passed${NC}"
    exit 0
fi
echo -e "${RED}${BOLD}$fail failed, $pass passed${NC}"
[ "${#failed[@]}" -gt 0 ] && echo -e "${RED}failing files: ${failed[*]}${NC}"
exit 1
