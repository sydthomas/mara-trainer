#!/bin/sh
# Stop the Slack bot LaunchAgent and remove it (logs are kept).
set -eu
LABEL="com.maratrainer.slack"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "removed $LABEL"
