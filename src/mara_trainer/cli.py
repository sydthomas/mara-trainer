"""`coach` command-line interface. Every command prints JSON on stdout so Claude can read it."""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import typer

from . import db, imports, paces
from .dates import date_in_timezone, parse_day
from .plan_rules import (
    RaceSetup,
    RecentRace,
    ScheduleValidationError,
    brief_feasibility_problems,
    build_schedule_brief,
    check_schedule,
    validate_schedule,
)
from .summaries import summarize, summarize_range, week_bounds, weekly_rollups

app = typer.Typer(
    help="Personal marathon coach CLI (local SQLite, JSON output).", no_args_is_help=True
)
race_app = typer.Typer(help="Race setup (goal race, availability, baseline).", no_args_is_help=True)
plan_app = typer.Typer(help="Plan guardrails and accepted plans.", no_args_is_help=True)
app.add_typer(race_app, name="race")
app.add_typer(plan_app, name="plan")

KM = 1000.0
MI = 1609.344


def _out(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


def _fail(message: str, code: int = 1, **extra: Any) -> NoReturn:
    typer.echo(json.dumps({"error": message, **extra}, indent=2, default=str), err=True)
    raise typer.Exit(code)


def _conn() -> Any:
    conn = db.connect()
    db.migrate(conn)
    return conn


def _parse_hms(value: str) -> int:
    parts = [int(p) for p in value.split(":")]
    if not 1 <= len(parts) <= 3:
        raise typer.BadParameter(f"expected H:MM:SS, got {value!r}")
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


@app.command("init-db")
def init_db(
    timezone: Annotated[str, typer.Option(help="Athlete IANA timezone")] = "America/Los_Angeles",
    name: Annotated[str, typer.Option(help="Display name")] = "Athlete",
    units: Annotated[str, typer.Option(help="km or mi")] = "mi",
) -> None:
    """Create/upgrade data/coach.db (or $COACH_DB_PATH) and set the athlete timezone."""
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        _fail(f"unknown IANA timezone: {timezone}")
    conn = db.connect()
    db.init_db(conn, timezone, name, units)
    _out({"ok": True, "db": str(db.db_path()), "timezone": timezone})


@app.command("import")
def import_cmd(
    file: Annotated[
        Path,
        typer.Argument(
            exists=True, dir_okay=False, help=".fit, .gpx, Apple Health export.xml/.zip"
        ),
    ],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate and preview only")] = False,
    allow_duplicate: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-duplicate", help="Session key confirmed as NOT a duplicate (repeatable)"
        ),
    ] = None,
) -> None:
    """Validate a workout file, flag duplicates, and import it (all-or-nothing)."""
    conn = _conn()
    try:
        tz = db.athlete_timezone(conn)
    except LookupError as exc:
        _fail(str(exc))
    try:
        decoded = imports.decode_file(file, tz)
    except imports.ImportError_ as exc:
        _fail(str(exc), code=1, code_name=exc.code)
    candidates = imports.preview(conn, decoded)
    report: dict[str, Any] = {
        "file": file.name,
        "source": decoded.source,
        "warnings": decoded.warnings,
        "sessions": [c.to_dict() for c in candidates],
        "check_in_days": len(decoded.check_ins),
    }
    if dry_run:
        report["dry_run"] = True
        _out(report)
        return
    result = imports.commit(conn, decoded, file.name, frozenset(allow_duplicate or []))
    report["result"] = result.__dict__
    if result.needs_confirmation:
        report["message"] = (
            "Possible duplicates found; nothing was imported. Confirm with the user, then re-run "
            "with --allow-duplicate KEY for each session that is genuinely a different run."
        )
        typer.echo(json.dumps(report, indent=2, default=str))
        raise typer.Exit(2)
    _out(report)


