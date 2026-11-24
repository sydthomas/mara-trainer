#!/usr/bin/env bash
# Deploy a version of the code and restart the bot (docs/hosting.md). Run as mara:
#   sudo -u mara /srv/mara/app/deploy/linux/deploy.sh [REF]
# REF defaults to origin/main; pass a release tag (v0.9.0) to deploy that instead. Exits
# nonzero unless the bot logs its "Slack bot running" line within 60 s of the restart.
# The only root action is `systemctl restart mara-slack.service` (sudoers rule from
# install-units.sh: mara ALL=(root) NOPASSWD: /usr/bin/systemctl restart mara-slack.service).
set -euo pipefail

if [[ "$(id -un)" != "mara" ]]; then
  echo "run as the mara user: sudo -u mara $0 $*" >&2
  exit 1
fi

# Everything runs inside main, which bash parses in full before running it: git reset below
# may rewrite this very file.
main() {
  local here app db ref current_tz since
  local -a tz_args=()
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  app=/srv/mara/app
  db=/srv/mara/data/coach.db
  ref="${1:-origin/main}"
  export UV_CACHE_DIR=/srv/mara/cache/uv
  export UV_PYTHON_INSTALL_DIR=/srv/mara/cache/python

  cd "$app"
  git fetch --tags --prune origin
  git reset --hard "$ref"
  echo "deploying $(git describe --tags --always) ($(git rev-parse --short HEAD))"

  /usr/local/bin/uv sync --frozen

  # Snapshot before migrations run (forward-only schema; CONTRIBUTING.md, guarded paths).
  if [[ -f "$db" ]]; then
    "$here/coach.sh" backup --out /srv/mara/backup/pre-deploy.db
  fi

  # init-db also sets the athlete timezone: keep the one already stored.
  if [[ -f "$db" ]]; then
    current_tz="$(sqlite3 -readonly "$db" "SELECT timezone FROM athlete WHERE id = 1" 2>/dev/null || true)"
    if [[ -n "$current_tz" ]]; then
      tz_args=(--timezone "$current_tz")
    fi
  fi
  "$here/coach.sh" init-db "${tz_args[@]}"

  since="$(date '+%Y-%m-%d %H:%M:%S')"
  sudo -n /usr/bin/systemctl restart mara-slack.service

  for _ in $(seq 1 60); do
    if journalctl -u mara-slack.service --since "$since" --no-pager -o cat \
        | grep "Slack bot running" >/dev/null; then  # no -q: under pipefail an early exit fails the pipe
      echo "bot running"
      exit 0
    fi
    sleep 1
  done
  echo "no 'Slack bot running' line within 60 s; last log lines:" >&2
  journalctl -u mara-slack.service --since "$since" --no-pager -o cat -n 30 >&2 || true
  exit 1
}

main "$@"
exit
