from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from mara_trainer import db
from mara_trainer.cli import app
from mara_trainer.coros_workouts import (
    COOLDOWN_METERS,
    WARMUP_METERS,
    coros_week,
    marathon_pace,
    tempo_range,
)
from mara_trainer.plan_rules import RaceSetup, RecentRace

# Synthetic athlete: 10K in 50:00, goal 4:30:00. Week 1 starts Monday 2026-11-02.
SETUP = RaceSetup(
    race_name="Synthetic Marathon",
    race_date="2027-04-25",
    goal="time",
    target_seconds=16200,
    training_days=(0, 2, 4, 6),
    long_run_day=0,
    weekly_distance_min_meters=40000,
    weekly_distance_max_meters=48000,
    longest_run_meters=16093,
    experience="first_marathon",
    recent_race=RecentRace(distance_meters=10000, duration_seconds=3000),
)
TEMPO = "Tempo: 1-2 mi easy, then 15-25 min comfortably hard (effort 7/10), easy cool-down."
MP = "Marathon effort: easy warm-up, middle miles at goal-race effort (effort 6/10)."
PLAN: dict[str, Any] = {
    "weeks": [
        {
            "week": 1,
            "start_date": "2026-11-02",
            "phase": "build",
            "sessions": [
                {"day": 1, "kind": "strength", "distance_meters": 0, "details": "Strength."},
                {"day": 2, "kind": "easy", "distance_meters": 6437, "details": "Easy (3-4/10)."},
                {"day": 3, "kind": "rest", "distance_meters": 0},
                {"day": 4, "kind": "workout", "distance_meters": 8047, "details": TEMPO},
                {"day": 5, "kind": "rest", "distance_meters": 0},
                {"day": 6, "kind": "easy", "distance_meters": 4828},
                {"day": 0, "kind": "long", "distance_meters": 16093, "details": "Long, easy."},
            ],
        }
    ]
}
INTENSITY_KEYS = {
    "intensityType",
    "sectionIntensity",
    "intensityPercentStart",
    "intensityPercentEnd",
    "intensityValueStart",
    "intensityValueEnd",
}


def _week(plan: dict[str, Any] = PLAN, setup: RaceSetup = SETUP, today: str = "2026-11-01"):  # type: ignore[no-untyped-def]
    week = coros_week(plan, setup, "2026-11-02", today)
    assert week is not None
    return week


def _by_kind(week: dict[str, Any], kind: str) -> dict[str, Any]:
    return next(w for w in week["workouts"] if w["kind"] == kind)


def _plan_with_workout(details: str, meters: int = 8047) -> dict[str, Any]:
    plan = json.loads(json.dumps(PLAN))
    for s in plan["weeks"][0]["sessions"]:
        if s["kind"] == "workout":
            s["details"], s["distance_meters"] = details, meters
    return plan


def test_tempo_range_from_riegel_10k_and_5k_plus_35s_per_mile() -> None:
    # 10K pace 300 s/km; 5K = 3000 * 0.5**1.06 s -> 287.8 s/km = 463.1 s/mi, +35 = 498.1 s/mi
    assert tempo_range(SETUP.recent_race) == (300, 310)
    assert tempo_range(None) is None


def test_marathon_pace_prefers_goal_then_riegel() -> None:
    assert marathon_pace(SETUP) == (384, "goal")  # 16200 s / 42.195 km
    no_goal = replace(SETUP, goal="finish", target_seconds=None)
    pace, basis = marathon_pace(no_goal)  # type: ignore[misc]
    assert basis == "recent_race" and 300 < pace < 400
    assert marathon_pace(replace(no_goal, recent_race=None)) is None


def test_week_lists_runs_with_coros_dates_and_skips_strength_and_rest() -> None:
    week = _week()
    assert (week["week"], week["phase"], week["start_date"]) == (1, "build", "2026-11-02")
    assert [(w["date"], w["coros_date"], w["kind"]) for w in week["workouts"]] == [
        ("2026-11-03", "20261103", "easy"),
        ("2026-11-05", "20261105", "workout"),
        ("2026-11-07", "20261107", "easy"),
        ("2026-11-08", "20261108", "long"),
    ]
    assert [(s["date"], s["kind"]) for s in week["skipped"]] == [("2026-11-02", "strength")]
    assert "COROS app" in week["skipped"][0]["reason"]


def test_easy_and_long_runs_are_one_distance_section_with_no_pace_alert() -> None:
    week = _week()
    for kind in ("easy", "long"):
        w = _by_kind(week, kind)
        assert w["target"] == "effort"
        course = w["course"]
        assert course["sportType"] == 1
        assert len(course["sections"]) == 1
        section = course["sections"][0]
        assert section["sectionType"] == 2 and section["targetType"] == 1
        assert not INTENSITY_KEYS & section.keys()
    long_run = _by_kind(week, "long")
    assert long_run["course"]["sections"][0]["targetValue"] == 16093
    assert long_run["course"]["courseName"] == "Long run 10 mi"
    assert long_run["course"]["courseDescription"].startswith("Long, easy.")
    no_details = week["workouts"][2]
    assert no_details["course"]["courseName"] == "Easy 3 mi"
    assert no_details["course"]["courseDescription"]  # never empty


