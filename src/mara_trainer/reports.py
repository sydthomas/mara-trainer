"""Read-only reports shared by the CLI and the coach's tool layer: plan brief, training summary,
plan validation. Each returns the same JSON-ready dict the CLI prints, so every caller gets the
same numbers from one place. Errors raise `ReportError` with a message for the runner.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import db
from .dates import date_in_timezone
from .plan_rules import (
    OUT_OF_BOUNDS,
    RaceSetup,
    ScheduleBrief,
    ValidationResult,
    brief_feasibility_problems,
    build_schedule_brief,
    check_schedule,
)
from .summaries import summarize, summarize_range, week_bounds, weekly_rollups


class ReportError(ValueError):
    pass


def _timezone(conn: sqlite3.Connection) -> str:
    try:
        return db.athlete_timezone(conn)
    except LookupError as exc:
        raise ReportError(str(exc)) from exc


def athlete_today(conn: sqlite3.Connection) -> str:
    """Today's date (YYYY-MM-DD) in the athlete's timezone."""
    return date_in_timezone(datetime.now(UTC), _timezone(conn))


def _iso_date(value: str, label: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ReportError(f"{label} must be YYYY-MM-DD, got {value!r}") from exc


def plan_brief(conn: sqlite3.Connection, today: str | None = None) -> dict[str, Any]:
    """Guardrails for the saved race setup, anchored at `today` (default: athlete-local today)."""
    setup = db.load_race_setup(conn)
    if setup is None:
        raise ReportError("no race set; run `coach race set` first")
    anchor = _iso_date(today, "today") if today else athlete_today(conn)
    brief = build_schedule_brief(setup, anchor)
    return {
        "brief_date": anchor,
        "setup": setup.to_dict(),
        "feasibility_problems": brief_feasibility_problems(brief),
        **brief.to_dict(),
    }


def summary(
    conn: sqlite3.Connection,
    week: str | None = None,
    weeks: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Trailing 7 athlete-local days, or the Monday-Sunday week containing `week`
    ('current', 'last' or YYYY-MM-DD); plus `weeks` weekly rollups when asked."""
    if weeks < 0:
        raise ReportError("weeks must be 0 or more")
    tz = _timezone(conn)
    acts, checks = db.load_activities(conn), db.load_check_ins(conn)
    now = now or datetime.now(UTC)
    if week:
        today = date.fromisoformat(date_in_timezone(now, tz))
        anchor = {
            "current": today.isoformat(),
            "last": (today - timedelta(days=7)).isoformat(),
        }.get(week) or _iso_date(week, "week")
        start, end = week_bounds(anchor)
        data: dict[str, Any] = {
            "summary": summarize_range(acts, checks, tz, start, end, now).to_dict()
        }
    else:
        data = {"summary": summarize(acts, checks, tz, now).to_dict()}
    if weeks:
        data["weekly"] = [s.to_dict() for s in weekly_rollups(acts, checks, tz, weeks, now)]
    data["has_accepted_plan"] = db.load_accepted_plan(conn) is not None
    return data


