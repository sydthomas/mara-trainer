"""Local progress dashboard: one self-contained HTML file built from the runner's own database.

Two layers. `build_data` returns a plain dict with a list of panels; every number in it comes
from the existing rollups (`summaries`), plan readers (`reports`), the fueling log, the
heart-rate-at-pace report (`hr_pace`, read from FIT files) and the body log (`body`); nothing is
re-derived here and no panel carries advice. `render_html` turns that dict into one HTML string:
inline CSS, inline SVG, no scripts and no external requests, so it opens offline and nothing
leaves the laptop.

Missing is not zero: a week with no logged runs has a null value and draws no bar, and a panel
with nothing to show says so. To add a panel, append a dict with an `id`, `title`, `status`
('ok' or 'no_data') and a `kind` that has a renderer in `_RENDERERS`.
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
from .summaries import Summary, summarize_range, week_bounds, weekly_rollups

MAX_WEEKS = 104
METERS = {"mi": 1609.344, "km": 1000.0}


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
        "note": note,
        "empty": empty,
        "rows": rows,
    }


def _planned_long(week: dict[str, Any] | None) -> int | None:
    if not week:
        return None
    return next((s["distance_meters"] for s in week["sessions"] if s["kind"] == "long"), None)


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
            "detail": f"week of {biggest.start_date}" if biggest else None,
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
                else f"{race.race_name}, {race.race_date}"
                if days is not None and days >= 0
                else f"{race.race_name} was on {race.race_date}"
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
    weeks: int = 16,
    fit_dir: Path | None = None,
) -> dict[str, Any]:
    """Panels for the last `weeks` Monday-Sunday weeks ending with the week containing `today`
    (athlete-local, default today). Runs after `today` are left out. `fit_dir` is the folder of
    FIT files for the heart-rate panel (read only); without one that panel has no data."""
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

    weekly = _columns_panel(
        "weekly_distance",
        "Weekly distance",
        "Distance run each week (Monday to Sunday) against the plan's weekly target.",
        [
            {
                "date": w.start_date,
                "value": w.distance_meters if w.run_count else None,
                "target": p["target_distance_meters"] if p else None,
            }
            for w, p in zip(rollups, plan_weeks, strict=True)
        ],
        measure="distance",
        value_label="Run",
        target_label="Plan target",
        note=no_plan,
        empty="Import runs or accept a plan to see weekly distance.",
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

    return {
        "generated_for": today,
        "weeks": weeks,
        "unit": unit,
        "race": {"name": race.race_name, "date": race.race_date} if race else None,
        "panels": [
            _milestones(everything, all_weeks, race, today),
            weekly,
            long_run,
            fuel,
            _hr_panel(fit_dir, tz, today, weeks, unit),
            _body_panel(conn, rollups[0].start_date, today),
        ],
    }


# ---------------------------------------------------------------- HTML

_CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;
--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--series:#2a78d6;
--good:#0ca30c;--warn:#fab219;--crit:#d03b3b}
@media (prefers-color-scheme: dark){:root{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;
--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
--border:rgba(255,255,255,.10);--series:#3987e5}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
font:16px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:760px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:22px;margin:0 0 2px}
h2{font-size:17px;margin:0 0 2px}
p{margin:0}
.sub,.note,footer,.tile small,summary{color:var(--ink2);font-size:14px}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:10px;
padding:16px;margin-top:16px}
.empty{margin-top:12px;padding:20px 12px;border:1px dashed var(--axis);border-radius:8px;
text-align:center;color:var(--ink2)}
.empty b{display:block;color:var(--ink)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;
margin-top:12px}
.tile{border:1px solid var(--border);border-radius:8px;padding:10px 12px}
.tile span{display:block;color:var(--ink2);font-size:14px}
.tile b{display:block;font-size:26px;font-weight:600;line-height:1.25}
.tile b.none{font-size:16px;font-weight:400;color:var(--muted);padding:6px 0}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;margin-top:10px;color:var(--ink2);font-size:14px}
.legend span{display:inline-flex;align-items:center;gap:6px}
.sw{width:12px;height:12px;border-radius:3px;background:var(--series)}
.tk{width:16px;height:2px;background:var(--ink)}
.chart{overflow-x:auto;margin-top:8px;direction:rtl}
svg{display:block;width:100%;height:auto;direction:ltr}
svg text{font-size:13px;fill:var(--muted);font-variant-numeric:tabular-nums}
svg text.val{fill:var(--ink);font-weight:600}
.grid{stroke:var(--grid);stroke-width:1}
.base{stroke:var(--axis);stroke-width:1}
.bar{fill:var(--series)}
.ring{stroke:var(--surface);stroke-width:6}
.target{stroke:var(--ink);stroke-width:2}
.line{stroke:var(--series);stroke-width:2;stroke-linecap:round}
.dot{fill:var(--series);stroke:var(--surface);stroke-width:2}
.st-green,.st-yellow,.st-red{stroke:var(--surface);stroke-width:2}
.st-green{fill:var(--good)}
.st-yellow{fill:var(--warn)}
.st-red{fill:var(--crit)}
.legend svg{width:14px;height:14px;flex:none}
svg text.row{fill:var(--ink2)}
.txt th,.txt td{text-align:left}
.hit{fill:transparent}
.hit:hover{fill:var(--border)}
.note{margin-top:8px}
details{margin-top:10px}
summary{cursor:pointer}
table{border-collapse:collapse;width:100%;margin-top:8px;font-size:14px;
font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:4px 8px;border-bottom:1px solid var(--grid)}
th:first-child,td:first-child{text-align:left}
th{color:var(--ink2);font-weight:600}
footer{margin-top:20px}
@media (min-width:600px){svg text{font-size:9px}}
"""

