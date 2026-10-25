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
    anchor = (
        _iso_date(today, "today") if today else date_in_timezone(datetime.now(UTC), _timezone(conn))
    )
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


def mark_notified(conn: sqlite3.Connection, kind: str, day: str) -> bool:
    """Record a proactive message; False if one of this kind was already sent that day."""
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO notifications(kind, date) VALUES (?, ?)", (kind, day)
        )
    return cur.rowcount == 1
