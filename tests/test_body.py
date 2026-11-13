from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mara_trainer import body, db
from mara_trainer.body import BodyEntry, BodyError
from mara_trainer.cli import app

runner = CliRunner()

MIGRATION = "011_niggle_log.sql"


def test_migration_applies_on_fresh_db() -> None:
    c = db.connect(":memory:")
    assert MIGRATION in db.migrate(c)
    assert db.migrate(c) == []
    cols = {r[1] for r in c.execute("PRAGMA table_info(niggle_log)")}
    assert cols == {
        "id",
        "date",
        "status",
        "context",
        "location",
        "swelling",
        "pain",
        "notes",
        "created_at",
    }
    c.close()


def test_migration_applies_on_db_at_010() -> None:
    """A DB that stopped at migration 010 (with rows in it) gets only 011, data untouched."""
    c = db.connect(":memory:")
    c.execute(
        "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, "
        "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
    )
    older = [(n, sql) for n, sql in db.migration_files() if n < MIGRATION]
    assert older[-1][0].startswith("010_")
    for name, sql in older:
        c.executescript(sql)
        c.execute("INSERT INTO schema_migrations(name) VALUES (?)", (name,))
    c.execute("INSERT INTO athlete(id, name, timezone, units) VALUES (1, 'Alex', 'UTC', 'mi')")
    c.execute("INSERT INTO fuel_log(date, stomach) VALUES ('2026-11-07', 4)")
    c.execute("INSERT INTO daily_check_ins(date) VALUES ('2026-11-07')")
    c.commit()
    before = c.execute("SELECT sql FROM sqlite_master WHERE name = 'daily_check_ins'").fetchone()[0]

    assert db.migrate(c) == [MIGRATION]

    assert c.execute("SELECT count(*) FROM fuel_log").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM daily_check_ins").fetchone()[0] == 1
    after = c.execute("SELECT sql FROM sqlite_master WHERE name = 'daily_check_ins'").fetchone()[0]
    assert after == before  # daily_check_ins is not touched
    assert body.add_entry(c, BodyEntry(date="2026-11-08", status="green"))["id"] == 1
    c.close()


def test_green_needs_nothing_else_and_nulls_stay_null(conn: sqlite3.Connection) -> None:
    saved = body.add_entry(conn, BodyEntry(date="2026-11-08", status="green"))
    assert saved["status"] == "green"
    for key in ("context", "location", "swelling", "pain", "notes"):
        assert saved[key] is None
    row = conn.execute("SELECT * FROM niggle_log WHERE id = ?", (saved["id"],)).fetchone()
    for key in ("context", "location", "swelling", "pain", "notes"):
        assert row[key] is None  # stored as NULL, not 0 or ''


def test_yellow_and_red_accept_but_do_not_require_details(conn: sqlite3.Connection) -> None:
    bare = body.add_entry(conn, BodyEntry(date="2026-11-08", status="red"))
    assert bare["location"] is None and bare["pain"] is None and bare["swelling"] is None
    full = body.add_entry(
        conn,
        BodyEntry(
            date="2026-11-08",
            status="yellow",
            context="after_run",
            location="  right midfoot ",
            swelling=False,
            pain=0,
            notes="  eased off after a mile ",
        ),
    )
    assert full["location"] == "right midfoot"
    assert full["notes"] == "eased off after a mile"
    assert full["swelling"] is False  # a real "no", not missing
    assert full["pain"] == 0  # a real zero, not missing
    assert full["context"] == "after_run"
    assert body.add_entry(conn, BodyEntry(date="2026-11-08", status="red", swelling=True))[
        "swelling"
    ]


def test_status_is_stored_as_reported(conn: sqlite3.Connection) -> None:
    """No classification: a green with pain 9 and swelling stays green, a red with none stays red."""
    green = body.add_entry(
        conn, BodyEntry(date="2026-11-08", status="green", pain=9, swelling=True)
    )
    red = body.add_entry(conn, BodyEntry(date="2026-11-08", status="red", pain=0, swelling=False))
    assert green["status"] == "green" and red["status"] == "red"
    assert set(green) == {
        "id",
        "date",
        "status",
        "context",
        "location",
        "swelling",
        "pain",
        "notes",
        "created_at",
    }


def test_list_last_since_and_several_per_day(conn: sqlite3.Connection) -> None:
    a = body.add_entry(conn, BodyEntry(date="2026-11-07", status="green", context="before_run"))
    b = body.add_entry(conn, BodyEntry(date="2026-11-07", status="yellow", context="after_run"))
    c = body.add_entry(conn, BodyEntry(date="2026-11-10", status="red", pain=6))
    got = body.list_entries(conn)
    assert [e["id"] for e in got["entries"]] == [c["id"], b["id"], a["id"]]  # newest first
    assert got["count"] == 3
    assert got["by_status"] == {"green": 1, "yellow": 1, "red": 1}
    assert got["coverage"] == {"context": 2, "location": 0, "swelling": 0, "pain": 1}
    assert [e["id"] for e in body.list_entries(conn, last=1)["entries"]] == [c["id"]]
    assert body.list_entries(conn, since="2026-11-08")["count"] == 1
    with pytest.raises(BodyError):
        body.list_entries(conn, last=0)
    with pytest.raises(BodyError):
        body.list_entries(conn, since="last week")


