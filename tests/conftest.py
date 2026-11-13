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
