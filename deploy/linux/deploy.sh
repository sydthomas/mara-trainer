#!/usr/bin/env bash
# Deploy a version of the code and restart the bot (docs/hosting.md). Run as mara:
#   ssh mara sudo -u mara /srv/mara/app/deploy/linux/deploy.sh [REF]
# REF defaults to origin/main; pass a release tag (v0.9.0) to deploy that instead.
# 1. Snapshots the DB (before any code changes) to /srv/mara/backup/pre-deploy.db.
# 2. Fetch + reset to REF, uv sync --frozen, init-db (migrations), restart, then waits up to
#    60 s for the bot's "Slack bot running" log line.
# 3. If any step in 2 fails: back to the previous commit (reset + uv sync + restart) and exit 1.
#    The DB is not rolled back automatically; the snapshot from step 1 is there if needed.
# The only root action is `systemctl restart mara-slack.service` (sudoers rule from
# install-units.sh: mara ALL=(root) NOPASSWD: /usr/bin/systemctl restart mara-slack.service).
set -euo pipefail

if [[ "$(id -un)" != "mara" ]]; then
  echo "run as the mara user: sudo -u mara $0 $*" >&2
  exit 1
fi

APP=/srv/mara/app
DB=/srv/mara/data/coach.db
SNAPSHOT=/srv/mara/backup/pre-deploy.db
UV=/usr/local/bin/uv
export UV_CACHE_DIR=/srv/mara/cache/uv
export UV_PYTHON_INSTALL_DIR=/srv/mara/cache/python
export TMPDIR=/srv/mara/tmp SQLITE_TMPDIR=/srv/mara/tmp

# Consistent copy with the sqlite3 CLI (works before the first uv sync), checked before it
# replaces the previous snapshot.
snapshot_db() {
  local tmp="$SNAPSHOT.partial" result
  rm -f "$tmp"
  sqlite3 "$DB" ".backup '$tmp'" || { rm -f "$tmp"; return 1; }
  result="$(sqlite3 -readonly "$tmp" "PRAGMA integrity_check" || true)"
  if [[ "$result" != "ok" ]]; then
    rm -f "$tmp"
    echo "pre-deploy snapshot failed its integrity check; nothing deployed" >&2
    return 1
  fi
  mv "$tmp" "$SNAPSHOT"
  echo "snapshot: $SNAPSHOT"
}

restart_and_wait() {
  local since
  since="$(date '+%Y-%m-%d %H:%M:%S')"
  sudo -n /usr/bin/systemctl restart mara-slack.service || return 1
  for _ in $(seq 1 60); do
    # no grep -q: under pipefail an early exit fails the pipe
    if journalctl -u mara-slack.service --since "$since" --no-pager -o cat \
        | grep "Slack bot running" >/dev/null; then
      echo "bot running"
      return 0
    fi
    sleep 1
  done
  echo "no 'Slack bot running' line within 60 s; last log lines:" >&2
  journalctl -u mara-slack.service --since "$since" --no-pager -o cat -n 30 >&2 || true
  return 1
}

# Each step returns on failure itself: set -e does not apply inside `if ! update`.
update() {
  local ref="$1" current_tz
  local -a tz_args=()
  git fetch --tags --prune origin || return 1
  git reset --hard "$ref" || return 1
  echo "deploying $(git describe --tags --always) ($(git rev-parse --short HEAD))"
  "$UV" sync --frozen || return 1
  # init-db also sets the athlete timezone: keep the one already stored.
  if [[ -f "$DB" ]]; then
    current_tz="$(sqlite3 -readonly "$DB" "SELECT timezone FROM athlete WHERE id = 1" 2>/dev/null || true)"
    if [[ -n "$current_tz" ]]; then
      tz_args=(--timezone "$current_tz")
    fi
  fi
  "$APP/deploy/linux/coach.sh" init-db "${tz_args[@]}" || return 1
  restart_and_wait
}

# Everything runs inside main, which bash parses in full before running it: git reset may
# rewrite this very file.
main() {
  local ref="${1:-origin/main}" previous
  cd "$APP"
  if [[ -f "$DB" ]]; then
    snapshot_db
  fi
  previous="$(git rev-parse HEAD)"
  if update "$ref"; then
    exit 0
  fi
  echo "deploy of $ref failed; rolling back to $previous" >&2
  if git reset --hard "$previous" && "$UV" sync --frozen && restart_and_wait; then
    echo "rolled back to $(git rev-parse --short HEAD); DB snapshot from before: $SNAPSHOT" >&2
  else
    echo "rollback failed too: check journalctl -u mara-slack; DB snapshot: $SNAPSHOT" >&2
  fi
  exit 1
}

main "$@"
exit