MISSING = "\N{EN DASH}"


def _amount(value: float | None, measure: str, unit: str) -> str:
    """A stored value as display text; missing shows as a dash, never as zero."""
    if value is None:
        return MISSING
    if measure == "distance":
        return f"{value / METERS[unit]:.1f} {unit}"
    if measure == "carbs_per_hour":
        return f"{value:g} g/h"
    return f"{value:,}"


def _scaled(value: float | None, measure: str, unit: str) -> float | None:
    if value is None:
        return None
    return value / METERS[unit] if measure == "distance" else float(value)


def _day(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%b %-d")


def _ticks(top: float) -> list[int]:
    step = next((s for s in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500) if top / s <= 5), 1000)
    return [i * step for i in range(max(1, math.ceil(top / step)) + 1)]


def _empty(panel: dict[str, Any]) -> str:
    return f'<div class="empty"><b>No data yet</b>{escape(str(panel.get("empty") or ""))}</div>'


def _svg_columns(panel: dict[str, Any], unit: str) -> str:
    measure = panel["measure"]
    rows = panel["rows"]
    values = [_scaled(r["value"], measure, unit) for r in rows]
    targets = [_scaled(r["target"], measure, unit) for r in rows]
    ticks = _ticks(max([v for v in values + targets if v is not None], default=1.0))
    left, right, top, bottom = 36, 12, 22, 26
    n = len(rows)
    band = max(24.0, (480 - left - right) / n)
    width = left + right + band * n
    height = 230
    floor = height - bottom
    scale = (floor - top) / ticks[-1]
    bar = min(24.0, band - 6)
    label_every = math.ceil(n / 8)
    last_value = max((i for i, v in enumerate(values) if v is not None), default=None)

    min_width = 0 if width <= 480 else width * 0.75  # more weeks than fit: scroll, newest first
    out = [
        f'<svg viewBox="0 0 {width:g} {height}" style="min-width:{min_width:g}px" '
        f'role="img" aria-label="{escape(panel["title"])}: {escape(panel["subtitle"])}">'
    ]
    axis_unit = unit if measure == "distance" else "g/h"
    out.append(f'<text x="0" y="12">{escape(axis_unit)}</text>')
    for tick in ticks:
        y = floor - tick * scale
        cls = "base" if tick == 0 else "grid"
        out.append(
            f'<line class="{cls}" x1="{left}" x2="{width - right:g}" y1="{y:g}" y2="{y:g}"/>'
        )
        out.append(f'<text x="{left - 6}" y="{y + 4:g}" text-anchor="end">{tick}</text>')
    for i, (row, value, target) in enumerate(zip(rows, values, targets, strict=True)):
        x0 = left + band * i
        mid = x0 + band / 2
        if value is not None and value > 0:
            h = value * scale
            r = min(4.0, h, bar / 2)
            x, y = mid - bar / 2, floor - h
            out.append(
                f'<path class="bar" d="M{x:g},{floor:g}V{y + r:g}q0,{-r:g} {r:g},{-r:g}'
                f'h{bar - 2 * r:g}q{r:g},0 {r:g},{r:g}V{floor:g}z"/>'
            )
        if target is not None:
            y = floor - target * scale
            x1, x2 = mid - band * 0.42, mid + band * 0.42
            for cls in ("ring", "target"):
                out.append(f'<line class="{cls}" x1="{x1:g}" x2="{x2:g}" y1="{y:g}" y2="{y:g}"/>')
        if i == last_value and value is not None:
            peak = max(value, target or 0.0)
            anchor = "end" if i == n - 1 and n > 1 else "middle"
            lx = x0 + band if anchor == "end" else mid
            out.append(
                f'<text class="val" x="{lx:g}" y="{floor - peak * scale - 7:g}" '
                f'text-anchor="{anchor}">{value:.1f}</text>'
            )
        if i % label_every == 0:
            out.append(
                f'<text x="{mid:g}" y="{height - 8}" text-anchor="middle">'
                f"{escape(_day(row['date']))}</text>"
            )
        tip = f"{panel['row_label']} {_day(row['date'])}: " + (
            f"{panel['value_label']} {_amount(row['value'], measure, unit)}"
            if row["value"] is not None
            else "nothing logged"
        )
        if panel["target_label"] and row["target"] is not None:
            tip += f", {panel['target_label']} {_amount(row['target'], measure, unit)}"
        out.append(
            f'<rect class="hit" x="{x0:g}" y="{top}" width="{band:g}" height="{floor - top}">'
            f"<title>{escape(tip)}</title></rect>"
        )
    out.append("</svg>")
    return "".join(out)


