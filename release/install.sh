#!/bin/bash
# Deploy agent-quota-maximizer into ~/opt/ and schedule it.
#
# Idempotent: safe to re-run after every change to aqm.py. It never overwrites
# config.json, and never touches data/, state/, logs/ or reports/.
#
# Development happens here in ~/dev/agent-quota-maximizer (see ~/AGENTS.md);
# ~/opt/agent-quota-maximizer holds the scheduled copy, its state and its logs.
# The OS-mandated scheduler directory gets a symlink only -- the real plist lives
# in ~/opt/ with everything else the job owns.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="$HOME/opt/agent-quota-maximizer"
LABEL="com.jeanlescut.agent-quota-maximizer"
PLIST="$LABEL.plist"
AGENTS_DIR="$HOME/Library/LaunchAgents"

echo "installing into $HOME_DIR"
mkdir -p "$HOME_DIR" "$HOME_DIR/logs" "$HOME_DIR/state/locks" \
         "$HOME_DIR/state/latest" "$HOME_DIR/artifacts" "$HOME_DIR/reports"

install -m 755 "$SRC/aqm.py" "$HOME_DIR/aqm.py"
install -m 644 "$SRC/$PLIST" "$HOME_DIR/$PLIST"

if [ -f "$HOME_DIR/config.json" ]; then
    echo "keeping the existing config.json"
else
    install -m 644 "$SRC/config.json.template" "$HOME_DIR/config.json"
    echo "wrote config.json with enabled=false -- edit it, then set enabled=true"
fi

# Symlinks only, both of them: ~/Library/LaunchAgents and ~/.local/bin never hold
# a real file for a project that lives in ~/opt.
mkdir -p "$AGENTS_DIR" "$HOME/.local/bin"
ln -sfn "$HOME_DIR/$PLIST" "$AGENTS_DIR/$PLIST"
ln -sfn "$HOME_DIR/aqm.py" "$HOME/.local/bin/aqm"

# bootout then bootstrap, so a changed plist is actually picked up. bootout fails
# when the job is not loaded, which is fine on a first install.
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$AGENTS_DIR/$PLIST"
echo "scheduled: $LABEL, every 300 s"

# Prove the deployed copy runs before walking away. The pipeline is read-only
# until stages 4 and 6 exist, and `enabled: false` stops it before any stage.
/usr/bin/python3 "$HOME_DIR/aqm.py" pipeline || true
echo
echo "done. logs in $HOME_DIR/logs/, data in $HOME_DIR/data/"
