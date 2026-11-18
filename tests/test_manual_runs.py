"""Runs logged by hand (source `manual`): validation, storage and dedup against watch imports.
Synthetic data only."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from mara_trainer import db, imports, manual_runs, reports
from mara_trainer.cli import app
from mara_trainer.imports import commit, decode_fit
from mara_trainer.manual_runs import ManualRun

from .fit_builder import build_fit

TODAY = "2026-03-04"  # athlete-local (conftest DB is America/Los_Angeles)
NOW = datetime(2026, 3, 6, 12, 0, tzinfo=UTC)
MI = 1609.344


def two_miles(day: str = TODAY, seconds: int = 1200, notes: str | None = None) -> ManualRun:
    return ManualRun(date=day, distance_meters=round(2 * MI), duration_seconds=seconds, notes=notes)


def rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM activities ORDER BY id").fetchall()


def watch_run(start: datetime, meters: float = 3330.0, seconds: float = 1250.0) -> bytes:
    return build_fit([{"start": start, "meters": meters, "seconds": seconds, "sport": "running"}])


# ---------------------------------------------------------------- validation


def test_valid_entry_has_no_problems() -> None:
    assert two_miles().problems(TODAY) == []
    assert two_miles("2026-03-01").problems(TODAY) == []  # a past day is fine


@pytest.mark.parametrize(
    ("run", "problem"),
    [
        (two_miles("2026-03-05"), "date can't be in the future"),
        (two_miles("2026-3-4"), "date must be YYYY-MM-DD"),
        (ManualRun(TODAY, 0, 1200), "distance must be"),
        (ManualRun(TODAY, 500001, 1200), "distance must be"),
        (ManualRun(TODAY, 3219, 0), "duration must be"),
        (ManualRun(TODAY, 3219, 604801), "duration must be"),
        (two_miles(notes="x" * 2001), "notes must be at most"),
    ],
)
def test_problems(run: ManualRun, problem: str) -> None:
    assert any(problem in p for p in run.problems(TODAY))


def test_draft_starts_at_local_midnight_with_a_manual_fingerprint() -> None:
    d = manual_runs.draft(two_miles(), "America/Los_Angeles")
    assert d.source == "manual"
    assert d.started_at == "2026-03-04T08:00:00.000Z"  # 00:00 PST
    assert d.fingerprint == imports.fingerprint(d.started_at, 3219, 1200)


# ---------------------------------------------------------------- saving


def test_add_saves_a_manual_activity(conn: sqlite3.Connection) -> None:
    result = manual_runs.add_run(conn, two_miles(notes=" forgot my watch "), today=TODAY)
    assert result["status"] == "saved"
    [row] = rows(conn)
    assert row["source"] == "manual" and row["source_id"] == f"manual:{row['fingerprint']}"
    assert (row["distance_meters"], row["duration_seconds"]) == (3219, 1200)
    assert row["notes"] == "forgot my watch" and row["rpe"] is None
    assert result["activity"]["id"] == row["id"]
    assert result["activity"]["date"] == TODAY
    # It counts on its own day in the summaries right away.
    week = reports.summary(conn, week=TODAY)["summary"]
    assert week["run_count"] == 1 and week["distance_meters"] == 3219


def test_add_writes_no_import_batch(conn: sqlite3.Connection) -> None:
    manual_runs.add_run(conn, two_miles(), today=TODAY)
    assert db.last_import_at(conn) is None  # "last synced" stays about the watch


def test_invalid_entry_raises_and_saves_nothing(conn: sqlite3.Connection) -> None:
    with pytest.raises(manual_runs.ManualRunError) as err:
        manual_runs.add_run(conn, two_miles("2026-03-05"), today=TODAY)
    assert "future" in str(err.value)
    assert rows(conn) == []


def test_same_entry_twice_needs_confirmation_then_both_are_kept(
    conn: sqlite3.Connection,
) -> None:
    """Two real runs with the same distance and time on one day: never a silent replay."""
    first = manual_runs.add_run(conn, two_miles(), today=TODAY)["activity"]
    again = manual_runs.add_run(conn, two_miles(), today=TODAY)
    assert again["status"] == "needs_confirmation"
    assert [d["id"] for d in again["possible_duplicates"]] == [first["id"]]
    assert len(rows(conn)) == 1
    second = manual_runs.add_run(conn, two_miles(), allow_duplicate=True, today=TODAY)
    assert second["status"] == "saved"
    a, b = rows(conn)
    assert a["started_at"] == "2026-03-04T08:00:00.000Z"
    assert b["started_at"] == "2026-03-04T08:00:01.000Z"  # 1 s later: its own fingerprint
    assert a["fingerprint"] != b["fingerprint"] and a["source_id"] != b["source_id"]
    third = manual_runs.add_run(conn, two_miles(), allow_duplicate=True, today=TODAY)
    assert third["status"] == "saved" and len(rows(conn)) == 3
    assert rows(conn)[2]["started_at"] == "2026-03-04T08:00:02.000Z"


def test_offset_skips_a_start_left_by_a_deleted_run(conn: sqlite3.Connection) -> None:
    ids = [
        manual_runs.add_run(conn, two_miles(), allow_duplicate=True, today=TODAY)["activity"]["id"]
        for _ in range(3)
    ]
    manual_runs.delete_run(conn, ids[0])  # two left, at +1 s and +2 s
    saved = manual_runs.add_run(conn, two_miles(), allow_duplicate=True, today=TODAY)
    assert saved["status"] == "saved" and len(rows(conn)) == 3


def test_another_run_that_day_needs_confirmation(conn: sqlite3.Connection) -> None:
    commit(conn, decode_fit(watch_run(datetime(2026, 3, 4, 15, 0, tzinfo=UTC)), NOW), "w.fit")
    result = manual_runs.add_run(conn, two_miles(), today=TODAY)
    assert result["status"] == "needs_confirmation"
    [dup] = result["possible_duplicates"]
    assert dup["source"] == "fit"
    assert len(rows(conn)) == 1  # nothing written
    kept = manual_runs.add_run(conn, two_miles(), allow_duplicate=True, today=TODAY)
    assert kept["status"] == "saved" and len(rows(conn)) == 2  # both kept, on her say-so


def test_runs_on_other_days_are_not_duplicates(conn: sqlite3.Connection) -> None:
    # 07:30 UTC on Mar 4 is still Mar 3 in Los Angeles; Mar 5 08:00 UTC is Mar 5 00:00 local.
    for start in (datetime(2026, 3, 4, 7, 30, tzinfo=UTC), datetime(2026, 3, 5, 8, 0, tzinfo=UTC)):
        commit(conn, decode_fit(watch_run(start), NOW), f"{start:%H}.fit")
    assert manual_runs.check(conn, two_miles(), today=TODAY)["possible_duplicates"] == []


def test_check_never_saves(conn: sqlite3.Connection) -> None:
    out = manual_runs.check(conn, two_miles(), today=TODAY)
    assert out["ok"] and out["possible_duplicates"] == [] and "exact_duplicate" not in out
    assert out["entry"] == {
        "date": TODAY,
        "distance_meters": 3219,
        "duration_seconds": 1200,
    }
    assert rows(conn) == []
    bad = manual_runs.check(conn, two_miles("2026-04-01"), today=TODAY)
    assert bad["ok"] is False and bad["problems"]


def test_delete_only_removes_manual_runs(conn: sqlite3.Connection) -> None:
    commit(conn, decode_fit(watch_run(datetime(2026, 3, 2, 15, 0, tzinfo=UTC)), NOW), "w.fit")
    saved = manual_runs.add_run(conn, two_miles(), today=TODAY)["activity"]
    watch_id = rows(conn)[0]["id"]
    assert manual_runs.delete_run(conn, watch_id) is False  # watch data is never deleted here
    assert manual_runs.delete_run(conn, saved["id"]) is True
    assert [r["source"] for r in rows(conn)] == ["fit"]


# ---------------------------------------------------------------- the watch file arrives later


def test_watch_import_after_a_manual_entry_is_flagged(conn: sqlite3.Connection) -> None:
    manual_runs.add_run(conn, two_miles(), today=TODAY)
    # The watch's copy: same local day, different distance, time and start: outside the
    # file-import tolerances, still flagged because a hand-logged run has no clock time.
    later = decode_fit(watch_run(datetime(2026, 3, 4, 16, 10, tzinfo=UTC)), NOW)
    [cand] = imports.preview(conn, later)
    assert [d["source"] for d in cand.possible_duplicates] == ["manual"]
    result = commit(conn, later, "watch.fit")
    assert result.needs_confirmation == [later.drafts[0].key] and result.imported == 0
    confirmed = commit(conn, later, "watch.fit", frozenset({later.drafts[0].key}))
    assert confirmed.imported == 1 and len(rows(conn)) == 2


def test_watch_import_on_another_day_is_not_flagged(conn: sqlite3.Connection) -> None:
    manual_runs.add_run(conn, two_miles(), today=TODAY)
    other = decode_fit(watch_run(datetime(2026, 3, 5, 16, 0, tzinfo=UTC)), NOW)
    assert imports.preview(conn, other)[0].possible_duplicates == []


def test_two_watch_runs_same_day_keep_the_file_tolerances(conn: sqlite3.Connection) -> None:
    """The same-day rule is only for hand-logged runs: watch vs watch is unchanged."""
    commit(conn, decode_fit(watch_run(datetime(2026, 3, 4, 15, 0, tzinfo=UTC)), NOW), "a.fit")
    other = decode_fit(watch_run(datetime(2026, 3, 4, 23, 0, tzinfo=UTC)), NOW)
    assert imports.preview(conn, other)[0].possible_duplicates == []


# ---------------------------------------------------------------- CLI


def run_cli(*args: str) -> tuple[int, dict[str, object]]:
    result = CliRunner().invoke(app, list(args))
    text = result.stdout if result.exit_code != 1 else result.stderr
    return result.exit_code, json.loads(text)


def test_cli_logs_a_run(tmp_db: Path) -> None:
    CliRunner().invoke(app, ["init-db"])
    today = datetime.now(UTC).astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
    code, out = run_cli("run", "log", "--date", today, "--distance", "2", "--duration", "0:20:00")
    assert code == 0 and out["status"] == "saved"
    assert out["activity"]["distance_meters"] == 3219
    args = ["run", "log", "--date", today, "--distance", "2", "--duration", "0:20:00"]
    code, again = run_cli(*args)
    assert code == 2 and again["status"] == "needs_confirmation"  # the same run again? ask
    code, kept = run_cli(*args, "--allow-duplicate")
    assert code == 0 and kept["status"] == "saved"


def test_cli_km_and_duplicates_exit_2(tmp_db: Path) -> None:
    CliRunner().invoke(app, ["init-db"])
    args = ["run", "log", "--date", "2026-03-04", "--duration", "25:00", "--unit", "km"]
    code, out = run_cli(*args, "--distance", "5", "--notes", "treadmill")
    assert code == 0 and out["activity"]["distance_meters"] == 5000
    code, dup = run_cli(*args, "--distance", "4")
    assert code == 2 and dup["status"] == "needs_confirmation" and dup["possible_duplicates"]
    code, kept = run_cli(*args, "--distance", "4", "--allow-duplicate")
    assert code == 0 and kept["status"] == "saved"
    code, gone = run_cli("run", "delete", str(kept["activity"]["id"]))  # type: ignore[index]
    assert code == 0 and gone == {"ok": True, "deleted": kept["activity"]["id"]}  # type: ignore[index]


def test_cli_refuses_future_and_bad_input(tmp_db: Path) -> None:
    CliRunner().invoke(app, ["init-db"])
    code, out = run_cli(
        "run", "log", "--date", "2999-01-01", "--distance", "2", "--duration", "20:00"
    )
    assert code == 1 and any("future" in p for p in out["problems"])  # type: ignore[union-attr]
    code, out = run_cli("run", "log", "--distance", "2", "--duration", "twenty")
    assert code == 1 and "duration" in str(out["error"])
    code, out = run_cli(
        "run", "log", "--distance", "2", "--duration", "20:00", "--unit", "furlongs"
    )
    assert code == 1
    code, out = run_cli("run", "delete", "999")
    assert code == 1