def test_open_items(conn: sqlite3.Connection) -> None:
    body.add_entry(conn, BodyEntry(date="2026-11-01", status="red"))
    body.add_entry(conn, BodyEntry(date="2026-11-03", status="green"))
    y = body.add_entry(conn, BodyEntry(date="2026-11-03", status="yellow", location="left calf"))
    r = body.add_entry(conn, BodyEntry(date="2026-11-05", status="red"))
    late = body.add_entry(conn, BodyEntry(date="2026-11-09", status="yellow"))
    assert [e["id"] for e in body.open_items(conn, "2026-11-02")] == [y["id"], r["id"], late["id"]]
    assert [e["id"] for e in body.open_items(conn, "2026-11-02", until="2026-11-08")] == [
        y["id"],
        r["id"],
    ]
    assert body.open_items(conn, "2026-12-01") == []
    with pytest.raises(BodyError):
        body.open_items(conn, "nope")


def test_rejects_bad_input(conn: sqlite3.Connection) -> None:
    for entry in (
        BodyEntry(date="11/08/2026", status="green"),
        BodyEntry(date="2026-11-08", status="orange"),
        BodyEntry(date="2026-11-08", status="yellow", context="during_run"),
        BodyEntry(date="2026-11-08", status="yellow", pain=11),
        BodyEntry(date="2026-11-08", status="yellow", pain=-1),
        BodyEntry(date="2026-11-08", status="yellow", location="x" * (body.LOCATION_MAX + 1)),
        BodyEntry(date="2026-11-08", status="yellow", notes="x" * (body.NOTES_MAX + 1)),
    ):
        with pytest.raises(BodyError):
            body.add_entry(conn, entry)
    assert conn.execute("SELECT count(*) FROM niggle_log").fetchone()[0] == 0


@pytest.mark.parametrize(
    "columns, values",
    [
        ("date, status", "'2026-11-08', 'orange'"),
        ("date", "'2026-11-08'"),  # status is NOT NULL
        ("date, status", "'Nov 8', 'green'"),
        ("date, status, context", "'2026-11-08', 'green', 'during_run'"),
        ("date, status, swelling", "'2026-11-08', 'red', 2"),
        ("date, status, pain", "'2026-11-08', 'red', 11"),
        ("date, status, pain", "'2026-11-08', 'red', -1"),
        ("date, status, location", f"'2026-11-08', 'red', '{'x' * 201}'"),
        ("date, status, notes", f"'2026-11-08', 'red', '{'x' * 2001}'"),
    ],
)
def test_check_constraints(conn: sqlite3.Connection, columns: str, values: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"INSERT INTO niggle_log ({columns}) VALUES ({values})")


def test_delete(conn: sqlite3.Connection) -> None:
    saved = body.add_entry(conn, BodyEntry(date="2026-11-08", status="yellow"))
    assert body.delete_entry(conn, saved["id"]) is True
    assert body.delete_entry(conn, saved["id"]) is False
    assert body.get_entry(conn, saved["id"]) is None


def test_cli_log_show_delete(tmp_db: Path) -> None:
    assert runner.invoke(app, ["init-db"]).exit_code == 0
    r = runner.invoke(
        app,
        [
            "body",
            "log",
            "--status",
            "yellow",
            "--date",
            "2026-11-08",
            "--context",
            "after_run",
            "--location",
            "right midfoot",
            "--no-swelling",
            "--pain",
            "3",
            "--notes",
            "fine walking",
        ],
    )
    assert r.exit_code == 0, r.output
    entry = json.loads(r.output)["entry"]
    assert entry["status"] == "yellow" and entry["swelling"] is False and entry["pain"] == 3
    assert set(json.loads(r.output)) == {"ok", "entry"}  # no advice, no classification

    green = runner.invoke(app, ["body", "log", "--status", "green", "--date", "2026-11-09"])
    assert green.exit_code == 0, green.output
    g = json.loads(green.output)["entry"]
    assert g["swelling"] is None and g["pain"] is None and g["location"] is None

    shown = json.loads(runner.invoke(app, ["body", "show"]).output)
    assert shown["count"] == 2 and shown["entries"][0]["id"] == g["id"]
    since = json.loads(runner.invoke(app, ["body", "show", "--since", "2026-11-09"]).output)
    assert since["count"] == 1
    assert json.loads(runner.invoke(app, ["body", "show", "--last", "1"]).output)["count"] == 1

    bad = runner.invoke(app, ["body", "log", "--status", "yellow", "--pain", "11"])
    assert bad.exit_code == 1
    assert runner.invoke(app, ["body", "log", "--status", "orange"]).exit_code == 1
    assert runner.invoke(app, ["body", "show", "--since", "yesterday"]).exit_code == 1
    assert runner.invoke(app, ["body", "delete", str(entry["id"])]).exit_code == 0
    assert runner.invoke(app, ["body", "delete", str(entry["id"])]).exit_code == 1


def test_cli_log_defaults_to_athlete_local_date(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mara_trainer import reports

    assert runner.invoke(app, ["init-db", "--timezone", "Pacific/Auckland"]).exit_code == 0
    # The athlete-local date, not the machine's date, is the default.
    monkeypatch.setattr(reports, "athlete_today", lambda _: "2031-01-02")
    r = runner.invoke(app, ["body", "log", "--status", "green"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["entry"]["date"] == "2031-01-02"


def test_cli_log_rejects_empty_date(tmp_db: Path) -> None:
    assert runner.invoke(app, ["init-db"]).exit_code == 0
    r = runner.invoke(app, ["body", "log", "--status", "green", "--date", ""])
    assert r.exit_code == 1, r.output
    assert json.loads(runner.invoke(app, ["body", "show"]).output)["count"] == 0
