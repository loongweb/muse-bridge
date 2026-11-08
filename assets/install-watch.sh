#!/usr/bin/env bash
# muse-bridge / install-watch.sh
#
# Install a wake-up job that polls the rendezvous and mirrors new mail into the
# local queue. It deliberately runs `watch --once` as a SHORT-LIVED process on a
# timer instead of holding a permanent connection: no reconnect state machine,
# no duplicate instances, and launchd/systemd re-runs it after sleep/wake.
#
# The watcher NEVER acks. It only notifies. A WorkBuddy session still has to run
# `bridge.py inbox`, handle the message, and explicitly `bridge.py ack <id>`.
#
# This script only WRITES a job file and prints the enable command. It does not
# enable, start, or modify any running service — that step stays with the user.
#
# Usage: ./install-watch.sh [config.json]
set -euo pipefail

CONFIG="${1:-$HOME/.config/muse-bridge/config.json}"
CONFIG="$(python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$CONFIG")"
HERE="$(cd "$(dirname "$0")" && pwd)"
BRIDGE="$HERE/../scripts/bridge.py"
PY="$(command -v python3)"
LOGDIR="${MUSE_BRIDGE_STATE:-$HOME/.local/state/muse-bridge}"

[ -f "$CONFIG" ] || { echo "config not found: $CONFIG" >&2; exit 2; }
[ -f "$BRIDGE" ] || { echo "bridge.py not found: $BRIDGE" >&2; exit 2; }
mkdir -p "$LOGDIR"
POLL="$(python3 -c 'import json,sys; print(int(json.load(open(sys.argv[1])).get("poll_sec",60)))' "$CONFIG")"

case "$(uname -s)" in
  Darwin)
    PLIST="${MUSE_BRIDGE_JOB_DIR:-$HOME/Library/LaunchAgents}/com.user.muse-bridge.watch.plist"
    mkdir -p "$(dirname "$PLIST")"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.muse-bridge.watch</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$BRIDGE</string>
        <string>--config</string>
        <string>$CONFIG</string>
        <string>watch</string>
        <string>--once</string>
    </array>
    <key>StartInterval</key>
    <integer>$POLL</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin</string>
        <key>MUSE_BRIDGE_STATE</key>
        <string>$LOGDIR</string>
    </dict>
    <!-- stdout is discarded on purpose: bridge.py writes its own size-capped,
         rotating bridge.log. Unbounded launchd stdout is a known failure mode. -->
    <key>StandardOutPath</key>
    <string>/dev/null</string>
    <key>StandardErrorPath</key>
    <string>/dev/null</string>
</dict>
</plist>
EOF
    echo "wrote $PLIST (every ${POLL}s, watch --once, no auto-ack)"
    echo
    echo "Enable (macOS):"
    echo "  launchctl bootstrap gui/\$(id -u) $PLIST"
    echo "Disable:"
    echo "  launchctl bootout gui/\$(id -u)/com.user.muse-bridge.watch"
    ;;
  Linux)
    UNIT="${MUSE_BRIDGE_JOB_DIR:-$HOME/.config/systemd/user}"
    mkdir -p "$UNIT"
    cat > "$UNIT/muse-bridge-watch.service" <<EOF
[Unit]
Description=muse-bridge rendezvous poll (never acks)

[Service]
Type=oneshot
Environment=MUSE_BRIDGE_STATE=$LOGDIR
ExecStart=$PY $BRIDGE --config $CONFIG watch --once
StandardOutput=null
StandardError=null
EOF
    cat > "$UNIT/muse-bridge-watch.timer" <<EOF
[Unit]
Description=Run muse-bridge watcher periodically

[Timer]
OnBootSec=30
OnUnitActiveSec=${POLL}s
Persistent=true

[Install]
WantedBy=timers.target
EOF
    echo "wrote $UNIT/muse-bridge-watch.{service,timer} (every ${POLL}s)"
    echo
    echo "Enable (Linux):"
    echo "  systemctl --user daemon-reload"
    echo "  systemctl --user enable --now muse-bridge-watch.timer"
    ;;
  *)
    echo "unsupported OS; run this manually instead:" >&2
    echo "  $PY $BRIDGE --config $CONFIG watch --once" >&2
    exit 2
    ;;
esac

echo
echo "Notifications are recorded in the auto-rotating $LOGDIR/bridge.log."
echo "The job NEVER acks: handle mail with 'inbox' then 'ack <id>' in a session."
