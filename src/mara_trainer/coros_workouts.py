"""Turn one week of the accepted plan into COROS workouts (the course JSON the official COROS MCP
`createScheduledWorkout` tool takes). Deterministic: Claude reads this back to the runner and
sends it; it never writes the numbers itself.

Watch targets follow the sources (mapping and citations in `docs/plan-rules-sources.md`):
- Easy and long runs: one distance section, no pace or heart-rate alert. The sources define easy
  by effort and conversation, not numbers (knowledge/training.md).
- Tempo / threshold workouts: 1 mi warm-up, paced middle, 1 mi cool-down. Pace range from the
  Riegel 10K pace (LA Road Runners: threshold sits between 5K and 10K pace) to Riegel 5K pace
  + 35 s/mi (Chicago: tempo is 30-35 s/mi slower than 5K pace). Needs a recent race result.
- Marathon-effort workouts: same shape, middle at goal marathon pace, else Riegel marathon pace.
- Other workouts (hills, intervals, fartlek): one distance section by effort; Chicago runs hills
  by effort, not pace.
- Strength (the MCP can't create it), race day and rest are not sent.

Units: meters and seconds per km (what COROS takes); per-mile strings are for display only.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from . import paces
from .plan_rules import MARATHON_METERS, RaceSetup, RecentRace
from .reports import plan_week

MI = 1609.344
KM = 1000.0
WARMUP_METERS = 1609  # her plan's "1-2 mi easy" warm-up, lower end
COOLDOWN_METERS = 1609
TEMPO_SLOWER_THAN_5K_SEC_PER_MILE = 35  # Chicago: 30-35 s/mi slower than 5K pace
MAX_DAYS_AHEAD = 90  # COROS schedules from today through today + 90 days

# COROS course enums (createScheduledWorkout): sport, section and target types.
RUNNING = 1
WARMUP, TRAINING, COOLDOWN = 1, 2, 4
DISTANCE = 1
PACE = 2

NAMES = {
    "easy": "Easy",
    "long": "Long run",
    "tempo": "Tempo",
    "marathon": "Marathon effort",
    "other": "Workout",
}
EFFORT_NOTE = "No pace alert: run it by feel."
DEFAULT_TEXT = {
    "easy": "Easy, conversational effort.",
    "long": "Long run at easy, conversational effort.",
    "workout": "Workout: see your plan for the details.",
}


def _pace(sec_per_km: float) -> int:
    return int(sec_per_km + 0.5)


def _per_mile(sec_per_km: int) -> str:
    return paces.format_pace(sec_per_km * MI / KM)


def tempo_range(recent: RecentRace | None) -> tuple[int, int] | None:
    """(fast, slow) tempo pace in s/km from a recent race, or None without one."""
    if recent is None:
        return None
    by_label = {
        p.label: p
        for p in paces.predict(recent.distance_meters, recent.duration_seconds).predictions
    }
    ten_k = by_label["10k"].sec_per_meter * KM
    five_k_plus = (by_label["5k"].sec_per_meter * MI + TEMPO_SLOWER_THAN_5K_SEC_PER_MILE) * KM / MI
    fast, slow = sorted((ten_k, five_k_plus))
    return _pace(fast), _pace(slow)


def marathon_pace(setup: RaceSetup) -> tuple[int, str] | None:
    """Marathon pace in s/km and its basis ("goal" or "recent_race"), or None."""
    if setup.target_seconds:
        return _pace(setup.target_seconds / MARATHON_METERS * KM), "goal"
    if setup.recent_race is None:
        return None
    report = paces.predict(setup.recent_race.distance_meters, setup.recent_race.duration_seconds)
    marathon = next(p for p in report.predictions if p.label == "marathon")
    return _pace(marathon.sec_per_meter * KM), "recent_race"


def workout_type(details: str | None) -> str:
    text = (details or "").lower()
    if "tempo" in text or "threshold" in text:
        return "tempo"
    if "marathon" in text or "goal-race" in text:
        return "marathon"
    return "other"


def _miles(meters: int) -> str:
    return f"{meters / MI:.1f}".removesuffix(".0")


def _section(kind: int, meters: int, pace: tuple[int, int] | None = None) -> dict[str, Any]:
    section: dict[str, Any] = {"sectionType": kind, "targetType": DISTANCE, "targetValue": meters}
    if pace is not None:
        section |= {
            "intensityType": PACE,
            "intensityValueStart": pace[0],
            "intensityValueEnd": pace[1],
        }
    return section


def _sections(meters: int, pace: tuple[int, int] | None, structured: bool) -> list[dict[str, Any]]:
    main = meters - WARMUP_METERS - COOLDOWN_METERS
    if not structured or main < MI:
        return [_section(TRAINING, meters, pace)]
    return [
        _section(WARMUP, WARMUP_METERS),
        _section(TRAINING, main, pace),
        _section(COOLDOWN, COOLDOWN_METERS),
    ]


def _workout(session: dict[str, Any], setup: RaceSetup) -> dict[str, Any]:
    kind, meters = session["kind"], session["distance_meters"]
    details = session.get("details") or DEFAULT_TEXT[kind]
    wtype = workout_type(details) if kind == "workout" else None
    pace: tuple[int, int] | None = None
    note = EFFORT_NOTE
    if wtype == "tempo":
        pace = tempo_range(setup.recent_race)
        if pace:
            note = (
                f"Watch pace {_per_mile(pace[0])}-{_per_mile(pace[1])}/mi, from your recent race "
                "(Riegel prediction) and the LA Road Runners and Chicago tempo definitions."
            )
    elif wtype == "marathon":
        mp = marathon_pace(setup)
        if mp:
            pace = (mp[0], mp[0])
            basis = "your goal time" if mp[1] == "goal" else "your recent race (Riegel prediction)"
            note = f"Watch pace {_per_mile(mp[0])}/mi, marathon pace from {basis}."
    if pace:
        note += " Effort comes first: if it feels harder than the plan says, slow down."
    name = NAMES[wtype or kind]
    return {
        "date": session["date"],
        "coros_date": session["date"].replace("-", ""),
        "weekday": session["weekday"],
        "kind": kind,
        "workout_type": wtype,
        "miles": session["miles"],
        "target": "pace" if pace else "effort",
        "course": {
            "sportType": RUNNING,
            "courseName": f"{name} {_miles(meters)} mi",
            "courseDescription": f"{details} {note}",
            "sections": _sections(meters, pace, structured=wtype in ("tempo", "marathon")),
        },
    }


def coros_week(
    plan: dict[str, Any] | None, setup: RaceSetup, day: str, today: str
) -> dict[str, Any] | None:
    """COROS workouts for the plan week containing `day`, or None if it isn't in the plan.
    Runs dated before `today` or more than 90 days after it are skipped (COROS rejects them)."""
    week = plan_week(plan, day)
    if week is None:
        return None
    first, last = date.fromisoformat(today), date.fromisoformat(today) + timedelta(MAX_DAYS_AHEAD)
    workouts, skipped = [], []

    def skip(session: dict[str, Any], reason: str) -> None:
        skipped.append({k: session[k] for k in ("date", "weekday", "kind")} | {"reason": reason})

    for session in week["sessions"]:
        when = date.fromisoformat(session["date"])
        if session["kind"] == "rest":
            continue
        if session["kind"] == "strength":
            skip(session, "Strength: the COROS MCP can't create it; add it in the COROS app.")
        elif session["kind"] == "race":
            skip(session, "Race day: not sent as a workout; start a run on the watch.")
        elif when < first:
            skip(session, "Date is past.")
        elif when > last:
            skip(session, "More than 90 days ahead; COROS can't schedule it yet.")
        else:
            workouts.append(_workout(session, setup))
    return {
        "week": week["week"],
        "of_weeks": week["of_weeks"],
        "start_date": week["start_date"],
        "phase": week["phase"],
        "target_miles": week["target_miles"],
        "workouts": workouts,
        "skipped": skipped,
    }
