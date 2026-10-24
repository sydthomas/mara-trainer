from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mara_coach import tools
from mara_trainer import fueling
from mara_trainer.cli import app
from mara_trainer.fueling import FuelEntry, FuelError

runner = CliRunner()


def _activity(conn: sqlite3.Connection) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO activities(source, source_id, fingerprint, title, started_at, "
            "distance_meters, duration_seconds) VALUES ('manual', 'a1', ?, 'Long run', "
            "'2026-11-07T15:00:00.000Z', 16093, 6000)",
            ("0" * 64,),
        )
    return int(cur.lastrowid or 0)


def test_carbs_per_hour() -> None:
    assert fueling.carbs_per_hour(60, 5400) == 40.0
    assert fueling.carbs_per_hour(None, 5400) is None
    assert fueling.carbs_per_hour(60, None) is None
    assert fueling.carbs_per_hour(0, 3600) == 0.0  # took nothing is a real zero


def test_add_and_list(conn: sqlite3.Connection) -> None:
    saved = fueling.add_entry(
        conn,
        FuelEntry(
            date="2026-11-07",
            duration_seconds=5400,
            pre_run="  toast and banana ",
            pre_run_minutes=90,
            during="2 gels",
            carbs_grams=50,
            stomach=4,
            energy=3,
            effort=6,
        ),
    )
    assert saved["pre_run"] == "toast and banana"
    assert saved["carbs_per_hour"] == 33.3
    assert saved["fluid_ml"] is None  # missing stays missing
    assert saved["effort"] == 6
    fueling.add_entry(conn, FuelEntry(date="2026-11-14", stomach=2, notes="side stitch"))
    got = fueling.list_entries(conn)
    assert [e["date"] for e in got["entries"]] == ["2026-11-14", "2026-11-07"]
    assert got["coverage"]["carbs_per_hour"] == 1 and got["coverage"]["stomach"] == 2
    assert got["carbs_per_hour_range"] == [33.3, 33.3]
    assert fueling.list_entries(conn, since="2026-11-10")["count"] == 1
    assert fueling.list_entries(conn, last=1)["count"] == 1


def test_activity_fills_distance_and_duration(conn: sqlite3.Connection) -> None:
    aid = _activity(conn)
    saved = fueling.add_entry(conn, FuelEntry(date="2026-11-07", activity_id=aid, carbs_grams=50))
    assert saved["distance_meters"] == 16093 and saved["duration_seconds"] == 6000
    assert saved["carbs_per_hour"] == 30.0
    with pytest.raises(FuelError, match="no activity"):
        fueling.add_entry(conn, FuelEntry(date="2026-11-07", activity_id=999, stomach=3))


def test_rejects_bad_input(conn: sqlite3.Connection) -> None:
    with pytest.raises(FuelError) as exc:
        fueling.add_entry(
            conn, FuelEntry(date="11/07/2026", stomach=6, energy=0, carbs_grams=-1, effort=11)
        )
    assert len(exc.value.problems) == 5
    with pytest.raises(FuelError, match="at least one detail"):
        fueling.add_entry(conn, FuelEntry(date="2026-11-07", notes="   "))
    with pytest.raises(FuelError):
        fueling.list_entries(conn, last=0)
    with pytest.raises(FuelError):
        fueling.list_entries(conn, since="yesterday")


def test_no_calorie_or_weight_columns(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(fuel_log)")}
    assert not {c for c in cols if any(w in c for w in ("calor", "kcal", "weight", "macro"))}


def test_delete(conn: sqlite3.Connection) -> None:
    saved = fueling.add_entry(conn, FuelEntry(date="2026-11-07", stomach=5))
    assert fueling.delete_entry(conn, saved["id"]) is True
    assert fueling.delete_entry(conn, saved["id"]) is False


def test_cli_log_show_delete(tmp_db: Path) -> None:
    r = runner.invoke(
        app,
        [
            "fuel",
            "log",
            "--date",
            "2026-11-07",
            "--distance",
            "10",
            "--duration",
            "1:40:00",
            "--during",
            "gel at 45 and 75 min",
            "--carbs",
            "50",
            "--stomach",
            "5",
        ],
    )
    assert r.exit_code == 0, r.output
    entry = json.loads(r.output)["entry"]
    assert entry["distance_meters"] == 16093 and entry["carbs_per_hour"] == 30.0
    shown = json.loads(runner.invoke(app, ["fuel", "show"]).output)
    assert shown["count"] == 1
    bad = runner.invoke(app, ["fuel", "log", "--date", "2026-11-07", "--stomach", "9"])
    assert bad.exit_code == 1
    assert runner.invoke(app, ["fuel", "delete", str(entry["id"])]).exit_code == 0
    assert runner.invoke(app, ["fuel", "delete", str(entry["id"])]).exit_code == 1


def test_fuel_log_tool_is_read_only(conn: sqlite3.Connection) -> None:
    fueling.add_entry(conn, FuelEntry(date="2026-11-07", carbs_grams=40, duration_seconds=3600))
    got = tools.call_tool(conn, "fuel_log", {"last": 5})
    assert got["count"] == 1 and got["entries"][0]["carbs_per_hour"] == 40.0
    with pytest.raises(tools.ToolError):
        tools.call_tool(conn, "fuel_log", {"last": 0})