@app.command("summary")
def summary_cmd(
    week: Annotated[
        str | None,
        typer.Option(
            "--week",
            help="Monday-Sunday week containing this date (YYYY-MM-DD or 'current'/'last')",
        ),
    ] = None,
    weeks: Annotated[int, typer.Option(help="Also return N weekly rollups (oldest first)")] = 0,
) -> None:
    """Training summary. Default: trailing 7 athlete-local days. Missing data stays null."""
    conn = _conn()
    try:
        tz = db.athlete_timezone(conn)
    except LookupError as exc:
        _fail(str(exc))
    acts, checks = db.load_activities(conn), db.load_check_ins(conn)
    now = datetime.now(UTC)
    if week:
        today = date_in_timezone(now, tz)
        anchor = {"current": today, "last": (date.fromisoformat(today).toordinal() - 7)}.get(
            week, week
        )
        if isinstance(anchor, int):
            anchor = date.fromordinal(anchor).isoformat()
        start, end = week_bounds(str(anchor))
        data: dict[str, Any] = {
            "summary": summarize_range(acts, checks, tz, start, end, now).to_dict()
        }
    else:
        data = {"summary": summarize(acts, checks, tz, now).to_dict()}
    if weeks:
        data["weekly"] = [s.to_dict() for s in weekly_rollups(acts, checks, tz, weeks, now)]
    plan = db.load_accepted_plan(conn)
    data["has_accepted_plan"] = plan is not None
    _out(data)


def _load_plan_inputs(plan_file: Path, today: str | None) -> tuple[dict[str, Any], RaceSetup, str]:
    try:
        raw = json.loads(plan_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"cannot read plan JSON: {exc}")
    if not isinstance(raw, dict):
        _fail("plan JSON must be an object with 'weeks'")
    try:
        if "setup" in raw:
            setup = RaceSetup.from_dict(raw["setup"])
        else:
            loaded = db.load_race_setup(_conn())
            if loaded is None:
                _fail("plan has no 'setup' and no race is set; run `coach race set` first")
            setup = loaded
    except (KeyError, TypeError, ValueError) as exc:
        _fail(f"invalid setup: {exc}")
    anchor = today or raw.get("brief_date")
    if not anchor:
        _fail("plan needs 'brief_date' (the date its brief was built) or pass --today")
    return raw, setup, str(anchor)


@app.command("validate-plan")
def validate_plan(
    plan_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    today: Annotated[str | None, typer.Option(help="Override brief_date (YYYY-MM-DD)")] = None,
    accept: Annotated[
        bool, typer.Option("--accept", help="Store as the accepted plan if valid")
    ] = False,
) -> None:
    """Run the deterministic guardrails on a proposed plan. Exit 0 = pass, 3 = violations."""
    raw, setup, anchor = _load_plan_inputs(plan_file, today)
    brief = build_schedule_brief(setup, anchor)
    result = check_schedule(raw, setup, brief)
    if not result.ok:
        typer.echo(json.dumps(result.to_dict(), indent=2))
        raise typer.Exit(3)
    if accept:
        try:
            schedule = validate_schedule(raw, setup, brief)
        except ScheduleValidationError as exc:  # pragma: no cover - same rules as above
            _fail(str(exc), 3)
        plan_id = db.save_plan(
            _conn(), {"setup": setup.to_dict(), "brief_date": anchor, **schedule}
        )
        _out({"ok": True, "accepted": True, "plan_id": plan_id, "weeks": len(brief.weeks)})
        return
    _out({"ok": True, "weeks": len(brief.weeks), "training_level": brief.training_level})


@plan_app.command("brief")
def plan_brief(
    today: Annotated[
        str | None, typer.Option(help="Brief start date (default: today, athlete-local)")
    ] = None,
) -> None:
    """Print the guardrails (weekly targets, long-run bounds, phases) for the saved race setup."""
    conn = _conn()
    setup = db.load_race_setup(conn)
    if setup is None:
        _fail("no race set; run `coach race set` first")
    anchor = today or date_in_timezone(datetime.now(UTC), db.athlete_timezone(conn))
    brief = build_schedule_brief(setup, anchor)
    _out(
        {
            "brief_date": anchor,
            "setup": setup.to_dict(),
            "feasibility_problems": brief_feasibility_problems(brief),
            **brief.to_dict(),
        }
    )


@plan_app.command("show")
def plan_show() -> None:
    """Print the currently accepted plan (or null)."""
    _out(db.load_accepted_plan(_conn()))


