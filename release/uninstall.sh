#!/bin/bash
# Unload the job and remove both symlinks. Deliberately leaves ~/opt/agent-quota-
# maximizer in place: data/, state/, logs/ and reports/ are the record of what the
# system did, and a reinstall should find them.
#
# To stop the system without uninstalling, set "enabled": false in config.json --
# the pipeline re-reads it every tick.
set -euo pipefail

LABEL="com.jeanlescut.agent-quota-maximizer"
PLIST="$LABEL.plist"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$PLIST"
[ -L "$HOME/.local/bin/aqm" ] && rm -f "$HOME/.local/bin/aqm"

echo "unloaded $LABEL and removed its symlinks"
echo "state, data, logs and reports left in $HOME/opt/agent-quota-maximizer"
