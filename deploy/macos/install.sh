#!/bin/sh
# Run the Slack bot as a macOS LaunchAgent (hosting Option 0, docs/hosting.md): starts at login,
# restarts if it exits, and keeps the Mac awake while it runs (on power: lid open or an external
# display; a closed lid without one still sleeps). Logs: ~/Library/Logs/mara-trainer/slack.log
# Undo: deploy/macos/uninstall.sh
set -eu

LABEL="com.maratrainer.slack"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
UV="$(command -v uv)"
LOGS="$HOME/Library/Logs/mara-trainer"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

[ -f "$REPO/.env" ] || { echo "missing $REPO/.env (see docs/slack-setup.md)" >&2; exit 1; }
mkdir -p "$LOGS" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-is</string>
    <string>$UV</string><string>run</string><string>coach</string><string>slack</string><string>run</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOGS/slack.log</string>
  <key>StandardErrorPath</key><string>$LOGS/slack.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL; status: launchctl print gui/$(id -u)/$LABEL | grep state"
echo "logs: tail -f $LOGS/slack.log"
