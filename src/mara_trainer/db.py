"""SQLite storage. Forward-only plain-SQL migrations in `mara_trainer/migrations/NNN_*.sql`."""

from __future__ import annotations

import json
import os
import sqlite3
from importlib import resources
from pathlib import Path
from typing import Any

from .plan_rules import RaceSetup
from .summaries import Activity, CheckIn

DEFAULT_DB = Path("data/coach.db")


def db_path() -> Path:
    return Path(os.environ.get("COACH_DB_PATH", DEFAULT_DB))


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path is not None else db_path()
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migration_files() -> list[tuple[str, str]]:
    pkg = resources.files("mara_trainer.migrations")
    files = sorted((f for f in pkg.iterdir() if f.name.endswith(".sql")), key=lambda f: f.name)
    return [(f.name, f.read_text(encoding="utf-8")) for f in files]


def migrate(conn: sqlite3.Connection) -> list[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, "
        "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
    )
    done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    applied = []
    for name, sql in migration_files():
        if name in done:
            continue
        with conn:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations(name) VALUES (?)", (name,))
        applied.append(name)
    return applied


def init_db(
    conn: sqlite3.Connection, timezone: str, name: str = "Athlete", units: str = "mi"
) -> None:
    migrate(conn)
    with conn:
        conn.execute(
            "INSERT INTO athlete(id, name, timezone, units) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET timezone=excluded.timezone",
            (name, timezone, units),
        )


def athlete_timezone(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT timezone FROM athlete WHERE id = 1").fetchone()
    if row is None:
        raise LookupError("database not initialized; run `uv run coach init-db`")
    return str(row["timezone"])


def save_race_setup(conn: sqlite3.Connection, setup: RaceSetup) -> None:
    with conn:
        conn.execute(
            "INSERT INTO race_setup(id, setup) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET "
            "setup=excluded.setup, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (json.dumps(setup.to_dict()),),
        )


def load_race_setup(conn: sqlite3.Connection) -> RaceSetup | None:
    row = conn.execute("SELECT setup FROM race_setup WHERE id = 1").fetchone()
    return RaceSetup.from_dict(json.loads(row["setup"])) if row else None


def save_plan(conn: sqlite3.Connection, plan: dict[str, Any], status: str = "accepted") -> int:
    with conn:
        if status == "accepted":
            conn.execute("UPDATE training_plans SET status='archived' WHERE status='accepted'")
        cur = conn.execute(
            "INSERT INTO training_plans(status, plan) VALUES (?, ?)", (status, json.dumps(plan))
        )
    return int(cur.lastrowid or 0)


def load_accepted_plan(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT plan FROM training_plans WHERE status='accepted' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return json.loads(row["plan"]) if row else None


def load_activities(conn: sqlite3.Connection) -> list[Activity]:
    return [
        Activity(r["started_at"], r["distance_meters"], r["duration_seconds"], r["rpe"])
        for r in conn.execute(
            "SELECT started_at, distance_meters, duration_seconds, rpe FROM activities"
        )
    ]


def load_check_ins(conn: sqlite3.Connection) -> list[CheckIn]:
    return [
        CheckIn(r["date"], r["sleep_hours"], r["hrv_ms"], r["resting_hr"])
        for r in conn.execute("SELECT date, sleep_hours, hrv_ms, resting_hr FROM daily_check_ins")
    ]
