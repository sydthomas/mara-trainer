from __future__ import annotations

import sqlite3

import pytest

from mara_trainer import db
from mara_trainer.plan_rules import RaceSetup


def test_migrations_are_idempotent(conn: sqlite3.Connection) -> None:
    assert db.migrate(conn) == []
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "athlete",
        "activities",
        "daily_check_ins",
        "import_batches",
        "race_setup",
        "training_plans",
        "sources",
        "schema_migrations",
    } <= names
    assert not {"users", "sessions"} & names  # auth dropped


def test_single_athlete_only(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO athlete(id, timezone) VALUES (2, 'UTC')")
    assert db.athlete_timezone(conn) == "America/Los_Angeles"


def test_strava_is_not_an_allowed_source(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO activities(source, source_id, fingerprint, title, started_at, "
            "distance_meters, duration_seconds) VALUES ('strava','1',?, 't', "
            "'2026-01-01T00:00:00.000Z', 1000, 300)",
            ("0" * 64,),
        )


def test_activity_bounds(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO activities(source, source_id, fingerprint, title, started_at, "
            "distance_meters, duration_seconds, rpe) VALUES ('manual','1',?, 't', "
            "'2026-01-01T00:00:00.000Z', 1000, 300, 11)",
            ("0" * 64,),
        )


def test_race_setup_roundtrip(conn: sqlite3.Connection) -> None:
    assert db.load_race_setup(conn) is None
    s = RaceSetup("Test", "2027-04-25", "finish", None, (0, 3), 0, 30000, 40000, 15000, "returning")
    db.save_race_setup(conn, s)
    db.save_race_setup(conn, s)
    assert db.load_race_setup(conn) == s


def test_accepting_plan_archives_previous(conn: sqlite3.Connection) -> None:
    db.save_plan(conn, {"v": 1})
    db.save_plan(conn, {"v": 2})
    assert db.load_accepted_plan(conn) == {"v": 2}
    statuses = [r[0] for r in conn.execute("SELECT status FROM training_plans ORDER BY id")]
    assert statuses == ["archived", "accepted"]


def test_uninitialized_db_raises() -> None:
    c = db.connect(":memory:")
    db.migrate(c)
    with pytest.raises(LookupError):
        db.athlete_timezone(c)