@race_app.command("set")
def race_set(
    name: Annotated[str, typer.Option(help="Race name")],
    date_: Annotated[str, typer.Option("--date", help="Race date YYYY-MM-DD")],
    days: Annotated[str, typer.Option(help="Available run days, e.g. sun,tue,thu,sat")],
    long_run_day: Annotated[str, typer.Option(help="Long-run weekday, e.g. sun")],
    weekly_min: Annotated[float, typer.Option(help="Recent weekly distance, low end")],
    weekly_max: Annotated[float, typer.Option(help="Recent weekly distance, high end")],
    longest: Annotated[float, typer.Option(help="Longest recent run")],
    unit: Annotated[str, typer.Option(help="Unit for distances: km or mi")] = "mi",
    goal_time: Annotated[
        str | None, typer.Option(help="Goal time H:MM:SS (omit for finish goal)")
    ] = None,
    experience: Annotated[str, typer.Option(help="first_marathon or returning")] = "returning",
    recent_race_distance: Annotated[
        float | None, typer.Option(help="Recent race distance (same unit)")
    ] = None,
    recent_race_time: Annotated[str | None, typer.Option(help="Recent race time H:MM:SS")] = None,
    notes: Annotated[str, typer.Option(help="Free-text context")] = "",
) -> None:
    """Save the goal race and baseline used to build plan guardrails."""
    if unit not in ("km", "mi"):
        _fail("--unit must be km or mi")
    factor = KM if unit == "km" else MI
    try:
        recent = None
        if (recent_race_distance is None) != (recent_race_time is None):
            _fail("give both --recent-race-distance and --recent-race-time, or neither")
        if recent_race_distance is not None and recent_race_time is not None:
            recent = RecentRace(round(recent_race_distance * factor), _parse_hms(recent_race_time))
        setup = RaceSetup(
            race_name=name,
            race_date=date_,
            goal="time" if goal_time else "finish",
            target_seconds=_parse_hms(goal_time) if goal_time else None,
            training_days=tuple(parse_day(d) for d in days.split(",") if d.strip()),
            long_run_day=parse_day(long_run_day),
            weekly_distance_min_meters=round(weekly_min * factor),
            weekly_distance_max_meters=round(weekly_max * factor),
            longest_run_meters=round(longest * factor),
            experience=experience,  # type: ignore[arg-type]
            recent_race=recent,
            notes=notes,
        )
    except ValueError as exc:
        _fail(str(exc))
    problems = setup.problems()
    if problems:
        _fail("invalid race setup", problems=problems)
    db.save_race_setup(_conn(), setup)
    _out({"ok": True, "setup": setup.to_dict()})


@race_app.command("show")
def race_show() -> None:
    """Print the saved race setup (or null)."""
    setup = db.load_race_setup(_conn())
    _out(setup.to_dict() if setup else None)


@app.command("pace")
def pace_cmd(
    distance: Annotated[
        float | None, typer.Option(help="Race distance (see --unit); default: saved recent race")
    ] = None,
    time: Annotated[str | None, typer.Option(help="Race time H:MM:SS")] = None,
    unit: Annotated[str, typer.Option(help="Unit for --distance: km or mi")] = "mi",
) -> None:
    """Predict 5K/10K/half/marathon times from one race result (Riegel 1981). No training zones."""
    if (distance is None) != (time is None):
        _fail("give both --distance and --time, or neither to use the saved recent race")
    if distance is not None and time is not None:
        if unit not in ("km", "mi"):
            _fail("--unit must be km or mi")
        try:
            seconds = _parse_hms(time)
        except (ValueError, typer.BadParameter):
            _fail(f"--time must be H:MM:SS, got {time!r}")
        meters: float = round(distance * (KM if unit == "km" else MI), 1)
        source = "arguments"
    else:
        conn = _conn()
        setup = db.load_race_setup(conn)
        if setup is None:
            _fail("no race set and no --distance/--time given; run `coach race set` first")
        if setup.recent_race is None:
            _fail(
                "the saved race has no recent race result; pass --distance and --time, or "
                "save one with `coach race set --recent-race-distance ... --recent-race-time ...`"
            )
        meters = setup.recent_race.distance_meters
        seconds = setup.recent_race.duration_seconds
        source = "race_setup"
    try:
        report = paces.predict(meters, seconds).to_dict()
    except ValueError as exc:
        _fail(str(exc))
    report["input"] = {"source": source, **report["input"]}
    _out(report)


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
