#!/usr/bin/env bash
# Move the coach's data from the laptop to the server, over Tailscale (docs/hosting.md).
# Run on the laptop, from anywhere in the repo:
#   deploy/linux/move-data.sh mara
# (`mara` = the Droplet's Tailscale MagicDNS name, User root in ~/.ssh/config; any ssh target
# that logs in as root works.)
# Stops the LaunchAgent bot (two bots would both answer), takes a consistent DB snapshot with
# `coach backup`, and copies it plus exports/ to /srv/mara/backup/incoming/ on the server.
# Deletes nothing on the laptop. Secrets (.env) are NOT copied: enter them on the server.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 mara   (ssh host of the Droplet, logging in as root)" >&2
  exit 2
fi
TARGET="$1"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LABEL="com.maratrainer.slack"
INCOMING=/srv/mara/backup/incoming

cd "$REPO"

# 1. Stop the laptop bot and disable it, so it doesn't start again at the next login next to the
#    server bot. The plist stays; the fallback is in docs/hosting.md (launchctl enable, then
#    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.maratrainer.slack.plist).
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl disable "gui/$(id -u)/$LABEL"
sleep 2
if pgrep -f "coach slack run" >/dev/null; then
  echo "a 'coach slack run' process is still running on this laptop; stop it, then re-run" >&2
  exit 1
fi
echo "laptop bot stopped"

# 2. Consistent snapshot (integrity-checked; nonzero exit stops here).
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
snapshot="data/backups/move-$stamp.db"
mkdir -p data/backups
uv run coach backup --out "$snapshot"

# 3. Copy to the volume on the server (staging folder; the runbook moves it into place).
ssh "$TARGET" "install -d -o mara -g mara -m 0700 $INCOMING"
scp -p "$snapshot" "$TARGET:$INCOMING/coach.db"
if [[ -d exports ]]; then
  scp -rp exports "$TARGET:$INCOMING/"
fi
ssh "$TARGET" "chown -R mara:mara $INCOMING"

cat <<EOF

Copied $snapshot and exports/ to $TARGET:$INCOMING/. Nothing on the laptop was deleted.
Keep the laptop bot stopped from now on: the server owns the database, and data/coach.db here
is a stale copy.

Next, on the server (ssh $TARGET):
  systemctl stop mara-slack.service
  rm -f /srv/mara/data/coach.db-journal /srv/mara/data/coach.db-wal /srv/mara/data/coach.db-shm
  install -o mara -g mara -m 0600 $INCOMING/coach.db /srv/mara/data/coach.db
  sudo -u mara cp -a $INCOMING/exports/. /srv/mara/exports/
  sudo -u mara /srv/mara/app/deploy/linux/coach.sh backup --out /srv/mara/backup/moved-check.db
  sudo -u mara /srv/mara/app/deploy/linux/coach.sh summary
  rm -rf $INCOMING
  sudo -u mara /srv/mara/app/deploy/linux/deploy.sh
EOF
