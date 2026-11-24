#!/usr/bin/env bash
# Backups to a private Backblaze B2 bucket with restic (docs/hosting.md). Runs as mara.
#   backup.sh              nightly run (mara-backup.timer): DB snapshot, restic backup of the
#                          snapshot + /srv/mara/exports, forget/prune; Sundays also restic check
#   backup.sh init         create the restic repository once
#   backup.sh restic ARGS  any restic command with the repository settings loaded, for example
#                          `backup.sh restic snapshots` or a restore (see the restore drill)
# Settings, one value per file in /srv/mara/secrets/restic/ (mode 0600, owned by mara):
#   repository       b2:<bucket>:mara    (restic repository string)
#   password         restic repository password (keep a copy in a password manager: without
#                    it the backups cannot be read)
#   b2-account-id    B2 application key ID   (key limited to that one bucket)
#   b2-account-key   B2 application key
# Values are passed to restic through files or the environment and never printed.
set -euo pipefail

if [[ "$(id -un)" != "mara" ]]; then
  echo "run as the mara user: sudo -u mara $0 $*" >&2
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRETS=/srv/mara/secrets/restic
SNAPSHOT_DIR=/srv/mara/backup
SNAPSHOT="$SNAPSHOT_DIR/coach.db"
EXPORTS=/srv/mara/exports

for name in repository password b2-account-id b2-account-key; do
  if [[ ! -s "$SECRETS/$name" ]]; then
    echo "missing $SECRETS/$name (see docs/hosting.md, Backups)" >&2
    exit 1
  fi
done
export RESTIC_REPOSITORY_FILE="$SECRETS/repository"
export RESTIC_PASSWORD_FILE="$SECRETS/password"
B2_ACCOUNT_ID="$(<"$SECRETS/b2-account-id")"
B2_ACCOUNT_KEY="$(<"$SECRETS/b2-account-key")"
export B2_ACCOUNT_ID B2_ACCOUNT_KEY
export RESTIC_CACHE_DIR=/srv/mara/cache/restic

case "${1:-}" in
  init)
    exec restic init
    ;;
  restic)
    shift
    exec restic "$@"
    ;;
  "")
    ;;
  *)
    echo "usage: $0 [init | restic ARGS...]" >&2
    exit 2
    ;;
esac

mkdir -p "$SNAPSHOT_DIR"
# coach backup exits nonzero if the integrity check fails; set -e then stops before upload.
"$HERE/coach.sh" backup --out "$SNAPSHOT"

restic backup --tag mara --host mara-coach "$SNAPSHOT" "$EXPORTS"
restic forget --tag mara --host mara-coach \
  --keep-daily 7 --keep-weekly 8 --keep-monthly 12 --prune

# Weekly: verify the repository structure (Sunday, date +%u = 7).
if [[ "$(date +%u)" == "7" ]]; then
  restic check
fi
echo "backup done: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
