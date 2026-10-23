"""Builds a plan that satisfies every guardrail for a given brief (test helper + sample generator)."""

from __future__ import annotations

from typing import Any

from mara_trainer.dates import parse_date, weekday_sun0
from mara_trainer.plan_rules import MARATHON_METERS, RaceSetup, ScheduleBrief


def _gap(a: int, b: int) -> int:
    return min(abs(a - b), 7 - abs(a - b))


def build_valid_plan(setup: RaceSetup, brief: ScheduleBrief) -> dict[str, Any]:
    weeks = []
    race_day = weekday_sun0(parse_date(setup.race_date))
    for wk in brief.weeks:
        if wk.phase == "race":
            sessions = [
                {
                    "day": d,
                    "kind": "race",
                    "distance_meters": MARATHON_METERS,
                    "details": "Race day. Start conservatively, fuel early.",
                }
                if d == race_day
                else {
                    "day": d,
                    "kind": "rest",
                    "distance_meters": 0,
                    "details": "Rest or very light walk; stay off your feet.",
                }
                for d in range(7)
            ]
        else:
            others = [d for d in sorted(setup.training_days) if d != setup.long_run_day]
            others = others[: brief.runs_per_week - 1]
            long_m = wk.long_run_target_meters
            remaining = wk.target_distance_meters - long_m
            n = len(others)
            split = [remaining // n] * n if n else []
            if n:
                split[-1] += remaining - sum(split)
            quality_day = None
            if wk.phase in ("build", "marathon_specific"):
                quality_day = next((d for d in others if _gap(d, setup.long_run_day) >= 2), None)
            by_day = dict(zip(others, split, strict=True))
            sessions = []
            for d in range(7):
                if d == setup.long_run_day:
                    sessions.append(
                        {
                            "day": d,
                            "kind": "long",
                            "distance_meters": long_m,
                            "details": "Long run at easy, conversational effort.",
                        }
                    )
                elif d in by_day:
                    kind = "workout" if d == quality_day else "easy"
                    details = (
                        "Warm-up, 3 x 8 min steady with 2 min jog, cool-down."
                        if kind == "workout"
                        else "Easy conversational run."
                    )
                    sessions.append(
                        {"day": d, "kind": kind, "distance_meters": by_day[d], "details": details}
                    )
                else:
                    sessions.append(
                        {"day": d, "kind": "rest", "distance_meters": 0, "details": "Rest day."}
                    )
        weeks.append({"week": wk.week, "sessions": sessions})
    return {"weeks": weeks}