def _table(panel: dict[str, Any], unit: str) -> str:
    measure = panel["measure"]
    target_label = panel["target_label"]
    head = f"<th>{escape(panel['row_label'])}</th><th>{escape(panel['value_label'])}</th>"
    if target_label:
        head += f"<th>{escape(target_label)}</th>"
    body = []
    for row in panel["rows"]:
        cells = (
            f"<td>{escape(row['date'])}</td><td>{escape(_amount(row['value'], measure, unit))}</td>"
        )
        if target_label:
            cells += f"<td>{escape(_amount(row['target'], measure, unit))}</td>"
        body.append(f"<tr>{cells}</tr>")
    return (
        "<details><summary>Show as a table</summary><table><thead><tr>"
        f"{head}</tr></thead><tbody>{''.join(body)}</tbody></table></details>"
    )


def _render_columns(panel: dict[str, Any], unit: str) -> str:
    parts = [f'<p class="sub">{escape(panel["subtitle"])}</p>']
    if panel["status"] != "ok":
        return parts[0] + _empty(panel)
    if panel["target_label"]:  # two marks: name both; one series needs no legend
        parts.append(
            f'<div class="legend"><span><i class="sw"></i>{escape(panel["value_label"])}</span>'
            f'<span><i class="tk"></i>{escape(panel["target_label"])}</span></div>'
        )
    parts.append(f'<div class="chart">{_svg_columns(panel, unit)}</div>')
    if panel["note"]:
        parts.append(f'<p class="note">{escape(panel["note"])}</p>')
    parts.append(_table(panel, unit))
    return "".join(parts)


def _render_tiles(panel: dict[str, Any], unit: str) -> str:
    if panel["status"] != "ok":
        return _empty(panel)
    tiles = []
    for tile in panel["tiles"]:
        if tile["value"] is None:
            value = '<b class="none">No data yet</b>'
        else:
            value = f"<b>{escape(_amount(tile['value'], tile['measure'], unit))}</b>"
        detail = f"<small>{escape(tile['detail'])}</small>" if tile["detail"] else ""
        tiles.append(f'<div class="tile"><span>{escape(tile["label"])}</span>{value}{detail}</div>')
    return f'<div class="tiles">{"".join(tiles)}</div>'


def _hr_ticks(low: float, high: float) -> list[int]:
    """Round bpm ticks that bracket the data. Heart rate has no meaningful zero baseline here,
    so the axis starts just under the lowest weekly mean."""
    step = next((s for s in (5, 10, 20) if (high - low + 4) / s <= 5), 50)
    first = math.floor((low - 2) / step) * step
    last = math.ceil((high + 2) / step) * step
    return list(range(first, last + step, step))


