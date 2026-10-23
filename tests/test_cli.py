from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from mara_trainer.cli import app

from .fit_builder import build_fit

runner = CliRunner()
EXAMPLE = Path(__file__).parents[1] / "examples" / "sample-plan.json"


def invoke(*args: str):  # type: ignore[no-untyped-def]
    return runner.invoke(app, list(args))


def test_help_lists_commands() -> None:
    r = invoke("--help")
    assert r.exit_code == 0
    for cmd in ("init-db", "import", "summary", "validate-plan", "race", "plan", "pace"):
        assert cmd in r.output


def test_init_db_and_summary(tmp_db: Path) -> None:
    assert invoke("init-db", "--timezone", "America/Los_Angeles").exit_code == 0
    assert tmp_db.exists()
    r = invoke("summary")
    assert r.exit_code == 0
    data = json.loads(r.output)
    assert data["summary"]["run_count"] == 0
    assert data["summary"]["average_sleep_hours"] is None


def test_init_db_rejects_bad_timezone(tmp_db: Path) -> None:
    assert invoke("init-db", "--timezone", "Mars/Olympus").exit_code == 1


def test_summary_requires_init(tmp_db: Path) -> None:
    assert invoke("summary").exit_code == 1


def test_import_flow(tmp_db: Path, tmp_path: Path) -> None:
    invoke("init-db")
    start = datetime.now(UTC) - timedelta(days=1)
    f = tmp_path / "run.fit"
    f.write_bytes(build_fit([{"start": start, "meters": 8000, "seconds": 2700}]))
    dry = invoke("import", str(f), "--dry-run")
    assert dry.exit_code == 0 and json.loads(dry.output)["dry_run"] is True
    assert json.loads(invoke("summary").output)["summary"]["run_count"] == 0
    r = invoke("import", str(f))
    assert r.exit_code == 0 and json.loads(r.output)["result"]["imported"] == 1
    again = invoke("import", str(f))
    assert json.loads(again.output)["result"]["already_present"] == 1
    near = tmp_path / "near.fit"
    near.write_bytes(
        build_fit([{"start": start + timedelta(seconds=20), "meters": 8050, "seconds": 2710}])
    )
    r2 = invoke("import", str(near))
    assert r2.exit_code == 2
    key = json.loads(r2.output)["result"]["needs_confirmation"][0]
    r3 = invoke("import", str(near), "--allow-duplicate", key)
    assert r3.exit_code == 0
    s = json.loads(invoke("summary", "--weeks", "2").output)
    assert s["summary"]["run_count"] == 2 and len(s["weekly"]) == 2


def test_import_bad_file(tmp_db: Path, tmp_path: Path) -> None:
    invoke("init-db")
    f = tmp_path / "bad.fit"
    f.write_bytes(b"garbage")
    assert invoke("import", str(f)).exit_code == 1


def test_validate_sample_plan(tmp_db: Path) -> None:
    r = invoke("validate-plan", str(EXAMPLE))
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["ok"] is True


def test_validate_plan_reports_violations(tmp_db: Path, tmp_path: Path) -> None:
    raw = json.loads(EXAMPLE.read_text())
    raw["weeks"][0]["sessions"][1]["distance_meters"] = 999
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(raw))
    r = invoke("validate-plan", str(bad))
    assert r.exit_code == 3
    assert json.loads(r.output)["violations"][0]["week"] == 1


def test_race_set_brief_and_accept(tmp_db: Path, tmp_path: Path) -> None:
    invoke("init-db")
    r = invoke(
        "race",
        "set",
        "--name",
        "Test Marathon",
        "--date",
        "2027-04-25",
        "--days",
        "sun,tue,thu,sat",
        "--long-run-day",
        "sun",
        "--weekly-min",
        "25",
        "--weekly-max",
        "30",
        "--longest",
        "10",
        "--goal-time",
        "3:59:00",
    )
    assert r.exit_code == 0, r.output
    setup = json.loads(r.output)["setup"]
    assert setup["training_days"] == [0, 2, 4, 6] and setup["target_seconds"] == 14340
    brief = json.loads(invoke("plan", "brief", "--today", "2027-01-04").output)
    assert len(brief["weeks"]) == 16
    raw = json.loads(EXAMPLE.read_text())
    raw.pop("setup")  # falls back to saved race setup (same values as the sample)
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(raw))
    acc = invoke("validate-plan", str(p), "--accept")
    assert acc.exit_code == 0, acc.output
    assert json.loads(invoke("plan", "show").output)["race_date"] == "2027-04-25"


def test_race_set_rejects_invalid(tmp_db: Path) -> None:
    invoke("init-db")
    r = invoke(
        "race",
        "set",
        "--name",
        "X",
        "--date",
        "2027-04-25",
        "--days",
        "sun,tue",
        "--long-run-day",
        "sat",
        "--weekly-min",
        "25",
        "--weekly-max",
        "30",
        "--longest",
        "10",
    )
    assert r.exit_code == 1


def _race_set(*extra: str):  # type: ignore[no-untyped-def]
    return invoke(
        "race",
        "set",
        "--name",
        "Test Marathon",
        "--date",
        "2027-04-25",
        "--days",
        "sun,tue,thu,sat",
        "--long-run-day",
        "sun",
        "--weekly-min",
        "25",
        "--weekly-max",
        "30",
        "--longest",
        "10",
        *extra,
    )


def test_pace_with_explicit_race(tmp_db: Path) -> None:
    r = invoke("pace", "--distance", "10", "--unit", "km", "--time", "50:00")
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["input"]["source"] == "arguments"
    assert data["input"]["distance_meters"] == 10000
    assert data["method"]["id"] == "riegel_1981"
    assert [p["label"] for p in data["predictions"]] == ["5k", "10k", "half_marathon", "marathon"]
    assert data["predictions"][1]["time"] == "0:50:00"
    assert data["predictions"][1]["pace_per_mile"] == "8:03"
    assert data["training_zones"] is None and data["caveats"]
    assert not tmp_db.exists()  # explicit input never touches the DB


def test_pace_defaults_to_saved_recent_race(tmp_db: Path) -> None:
    invoke("init-db")
    assert (
        _race_set("--recent-race-distance", "13.1", "--recent-race-time", "1:55:00").exit_code == 0
    )
    r = invoke("pace")
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["input"]["source"] == "race_setup"
    assert data["input"]["distance_meters"] == round(13.1 * 1609.344)
    assert data["input"]["duration_seconds"] == 6900


def test_pace_errors_without_recent_race(tmp_db: Path) -> None:
    invoke("init-db")
    r = invoke("pace")
    assert r.exit_code == 1
    assert "error" in json.loads(r.output)
    _race_set()
    r2 = invoke("pace")
    assert r2.exit_code == 1
    assert "recent race" in json.loads(r2.output)["error"]


def test_pace_rejects_partial_or_bad_input(tmp_db: Path) -> None:
    assert invoke("pace", "--distance", "10").exit_code == 1
    assert invoke("pace", "--distance", "100", "--time", "9:00:00").exit_code == 1
    assert invoke("pace", "--distance", "5", "--time", "x:y", "--unit", "km").exit_code != 0
    assert invoke("pace", "--distance", "5", "--time", "25:00", "--unit", "yd").exit_code == 1
