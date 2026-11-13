from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from mara_trainer import db


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = db.connect(":memory:")
    db.init_db(c, "America/Los_Angeles")
    yield c
    c.close()


@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "coach.db"
    monkeypatch.setenv("COACH_DB_PATH", str(path))
    return path


@pytest.fixture(autouse=True)
def no_real_fit_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Sunday check-in reads FIT files from the sync folder: point it at an empty one so no
    test ever opens the runner's real exports."""
    monkeypatch.setattr("mara_coach.slack_bot.HR_PACE_DIR", tmp_path / "no-fit-files")


_PRIVATE_ENV = (
    "COACH_LAT",
    "COACH_LON",
    "COACH_NWS_CONTACT",
    "COACH_PLACES",
    "ANTHROPIC_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_ALLOWED_USER_ID",
)


@pytest.fixture(autouse=True)
def no_real_env_or_weather_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reads the developer's `.env`, sees its values, or makes a real weather request.

    `load_env` writes straight into `os.environ`, so one unpatched call used to leave the real
    location and contact set for every later test, and the Sunday check-in tests then fetched a
    live forecast with them. Tests that need a value set it themselves; tests that need a
    response inject a fake fetch.
    """
    monkeypatch.setattr("mara_coach.cli.load_env", lambda: None)
    for key in _PRIVATE_ENV:
        monkeypatch.delenv(key, raising=False)

    def blocked(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("a test tried a real weather request; inject a fake fetch")

    monkeypatch.setattr("mara_coach.weather.http_get", blocked)