def test_tempo_has_warmup_paced_middle_and_cooldown_summing_to_plan_distance() -> None:
    w = _by_kind(_week(), "workout")
    assert w["target"] == "pace" and w["workout_type"] == "tempo"
    warm, main, cool = w["course"]["sections"]
    assert (warm["sectionType"], warm["targetValue"]) == (1, WARMUP_METERS)
    assert (cool["sectionType"], cool["targetValue"]) == (4, COOLDOWN_METERS)
    assert not INTENSITY_KEYS & (warm.keys() | cool.keys())
    assert main["sectionType"] == 2 and main["targetType"] == 1
    assert (main["intensityType"], main["intensityValueStart"], main["intensityValueEnd"]) == (
        2,
        300,
        310,
    )
    assert warm["targetValue"] + main["targetValue"] + cool["targetValue"] == 8047
    assert w["course"]["courseName"] == "Tempo 5 mi"
    assert "8:03-8:19/mi" in w["course"]["courseDescription"]
    assert "slow down" in w["course"]["courseDescription"]


def test_marathon_effort_workout_uses_goal_pace() -> None:
    w = _by_kind(_week(_plan_with_workout(MP)), "workout")
    assert w["workout_type"] == "marathon" and w["target"] == "pace"
    main = w["course"]["sections"][1]
    assert (main["intensityValueStart"], main["intensityValueEnd"]) == (384, 384)
    assert "goal" in w["course"]["courseDescription"]


def test_paced_workouts_fall_back_to_effort_without_a_race_result() -> None:
    bare = replace(SETUP, goal="finish", target_seconds=None, recent_race=None)
    for details in (TEMPO, MP):
        w = _by_kind(_week(_plan_with_workout(details), bare), "workout")
        assert w["target"] == "effort"
        assert all(not INTENSITY_KEYS & s.keys() for s in w["course"]["sections"])


def test_other_workouts_and_short_tempos() -> None:
    hills = _by_kind(_week(_plan_with_workout("Hills: 6 x 60 s uphill, jog down.")), "workout")
    assert hills["workout_type"] == "other" and hills["target"] == "effort"
    assert len(hills["course"]["sections"]) == 1
    short = _by_kind(_week(_plan_with_workout(TEMPO, 4000)), "workout")
    assert short["target"] == "pace"
    (only,) = short["course"]["sections"]  # too short for warm-up + cool-down
    assert only["targetValue"] == 4000 and only["intensityValueStart"] == 300


def test_race_day_past_and_far_dates_are_skipped() -> None:
    plan = json.loads(json.dumps(PLAN))
    plan["weeks"][0]["sessions"][-1] = {"day": 0, "kind": "race", "distance_meters": 42195}
    week = _week(plan, today="2026-11-04")
    assert [w["date"] for w in week["workouts"]] == ["2026-11-05", "2026-11-07"]
    reasons = {s["date"]: s["reason"] for s in week["skipped"]}
    assert "past" in reasons["2026-11-03"]
    assert "race" in reasons["2026-11-08"].lower()
    far = coros_week(PLAN, SETUP, "2026-11-02", "2026-08-01")
    assert far is not None and far["workouts"] == []
    assert all("90 days" in s["reason"] for s in far["skipped"] if s["kind"] != "strength")


def test_week_outside_plan_is_none() -> None:
    assert coros_week(PLAN, SETUP, "2027-01-04", "2026-11-01") is None
    assert coros_week(None, SETUP, "2026-11-02", "2026-11-01") is None


def test_course_text_is_plain_and_short() -> None:
    for w in _week()["workouts"]:
        name, desc = w["course"]["courseName"], w["course"]["courseDescription"]
        assert 0 < len(name) <= 100 and desc
        assert "intensity" not in desc.lower() and "targetType" not in desc


def test_cli_coros_week(tmp_db: Path) -> None:
    runner = CliRunner()
    runner.invoke(app, ["init-db", "--timezone", "America/Los_Angeles"])
    conn = db.connect()
    db.save_race_setup(conn, SETUP)
    db.save_plan(conn, PLAN)
    conn.close()
    r = runner.invoke(app, ["coros", "week", "--date", "2026-11-02", "--today", "2026-11-01"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["week"] == 1 and len(data["workouts"]) == 4
    missing = runner.invoke(app, ["coros", "week", "--date", "2027-01-04", "--today", "2026-11-01"])
    assert missing.exit_code == 1 and "isn't in the accepted plan" in missing.output
