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