def _svg_hr(panel: dict[str, Any]) -> str:
    rows = panel["rows"]
    values = [r["value"] for r in rows]
    known = [v for v in values if v is not None]
    ticks = _hr_ticks(min(known), max(known))
    left, right, top, bottom = 36, 12, 22, 26
    n = len(rows)
    band = max(24.0, (480 - left - right) / n)
    width = left + right + band * n
    height = 230
    floor = height - bottom
    scale = (floor - top) / (ticks[-1] - ticks[0])
    label_every = math.ceil(n / 8)
    last_value = max(i for i, v in enumerate(values) if v is not None)

    def at(i: int) -> tuple[float, float]:
        return left + band * (i + 0.5), floor - (values[i] - ticks[0]) * scale

    min_width = 0 if width <= 480 else width * 0.75
    out = [
        f'<svg viewBox="0 0 {width:g} {height}" style="min-width:{min_width:g}px" '
        f'role="img" aria-label="{escape(panel["title"])}: {escape(panel["subtitle"])}">',
        '<text x="0" y="12">bpm</text>',
    ]
    for tick in ticks:
        y = floor - (tick - ticks[0]) * scale
        cls = "base" if tick == ticks[0] else "grid"
        out.append(
            f'<line class="{cls}" x1="{left}" x2="{width - right:g}" y1="{y:g}" y2="{y:g}"/>'
        )
        out.append(f'<text x="{left - 6}" y="{y + 4:g}" text-anchor="end">{tick}</text>')
    for i in range(1, n):  # join only neighbouring weeks that both have data: a gap stays a gap
        if values[i - 1] is not None and values[i] is not None:
            (x1, y1), (x2, y2) = at(i - 1), at(i)
            out.append(f'<line class="line" x1="{x1:g}" y1="{y1:g}" x2="{x2:g}" y2="{y2:g}"/>')
    hits = []
    for i, row in enumerate(rows):
        x0 = left + band * i
        mid = x0 + band / 2
        if values[i] is not None:
            x, y = at(i)
            out.append(f'<circle class="dot" cx="{x:g}" cy="{y:g}" r="5"/>')
            if i == last_value:
                anchor = "end" if i == n - 1 and n > 1 else "middle"
                lx = x0 + band if anchor == "end" else mid
                out.append(
                    f'<text class="val" x="{lx:g}" y="{y - 10:g}" text-anchor="{anchor}">'
                    f"{values[i]:.0f}</text>"
                )
        if i % label_every == 0:
            out.append(
                f'<text x="{mid:g}" y="{height - 8}" text-anchor="middle">'
                f"{escape(_day(row['date']))}</text>"
            )
        tip = f"Week of {_day(row['date'])}: " + (
            f"{values[i]:g} bpm, {row['runs_used']} run(s)"
            if values[i] is not None
            else "no steady stretch in the band"
        )
        hits.append(
            f'<rect class="hit" x="{x0:g}" y="{top}" width="{band:g}" height="{floor - top}">'
            f"<title>{escape(tip)}</title></rect>"
        )
    out += [*hits, "</svg>"]
    return "".join(out)


def _render_hr_pace(panel: dict[str, Any], unit: str) -> str:
    parts = [f'<p class="sub">{escape(panel["subtitle"])}</p>']
    if panel["status"] != "ok":
        parts.append(_empty(panel))
    else:
        parts.append(f'<div class="chart">{_svg_hr(panel)}</div>')
        if panel["trend"]:
            parts.append(f'<p class="note">{escape(panel["trend"])}.</p>')
    if panel["note"]:
        parts.append(f'<p class="note">{escape(panel["note"])}</p>')
    parts.append(f'<p class="note">{escape(panel["caveat"])}</p>')
    if panel["status"] == "ok":
        cells = []
        for r in panel["rows"]:
            value = MISSING if r["value"] is None else f"{r['value']:g} bpm"
            cells.append(
                f"<tr><td>{escape(r['date'])}</td><td>{escape(value)}</td>"
                f"<td>{r['runs_used']}</td></tr>"
            )
        parts.append(
            "<details><summary>Show as a table</summary><table><thead><tr><th>Week of</th>"
            f"<th>Mean heart rate</th><th>Runs used</th></tr></thead><tbody>{''.join(cells)}"
            "</tbody></table></details>"
        )
    return "".join(parts)


# Status is never colour alone: each has its own shape and its own labelled row.
_STATUS_LABELS = {"green": "Green", "yellow": "Yellow", "red": "Red"}


def _status_mark(status: str, x: float, y: float) -> str:
    cls = f"st-{status}"
    if status == "green":
        return f'<circle class="{cls}" cx="{x:g}" cy="{y:g}" r="5.5"/>'
    if status == "yellow":
        return f'<path class="{cls}" d="M{x:g},{y - 6.5:g}L{x + 6.5:g},{y + 5:g}H{x - 6.5:g}z"/>'
    return f'<rect class="{cls}" x="{x - 5:g}" y="{y - 5:g}" width="10" height="10"/>'


