#!/usr/bin/env bash
# Install (or refresh) the systemd units and the deploy sudoers rule. Run as root; bootstrap.sh
# runs it, and re-run it after adding or removing an optional secret (COACH_LAT, COACH_LON,
# COACH_PLACES, COACH_NWS_CONTACT) so the bot's credential list matches the files present.
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_DIR=/srv/mara/secrets/env
DROPIN_DIR=/etc/systemd/system/mara-slack.service.d

install -m 0644 "$HERE/mara-slack.service" /etc/systemd/system/mara-slack.service
install -m 0644 "$HERE/mara-backup.service" /etc/systemd/system/mara-backup.service
install -m 0644 "$HERE/mara-backup.timer" /etc/systemd/system/mara-backup.timer

# Optional settings: a LoadCredential line only for the files that exist.
mkdir -p "$DROPIN_DIR"
{
  echo "# Written by deploy/linux/install-units.sh; re-run it after changing optional secrets."
  echo "[Service]"
  for name in COACH_LAT COACH_LON COACH_PLACES COACH_NWS_CONTACT; do
    if [[ -s "$ENV_DIR/$name" ]]; then
      echo "LoadCredential=$name:$ENV_DIR/$name"
    fi
  done
} > "$DROPIN_DIR/optional-credentials.conf"

# deploy.sh (run as mara) may restart the bot and nothing else.
SUDOERS=/etc/sudoers.d/mara-deploy
echo "mara ALL=(root) NOPASSWD: /usr/bin/systemctl restart mara-slack.service" > "$SUDOERS.tmp"
chmod 0440 "$SUDOERS.tmp"
visudo -cf "$SUDOERS.tmp" >/dev/null
mv "$SUDOERS.tmp" "$SUDOERS"

systemctl daemon-reload
systemctl enable mara-slack.service mara-backup.timer >/dev/null
echo "units installed; optional credentials:"
grep '^LoadCredential=' "$DROPIN_DIR/optional-credentials.conf" | cut -d: -f1 | sed 's/^LoadCredential=/  /' || echo "  (none)"
