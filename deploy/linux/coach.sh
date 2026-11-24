#!/usr/bin/env bash
# Run `coach` on the server as the mara user with the same settings as the bot
# (docs/hosting.md). Example, from the laptop:
#   ssh mara-coach sudo -u mara /srv/mara/app/deploy/linux/coach.sh summary
# CREDENTIALS_DIRECTORY points at the secrets the systemd unit loads, so commands that need an
# API key (coach ask) work the same way as in the bot. Nothing here prints a secret.
set -euo pipefail

if [[ "$(id -un)" != "mara" ]]; then
  echo "run as the mara user: sudo -u mara $0 $*" >&2
  exit 1
fi

export COACH_DB_PATH="${COACH_DB_PATH:-/srv/mara/data/coach.db}"  # override: restore drill
export COACH_SECRET_BACKEND=file
export COACH_SECRETS_DIR=/srv/mara/secrets
export CREDENTIALS_DIRECTORY="${CREDENTIALS_DIRECTORY:-/srv/mara/secrets/env}"
export UV_CACHE_DIR=/srv/mara/cache/uv
export UV_PYTHON_INSTALL_DIR=/srv/mara/cache/python

cd /srv/mara/app
exec /usr/local/bin/uv run --no-sync coach "$@"
