#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 Droplet for the coach (docs/hosting.md, runbook).
# Run as root, from root's own clone (never from the mara-owned /srv/mara/app), after the DO
# Volume is mounted at /srv/mara:
#   git clone --depth 1 https://github.com/sydthomas/mara-trainer.git /root/mara-deploy
#   bash /root/mara-deploy/deploy/linux/bootstrap.sh [REPO_URL]
# Safe to re-run: every step checks before it changes anything. The firewall is enabled only
# once Tailscale is up, so a first run over public SSH does not lock you out; run
# `tailscale up --ssh --hostname mara`, then run this again.
set -euo pipefail

REPO_URL="${1:-https://github.com/sydthomas/mara-trainer.git}"
ROOT=/srv/mara
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi
# Root only runs files only root can change (the bot user owns /srv/mara/app).
if [[ "$(stat -c %U "$HERE")" != "root" || "$(stat -c %U "$HERE/install-units.sh")" != "root" ]]; then
  echo "run this from a root-owned clone (/root/mara-deploy), not from $HERE" >&2
  exit 1
fi

# 1. The data must live on the attached volume, never on the boot disk.
if ! mountpoint -q "$ROOT"; then
  echo "$ROOT is not a mount point: attach the DO Volume and mount it there first (runbook step 3)" >&2
  exit 1
fi
if [[ "$(findmnt -n -o SOURCE "$ROOT")" == "$(findmnt -n -o SOURCE /)" ]]; then
  echo "$ROOT is on the boot disk's device; mount the DO Volume there instead" >&2
  exit 1
fi

# 2. Packages: git, restic, sqlite3 from Ubuntu; uv and Tailscale from their official installers.
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git restic sqlite3 curl ca-certificates ufw
if [[ ! -x /usr/local/bin/uv ]]; then
  curl -LsSf https://astral.sh/uv/install.sh \
    | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh
fi
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

# 3. The service user: no password, no SSH keys; reach it with `sudo -u mara`.
if ! id mara >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$ROOT/home" --create-home --shell /bin/bash mara
fi
usermod -aG systemd-journal mara  # deploy.sh reads the bot's log

# 4. Layout on the volume. secrets/ and tmp/ are 0700; nothing personal goes on the boot disk.
install -d -o mara -g mara -m 0750 "$ROOT/data" "$ROOT/exports" "$ROOT/backup" "$ROOT/cache"
install -d -o mara -g mara -m 0700 "$ROOT/secrets" "$ROOT/secrets/env" "$ROOT/secrets/restic" \
  "$ROOT/tmp"
chown mara:mara "$ROOT"
chmod 0750 "$ROOT"

# 5. Shell history: commands typed with a leading space stay out of root's history (they can
#    hold health details). Ubuntu's default .bashrc sets this; make sure.
grep -q '^HISTCONTROL=.*ignorespace\|^HISTCONTROL=ignoreboth' /root/.bashrc 2>/dev/null \
  || echo 'HISTCONTROL=ignoreboth' >> /root/.bashrc

# 6. The code, with data/ and exports/ pointing at the volume folders. The app finds FIT files
#    at <repo>/exports/coros (mara_coach.coros.EXPORTS, from the package path) and writes
#    data/ relative to the working directory, so both are symlinks.
if [[ ! -d "$ROOT/app/.git" ]]; then
  sudo -u mara git clone "$REPO_URL" "$ROOT/app"
fi
for d in data exports; do
  if [[ ! -L "$ROOT/app/$d" ]]; then
    if [[ -e "$ROOT/app/$d" ]]; then
      echo "$ROOT/app/$d exists and is not a symlink; move its contents to $ROOT/$d first" >&2
      exit 1
    fi
    sudo -u mara ln -s "$ROOT/$d" "$ROOT/app/$d"
  fi
  # The .gitignore patterns (data/, exports/) match folders, not symlinks.
  grep -qx "/$d" "$ROOT/app/.git/info/exclude" \
    || sudo -u mara sh -c "echo /$d >> $ROOT/app/.git/info/exclude"
done

# 7. systemd units, journald (memory only), sudoers; from this root-owned clone.
bash "$HERE/install-units.sh"

# 8. Firewall: nothing inbound except over Tailscale.
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow in on tailscale0 >/dev/null
if tailscale status >/dev/null 2>&1; then
  ufw --force enable
  ufw status verbose
else
  cat <<'EOF'

Tailscale is installed but not connected, so the firewall is NOT enabled yet.
  1. tailscale up --ssh --hostname mara    (sign in with the link it prints)
  2. Tailscale admin console: disable key expiry for "mara"
  3. from the laptop: ssh mara              (check it works over Tailscale; runbook step 4)
  4. run this script again: it enables ufw (deny incoming, allow tailscale0)
EOF
  exit 0
fi

# 9. Recovery: once ufw is on, the DO Droplet Console (it uses SSH) is blocked too; the
#    Recovery Console needs a root password.
if [[ "$(passwd -S root | awk '{print $2}')" != "P" ]]; then
  echo
  echo "root has no password: set one now (passwd root) and keep it in the password manager;"
  echo "the DO Recovery Console needs it if Tailscale ever fails (docs/hosting.md, Recovery)."
fi

cat <<EOF

Bootstrap done. Next (docs/hosting.md, runbook):
  - secrets: one value per file in $ROOT/secrets/env/ and $ROOT/secrets/restic/ (mode 0600, owner mara)
  - optional secrets added? re-run: bash /root/mara-deploy/deploy/linux/install-units.sh
  - move the data from the laptop: deploy/linux/move-data.sh mara
  - first deploy: sudo -u mara $ROOT/app/deploy/linux/deploy.sh
EOF
