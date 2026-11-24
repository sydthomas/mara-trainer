"""Load secrets and settings into the environment, never overriding variables already set.
Values are never printed or logged.

1. systemd credentials (server): each file in `$CREDENTIALS_DIRECTORY` (`LoadCredential=`)
   becomes an env var named after the file, e.g. `ANTHROPIC_API_KEY`; trailing newline stripped.
2. The repo's `.env` (`KEY=value` lines; laptop).

Credentials come first, so on a server they win over a stray `.env`."""

from __future__ import annotations

import os
import re
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def load_env(path: Path = ENV_FILE) -> None:
    _load_credentials()
    _load_dotenv(path)


def _load_credentials() -> None:
    folder = os.environ.get("CREDENTIALS_DIRECTORY")
    if not folder or not Path(folder).is_dir():
        return
    for item in sorted(Path(folder).iterdir()):
        if not _NAME.fullmatch(item.name) or item.name in os.environ or not item.is_file():
            continue
        try:
            value = item.read_text()
        except (OSError, UnicodeDecodeError):
            continue  # unreadable: skip it, never mention the content
        os.environ[item.name] = value.rstrip("\r\n")


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
