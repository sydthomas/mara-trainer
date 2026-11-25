#!/usr/bin/env bash
# Install (or refresh) the systemd units, journald settings and sudoers rules. Run as root from
# root's own clone (bootstrap.sh runs it):
#   git -C /root/mara-deploy pull && bash /root/mara-deploy/deploy/linux/install-units.sh
# Re-run it after adding or removing an optional secret (COACH_LAT, COACH_LON, COACH_PLACES,
# COACH_NWS_CONTACT) so the bot's credential list matches the files present.
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_DIR=/srv/mara/secrets/env
DROPIN_DIR=/etc/systemd/system/mara-slack.service.d

# Units decide which user runs what, so root takes them only from a copy only root can change.
for f in "$HERE" "$HERE"/mara-slack.service "$HERE"/mara-backup.service "$HERE"/mara-backup.timer; do
  if [[ "$(stat -c %U "$f")" != "root" ]]; then
    echo "$f is not owned by root: run this from root's clone (/root/mara-deploy)" >&2
    exit 1
  fi
done

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

# Logs can hold health details: keep the journal in memory only (lost on reboot) and don't
# copy it to /var/log/syslog, so nothing personal is written to the boot disk.
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/mara-volatile.conf <<'EOF'
# Written by deploy/linux/install-units.sh (docs/hosting.md): logs stay off the boot disk.
[Journal]
Storage=volatile
RuntimeMaxUse=64M
ForwardToSyslog=no
EOF
systemctl restart systemd-journald
rm -rf /var/log/journal/*  # persistent logs from before this setting (no bot data yet)

# deploy.sh (run as mara) may restart the bot and nothing else. sudo does not log commands
# run by mara or root (`sudo -u mara coach.sh ...` arguments can hold health details).
SUDOERS=/etc/sudoers.d/mara-deploy
cat > "$SUDOERS.tmp" <<'EOF'
# Written by deploy/linux/install-units.sh (docs/hosting.md).
Defaults:mara !syslog, !logfile
Defaults:root !syslog, !logfile
mara ALL=(root) NOPASSWD: /usr/bin/systemctl restart mara-slack.service
EOF
chmod 0440 "$SUDOERS.tmp"
visudo -cf "$SUDOERS.tmp" >/dev/null
mv "$SUDOERS.tmp" "$SUDOERS"

systemctl daemon-reload
# The bot starts at boot, but not now (deploy.sh starts it once data and secrets are in place).
# The backup timer starts now too: enable alone would leave it idle until the next reboot.
systemctl enable mara-slack.service >/dev/null
systemctl enable --now mara-backup.timer >/dev/null
echo "units installed; optional credentials:"
grep '^LoadCredential=' "$DROPIN_DIR/optional-credentials.conf" | cut -d: -f1 | sed 's/^LoadCredential=/  /' || echo "  (none)"
