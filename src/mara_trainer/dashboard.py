"""Local progress dashboard: one self-contained HTML file built from the runner's own database.

Two layers. `build_data` returns a plain dict; every number in it comes from the existing
rollups (`summaries`), plan readers (`reports`), the fueling log, the heart-rate-at-pace report
(`hr_pace`, read from FIT files) and the body log (`body`); nothing is re-derived here and no
section carries advice (planned next to done, never a verdict). `panels` are the sections the
page shows; `extra` are data panels kept for later versions and not rendered.

`render_html` turns that dict into one HTML string: dark, phone first, inline CSS and inline
SVG, no scripts and no external requests, so it opens offline and nothing leaves the laptop.
Three views share the page through CSS radio tabs: Today (today's session, this week's strip,
where the plan is, weekly miles, milestones), Week (each day of this week as a row) and Block
(every week of the accepted plan, grouped by phase, planned next to done).

Missing is not zero: a week or day with no logged runs has a null value and shows a dash or
nothing, and a section with nothing to show says so.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from html import escape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import body, db, fueling, hr_pace, reports
from .dates import date_in_timezone, parse_instant
from .summaries import Activity, Summary, summarize_range, week_bounds, weekly_rollups

MAX_WEEKS = 104
DEFAULT_WEEKS = 12
METERS = {"mi": 1609.344, "km": 1000.0}
MARKED_STATUSES = tuple(s for s in body.STATUSES if s != "green")  # weekly-distance marks
NOT_RUNS = ("rest", "strength")  # plan kinds with no distance to run


class DashboardError(ValueError):
    pass


# ---------------------------------------------------------------- data


def _columns_panel(
    panel_id: str,
    title: str,
    subtitle: str,
    rows: list[dict[str, Any]],
    *,
    measure: str,
    value_label: str,
    target_label: str | None = None,
    row_label: str = "Week of",
    marks_label: str | None = None,
    note: str | None = None,
    empty: str,
) -> dict[str, Any]:
    has_data = any(r["value"] is not None or r["target"] is not None for r in rows)
    return {
        "id": panel_id,
        "title": title,
        "kind": "columns",
        "status": "ok" if has_data else "no_data",
        "subtitle": subtitle,
        "measure": measure,  # 'distance' (meters) or 'carbs_per_hour' (g/h)
        "value_label": value_label,
        "target_label": target_label,
        "row_label": row_label,
        "marks_label": marks_label,  # set when rows carry `body` (yellow/red counts per week)
        "note": note,
        "empty": empty,
        "rows": rows,
    }


def _planned_long(week: dict[str, Any] | None) -> int | None:
    if not week:
        return None
    return next((s["distance_meters"] for s in week["sessions"] if s["kind"] == "long"), None)


def _day_state(kind: str | None, run_count: int, day: str, today: str) -> str:
    """How a day reads on the page: 'done' (a run was logged that day), 'rest', 'strength',
    'none' (the plan has no session), else a run day that is 'today', 'planned' (ahead) or
    'past' (before today, nothing logged). Labels only; no verdict."""
    if run_count:
        return "done"
    if kind is None:
        return "none"
    if kind in NOT_RUNS:
        return kind
    if day == today:
        return "today"
    return "planned" if day > today else "past"


def _this_week(
    plan: dict[str, Any] | None, activities: list[Activity], tz: str, today: str, now: datetime
) -> dict[str, Any]:
    """The Monday-Sunday week containing `today`: each day's planned session (`reports.plan_week`)
    next to the runs logged that athlete-local day (`summarize_range`). A day with no run has no
    done distance (missing, not zero). `left` = run days from today on with nothing logged yet.
    The seven days are always there; without a plan week only the planned parts are missing."""
    start, end = week_bounds(today)
    week = reports.plan_week(plan, today)
    planned = {s["date"]: s for s in week["sessions"]} if week else {}
    rows: list[dict[str, Any]] = []
    for offset in range(7):
        day = (date.fromisoformat(start) + timedelta(days=offset)).isoformat()
        session = planned.get(day)
        kind = session["kind"] if session else None
        done = summarize_range(activities, [], tz, day, day, now)
        rows.append(
            {
                "date": day,
                "kind": kind,  # None = no session for this day (or no plan week)
                "planned_meters": (
                    session["distance_meters"] if session and kind not in NOT_RUNS else None
                ),
                "details": session.get("details") if session else None,
                "done_meters": done.distance_meters if done.run_count else None,
                "run_count": done.run_count,
                "future": day > today,
                "state": _day_state(kind, done.run_count, day, today),
            }
        )
    left = [r for r in rows if r["state"] in ("today", "planned")]
    done_rows = [r for r in rows if r["done_meters"] is not None]
    up_next = next((r["date"] for r in rows if r["state"] == "planned"), None)
    return {
        "id": "this_week",
        "title": "This week",
        "kind": "week",
        "status": "ok" if week else "no_data",
        "empty": ("This week is not in the accepted plan." if plan else "No accepted plan yet."),
        "start": start,
        "end": end,
        "week": week["week"] if week else None,
        "of_weeks": week["of_weeks"] if week else None,
        "phase": week["phase"] if week else None,
        "planned_meters": (
            sum(r["planned_meters"] or 0 for r in rows)
            if any(r["planned_meters"] for r in rows)
            else None
        ),
        "done_meters": sum(r["done_meters"] for r in done_rows) if done_rows else None,
        "left": [
            {"date": r["date"], "kind": r["kind"], "planned_meters": r["planned_meters"]}
            for r in left
        ],
        "left_meters": sum(r["planned_meters"] or 0 for r in left) if left else None,
        "up_next": up_next,  # the first run day after today with nothing logged
        "rows": rows,
    }


def _block(
    plan: dict[str, Any] | None,
    activities: list[Activity],
    tz: str,
    today: str,
    now: datetime,
    flagged: dict[str, dict[str, int]],
) -> dict[str, Any]:
    """Every week of the accepted plan, Monday first: phase, planned distance (the sum of the
    week's sessions, so a lowered week shows what it has now), planned long run, and for weeks
    that have started the distance done so far and the longest run (null when nothing was
    logged; weeks ahead have no done values). `body` = that week's yellow/red counts."""
    weeks: list[dict[str, Any]] = []
    for w in (plan or {}).get("weeks", []):
        start = w.get("start_date")
        if not start:
            continue
        end = (date.fromisoformat(start) + timedelta(days=6)).isoformat()
        state = "ahead" if start > today else "past" if end < today else "current"
        runs = [s for s in w["sessions"] if s["kind"] not in NOT_RUNS]
        done = (
            summarize_range(activities, [], tz, start, min(end, today), now)
            if state != "ahead"
            else None
        )
        weeks.append(
            {
                "week": w.get("week"),
                "start": start,
                "end": end,
                "phase": w.get("phase"),
                "state": state,
                "planned_meters": sum(s["distance_meters"] for s in runs) if runs else None,
                "planned_long_meters": next(
                    (s["distance_meters"] for s in runs if s["kind"] == "long"), None
                ),
                "run_days": len([s for s in runs if s["distance_meters"] > 0]),
                "done_meters": done.distance_meters if done and done.run_count else None,
                "longest_meters": done.longest_run_meters if done and done.run_count else None,
                "body": {
                    s: flagged[start][s] for s in MARKED_STATUSES if s in flagged.get(start, {})
                },
            }
        )
    phases: list[dict[str, Any]] = []
    for row in weeks:
        if phases and phases[-1]["phase"] == row["phase"]:
            phases[-1]["weeks"].append(row)
        else:
            phases.append({"phase": row["phase"], "weeks": [row]})
    return {
        "id": "block",
        "title": "Training block",
        "kind": "block",
        "status": "ok" if weeks else "no_data",
        "empty": "No accepted plan yet." if not plan else "The accepted plan has no dated weeks.",
        "phases": phases,
    }


def _today(week: dict[str, Any], plan: dict[str, Any] | None, today: str) -> dict[str, Any]:
    """Today's planned session and what was logged today, from this week's rows."""
    row = next((r for r in week["rows"] if r["date"] == today), None)
    kind = row["kind"] if row else None
    state = row["state"] if row else None
    if state in ("today", "past", "planned"):
        state = "not_yet"
    if state == "none":
        state = None
    return {
        "id": "today",
        "title": "Today",
        "kind": "today",
        "status": "ok" if kind is not None else "no_data",
        "date": today,
        "session_kind": kind,
        "planned_meters": row["planned_meters"] if row else None,
        "details": row["details"] if row else None,
        "state": state,  # 'done', 'not_yet', 'rest', 'strength' or None (no session)
        "done_meters": row["done_meters"] if row else None,
        "run_count": row["run_count"] if row else 0,
        "empty": ("Today is not in the accepted plan." if plan else "No accepted plan yet."),
    }


def _weeks_to_race(race_date: str, today: str) -> int | None:
    """Monday-Sunday weeks from this week through race week, counted as the plan brief does
    (`build_schedule_brief`): race week alone is 1. None once race day has passed."""
    if race_date < today:
        return None
    this_monday = date.fromisoformat(week_bounds(today)[0])
    return (date.fromisoformat(race_date) - this_monday).days // 7 + 1


def _where(
    race: Any, week: dict[str, Any], plan: dict[str, Any] | None, today: str
) -> dict[str, Any]:
    return {
        "id": "where",
        "title": "Where you are",
        "kind": "where",
        "status": "ok" if race else "no_data",
        "race_name": race.race_name if race else None,
        "race_date": race.race_date if race else None,
        "weeks_to_race": _weeks_to_race(race.race_date, today) if race else None,
        "week": week["week"],
        "of_weeks": week["of_weeks"],
        "phase": week["phase"],
        "plan_note": (
            None
            if week["week"] is not None
            else "This week is not in the accepted plan."
            if plan
            else "No accepted plan yet."
        ),
        "empty": "No race set yet.",
    }


def _import_label(at: str, tz: str, today: str) -> str:
    """'Last run imported today, 7:42 am' or '... Mar 14, 11:02 pm', in athlete-local time.
    This is when the newest file was imported, not when COROS was last checked."""
    local = parse_instant(at).astimezone(ZoneInfo(tz))
    clock = local.strftime("%-I:%M %p").lower()
    day = local.date().isoformat()
    when = "today" if day == today else local.strftime("%b %-d")
    if day[:4] != today[:4]:
        when += local.strftime(", %Y")
    return f"Last run imported {when}, {clock}"


def _body_by_week(conn: sqlite3.Connection, start: str, today: str) -> dict[str, dict[str, int]]:
    """Yellow and red body checks from `start` through `today`, counted per Monday-Sunday week
    exactly as reported (`body.open_items`). Green entries are not counted here."""
    out: dict[str, dict[str, int]] = {}
    for entry in body.open_items(conn, start, today):
        counts = out.setdefault(week_bounds(entry["date"])[0], {})
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return out


def _milestones(
    everything: Summary, all_weeks: list[Summary], race: Any, today: str
) -> dict[str, Any]:
    ran = [w for w in all_weeks if w.run_count]
    biggest = max(ran, key=lambda w: w.distance_meters, default=None)
    days = (date.fromisoformat(race.race_date) - date.fromisoformat(today)).days if race else None
    tiles = [
        {
            "id": "longest_run",
            "label": "Longest run so far",
            "measure": "distance",
            "value": everything.longest_run_meters,
            "detail": None,
        },
        {
            "id": "biggest_week",
            "label": "Biggest week",
            "measure": "distance",
            "value": biggest.distance_meters if biggest else None,
            "detail": f"week of {_day(biggest.start_date)}" if biggest else None,
        },
        {
            "id": "runs_logged",
            "label": "Runs logged",
            "measure": "count",
            "value": everything.run_count or None,
            "detail": None,
        },
        {
            "id": "days_to_race",
            "label": "Days to race",
            "measure": "count",
            "value": days if days is not None and days >= 0 else None,
            "detail": (
                None
                if race is None
                else f"{race.race_name}, {_long_day(race.race_date)}"
                if days is not None and days >= 0
                else f"{race.race_name} was on {_long_day(race.race_date)}"
            ),
        },
    ]
    return {
        "id": "milestones",
        "title": "Milestones",
        "kind": "tiles",
        "status": "ok" if any(t["value"] is not None for t in tiles) else "no_data",
        "empty": "Import a run or set a race to see milestones.",
        "tiles": tiles,
    }


def _hr_panel(fit_dir: Path | None, tz: str, today: str, weeks: int, unit: str) -> dict[str, Any]:
    """Weekly mean heart rate at the most-used pace band, straight from `hr_pace.report`."""
    report = hr_pace.report(fit_dir, tz, today, weeks, unit=unit) if fit_dir is not None else None
    rows: list[dict[str, Any]] = []
    band = note = trend = None
    empty = "Sync or export runs as FIT files to see heart rate at your usual pace."
    if report is not None:
        rows = [
            {"date": w["week_start"], "value": w["mean_hr"], "runs_used": w["runs_used"]}
            for w in report["weeks"]
        ]
        band = hr_pace.band_label(report["band"])
        trend = hr_pace.trend_line(report["trend"])
        coverage = report["coverage"]
        if coverage["runs_in_window"]:
            note = (
                f"{coverage['runs_used']} of {coverage['runs_in_window']} runs in these weeks "
                "have a steady stretch in this band with heart rate."
            )
            empty = "No run in these weeks has a steady stretch at one pace with heart rate."
    return {
        "id": "hr_at_pace",
        "title": "Heart rate at your usual pace",
        "kind": "hr_pace",
        "status": "ok" if any(r["value"] is not None for r in rows) else "no_data",
        "subtitle": (
            f"Mean heart rate each week on steady stretches at {band}, the pace band used most."
            if band
            else "Mean heart rate each week on steady stretches at the pace band used most."
        ),
        "band": band,
        "trend": trend,  # one fixed sentence, or None without enough weeks of data
        "caveat": hr_pace.TREND_CAVEAT,
        "note": note,
        "empty": empty,
        "rows": rows,
    }


def _body_panel(conn: sqlite3.Connection, start: str, today: str) -> dict[str, Any]:
    """Body-log entries from `start` through `today`, oldest first, exactly as reported."""
    listed = body.list_entries(conn, last=body.MAX_LAST, since=start)["entries"]
    rows = [
        {"id": e["id"], "date": e["date"], "status": e["status"], "location": e["location"]}
        for e in reversed(listed)
        if e["date"] <= today
    ]
    return {
        "id": "body_log",
        "title": "Body check log",
        "kind": "timeline",
        "status": "ok" if rows else "no_data",
        "subtitle": "Each body check by date and the status given: green, yellow or red.",
        "note": (
            f"Showing the latest {body.MAX_LAST} entries." if len(listed) == body.MAX_LAST else None
        ),
        "empty": "Log a body check to see it here.",
        "start": start,  # Monday of the first week shown
        "end": today,
        "rows": rows,
    }


def build_data(
    conn: sqlite3.Connection,
    today: str | None = None,
    weeks: int = DEFAULT_WEEKS,
    fit_dir: Path | None = None,
    include_extra: bool = False,
) -> dict[str, Any]:
    """Sections for the week containing `today` (athlete-local, default today) and the last
    `weeks` Monday-Sunday weeks ending with it. Runs after `today` are left out.
    `include_extra` adds the data panels the page doesn't show (long run, fueling, heart rate at
    pace from the FIT files in `fit_dir`, body log); off by default, so no FIT file is decoded
    for a normal build."""
    if not 1 <= weeks <= MAX_WEEKS:
        raise DashboardError(f"weeks must be 1-{MAX_WEEKS}")
    try:
        tz = db.athlete_timezone(conn)
    except LookupError as exc:
        raise DashboardError(str(exc)) from exc
    if today is None:
        now = datetime.now(UTC)
        today = date_in_timezone(now, tz)
    else:
        try:
            day = date.fromisoformat(today)
        except ValueError as exc:
            raise DashboardError(f"today must be YYYY-MM-DD, got {today!r}") from exc
        today = day.isoformat()
        now = datetime.combine(day, time.max, tzinfo=ZoneInfo(tz)).astimezone(UTC)
    row = conn.execute("SELECT units FROM athlete WHERE id = 1").fetchone()
    unit = "km" if row and row["units"] == "km" else "mi"

    activities = db.load_activities(conn)
    plan = db.load_accepted_plan(conn)
    race = db.load_race_setup(conn)
    rollups = weekly_rollups(activities, [], tz, weeks, now)
    plan_weeks = [reports.plan_week(plan, w.start_date) for w in rollups]
    no_plan = None if plan else "No accepted plan yet, so only logged runs are shown."
    flagged = _body_by_week(conn, rollups[0].start_date, today)
    starts = [w.get("start_date") for w in (plan or {}).get("weeks", [])]
    block_start = min([d for d in starts if d] + [today])
    block_flags = _body_by_week(conn, block_start, today)

    weekly = _columns_panel(
        "weekly_distance",
        "Weekly distance",
        "Distance run each week (Monday to Sunday) against the plan's weekly target.",
        [
            {
                "date": w.start_date,
                "value": w.distance_meters if w.run_count else None,
                "target": p["target_distance_meters"] if p else None,
                # yellow / red body checks that week, in that order; {} = none
                "body": {
                    s: flagged[w.start_date][s]
                    for s in MARKED_STATUSES
                    if s in flagged.get(w.start_date, {})
                },
            }
            for w, p in zip(rollups, plan_weeks, strict=True)
        ],
        measure="distance",
        value_label="Done",
        target_label="Plan",
        marks_label="Yellow or red body checks",
        note=no_plan,
        empty="Import runs or accept a plan to see weekly miles.",
    )
    long_run = _columns_panel(
        "long_run",
        "Long-run build-up",
        "Longest run each week against the plan's long run.",
        [
            {"date": w.start_date, "value": w.longest_run_meters, "target": _planned_long(p)}
            for w, p in zip(rollups, plan_weeks, strict=True)
        ],
        measure="distance",
        value_label="Longest run",
        target_label="Planned long run",
        note=no_plan,
        empty="Import runs or accept a plan to see the long-run build-up.",
    )

    entries = fueling.entries_between(conn, rollups[0].start_date, today)
    rated = [e for e in entries if e["carbs_per_hour"] is not None]
    fuel = _columns_panel(
        "fueling",
        "Carbs per hour",
        "Grams of carbohydrate per hour on each run in the fueling log.",
        [{"date": e["date"], "value": e["carbs_per_hour"], "target": None} for e in rated],
        measure="carbs_per_hour",
        value_label="Carbs per hour",
        row_label="Run on",
        note=(
            f"{len(rated)} of {len(entries)} logged runs have both carbs and duration."
            if entries
            else None
        ),
        empty=(
            "Fueling entries need both carbs and a duration to show here."
            if entries
            else "Log fueling after a long run to see carbs per hour."
        ),
    )

    everything = summarize_range(activities, [], tz, date.min.isoformat(), today, now)
    all_weeks: list[Summary] = []
    if everything.run_count:
        first = min(date_in_timezone(parse_instant(a.started_at), tz) for a in activities)
        span = date.fromisoformat(week_bounds(today)[0]) - date.fromisoformat(week_bounds(first)[0])
        all_weeks = weekly_rollups(activities, [], tz, span.days // 7 + 1, now)

    week = _this_week(plan, activities, tz, today, now)
    imported = db.last_import_at(conn)
    return {
        "generated_for": today,
        "weeks": weeks,
        "unit": unit,
        "race": {"name": race.race_name, "date": race.race_date} if race else None,
        "last_import": (
            {"at": imported, "label": _import_label(imported, tz, today)} if imported else None
        ),
        "panels": [
            _today(week, plan, today),
            week,
            _where(race, week, plan, today),
            weekly,
            _milestones(everything, all_weeks, race, today),
            _block(plan, activities, tz, today, now, block_flags),
        ],
        # Kept for later versions; not rendered on the page.
        "extra": (
            [
                long_run,
                fuel,
                _hr_panel(fit_dir, tz, today, weeks, unit),
                _body_panel(conn, rollups[0].start_date, today),
            ]
            if include_extra
            else []
        ),
    }


# ---------------------------------------------------------------- HTML

_CSS = """
:root{color-scheme:dark;--bg:#000;--card:#1c1c1e;--card2:#2c2c2e;--card3:#3a3a3c;--text:#f2f2f7;
--muted:#8e8e93;--faint:#636366;--line:#38383a;--ac:#9085e9;--yellow:#ffd60a;--red:#ff6961}
*{box-sizing:border-box}
body{margin:0;overflow-x:hidden;background:var(--bg);color:var(--text);
font:15px/1.4 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,sans-serif;
font-variant-numeric:tabular-nums}
main{max-width:440px;margin:0 auto;padding:16px 12px 32px}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.eyebrow{color:var(--muted);font-size:13px;font-weight:600;margin:4px 4px 0}
h1{font-size:32px;margin:2px 4px 14px;letter-spacing:-.02em}
h2{font-size:17px;margin:0 0 10px}
p{margin:0}
.card{background:var(--card);border-radius:20px;padding:16px;margin:0 0 12px;list-style:none}
.hero .big{font-size:56px;font-weight:700;letter-spacing:-.03em;line-height:1.05}
.unit{font-size:17px;font-weight:600;color:var(--muted);letter-spacing:0}
.big2{font-size:30px;font-weight:700;letter-spacing:-.02em;line-height:1.15}
.kicker{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--ac)}
.muted{color:var(--muted)}
.sync{color:var(--muted);font-size:12px;margin-top:10px}
.empty{color:var(--muted);margin-top:4px}
.pill{display:inline-flex;align-items:center;gap:6px;margin-top:12px;padding:6px 12px;
border-radius:999px;background:var(--card2);font-size:13px;font-weight:600;
border:1px solid transparent}
.pill svg{width:12px;height:12px}
.pill.done{color:var(--ac)}
.pill.sm{margin-top:0;padding:3px 10px;font-size:12px;background:transparent;border-color:var(--ac);
color:var(--ac)}
.chip{font-size:12px;font-weight:600;padding:3px 10px;border-radius:999px;background:var(--card2);
color:var(--text)}
.row{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.row.tight{margin-top:12px;font-size:13px}
.row.tight>span:last-child{text-align:right}
.strip{display:grid;grid-template-columns:repeat(7,1fr);gap:2px;text-align:center}
.day{display:flex;flex-direction:column;align-items:center;gap:4px;padding:6px 0;border-radius:12px}
.day.is-today{background:var(--card2)}
.dn{font-size:12px;color:var(--muted);font-weight:600}
.dm{font-size:12px;color:var(--text)}
.dot{width:30px;height:30px;flex:none}
.g-done circle{fill:var(--ac)}
.g-done path{fill:none;stroke:#fff;stroke-width:2.6;stroke-linecap:round;stroke-linejoin:round}
.g-today .o{fill:none;stroke:var(--ac);stroke-width:3}
.g-today .i{fill:var(--ac)}
.g-gym circle{fill:none;stroke:var(--muted);stroke-width:2;stroke-dasharray:3 3}
.g-gym path{stroke:#c7c7cc;stroke-width:2;stroke-linecap:round}
.g-rest circle{fill:var(--faint)}
.g-plan circle{fill:none;stroke:var(--muted);stroke-width:2}
.g-past circle{fill:none;stroke:var(--faint);stroke-width:1.25}
.bar{height:6px;background:var(--card2);border-radius:999px;margin-top:8px;overflow:hidden}
.bar div{height:100%;border-radius:999px;background:var(--ac)}
.chart{width:100%;height:auto;display:block}
.grid{stroke:var(--line);stroke-width:1}
.tick{fill:var(--muted);font-size:10px}
.target{fill:none;stroke:var(--muted);stroke-width:1.5;stroke-dasharray:3 2}
.done{fill:var(--ac)}
.done.now{opacity:.55}
.hit{fill:transparent}
.hit:hover{fill:rgba(255,255,255,.06)}
.st-yellow{fill:var(--yellow)}
.st-red{fill:var(--red)}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;color:var(--muted);
margin:-4px 0 6px}
.legend span{display:inline-flex;align-items:center;gap:6px}
.legend svg{width:10px;height:10px}
.sw{display:inline-block;width:10px;height:10px;border-radius:3px;background:var(--ac)}
.sw.outline{background:none;border:1.5px dashed var(--muted)}
.note{color:var(--muted);font-size:12px;margin-top:6px}
.tbl{margin-top:8px;font-size:12px;color:var(--muted)}
.tbl summary{cursor:pointer}
.tbl table{width:100%;border-collapse:collapse;margin-top:6px}
.tbl th,.tbl td{text-align:left;padding:3px 0;border-bottom:1px solid var(--line)}
.ms{list-style:none;padding:0;margin:0}
.ms li{display:flex;justify-content:space-between;gap:12px;padding:9px 0;
border-bottom:1px solid var(--line)}
.ms li:last-child{border:0}
.ms li>span:first-child{white-space:nowrap}
.ms li>span:last-child{text-align:right}
.ms small{display:block;font-size:12px}
.headrow{display:flex;gap:16px;align-items:center}
.ring{width:84px;height:84px;flex:none}
.ring .track{fill:none;stroke:var(--card2);stroke-width:8}
.ring .arc{fill:none;stroke:var(--ac);stroke-width:8;stroke-linecap:round}
.ringv{fill:var(--text);font-size:17px;font-weight:700}
.ringl{fill:var(--muted);font-size:9px}
.days{padding:6px 12px}
.dayrow{display:grid;grid-template-columns:40px 30px 1fr auto;gap:10px;align-items:center;
padding:10px 4px;border-bottom:1px solid var(--line)}
.dayrow:last-child{border:0}
.dayrow.now{background:var(--card2);border-radius:14px;border-bottom-color:transparent;
box-shadow:inset 3px 0 0 var(--ac)}
.when{display:flex;flex-direction:column;font-size:13px;line-height:1.2}
.what{display:flex;flex-direction:column;font-size:14px;line-height:1.3;min-width:0}
.what .muted{font-size:12px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;
-webkit-box-orient:vertical}
.ok{font-size:13px;font-weight:600;color:var(--ac);white-space:nowrap}
.next{font-size:13px;white-space:nowrap}
.tabs input{position:absolute;opacity:0;pointer-events:none}
.seg{display:grid;grid-template-columns:1fr 1fr 1fr;background:var(--card);border-radius:10px;
padding:2px;margin:2px 4px 14px}
.seg label{text-align:center;padding:7px 0;font-size:14px;font-weight:600;color:var(--muted);
border-radius:8px;cursor:pointer}
#v-today:checked~.seg label[for=v-today],#v-week:checked~.seg label[for=v-week],
#v-block:checked~.seg label[for=v-block]{
background:var(--card3);color:var(--text)}
#v-today:focus-visible~.seg label[for=v-today],#v-week:focus-visible~.seg label[for=v-week],
#v-block:focus-visible~.seg label[for=v-block]{
outline:2px solid var(--ac)}
.view{display:none}
#v-today:checked~.view.today,#v-week:checked~.view.week,#v-block:checked~.view.block{
display:block}
.wks{list-style:none;padding:0;margin:0}
.phead{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:0 8px}
.phead .muted{font-size:12px}
.wkrow{display:grid;grid-template-columns:44px minmax(0,1fr) auto;gap:10px;align-items:center;
padding:9px 4px;border-bottom:1px solid var(--line)}
.wkrow:last-child{border:0}
.wkrow.now{background:var(--card2);border-radius:14px;border-bottom-color:transparent;
box-shadow:inset 3px 0 0 var(--ac)}
.wkrow.ahead .what b{font-weight:600}
.wkrow .bar{margin-top:6px;height:4px}
.wkrow .ok{text-align:right;font-size:12px;white-space:normal}
.wkrow .marks{display:inline-flex;gap:3px;margin-left:6px;vertical-align:middle}
.wkrow .marks svg{width:10px;height:10px}
footer{color:var(--faint);font-size:12px;margin:20px 4px 0}
"""

MISSING = "\N{EN DASH}"

_KIND_LABELS = {
    "easy": "Easy run",
    "workout": "Workout",
    "long": "Long run",
    "race": "Race",
    "strength": "Strength",
    "rest": "Rest",
}
_PHASE_LABELS = {
    "base": "Base",
    "build": "Build",
    "marathon_specific": "Marathon specific",
    "taper": "Taper",
    "race": "Race week",
}


def _num(meters: float | None, unit: str) -> str:
    """Distance as a bare number in the display unit; missing shows as a dash, never zero."""
    return MISSING if meters is None else f"{meters / METERS[unit]:.1f}"


def _amount(value: float | None, measure: str, unit: str) -> str:
    """A stored value as display text; missing shows as a dash, never as zero."""
    if value is None:
        return MISSING
    if measure == "distance":
        return f"{value / METERS[unit]:.1f} {unit}"
    if measure == "carbs_per_hour":
        return f"{value:g} g/h"
    return f"{value:,}"


def _day(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%b %-d")


def _long_day(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%b %-d, %Y")


def _kind(kind: str | None) -> str:
    return _KIND_LABELS.get(kind or "", (kind or "").capitalize()) if kind else MISSING


def _ticks(top: float) -> list[int]:
    step = next((s for s in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500) if top / s <= 4), 1000)
    return [i * step for i in range(max(1, math.ceil(top / step)) + 1)]


def _progress(part: float | None, whole: float | None) -> str:
    if not part or not whole:
        return '<div class="bar" aria-hidden="true"></div>'
    pct = max(0.0, min(1.0, part / whole)) * 100
    return f'<div class="bar" aria-hidden="true"><div style="width:{pct:.0f}%"></div></div>'


# Each day state has its own shape, never colour alone.
_GLYPHS = {
    "done": (
        "g-done",
        '<circle cx="18" cy="18" r="15"/><path d="M11 18.5l4.5 4.5 9.5-10"/>',
    ),
    "today": (
        "g-today",
        '<circle class="o" cx="18" cy="18" r="14"/><circle class="i" cx="18" cy="18" r="5"/>',
    ),
    "strength": (
        "g-gym",
        '<circle cx="18" cy="18" r="14"/><path d="M10 18h16M12 14v8M24 14v8M9 16v4M27 16v4"/>',
    ),
    "rest": ("g-rest", '<circle cx="18" cy="18" r="3"/>'),
    "none": ("g-rest", '<circle cx="18" cy="18" r="3"/>'),
    "planned": ("g-plan", '<circle cx="18" cy="18" r="14"/>'),
    "past": ("g-past", '<circle cx="18" cy="18" r="14"/>'),
}


def _glyph(state: str) -> str:
    cls, shapes = _GLYPHS[state]
    return f'<svg viewBox="0 0 36 36" class="dot {cls}" aria-hidden="true">{shapes}</svg>'


def _row_title(row: dict[str, Any]) -> str:
    """The day's session name; a logged run on a day with no plan session reads 'Run'."""
    if row["kind"] is None:
        return "Run" if row["done_meters"] is not None else MISSING
    return _kind(row["kind"])


def _day_text(row: dict[str, Any], unit: str) -> str:
    """One day in words, for screen readers: 'Tue Mar 10: Easy run 3.7 mi, done 3.7 mi'."""
    name = _row_title(row) if row["kind"] or row["done_meters"] is not None else "no session"
    text = f"{date.fromisoformat(row['date']).strftime('%a %b %-d')}: {name}"
    if row["planned_meters"] is not None:
        text += f" {_amount(row['planned_meters'], 'distance', unit)}"
    if row["done_meters"] is not None:
        text += f", done {_amount(row['done_meters'], 'distance', unit)}"
    elif row["state"] == "today":
        text += ", today"
    elif row["state"] == "past":
        text += ", nothing logged"
    return text


def _sync_line(data: dict[str, Any]) -> str:
    last = data["last_import"]
    return f'<p class="sync">{escape(last["label"] if last else "No runs imported yet.")}</p>'


def _empty(text: str) -> str:
    return f'<p class="empty">{escape(text)}</p>'


def _render_today(p: dict[str, Any], data: dict[str, Any], unit: str) -> str:
    parts = []
    done_pill = ""
    if p["done_meters"] is not None:
        done_pill = (
            '<div class="pill done"><svg viewBox="0 0 12 12" aria-hidden="true">'
            '<path d="M2.5 6.2l2.3 2.3 4.7-5" fill="none" stroke="currentColor" stroke-width="1.6"'
            ' stroke-linecap="round" stroke-linejoin="round"/></svg>'
            f"Done · {escape(_amount(p['done_meters'], 'distance', unit))}"
            + (f" ({p['run_count']} runs)" if p["run_count"] > 1 else "")
            + "</div>"
        )
    if p["status"] != "ok":
        parts.append('<div class="kicker">Today</div>')
        parts.append(_empty(p["empty"]))
        parts.append(done_pill)
    else:
        parts.append(f'<div class="kicker">{escape(_kind(p["session_kind"]))}</div>')
        if p["planned_meters"] is not None:
            parts.append(
                f'<div class="big">{_num(p["planned_meters"], unit)}'
                f'<span class="unit"> {unit}</span></div>'
            )
        if p["details"]:
            parts.append(f'<p class="muted">{escape(p["details"])}</p>')
        if p["state"] == "not_yet":
            parts.append(
                '<div class="pill"><svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6"'
                ' cy="6" r="4.5" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>'
                "Not done yet</div>"
            )
        parts.append(done_pill)
    parts.append(_sync_line(data))
    return f'<section class="card hero" id="today">{"".join(parts)}</section>'


def _left_text(p: dict[str, Any], unit: str) -> str:
    if not p["left"]:
        return "Nothing left on the plan this week"
    runs = ", ".join(
        f"{date.fromisoformat(d['date']).strftime('%a')} {_num(d['planned_meters'], unit)}"
        for d in p["left"]
    )
    return f"Left: {runs}"


def _render_strip(p: dict[str, Any], data: dict[str, Any], unit: str) -> str:
    head = '<div class="row"><h2>This week</h2><span class="muted">Mon to Sun</span></div>'
    days = []
    for row in p["rows"]:
        state = row["state"]
        if state == "done":
            note = _num(row["done_meters"], unit)
        elif state in ("today", "planned", "past"):
            note = _num(row["planned_meters"], unit)
        else:
            note = {"strength": "Gym", "rest": "Rest"}.get(state, MISSING)
        today = " is-today" if row["date"] == data["generated_for"] else ""
        letter = date.fromisoformat(row["date"]).strftime("%a")[0]
        days.append(
            f'<div class="day{today}"><span class="dn" aria-hidden="true">{letter}</span>'
            f'{_glyph(state)}<span class="dm" aria-hidden="true">{escape(note)}</span>'
            f'<span class="sr">{escape(_day_text(row, unit))}</span></div>'
        )
    strip = f'<div class="strip">{"".join(days)}</div>'
    if p["status"] != "ok":  # no plan week: what was done, and the plain reason for the rest
        done = (
            f'<div class="row tight"><span><b>{_num(p["done_meters"], unit)}</b> '
            f'<span class="muted">{unit} done</span></span></div>'
            if p["done_meters"] is not None
            else ""
        )
        body_html = f"{head}{strip}{done}{_empty(p['empty'])}"
        return f'<section class="card" id="this_week">{body_html}</section>'
    totals = (
        f'<div class="row tight"><span><b>{_num(p["done_meters"], unit)}</b> '
        f'<span class="muted">of {_num(p["planned_meters"], unit)} {unit}</span></span>'
        f'<span class="muted">{escape(_left_text(p, unit))}</span></div>'
    )
    return (
        f'<section class="card" id="this_week">{head}{strip}'
        f"{totals}{_progress(p['done_meters'], p['planned_meters'])}</section>"
    )


def _race_line(p: dict[str, Any]) -> str:
    weeks = p["weeks_to_race"]
    if weeks is None:
        return f'<div class="big2">Race day was {_long_day(p["race_date"])}.</div>'
    if weeks == 1:
        return '<div class="big2">Race week</div>'
    return f'<div class="big2">{weeks}<span class="unit"> weeks to race</span></div>'


def _render_where(p: dict[str, Any], data: dict[str, Any], unit: str) -> str:
    chip = (
        f'<span class="chip">{escape(_PHASE_LABELS.get(p["phase"], str(p["phase"])))}</span>'
        if p["phase"]
        else ""
    )
    parts = [f'<div class="row"><h2>Where you are</h2>{chip}</div>']
    if p["status"] != "ok":
        parts.append(_empty(p["empty"]))
    else:
        parts.append(_race_line(p))
        parts.append(f'<p class="muted">{escape(p["race_name"])}, {_long_day(p["race_date"])}</p>')
        if p["week"] is not None:
            parts.append(f'<p class="muted">Week {p["week"]} of {p["of_weeks"]}</p>')
            parts.append(_progress(p["week"], p["of_weeks"]))
        else:
            parts.append(_empty(p["plan_note"]))
    return f'<section class="card" id="where">{"".join(parts)}</section>'


def _status_mark(status: str, x: float, y: float) -> str:
    """Yellow is a triangle, red a square: shape and colour, never colour alone."""
    cls = f"st-{status}"
    if status == "yellow":
        return f'<path class="{cls}" d="M{x:g},{y - 5.5:g}L{x + 5.5:g},{y + 4.5:g}H{x - 5.5:g}z"/>'
    if status == "red":
        return f'<rect class="{cls}" x="{x - 4.5:g}" y="{y - 4.5:g}" width="9" height="9"/>'
    return f'<circle class="{cls}" cx="{x:g}" cy="{y:g}" r="5"/>'


def _body_counts(counts: dict[str, int]) -> str:
    """'2 yellow, 1 red': the week's counts as reported, or 'none'."""
    return ", ".join(f"{n} {status}" for status, n in counts.items()) or "none"


def _weekly_svg(p: dict[str, Any], data: dict[str, Any], unit: str, uid: str) -> str:
    rows = p["rows"]
    n = len(rows)
    values = [None if r["value"] is None else r["value"] / METERS[unit] for r in rows]
    targets = [None if r["target"] is None else r["target"] / METERS[unit] for r in rows]
    ticks = _ticks(max([v for v in values + targets if v is not None], default=1.0))
    marks = [[s for s in MARKED_STATUSES if s in (r.get("body") or {})] for r in rows]
    strip = len(MARKED_STATUSES) * 12 + 4 if any(marks) else 0  # one fixed row per status
    w, left, top = 340, 26, 14 + strip
    base = top + 104
    h = base + 30
    slot = (w - left) / n
    bar = max(2.0, min(16.0, slot * 0.6))
    vmax = ticks[-1]

    def y(v: float) -> float:
        return base - (base - top) * v / vmax

    unit_word = "miles" if unit == "mi" else "kilometres"
    desc = f"Weekly {unit_word}, last {n} weeks: done as filled bars, plan as dashed outlines." + (
        " Weeks with a yellow or red body check are marked above the bars." if strip else ""
    )
    out = [
        f'<svg viewBox="0 0 {w} {h}" role="img" aria-labelledby="{uid}" class="chart">'
        f'<title id="{uid}">{escape(desc)}</title>'
    ]
    for v in ticks:
        out.append(f'<line x1="{left}" x2="{w}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>')
        out.append(
            f'<text x="{left - 6}" y="{y(v) + 3:.1f}" class="tick" text-anchor="end">{v}</text>'
        )
    label_every = math.ceil(n / 6)
    week_end = week_bounds(data["generated_for"])[1]
    for k, row in enumerate(rows):
        cx = left + slot * k + slot / 2
        x0 = cx - bar / 2
        current = k == n - 1
        so_far = " so far" if current and data["generated_for"] < week_end else ""
        value, target = values[k], targets[k]
        tip = f"Week of {_day(row['date'])}: "
        tip += "no runs logged" if value is None else f"{value:.1f} {unit}{so_far}"
        if target is not None:
            tip += f", plan {target:.1f} {unit}"
        if marks[k]:
            tip += f", body checks: {_body_counts({s: row['body'][s] for s in marks[k]})}"
        out.append(f"<g><title>{escape(tip)}</title>")
        if target is not None and target > 0:
            out.append(
                f'<rect x="{x0 - 2:.1f}" y="{y(target):.1f}" width="{bar + 4:.1f}" '
                f'height="{base - y(target):.1f}" rx="4" class="target"/>'
            )
        if value is not None and value > 0:
            yt = y(value)
            r = min(4.0, bar / 2, base - yt)
            out.append(
                f'<path class="done{" now" if current else ""}" d="M{x0:.1f},{base:.1f} '
                f"V{yt + r:.1f} Q{x0:.1f},{yt:.1f} {x0 + r:.1f},{yt:.1f} H{x0 + bar - r:.1f} "
                f'Q{x0 + bar:.1f},{yt:.1f} {x0 + bar:.1f},{yt + r:.1f} V{base:.1f} Z"/>'
            )
        out.append(
            f'<rect class="hit" x="{cx - slot / 2:.1f}" y="{top}" width="{slot:.1f}" '
            f'height="{base - top}"/></g>'
        )
        if marks[k]:  # after the bar group, so the marks' own hover text is on top
            counts = {s: row["body"][s] for s in marks[k]}
            noun = "body check" if sum(counts.values()) == 1 else "body checks"
            mark_tip = f"{_body_counts(counts)} {noun}, week of {_day(row['date'])}"
            drawn = "".join(
                _status_mark(s, cx, 8 + 12 * MARKED_STATUSES.index(s)) for s in marks[k]
            )
            out.append(f"<g><title>{escape(mark_tip)}</title>{drawn}</g>")
        if (n - 1 - k) % label_every == 0:  # counted back from this week, so "now" has room
            label = "now" if current else _day(row["date"])
            out.append(
                f'<text x="{cx:.1f}" y="{base + 14}" class="tick" text-anchor="middle">'
                f"{escape(label)}</text>"
            )
    out.append("</svg>")
    return "".join(out)


def _render_weekly(p: dict[str, Any], data: dict[str, Any], unit: str) -> str:
    title = "Weekly miles" if unit == "mi" else "Weekly kilometres"
    head = f"<h2>{title}</h2>"
    if p["status"] != "ok":
        return f'<section class="card" id="weekly_distance">{head}{_empty(p["empty"])}</section>'
    seen = [s for s in MARKED_STATUSES if any(s in (r.get("body") or {}) for r in p["rows"])]
    legend = (
        '<div class="legend"><span><i class="sw"></i>Done</span>'
        '<span><i class="sw outline"></i>Plan</span>'
        + "".join(
            f'<span><svg viewBox="0 0 12 12" aria-hidden="true">{_status_mark(s, 6, 6.5)}</svg>'
            f"{s.capitalize()} body check</span>"
            for s in seen
        )
        + "</div>"
    )
    marks_col = bool(seen)
    th = f"<th>Week of</th><th>Plan {unit}</th><th>Done {unit}</th>"
    if marks_col:
        th += f"<th>{escape(p['marks_label'])}</th>"
    body_rows = []
    for r in p["rows"]:
        cells = f"<td>{_day(r['date'])}</td><td>{_num(r['target'], unit)}</td>"
        cells += f"<td>{_num(r['value'], unit)}</td>"
        if marks_col:
            cells += f"<td>{escape(_body_counts(r.get('body') or {}))}</td>"
        body_rows.append(f"<tr>{cells}</tr>")
    table = (
        '<details class="tbl"><summary>Show as a table</summary><table><thead><tr>'
        f"{th}</tr></thead><tbody>{''.join(body_rows)}</tbody></table></details>"
    )
    note = f'<p class="note">{escape(p["note"])}</p>' if p["note"] else ""
    return (
        f'<section class="card" id="weekly_distance">{head}{legend}'
        f"{_weekly_svg(p, data, unit, 'wk-title')}{note}{table}</section>"
    )


def _render_milestones(p: dict[str, Any], data: dict[str, Any], unit: str) -> str:
    head = "<h2>Milestones</h2>"
    if p["status"] != "ok":
        return f'<section class="card" id="milestones">{head}{_empty(p["empty"])}</section>'
    items = []
    for t in p["tiles"]:
        detail = f'<small class="muted">{escape(t["detail"])}</small>' if t["detail"] else ""
        value = escape(_amount(t["value"], t["measure"], unit))
        items.append(
            f"<li><span>{escape(t['label'])}</span><span><b>{value}</b>{detail}</span></li>"
        )
    listed = f'<ul class="ms">{"".join(items)}</ul>'
    return f'<section class="card" id="milestones">{head}{listed}</section>'


_TODAY_RENDERERS = {
    "today": _render_today,
    "this_week": _render_strip,
    "where": _render_where,
    "weekly_distance": _render_weekly,
    "milestones": _render_milestones,
}


def _ring(done: float | None, planned: float | None, unit: str) -> str:
    r = 30
    c = 2 * math.pi * r
    arc = ""
    if done and planned:
        frac = max(0.0, min(1.0, done / planned))
        arc = (
            f'<circle class="arc" cx="38" cy="38" r="{r}" stroke-dasharray="{c * frac:.1f} '
            f'{c:.1f}" transform="rotate(-90 38 38)"/>'
        )
    label = (
        f"{_amount(done, 'distance', unit)} done of {_amount(planned, 'distance', unit)} planned"
        if done is not None
        else f"No runs logged yet this week, {_amount(planned, 'distance', unit)} planned"
    )
    return (
        f'<svg viewBox="0 0 76 76" class="ring" role="img" aria-label="{escape(label)}">'
        f'<circle class="track" cx="38" cy="38" r="{r}"/>{arc}'
        f'<text x="38" y="37" text-anchor="middle" class="ringv">{_num(done, unit)}</text>'
        f'<text x="38" y="51" text-anchor="middle" class="ringl">of {_num(planned, unit)}</text>'
        "</svg>"
    )


def _render_week_view(data: dict[str, Any], unit: str) -> str:
    week = next(p for p in data["panels"] if p["id"] == "this_week")
    where = next(p for p in data["panels"] if p["id"] == "where")
    today = data["generated_for"]
    if week["week"] is not None:
        phase = _PHASE_LABELS.get(week["phase"], str(week["phase"]))
        eyebrow = f"Week {week['week']} of {week['of_weeks']} · {phase}"
    else:
        eyebrow = f"{_day(week['start'])} to {_day(week['end'])}"
    race = _race_line(where) if where["status"] == "ok" else _empty(where["empty"])
    out = [f'<p class="eyebrow">{escape(eyebrow)}</p><h1>This week</h1>']
    if week["status"] != "ok":  # no plan week: the days still show what was done
        done = (
            f'<p class="muted">{escape(_amount(week["done_meters"], "distance", unit))} done '
            "this week</p>"
            if week["done_meters"] is not None
            else ""
        )
        out.append(
            f'<section class="card" id="week_head">{race}{done}{_empty(week["empty"])}'
            f"{_sync_line(data)}</section>"
        )
    else:
        left = (
            f"{_amount(week['left_meters'], 'distance', unit)} left this week"
            if week["left_meters"]
            else "Nothing left on the plan this week"
        )
        out.append(
            f'<section class="card headrow" id="week_head">'
            f"{_ring(week['done_meters'], week['planned_meters'], unit)}"
            f'<div>{race}<p class="muted">{escape(left)}</p>{_sync_line(data)}</div></section>'
        )
    rows = []
    for row in week["rows"]:
        state = row["state"]
        is_today = row["date"] == today
        if state == "done":
            done = _amount(row["done_meters"], "distance", unit)
            right = f'<span class="ok">Done · {escape(done)}</span>'
        elif is_today:
            right = '<span class="pill sm">Today</span>'
        elif row["date"] == week["up_next"]:
            right = '<span class="muted next">Up next</span>'
        elif state == "past":
            right = f'<span class="muted">{MISSING}</span>'
        else:
            right = "<span></span>"
        title = _row_title(row)
        if row["planned_meters"] is not None and state != "done":
            title += f" {_amount(row['planned_meters'], 'distance', unit)}"
        details = f'<span class="muted">{escape(row["details"])}</span>' if row["details"] else ""
        when = date.fromisoformat(row["date"])
        rows.append(
            f'<li class="dayrow{" now" if is_today else ""}">'
            f'<div class="when"><b>{when.strftime("%a")}</b>'
            f'<span class="muted">{when.day}</span></div>{_glyph(state)}'
            f'<div class="what"><b>{escape(title)}</b>{details}</div>{right}</li>'
        )
    out.append(f'<ul class="card days" id="week_days">{"".join(rows)}</ul>')
    return "".join(out)


def _block_row(row: dict[str, Any], unit: str) -> str:
    plan = (
        f"{_amount(row['planned_meters'], 'distance', unit)} planned"
        if row["planned_meters"] is not None
        else "No runs planned"
    )
    sub = []
    if row["planned_long_meters"] is not None:
        sub.append(f"long run {_amount(row['planned_long_meters'], 'distance', unit)}")
    if row["run_days"]:
        sub.append(f"{row['run_days']} run{'s' if row['run_days'] != 1 else ''}")
    if row["state"] == "ahead":
        right = "<span></span>"
    elif row["done_meters"] is None:
        right = f'<span class="muted">{MISSING}</span>'
    else:
        so_far = " so far" if row["state"] == "current" else ""
        longest = _amount(row["longest_meters"], "distance", unit)
        right = (
            f'<span class="ok">{escape(_amount(row["done_meters"], "distance", unit))} done'
            f'{so_far}<br><span class="muted">longest {escape(longest)}</span></span>'
        )
    marks = ""
    if row["body"]:
        tip = f"Body checks: {_body_counts(row['body'])}"
        drawn = "".join(
            f'<svg viewBox="0 0 12 12" aria-hidden="true">{_status_mark(s, 6, 6.5)}</svg>'
            for s in row["body"]
        )
        marks = (
            f'<span class="marks" title="{escape(tip)}">{drawn}'
            f'<span class="sr">{escape(tip)}</span></span>'
        )
    bar = _progress(row["done_meters"], row["planned_meters"]) if row["state"] != "ahead" else ""
    cls = {"current": " now", "ahead": " ahead"}.get(row["state"], "")
    return (
        f'<li class="wkrow{cls}"><div class="when"><b>Wk {row["week"]}</b>'
        f'<span class="muted">{_day(row["start"])}</span></div>'
        f'<div class="what"><b>{escape(plan)}{marks}</b>'
        f'<span class="muted">{escape(", ".join(sub))}</span>{bar}</div>{right}</li>'
    )


def _render_block_view(data: dict[str, Any], unit: str) -> str:
    block = next(p for p in data["panels"] if p["id"] == "block")
    where = next(p for p in data["panels"] if p["id"] == "where")
    out = ['<p class="eyebrow">The whole plan</p><h1>Training block</h1>']
    head = _race_line(where) if where["status"] == "ok" else _empty(where["empty"])
    if where["status"] == "ok" and where["week"] is not None:
        head += f'<p class="muted">This is week {where["week"]} of {where["of_weeks"]}</p>'
    out.append(f'<section class="card" id="block">{head}')
    if block["status"] != "ok":
        out.append(_empty(block["empty"]))
    out.append("</section>")
    for group in block["phases"]:
        label = _PHASE_LABELS.get(group["phase"], str(group["phase"]))
        first, last = group["weeks"][0], group["weeks"][-1]
        span = f"{_day(first['start'])} to {_day(last['end'])}"
        n = len(group["weeks"])
        rows = "".join(_block_row(r, unit) for r in group["weeks"])
        out.append(
            f'<section class="card days"><div class="phead"><h2>{escape(label)}</h2>'
            f'<span class="muted">{n} week{"s" if n != 1 else ""} · {escape(span)}</span></div>'
            f'<ul class="wks">{rows}</ul></section>'
        )
    return "".join(out)


def render_html(data: dict[str, Any]) -> str:
    """The whole dashboard as one offline HTML document: Today, Week and Block views, no
    scripts."""
    unit = data["unit"]
    today = date.fromisoformat(data["generated_for"])
    today_view = f'<p class="eyebrow">{today.strftime("%A, %b %-d")}</p><h1>Today</h1>' + "".join(
        _TODAY_RENDERERS[p["id"]](p, data, unit)
        for p in data["panels"]
        if p["id"] in _TODAY_RENDERERS
    )
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Training progress</title><style>{_CSS}</style></head><body>"
        '<main class="tabs">'
        '<input type="radio" name="view" id="v-today" checked>'
        '<input type="radio" name="view" id="v-week">'
        '<input type="radio" name="view" id="v-block">'
        '<div class="seg"><label for="v-today">Today</label><label for="v-week">Week</label>'
        '<label for="v-block">Block</label></div>'
        f'<div class="view today">{today_view}<!-- /view --></div>'
        f'<div class="view week">{_render_week_view(data, unit)}<!-- /view --></div>'
        f'<div class="view block">{_render_block_view(data, unit)}<!-- /view --></div>'
        "<footer>Built on this computer from the local training database. "
        "Nothing on this page is uploaded.</footer></main></body></html>\n"
    )