def _svg_timeline(panel: dict[str, Any]) -> str:
    start = date.fromisoformat(panel["start"])
    weeks = (date.fromisoformat(panel["end"]) - start).days // 7 + 1
    left, right, top, bottom, row_height = 52, 12, 8, 26, 36
    band = max(24.0, (480 - left - right) / weeks)
    width = left + right + band * weeks
    height = top + row_height * len(body.STATUSES) + bottom
    floor = height - bottom
    label_every = math.ceil(weeks / 8)
    min_width = 0 if width <= 480 else width * 0.75
    out = [
        f'<svg viewBox="0 0 {width:g} {height}" style="min-width:{min_width:g}px" '
        f'role="img" aria-label="{escape(panel["title"])}: {escape(panel["subtitle"])}">'
    ]
    row_y = {}
    for i, status in enumerate(body.STATUSES):
        y = row_y[status] = top + row_height * (i + 0.5)
        out.append(f'<line class="grid" x1="{left}" x2="{width - right:g}" y1="{y:g}" y2="{y:g}"/>')
        out.append(
            f'<text class="row" x="{left - 12}" y="{y + 4:g}" text-anchor="end">'
            f"{_STATUS_LABELS[status]}</text>"
        )
    out.append(f'<line class="base" x1="{left}" x2="{width - right:g}" y1="{floor}" y2="{floor}"/>')
    for i in range(0, weeks, label_every):
        monday = (start + timedelta(days=7 * i)).isoformat()
        out.append(f'<text x="{left + band * i:g}" y="{height - 8}">{escape(_day(monday))}</text>')
    same: dict[tuple[str, str], int] = {}
    marks, hits = [], []
    for row in panel["rows"]:
        key = (row["date"], row["status"])
        nth = same[key] = same.get(key, -1) + 1  # several on one day: step down, don't hide
        days = (date.fromisoformat(row["date"]) - start).days
        x = left + (days + 0.5) * band / 7
        y = row_y[row["status"]] + min(nth, 2) * 5
        marks.append(_status_mark(row["status"], x, y))
        tip = f"{_day(row['date'])}: {row['status']}"
        if row["location"]:
            tip += f", {row['location']}"
        hits.append(
            f'<circle class="hit" cx="{x:g}" cy="{y:g}" r="11">'
            f"<title>{escape(tip)}</title></circle>"
        )
    out += [*marks, *hits, "</svg>"]
    return "".join(out)


def _render_timeline(panel: dict[str, Any], unit: str) -> str:
    parts = [f'<p class="sub">{escape(panel["subtitle"])}</p>']
    if panel["status"] != "ok":
        return parts[0] + _empty(panel)
    legend = "".join(
        f'<span><svg viewBox="0 0 14 14" aria-hidden="true">{_status_mark(s, 7, 7)}</svg>'
        f"{_STATUS_LABELS[s]}</span>"
        for s in body.STATUSES
    )
    parts.append(f'<div class="legend">{legend}</div>')
    parts.append(f'<div class="chart">{_svg_timeline(panel)}</div>')
    if panel["note"]:
        parts.append(f'<p class="note">{escape(panel["note"])}</p>')
    body_rows = "".join(
        f"<tr><td>{escape(r['date'])}</td><td>{escape(_STATUS_LABELS[r['status']])}</td>"
        f"<td>{escape(r['location'] or MISSING)}</td></tr>"
        for r in panel["rows"]
    )
    parts.append(
        '<details><summary>Show as a table</summary><table class="txt"><thead><tr><th>Date</th>'
        f"<th>Status</th><th>Where</th></tr></thead><tbody>{body_rows}</tbody></table></details>"
    )
    return "".join(parts)


_RENDERERS = {
    "columns": _render_columns,
    "tiles": _render_tiles,
    "hr_pace": _render_hr_pace,
    "timeline": _render_timeline,
}


def render_html(data: dict[str, Any]) -> str:
    """The whole dashboard as one offline HTML document."""
    unit = data["unit"]
    race = data["race"]
    heading = f"{race['name']}, {_day(race['date'])} {race['date'][:4]}" if race else "No race set"
    start = (date.fromisoformat(week_bounds(data["generated_for"])[0])) - timedelta(
        days=7 * (data["weeks"] - 1)
    )
    sections = "".join(
        f'<section class="panel" id="{escape(p["id"])}"><h2>{escape(p["title"])}</h2>'
        f"{_RENDERERS[p['kind']](p, unit)}</section>"
        for p in data["panels"]
    )
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Training progress</title><style>{_CSS}</style></head><body><main>"
        f'<h1>Training progress</h1><p class="sub">{escape(heading)}</p>'
        f'<p class="sub">{data["weeks"]} weeks from {escape(_day(start.isoformat()))} to '
        f"{escape(_day(data['generated_for']))} {escape(data['generated_for'][:4])}</p>"
        f"{sections}"
        "<footer>Built on this computer from the local training database. "
        "Nothing on this page is uploaded.</footer></main></body></html>\n"
    )
