from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mara_trainer import backup, db
from mara_trainer.cli import app

runner = CliRunner()


def _make_db(path: Path) -> None:
    conn = db.connect(path)
    db.init_db(conn, "America/Los_Angeles")
    with conn:
        conn.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
        conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
    conn.close()


def test_snapshot_copies_data_and_checks_integrity(tmp_path: Path) -> None:
    src = tmp_path / "coach.db"
    _make_db(src)
    out = tmp_path / "snap" / "copy.db"
    result = backup.snapshot(src, out)
    assert result.integrity == "ok" and result.ok
    assert result.path == out and result.bytes == out.stat().st_size > 0
    copy = sqlite3.connect(out)
    assert copy.execute("SELECT count(*) FROM t").fetchone()[0] == 100
    assert copy.execute("SELECT timezone FROM athlete").fetchone()[0] == "America/Los_Angeles"
    copy.close()


def test_snapshot_into_directory_uses_timestamped_name(tmp_path: Path) -> None:
    src = tmp_path / "coach.db"
    _make_db(src)
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    result = backup.snapshot(src, out_dir)
    assert result.path.parent == out_dir
    assert result.path.name.startswith("coach-") and result.path.suffix == ".db"


def test_snapshot_sees_committed_data_while_source_is_open(tmp_path: Path) -> None:
    src = tmp_path / "coach.db"
    _make_db(src)
    writer = sqlite3.connect(src)
    writer.execute("INSERT INTO t VALUES (999)")
    writer.commit()
    writer.execute("BEGIN")
    writer.execute("INSERT INTO t VALUES (1000)")  # uncommitted: must not appear in the copy
    result = backup.snapshot(src, tmp_path / "copy.db")
    writer.rollback()
    writer.close()
    copy = sqlite3.connect(result.path)
    values = {r[0] for r in copy.execute("SELECT x FROM t")}
    copy.close()
    assert 999 in values and 1000 not in values


def test_snapshot_refuses_missing_source(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        backup.snapshot(tmp_path / "nope.db", tmp_path / "copy.db")
    assert not (tmp_path / "nope.db").exists()  # never creates an empty DB


def test_snapshot_refuses_to_overwrite_source(tmp_path: Path) -> None:
    src = tmp_path / "coach.db"
    _make_db(src)
    with pytest.raises(ValueError):
        backup.snapshot(src, src)


def test_cli_backup_prints_json(tmp_db: Path, tmp_path: Path) -> None:
    _make_db(tmp_db)
    out = tmp_path / "out.db"
    r = runner.invoke(app, ["backup", "--out", str(out)])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data == {"path": str(out), "bytes": out.stat().st_size, "integrity": "ok"}


def test_cli_backup_fails_when_integrity_check_fails(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_db(tmp_db)
    monkeypatch.setattr(backup, "integrity_check", lambda path: "*** in database main ***")
    r = runner.invoke(app, ["backup", "--out", str(tmp_path / "out.db")])
    assert r.exit_code == 3
    assert json.loads(r.stdout)["integrity"] != "ok"


def test_cli_backup_missing_db(tmp_db: Path, tmp_path: Path) -> None:
    r = runner.invoke(app, ["backup", "--out", str(tmp_path / "out.db")])
    assert r.exit_code == 1
    assert not tmp_db.exists()


def test_failed_check_keeps_previous_good_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "coach.db"
    _make_db(src)
    out = tmp_path / "copy.db"
    backup.snapshot(src, out)
    good = out.read_bytes()
    conn = sqlite3.connect(src)
    with conn:
        conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(5000)])
    conn.close()
    monkeypatch.setattr(backup, "integrity_check", lambda path: "row 3 missing from index")
    result = backup.snapshot(src, out)
    assert not result.ok and result.integrity == "row 3 missing from index"
    assert out.read_bytes() == good  # never replaced by a copy that failed the check
    assert not list(tmp_path.glob(".*.partial"))


def test_error_during_copy_removes_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "coach.db"
    _make_db(src)

    def boom(path: Path) -> str:
        raise sqlite3.DatabaseError("disk I/O error")

    monkeypatch.setattr(backup, "integrity_check", boom)
    with pytest.raises(sqlite3.DatabaseError):
        backup.snapshot(src, tmp_path / "copy.db")
    assert not (tmp_path / "copy.db").exists()
    assert not list(tmp_path.glob(".*.partial"))
