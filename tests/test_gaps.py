"""Source gaps: the private backlog of questions no approved source covers. Synthetic topics only."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mara_trainer import db, gaps
from mara_trainer.cli import app
from mara_trainer.gaps import GapError
from mara_trainer.plan_rules import RaceSetup

runner = CliRunner()
EXAMPLE = Path(__file__).parents[1] / "examples" / "sample-plan.json"
MIGRATION = "012_source_gaps.sql"
T1 = "2026-11-01T15:00:00.000Z"
T2 = "2026-11-20T09:30:00.000Z"


def test_migration_applies_and_has_the_columns() -> None:
    c = db.connect(":memory:")
    assert MIGRATION in db.migrate(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(source_gaps)")}
    assert cols == {
        "id",
        "topic",
        "summary",
        "category",
        "safety",
        "asks",
        "first_asked",
        "last_asked",
        "status",
        "closed_reason",
        "closed_at",
        "notes",
    }


def test_one_active_row_per_topic_in_sql(conn: sqlite3.Connection) -> None:
    insert = (
        "INSERT INTO source_gaps(topic, summary, category, first_asked, last_asked) "
        "VALUES ('x', 's', 'other', ?, ?)"
    )
    conn.execute(insert, (T1, T1))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (T1, T1))


def test_normalize_topic() -> None:
    assert gaps.normalize_topic("  Compression Socks!! ") == "compression-socks"
    assert gaps.normalize_topic("Foam_rolling / recovery") == "foam-rolling-recovery"
    with pytest.raises(GapError):
        gaps.normalize_topic(" ?! ")


def test_log_new_gap(conn: sqlite3.Connection) -> None:
    out = gaps.log_gap(conn, "Compression socks", "Do they help recovery?", "recovery", now=T1)
    assert out["created"] is True
    gap = out["gap"]
    assert gap["topic"] == "compression-socks"
    assert gap["asks"] == 1 and gap["status"] == "open" and gap["safety"] is False
    assert gap["first_asked"] == gap["last_asked"] == T1
    assert gap["closed_at"] is None and gap["closed_reason"] is None


def test_repeat_ask_counts_and_keeps_first_summary(conn: sqlite3.Connection) -> None:
    gaps.log_gap(conn, "compression-socks", "First wording.", "recovery", now=T1)
    out = gaps.log_gap(conn, "Compression socks", "Second wording.", "gear", True, now=T2)
    assert out["created"] is False
    gap = out["gap"]
    assert gap["asks"] == 2
    assert gap["summary"] == "First wording." and gap["category"] == "recovery"
    assert gap["first_asked"] == T1 and gap["last_asked"] == T2
    assert gap["safety"] is True  # a later ask can flag it, never unflag it
    assert len(gaps.list_gaps(conn, "all")) == 1


def test_log_validates(conn: sqlite3.Connection) -> None:
    with pytest.raises(GapError, match="category"):
        gaps.log_gap(conn, "x", "y", "magic")
    with pytest.raises(GapError, match="summary"):
        gaps.log_gap(conn, "x", "  ", "other")
    with pytest.raises(GapError, match="summary"):
        gaps.log_gap(conn, "x", "a" * 201, "other")
    with pytest.raises(GapError, match="topic"):
        gaps.log_gap(conn, "a" * 61, "y", "other")


def test_close_then_reopen_is_a_new_row(conn: sqlite3.Connection) -> None:
    gap_id = gaps.log_gap(conn, "sauna", "Sauna after runs?", "recovery", now=T1)["gap"]["id"]
    closed = gaps.close_gap(conn, gap_id, "source approved", now=T2)
    assert closed["status"] == "closed"
    assert closed["closed_reason"] == "source approved" and closed["closed_at"] == T2
    with pytest.raises(GapError, match="already closed"):
        gaps.close_gap(conn, gap_id, "again")
    with pytest.raises(GapError, match="no source gap"):
        gaps.close_gap(conn, 999, "x")
    with pytest.raises(GapError, match="reason"):
        gaps.close_gap(conn, gap_id, " ")
    again = gaps.log_gap(conn, "sauna", "Asked again.", "recovery", now=T2)
    assert again["created"] is True and again["gap"]["id"] != gap_id
    assert [g["id"] for g in gaps.list_gaps(conn, "closed")] == [gap_id]
    assert len(gaps.list_gaps(conn, "open")) == 1 and len(gaps.list_gaps(conn, "all")) == 2


def test_list_rejects_unknown_status(conn: sqlite3.Connection) -> None:
    with pytest.raises(GapError):
        gaps.list_gaps(conn, "done")


def _gap(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "topic": "t",
        "category": "other",
        "safety": False,
        "asks": 1,
        "last_asked": "2026-11-20T00:00:00.000Z",
        "status": "open",
    }
    return base | overrides


NO_RACE: dict[str, int | None] = {"days_to_race": None}


def test_score_one_recent_ask() -> None:
    assert gaps.score(_gap(), "2026-11-24", NO_RACE) == {"score": 1.0, "reasons": ["asked once"]}


def test_score_old_asks_count_half() -> None:
    out = gaps.score(_gap(asks=3, last_asked="2026-10-01T00:00:00.000Z"), "2026-11-24", NO_RACE)
    assert out["score"] == 1.5
    assert out["reasons"] == ["asked 3 times", "last asked 54 days ago, so asks count half"]


def test_score_safety_doubles_asks() -> None:
    out = gaps.score(_gap(asks=2, safety=True), "2026-11-24", NO_RACE)
    assert out == {"score": 4.0, "reasons": ["asked 2 times", "health topic"]}


def test_score_race_relevance_and_researching() -> None:
    soon: dict[str, int | None] = {"days_to_race": 20}
    out = gaps.score(_gap(category="fueling", status="researching"), "2026-11-24", soon)
    assert out["score"] == 4.0
    assert out["reasons"] == ["asked once", "race in 3 weeks", "being researched"]
    # Training topics aren't tied to the race window; a far race adds nothing.
    assert gaps.score(_gap(category="training"), "2026-11-24", soon)["score"] == 1.0
    far: dict[str, int | None] = {"days_to_race": 60}
    assert gaps.score(_gap(category="fueling"), "2026-11-24", far)["score"] == 1.0
    past: dict[str, int | None] = {"days_to_race": -1}
    assert gaps.score(_gap(category="gear"), "2026-11-24", past)["score"] == 1.0
    race_day: dict[str, int | None] = {"days_to_race": 0}
    assert "race today" in gaps.score(_gap(category="gear"), "2026-11-24", race_day)["reasons"]


def test_phase_info_reads_the_saved_race(conn: sqlite3.Connection) -> None:
    tz = "America/Los_Angeles"
    assert gaps.phase_info(conn, "2026-11-24") == {"days_to_race": None, "timezone": tz}
    setup = RaceSetup.from_dict(json.loads(EXAMPLE.read_text())["setup"])
    db.save_race_setup(conn, setup)
    info = gaps.phase_info(conn, setup.race_date)
    assert info == {"days_to_race": 0, "timezone": tz}


def test_ranked_sorts_by_score(conn: sqlite3.Connection) -> None:
    gaps.log_gap(conn, "gear-a", "One ask.", "gear", now=T2)
    gaps.log_gap(conn, "hurt-b", "Health one.", "health", True, now=T2)
    rows = gaps.ranked(conn, "open", "2026-11-24", NO_RACE)
    assert [r["topic"] for r in rows] == ["hurt-b", "gear-a"]
    assert rows[0]["score"] == 2.0 and rows[0]["reasons"] == ["asked once", "health topic"]


def test_cli_log_list_close(tmp_db: Path) -> None:
    assert runner.invoke(app, ["init-db"]).exit_code == 0
    r = runner.invoke(
        app,
        [
            "gaps",
            "log",
            "--topic",
            "Massage",
            "--summary",
            "Does massage help?",
            "--category",
            "recovery",
        ],
    )
    assert r.exit_code == 0, r.output
    logged = json.loads(r.output)
    assert logged["ok"] is True and logged["created"] is True
    gap_id = logged["gap"]["id"]
    runner.invoke(
        app,
        ["gaps", "log", "--topic", "ache-x", "--summary", "S.", "--category", "health", "--safety"],
    )
    r = runner.invoke(app, ["gaps", "list"])
    assert r.exit_code == 0, r.output
    listed = json.loads(r.output)
    assert listed["status"] == "open" and listed["count"] == 2
    assert [g["topic"] for g in listed["gaps"]] == ["ache-x", "massage"]
    assert "reasons" in listed["gaps"][0] and "score" in listed["gaps"][0]

    r = runner.invoke(app, ["gaps", "close", str(gap_id), "--reason", "source approved"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["gap"]["status"] == "closed"
    assert json.loads(runner.invoke(app, ["gaps", "list", "--status", "all"]).output)["count"] == 2
    assert json.loads(runner.invoke(app, ["gaps", "list"]).output)["count"] == 1


def test_cli_errors(tmp_db: Path) -> None:
    assert runner.invoke(app, ["init-db"]).exit_code == 0
    r = runner.invoke(app, ["gaps", "list", "--status", "done"])
    assert r.exit_code == 1 and "status" in r.output
    r = runner.invoke(app, ["gaps", "close", "5", "--reason", "x"])
    assert r.exit_code == 1 and "no source gap" in r.output
    r = runner.invoke(app, ["gaps", "log", "--topic", "x", "--summary", "y", "--category", "z"])
    assert r.exit_code == 1 and "category" in r.output


def test_same_topic_within_the_repeat_window_counts_once(conn: sqlite3.Connection) -> None:
    gaps.log_gap(conn, "Ice baths", "Do ice baths help?", "recovery", now=T1)
    soon = "2026-11-01T15:10:00.000Z"  # a follow-up turn in the same thread
    again = gaps.log_gap(conn, "ice baths", "follow-up", "recovery", now=soon)
    assert again["created"] is False and again["gap"]["asks"] == 1
    assert again["gap"]["last_asked"] == soon
    later = gaps.log_gap(conn, "ice baths", "later", "recovery", now=T2)
    assert later["gap"]["asks"] == 2


def test_log_gap_counts_across_connections(tmp_path: Path) -> None:
    """Two processes (bot + CLI) on one file: the second ask counts, no IntegrityError."""
    path = tmp_path / "race.db"
    a, b = db.connect(str(path)), db.connect(str(path))
    db.migrate(a)
    gaps.log_gap(a, "Sauna", "Sauna after runs?", "recovery", now=T1)
    out = gaps.log_gap(b, "sauna", "again", "recovery", now=T2)
    assert out["created"] is False and out["gap"]["asks"] == 2


def test_research_sets_status_and_earns_the_bonus(conn: sqlite3.Connection) -> None:
    gap = gaps.log_gap(conn, "Altitude", "Altitude tents?", "training", now=T1)["gap"]
    moved = gaps.research_gap(conn, gap["id"])
    assert moved["status"] == "researching"
    assert "being researched" in gaps.score(moved, "2026-11-02", NO_RACE)["reasons"]
    with pytest.raises(GapError):
        gaps.research_gap(conn, 999)


def test_cli_research(tmp_db: Path) -> None:
    log = ["gaps", "log", "--topic", "altitude", "--summary", "Tents?", "--category", "training"]
    gap_id = json.loads(runner.invoke(app, log).output)["gap"]["id"]
    out = json.loads(runner.invoke(app, ["gaps", "research", str(gap_id)]).output)
    assert out["ok"] is True and out["gap"]["status"] == "researching"


def test_age_uses_the_runner_local_date() -> None:
    # 2026-11-02 04:00Z is still Nov 1 in Los Angeles: asked "today", not 1 day ago or -1.
    gap = _gap(asks=1, last_asked="2026-11-02T04:00:00.000Z")
    info = {"days_to_race": None, "timezone": "America/Los_Angeles"}
    assert gaps.ask_age(gap, "2026-11-01", info) == 0