def read_plan_file(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportError(f"cannot read plan JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ReportError("plan JSON must be an object with 'weeks'")
    return raw


@dataclass(frozen=True)
class PlanCheck:
    raw: dict[str, Any]
    setup: RaceSetup
    brief: ScheduleBrief
    brief_date: str
    result: ValidationResult


def check_plan(
    raw: dict[str, Any], saved_setup: RaceSetup | None, today: str | None = None
) -> PlanCheck:
    """Run the guardrails on a proposed plan. Uses the plan's own `setup` if it has one, else the
    saved race setup; anchors at `today` or the plan's `brief_date`. Never stores anything."""
    try:
        setup = RaceSetup.from_dict(raw["setup"]) if "setup" in raw else saved_setup
    except (KeyError, TypeError, ValueError) as exc:
        raise ReportError(f"invalid setup: {exc}") from exc
    if setup is None:
        raise ReportError("plan has no 'setup' and no race is set; run `coach race set` first")
    anchor = today or raw.get("brief_date")
    if not anchor:
        raise ReportError("plan needs 'brief_date' (the date its brief was built) or pass --today")
    anchor = _iso_date(str(anchor), "brief_date")
    brief = build_schedule_brief(setup, anchor)
    return PlanCheck(raw, setup, brief, anchor, check_schedule(raw, setup, brief))


def planned_session(plan: dict[str, Any] | None, day: str) -> dict[str, Any] | None:
    """The accepted plan's session on an athlete-local date, or None. Plan weeks start on Monday
    (`start_date`); session `day` is 0=Sunday ... 6=Saturday."""
    if not plan:
        return None
    target = date.fromisoformat(day)
    for week in plan.get("weeks", []):
        start = week.get("start_date")
        if not start:
            continue
        offset = (target - date.fromisoformat(start)).days
        if 0 <= offset <= 6:
            weekday = (offset + 1) % 7  # Monday start -> 0=Sunday numbering
            return next((s for s in week["sessions"] if s["day"] == weekday), None)
    return None


WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def plan_week(plan: dict[str, Any] | None, day: str) -> dict[str, Any] | None:
    """The accepted plan's Monday-Sunday week containing `day`, with a date and miles for each
    session (Monday first), or None if that week isn't in the plan."""
    if not plan:
        return None
    target = date.fromisoformat(day)
    for week in plan.get("weeks", []):
        start = week.get("start_date")
        if not start or not 0 <= (target - date.fromisoformat(start)).days <= 6:
            continue
        monday = date.fromisoformat(start)
        by_day = {s["day"]: s for s in week["sessions"]}
        sessions = []
        for offset in range(7):
            session = by_day.get((offset + 1) % 7)
            if session is None:
                continue
            sessions.append(
                {
                    "date": (monday + timedelta(days=offset)).isoformat(),
                    "weekday": WEEKDAYS[(offset + 1) % 7],
                    "kind": session["kind"],
                    "distance_meters": session["distance_meters"],
                    "miles": round(session["distance_meters"] / 1609.344, 1),
                    "details": session.get("details"),
                }
            )
        target_m = week.get("target_distance_meters")
        return {
            "week": week.get("week"),
            "of_weeks": len(plan["weeks"]),
            "start_date": start,
            "phase": week.get("phase"),
            "target_distance_meters": target_m,
            "target_miles": round(target_m / 1609.344, 1) if target_m else None,
            "sessions": sessions,
        }
    return None


def dated_plan(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    """The accepted plan with every week from `plan_week`, so readers never map day numbers."""
    if not plan:
        return None
    return {**plan, "weeks": [plan_week(plan, w["start_date"]) for w in plan["weeks"]]}


MIN_RUN_METERS = 1000  # same floor as the plan guardrails


def _cut_problems(planned: dict[int, dict[str, Any]], sessions: list[dict[str, Any]]) -> list[str]:
    """A cut (illness, a warning sign) may only shorten or rest what was planned."""
    problems = []
    for s in sessions:
        was = planned[s["day"]]
        name = WEEKDAYS[s["day"]]
        distance = s["distance_meters"]
        if was["kind"] == "race" and s != was:
            problems.append("a cut can't change the race")
        if distance > was["distance_meters"]:
            problems.append(f"a cut can't add distance on {name}")
        if s["kind"] not in (was["kind"], "rest") and (was["kind"], s["kind"]) != (
            "workout",
            "easy",
        ):
            problems.append(f"a cut can't turn {name}'s {was['kind']} into {s['kind']}")
        if s["kind"] == "rest" and distance != 0:
            problems.append(f"{name}'s rest must be 0 m")
        if s["kind"] != "rest" and distance and distance < MIN_RUN_METERS:
            problems.append(f"{name}'s run must be at least {MIN_RUN_METERS} m")
    if sum(s["distance_meters"] for s in sessions) >= sum(
        s["distance_meters"] for s in planned.values()
    ):
        problems.append("a cut must lower the week's total")
    return problems


def revise_week(
    plan: dict[str, Any] | None,
    start_date: str,
    sessions: list[dict[str, Any]],
    today: str,
    saved_setup: RaceSetup | None,
    cut: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    """The accepted plan with one week's sessions replaced, or None and the problems. Sessions
    carry dates, not day numbers. Days before `today` can't change. The week must pass the plan
    guardrails, unless `cut` is set: then it may only shorten or rest sessions. Stores nothing."""
    week = next(
        (w for w in (plan or {}).get("weeks", []) if w.get("start_date") == start_date), None
    )
    if plan is None or week is None:
        return None, [f"week of {start_date} isn't in the accepted plan"]
    monday = date.fromisoformat(start_date)
    planned = {s["day"]: s for s in week["sessions"]}
    revised_sessions = []
    for session in sessions:
        offset = (date.fromisoformat(session["date"]) - monday).days
        if not 0 <= offset <= 6:
            return None, [f"{session['date']} isn't in the week of {start_date}"]
        fields = {k: v for k, v in session.items() if k != "date"}
        revised_sessions.append({"day": (offset + 1) % 7, **fields})
    if sorted(s["day"] for s in revised_sessions) != list(range(7)):
        return None, ["each day of the week must appear exactly once"]
    for session, revised_session in zip(sessions, revised_sessions, strict=True):
        if session["date"] < today and revised_session != planned[revised_session["day"]]:
            return None, [f"{session['date']} has already passed and can't change"]
    revised = {
        **plan,
        "weeks": [{**w, "sessions": revised_sessions} if w is week else w for w in plan["weeks"]],
    }
    violations = [
        v
        for v in check_plan(revised, saved_setup).result.violations
        if v.week in (week["week"], None)
    ]
    if cut:
        problems = _cut_problems(planned, revised_sessions)
        problems += [v.message for v in violations if v.code != OUT_OF_BOUNDS]
    else:
        problems = [v.message for v in violations]
    return (None, problems) if problems else (revised, [])


def week_card(week: dict[str, Any], focus: str | None = None) -> str:
    """One week as plain text to read on a phone or paste into notes."""
    total = sum(s["distance_meters"] for s in week["sessions"]) / 1609.344
    lines = [f"Week {week['week']} of {week['of_weeks']}, {week['phase']}: {total:.1f} mi"]
    if focus:
        lines.append(f"Focus: {focus}")
    for s in week["sessions"]:
        day = date.fromisoformat(s["date"]).strftime("%a %b %-d")
        miles = f" {s['miles']:g} mi" if s["distance_meters"] else ""
        lines.append(f"{day}: {s['kind'].capitalize()}{miles}. {s['details']}")
    return "\n".join(lines)


def mark_notified(conn: sqlite3.Connection, kind: str, day: str) -> bool:
    """Record a proactive message; False if one of this kind was already sent that day."""
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO notifications(kind, date) VALUES (?, ?)", (kind, day)
        )
    return cur.rowcount == 1
