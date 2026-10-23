"""Guardrail tests: build_schedule_brief + check/validate_schedule (the legacy code had none)."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from mara_trainer.dates import js_round, parse_date, weekday_sun0
from mara_trainer.plan_rules import (
    INVALID,
    MARATHON_METERS,
    OUT_OF_BOUNDS,
    RaceSetup,
    RecentRace,
    ScheduleValidationError,
    brief_feasibility_problems,
    build_schedule_brief,
    check_schedule,
    validate_schedule,
)

from .plan_factory import build_valid_plan

MI = 1609.344
TODAY = "2027-01-04"  # a Monday


def make_setup(**over: Any) -> RaceSetup:
    base = RaceSetup(
        race_name="Sample Spring Marathon",
        race_date="2027-04-25",  # Sunday, 16 weeks after TODAY's week
        goal="finish",
        target_seconds=None,
        training_days=(0, 2, 4, 6),
        long_run_day=0,
        weekly_distance_min_meters=round(25 * MI),
        weekly_distance_max_meters=round(30 * MI),
        longest_run_meters=round(10 * MI),
        experience="returning",
    )
    return replace(base, **over)


@pytest.fixture
def setup() -> RaceSetup:
    return make_setup()


@pytest.fixture
def brief(setup: RaceSetup):  # type: ignore[no-untyped-def]
    return build_schedule_brief(setup, TODAY, generated_at="fixed")


@pytest.fixture
def plan(setup: RaceSetup, brief) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return build_valid_plan(setup, brief)


def violations(plan: dict[str, Any], setup: RaceSetup, brief) -> list[str]:  # type: ignore[no-untyped-def]
    return [v.message for v in check_schedule(plan, setup, brief).violations]


def week_index(brief, phase: str) -> int:  # type: ignore[no-untyped-def]
    return next(i for i, w in enumerate(brief.weeks) if w.phase == phase)


def session(plan: dict[str, Any], wi: int, day: int) -> dict[str, Any]:
    return next(s for s in plan["weeks"][wi]["sessions"] if s["day"] == day)


# ------------------------------------------------------------------ js rounding / dates


def test_js_round_matches_javascript() -> None:
    assert js_round(2.5) == 3 and js_round(3.5) == 4 and js_round(-2.5) == -2


def test_weekday_convention_sunday_is_zero() -> None:
    assert weekday_sun0(parse_date("2027-04-25")) == 0
    assert weekday_sun0(parse_date("2027-01-04")) == 1


# ------------------------------------------------------------------ brief


def test_brief_week_count_and_dates(brief) -> None:  # type: ignore[no-untyped-def]
    assert len(brief.weeks) == 16
    assert brief.weeks[0].start_date == "2027-01-04"
    assert brief.weeks[-1].start_date == "2027-04-19"
    assert [w.week for w in brief.weeks] == list(range(1, 17))


def test_brief_starts_on_monday_even_midweek(setup: RaceSetup) -> None:
    b = build_schedule_brief(setup, "2027-01-07")  # Thursday
    assert b.weeks[0].start_date == "2027-01-04"


def test_brief_phases_three_week_taper_and_race_week(brief) -> None:  # type: ignore[no-untyped-def]
    phases = [w.phase for w in brief.weeks]
    assert phases[-1] == "race"
    assert phases[-3:-1] == ["taper", "taper"]
    assert phases[:4] == ["base"] * 4  # max(3, floor(16 * .25)) = 4
    assert "build" in phases and "marathon_specific" in phases
    assert phases.count("taper") == 2  # race week + 2 taper weeks = 3-week taper


def test_brief_race_week_targets(brief) -> None:  # type: ignore[no-untyped-def]
    race = brief.weeks[-1]
    assert race.target_distance_meters == race.long_run_target_meters == MARATHON_METERS
    assert race.long_run_maximum_meters == MARATHON_METERS


def test_brief_training_level_and_runs(setup: RaceSetup, brief) -> None:  # type: ignore[no-untyped-def]
    assert brief.training_level == "intermediate"
    assert brief.runs_per_week == 4  # min(4 available days, 5 preferred)


@pytest.mark.parametrize(
    ("over", "level"),
    [
        (
            {"weekly_distance_min_meters": 8000, "weekly_distance_max_meters": 12000},
            "base_building",
        ),
        ({"experience": "first_marathon"}, "novice"),
        (
            {
                "weekly_distance_min_meters": 70000,
                "weekly_distance_max_meters": 80000,
                "training_days": (0, 1, 2, 4, 6),
            },
            "advanced",
        ),
        (
            {"weekly_distance_min_meters": 70000, "weekly_distance_max_meters": 80000},
            "intermediate",
        ),
    ],
)
def test_brief_training_levels(over: dict[str, Any], level: str) -> None:
    assert build_schedule_brief(make_setup(**over), TODAY).training_level == level


def test_brief_recovery_weeks_dip(brief) -> None:  # type: ignore[no-untyped-def]
    w = brief.weeks
    # every 4th week (index 3, 7, 11) is a cutback week
    for i in (3, 7, 11):
        assert w[i].target_distance_meters < w[i - 1].target_distance_meters


def test_brief_never_exceeds_caps(setup: RaceSetup) -> None:
    big = make_setup(
        weekly_distance_min_meters=100000,
        weekly_distance_max_meters=120000,
        training_days=(0, 1, 2, 3, 4, 5, 6),
        longest_run_meters=30000,
    )
    b = build_schedule_brief(big, TODAY)
    baseline = 110000
    for w in b.weeks[:-1]:
        assert w.target_distance_meters <= baseline  # peak = max(baseline, cap) = baseline
        assert w.long_run_target_meters <= w.long_run_maximum_meters


def test_brief_long_run_bounds_are_ordered(brief) -> None:  # type: ignore[no-untyped-def]
    for w in brief.weeks:
        assert w.long_run_target_meters <= w.long_run_maximum_meters
        if w.phase == "taper":
            assert w.long_run_target_meters == w.long_run_maximum_meters


def test_brief_targets_build_from_baseline(setup: RaceSetup, brief) -> None:  # type: ignore[no-untyped-def]
    baseline = js_round((setup.weekly_distance_min_meters + setup.weekly_distance_max_meters) / 2)
    assert brief.weeks[0].target_distance_meters == baseline
    peak = max(w.target_distance_meters for w in brief.weeks[:-1])
    assert peak > baseline
    assert peak <= js_round(baseline * 1.06 ** (16 - 3))


def test_brief_race_far_away_caps_at_24_weeks(setup: RaceSetup) -> None:
    b = build_schedule_brief(replace(setup, race_date="2027-12-05"), TODAY)
    assert len(b.weeks) == 24
    assert b.weeks[-1].phase == "race"
    assert b.weeks[-1].start_date == "2027-11-29"
    assert any("more than 24 weeks" in n for n in b.notes)


def test_brief_base_builder_far_race_is_open_block() -> None:
    s = make_setup(
        race_date="2027-12-05", weekly_distance_min_meters=8000, weekly_distance_max_meters=12000
    )
    b = build_schedule_brief(s, TODAY)
    assert len(b.weeks) == 24
    assert b.weeks[0].start_date == "2027-01-04"
    assert "race" not in {w.phase for w in b.weeks}
    assert "taper" not in {w.phase for w in b.weeks}


def test_brief_race_in_past_gives_no_weeks(setup: RaceSetup) -> None:
    assert build_schedule_brief(replace(setup, race_date="2026-12-01"), TODAY).weeks == []


def test_brief_is_deterministic(setup: RaceSetup) -> None:
    a = build_schedule_brief(setup, TODAY, generated_at="x").to_dict()
    b = build_schedule_brief(setup, TODAY, generated_at="x").to_dict()
    assert a == b


# ------------------------------------------------------------------ validator: happy path


def test_valid_plan_passes(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    assert violations(plan, setup, brief) == []
    out = validate_schedule(plan, setup, brief, generated_at="fixed")
    assert out["status"] == "draft"
    assert len(out["weeks"]) == 16
    assert out["weeks"][0]["phase"] == "base"


def test_sample_plan_in_examples_passes() -> None:
    raw = json.loads((Path(__file__).parents[1] / "examples" / "sample-plan.json").read_text())
    s = RaceSetup.from_dict(raw["setup"])
    b = build_schedule_brief(s, raw["brief_date"])
    assert check_schedule(raw, s, b).ok


@pytest.mark.parametrize(
    "level_over",
    [
        {
            "weekly_distance_min_meters": 8000,
            "weekly_distance_max_meters": 12000,
            "longest_run_meters": 5000,
        },
        {"experience": "first_marathon"},
        {
            "weekly_distance_min_meters": 70000,
            "weekly_distance_max_meters": 80000,
            "training_days": (0, 1, 2, 4, 6),
        },
    ],
)
def test_factory_plans_pass_for_all_levels(level_over: dict[str, Any]) -> None:
    s = make_setup(**level_over)
    b = build_schedule_brief(s, TODAY)
    assert check_schedule(build_valid_plan(s, b), s, b).ok


# ------------------------------------------------------------------ validator: shape errors


@pytest.mark.parametrize("bad", [None, [], {"weeks": "nope"}, {}])
def test_rejects_non_plan(bad: Any, setup, brief) -> None:  # type: ignore[no-untyped-def]
    r = check_schedule(bad, setup, brief)
    assert not r.ok and r.violations[0].code == INVALID


def test_rejects_wrong_week_count(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    plan["weeks"].pop()
    r = check_schedule(plan, setup, brief)
    assert r.violations[0].code == INVALID and "16" in r.violations[0].message


def test_rejects_six_sessions(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    plan["weeks"][0]["sessions"].pop()
    assert check_schedule(plan, setup, brief).violations[0].code == INVALID


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("day", 7),
        ("day", "sun"),
        ("kind", "tempo"),
        ("distance_meters", -1),
        ("distance_meters", 5000.5),
        ("details", ""),
        ("details", "x" * 501),
        ("day", True),
    ],
)
def test_rejects_bad_session_fields(field: str, value: Any, plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    plan["weeks"][0]["sessions"][0][field] = value
    r = check_schedule(plan, setup, brief)
    assert not r.ok and r.violations[0].code == INVALID


def test_rejects_misnumbered_week(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    plan["weeks"][2]["week"] = 9
    r = check_schedule(plan, setup, brief)
    assert [v.code for v in r.violations] == [INVALID]


# ------------------------------------------------------------------ validator: training rules


def test_rejects_duplicate_days(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    plan["weeks"][0]["sessions"][1]["day"] = 0
    assert any("exactly once" in m for m in violations(plan, setup, brief))


def test_rejects_run_on_unavailable_day(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    s = session(plan, 0, 2)
    rest = session(plan, 0, 3)
    rest.update(kind="easy", distance_meters=s["distance_meters"], details="Easy")
    s.update(kind="rest", distance_meters=0)
    assert any("non-training days [3]" in m for m in violations(plan, setup, brief))


def test_rejects_wrong_run_count(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    s = session(plan, 0, 2)
    moved = s["distance_meters"]
    s.update(kind="rest", distance_meters=0)
    session(plan, 0, 4)["distance_meters"] += moved
    msgs = violations(plan, setup, brief)
    assert any("3 runs scheduled; brief requires 4" in m for m in msgs)


def test_rejects_missing_long_run(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    session(plan, 0, 0)["kind"] = "easy"
    assert any("exactly one long run" in m for m in violations(plan, setup, brief))


def test_rejects_two_long_runs(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    session(plan, 0, 4)["kind"] = "long"
    assert any("found 2" in m for m in violations(plan, setup, brief))


def test_rejects_long_run_on_wrong_day(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    a, b = session(plan, 0, 0), session(plan, 0, 6)
    a["kind"], b["kind"] = b["kind"], a["kind"]
    a["distance_meters"], b["distance_meters"] = b["distance_meters"], a["distance_meters"]
    assert any("long run must be on day 0" in m for m in violations(plan, setup, brief))


@pytest.mark.parametrize("delta", [-1, +1])
def test_long_run_bounds(delta: int, plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    wk = brief.weeks[0]
    long = session(plan, 0, 0)
    easy = session(plan, 0, 6)
    new = (wk.long_run_target_meters - 1) if delta < 0 else (wk.long_run_maximum_meters + 1)
    easy["distance_meters"] -= new - long["distance_meters"]
    long["distance_meters"] = new
    assert any("long run" in m and "outside" in m for m in violations(plan, setup, brief))


def test_long_run_at_maximum_is_ok(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    wk = brief.weeks[0]
    long, easy = session(plan, 0, 0), session(plan, 0, 6)
    extra = wk.long_run_maximum_meters - long["distance_meters"]
    long["distance_meters"] += extra
    easy["distance_meters"] -= extra
    assert violations(plan, setup, brief) == []


def test_rejects_workout_in_base_week(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    session(plan, 0, 4)["kind"] = "workout"
    assert any("no quality workouts during base" in m for m in violations(plan, setup, brief))


def test_rejects_workout_in_taper_week(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    wi = week_index(brief, "taper")
    session(plan, wi, 4)["kind"] = "workout"
    assert any("during taper" in m for m in violations(plan, setup, brief))


def test_allows_one_workout_in_build(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    wi = week_index(brief, "build")
    kinds = [s["kind"] for s in plan["weeks"][wi]["sessions"]]
    assert kinds.count("workout") == 1
    assert violations(plan, setup, brief) == []


def test_rejects_two_workouts(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    wi = week_index(brief, "build")
    for d in (2, 4):
        session(plan, wi, d)["kind"] = "workout"
    assert any("at most one quality workout" in m for m in violations(plan, setup, brief))


@pytest.mark.parametrize("day", [6, 1])
def test_rejects_workout_adjacent_to_long_run(day: int, setup: RaceSetup) -> None:
    # Make Sat (6) and Mon (1) training days so adjacency is testable (circular week).
    s = replace(setup, training_days=(0, 1, 3, 6))
    b = build_schedule_brief(s, TODAY)
    p = build_valid_plan(s, b)
    wi = week_index(b, "build")
    for sess in p["weeks"][wi]["sessions"]:
        if sess["kind"] == "workout":
            sess["kind"] = "easy"
    session(p, wi, day)["kind"] = "workout"
    msgs = [v.message for v in check_schedule(p, s, b).violations]
    assert any("between the long run and the workout" in m for m in msgs)


def test_rejects_two_strength_days(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    for d in (1, 3):
        session(plan, 0, d)["kind"] = "strength"
    assert any("strength" in m for m in violations(plan, setup, brief))


def test_one_strength_day_ok(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    session(plan, 0, 1)["kind"] = "strength"
    assert violations(plan, setup, brief) == []


def test_rejects_distance_on_rest_day(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    session(plan, 0, 1)["distance_meters"] = 500
    assert any("0 m" in m for m in violations(plan, setup, brief))


def test_rejects_tiny_run(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    a, b = session(plan, 0, 2), session(plan, 0, 4)
    b["distance_meters"] += a["distance_meters"] - 999
    a["distance_meters"] = 999
    assert any("at least 1000 m" in m for m in violations(plan, setup, brief))


@pytest.mark.parametrize(("delta", "ok"), [(100, True), (-100, True), (101, False), (-101, False)])
def test_weekly_total_tolerance(delta: int, ok: bool, plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    session(plan, 0, 2)["distance_meters"] += delta
    assert (violations(plan, setup, brief) == []) is ok


def test_rejects_race_outside_race_week(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    session(plan, 0, 1).update(kind="race", distance_meters=0)
    assert any("only allowed in race week" in m for m in violations(plan, setup, brief))


# ------------------------------------------------------------------ race week


def test_race_week_wrong_day(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    a, b = session(plan, -1, 0), session(plan, -1, 6)
    a.update(kind="rest", distance_meters=0)
    b.update(kind="race", distance_meters=MARATHON_METERS)
    assert any("race must be on day 0" in m for m in violations(plan, setup, brief))


def test_race_week_wrong_distance(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    session(plan, -1, 0)["distance_meters"] = 42000
    assert any("42195" in m for m in violations(plan, setup, brief))


def test_race_week_shakeout_run_not_allowed(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    session(plan, -1, 4).update(kind="easy", distance_meters=5000)
    assert any("must be rest" in m for m in violations(plan, setup, brief))


def test_race_week_missing_race(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    session(plan, -1, 0).update(kind="rest", distance_meters=0)
    assert any("exactly one race" in m for m in violations(plan, setup, brief))


# ------------------------------------------------------------------ reporting


def test_reports_all_violations_with_week_numbers(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    session(plan, 0, 4)["kind"] = "workout"
    session(plan, 1, 1)["distance_meters"] = 300
    r = check_schedule(plan, setup, brief)
    assert {v.week for v in r.violations} >= {1, 2}
    assert all(v.code == OUT_OF_BOUNDS for v in r.violations)


def test_validate_schedule_raises_with_code(plan, setup, brief) -> None:  # type: ignore[no-untyped-def]
    bad = copy.deepcopy(plan)
    session(bad, 0, 4)["kind"] = "workout"
    with pytest.raises(ScheduleValidationError) as exc:
        validate_schedule(bad, setup, brief)
    assert exc.value.code == OUT_OF_BOUNDS


# ------------------------------------------------------------------ race setup validation


def test_setup_roundtrip(setup: RaceSetup) -> None:
    s = replace(setup, recent_race=RecentRace(21097, 6300))
    assert RaceSetup.from_dict(s.to_dict()) == s


@pytest.mark.parametrize(
    ("over", "fragment"),
    [
        ({"training_days": (0, 0, 2)}, "unique"),
        ({"training_days": ()}, "1-7"),
        ({"long_run_day": 3}, "one of training_days"),
        ({"weekly_distance_min_meters": 50000}, "<="),
        ({"race_date": "April 25"}, "YYYY-MM-DD"),
        ({"target_seconds": 0}, "target_seconds"),
        ({"recent_race": RecentRace(500, 100)}, "recent_race.distance"),
    ],
)
def test_setup_problems(over: dict[str, Any], fragment: str) -> None:
    assert any(fragment in p for p in make_setup(**over).problems())


# ------------------------------------------------------------------ feasibility (legacy gap)


def test_feasibility_flags_long_run_exceeding_weekly_target() -> None:
    # Legacy quirk: a low weekly baseline with a long recent long run yields a brief whose
    # long-run minimum is larger than the whole weekly target, so no plan can pass.
    s = make_setup(weekly_distance_min_meters=8000, weekly_distance_max_meters=12000)
    b = build_schedule_brief(s, TODAY)
    warnings = brief_feasibility_problems(b)
    assert warnings and warnings[0].startswith("week 1:")


def test_feasibility_clean_for_normal_setup(brief) -> None:  # type: ignore[no-untyped-def]
    assert brief_feasibility_problems(brief) == []
