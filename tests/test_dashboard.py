"""Progress dashboard: sections come from the existing rollups and plan readers; missing stays
missing; one offline HTML file with a Today and a Week view. Synthetic data only."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from mara_trainer import body, dashboard, db, fueling, hr_pace, reports
from mara_trainer.cli import app
from mara_trainer.plan_rules import RaceSetup, build_schedule_brief, validate_schedule

from .plan_factory import build_valid_plan
from .test_hr_pace import write_run

TODAY = "2026-03-15"  # a Sunday (long-run day); its Monday-Sunday week starts 2026-03-09
PLAN_ANCHOR = "2026-02-23"  # a Monday, so plan week 1 starts there
SETUP = RaceSetup(
    race_name="Test <City> Marathon",
    race_date="2026-06-14",
    goal="finish",
    target_seconds=None,
    training_days=(0, 2, 4, 6),
    long_run_day=0,
    weekly_distance_min_meters=40000,
    weekly_distance_max_meters=48000,
    longest_run_meters=16000,
    experience="returning",
)
TODAY_SHOWN = ["today", "this_week", "where", "weekly_distance", "milestones"]
SHOWN = [*TODAY_SHOWN, "block"]
NOT_SHOWN = ["long_run", "fueling", "hr_at_pace", "body_log"]
JUDGEMENT = ("missed", "skipped", "behind", "should", "on track", "great", "well done", "failed")


def add_run(conn: sqlite3.Connection, started_at: str, meters: int, seconds: int = 3600) -> None:
    with conn:
        conn.execute(
            "INSERT INTO activities(source, source_id, fingerprint, title, started_at, "
            "distance_meters, duration_seconds) VALUES ('manual', ?, ?, 'Run', ?, ?, ?)",
            (started_at, f"{abs(hash(started_at)):064d}"[:64], started_at, meters, seconds),
        )


def add_batch(conn: sqlite3.Connection, created_at: str) -> None:
    n = conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0]
    with conn:
        conn.execute(
            "INSERT INTO import_batches(file_hash, filename, source, result, created_at) "
            "VALUES (?, 'run.fit', 'fit', '{}', ?)",
            (f"{n:064d}", created_at),
        )


@pytest.fixture
def full(conn: sqlite3.Connection) -> sqlite3.Connection:
    db.save_race_setup(conn, SETUP)
    brief = build_schedule_brief(SETUP, PLAN_ANCHOR)
    db.save_plan(conn, validate_schedule(build_valid_plan(SETUP, brief), SETUP, brief))
    add_run(conn, "2026-03-03T16:00:00.000Z", 8000)  # week of 03-02
    add_run(conn, "2026-03-08T16:00:00.000Z", 15000)  # Sunday, still week of 03-02
    add_run(conn, "2026-03-10T16:00:00.000Z", 6000)  # Tuesday of the week of 03-09
    add_run(conn, "2026-03-15T16:00:00.000Z", 12000)  # today
    fueling.add_entry(
        conn, fueling.FuelEntry(date="2026-03-08", duration_seconds=5400, carbs_grams=60)
    )
    fueling.add_entry(conn, fueling.FuelEntry(date="2026-03-15", stomach=4))  # no carbs: no rate
    return conn


def edit_week(conn: sqlite3.Connection, changes: dict[int, dict[str, Any]]) -> None:
    """Re-save the accepted plan with sessions of the week of 03-09 changed (by 0=Sunday day)."""
    plan = db.load_accepted_plan(conn)
    assert plan is not None
    week = next(w for w in plan["weeks"] if w["start_date"] == "2026-03-09")
    week["sessions"] = [{**s, **changes.get(s["day"], {})} for s in week["sessions"]]
    db.save_plan(conn, plan)


def panel(data: dict[str, Any], panel_id: str) -> dict[str, Any]:
    return next(p for p in data["panels"] + data["extra"] if p["id"] == panel_id)


def view(page: str, name: str) -> str:
    return page.split(f'<div class="view {name}">')[1].split("<!-- /view -->")[0]


def section(page: str, panel_id: str) -> str:
    return page.split(f'id="{panel_id}"')[1].split("</section>")[0]


def week_plan(conn: sqlite3.Connection, day: str = TODAY) -> dict[str, Any]:
    week = reports.plan_week(db.load_accepted_plan(conn), day)
    assert week is not None
    return week


# ---------------------------------------------------------------- shape and empty states


def test_empty_db_every_panel_says_no_data(conn: sqlite3.Connection) -> None:
    data = dashboard.build_data(conn, TODAY, 4)
    assert data["generated_for"] == TODAY and data["weeks"] == 4 and data["unit"] == "mi"
    assert [p["id"] for p in data["panels"]] == SHOWN
    assert data["extra"] == []
    assert all(p["status"] == "no_data" for p in data["panels"])
    assert data["last_import"] is None
    weekly = panel(data, "weekly_distance")["rows"]
    assert len(weekly) == 4
    assert all(r["value"] is None and r["target"] is None for r in weekly)  # missing, not zero
    page = dashboard.render_html(data)
    today, week = view(page, "today"), view(page, "week")
    assert "No accepted plan yet." in today and "No accepted plan yet." in week
    assert "No race set yet." in today and "No race set yet." in week
    assert "No runs imported yet." in today and "No runs imported yet." in week
    assert "Import runs or accept a plan to see weekly miles." in today
    assert "<svg" not in section(page, "weekly_distance")
    assert "0.0" not in page and "0 mi" not in page
    assert "No accepted plan yet." in view(page, "block")


def test_default_is_twelve_weeks(conn: sqlite3.Connection) -> None:
    assert dashboard.build_data(conn, TODAY)["weeks"] == 12


def test_bad_arguments(conn: sqlite3.Connection) -> None:
    with pytest.raises(dashboard.DashboardError):
        dashboard.build_data(conn, TODAY, 0)
    with pytest.raises(dashboard.DashboardError):
        dashboard.build_data(conn, "next week", 4)


# ---------------------------------------------------------------- today


def test_today_done_sums_two_runs_that_local_day(full: sqlite3.Connection) -> None:
    add_run(full, "2026-03-16T02:00:00.000Z", 3000)  # 7pm Sunday in Los Angeles: same local day
    add_run(full, "2026-03-16T08:00:00.000Z", 5000)  # 1am Monday local: not today
    today = panel(dashboard.build_data(full, TODAY, 4), "today")
    long_run = next(s for s in week_plan(full)["sessions"] if s["date"] == TODAY)
    assert today["status"] == "ok" and today["date"] == TODAY
    assert (
        today["session_kind"] == "long" and today["planned_meters"] == long_run["distance_meters"]
    )
    assert today["details"] == long_run["details"]
    assert today["state"] == "done"
    assert today["done_meters"] == 15000 and today["run_count"] == 2
    hero = section(dashboard.render_html(dashboard.build_data(full, TODAY, 4)), "today")
    assert "Long run" in hero and "Done" in hero and "9.3 mi" in hero
    assert "Not done yet" not in hero


def test_today_run_day_without_a_run_is_not_done_yet(full: sqlite3.Connection) -> None:
    data = dashboard.build_data(full, "2026-03-14", 4)  # Saturday: a run day, nothing logged
    today = panel(data, "today")
    assert today["state"] == "not_yet"
    assert today["done_meters"] is None and today["run_count"] == 0  # missing, not zero
    hero = section(dashboard.render_html(data), "today")
    assert "Not done yet" in hero and "0.0" not in hero


def test_today_rest_day(full: sqlite3.Connection) -> None:
    data = dashboard.build_data(full, "2026-03-11", 4)  # Wednesday: rest in the plan
    today = panel(data, "today")
    assert (today["session_kind"], today["state"], today["planned_meters"]) == (
        "rest",
        "rest",
        None,
    )
    hero = section(dashboard.render_html(data), "today")
    assert "Rest" in hero and "Not done yet" not in hero and "0.0" not in hero


def test_today_strength_day(full: sqlite3.Connection) -> None:
    edit_week(full, {3: {"kind": "strength", "details": "Squats <b>& planks</b>"}})
    data = dashboard.build_data(full, "2026-03-11", 4)
    today = panel(data, "today")
    assert (today["session_kind"], today["state"], today["planned_meters"]) == (
        "strength",
        "strength",
        None,
    )
    hero = section(dashboard.render_html(data), "today")
    assert "Strength" in hero and "Not done yet" not in hero
    assert "Squats &lt;b&gt;&amp; planks&lt;/b&gt;" in hero and "<b>&" not in hero


def test_today_outside_the_plan_says_so(full: sqlite3.Connection) -> None:
    data = dashboard.build_data(full, "2026-02-18", 1)  # the week before plan week 1
    assert panel(data, "today")["status"] == "no_data"
    assert panel(data, "this_week")["status"] == "no_data"
    page = dashboard.render_html(data)
    assert "Today is not in the accepted plan." in view(page, "today")
    assert "This week is not in the accepted plan." in view(page, "today")
    assert "This week is not in the accepted plan." in view(page, "week")


def test_today_default_is_the_athlete_local_date(
    full: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Frozen:
            return cls(2026, 3, 11, 2, 0, tzinfo=UTC)  # still Tue Mar 10, 7pm in Los Angeles

    monkeypatch.setattr(dashboard, "datetime", Frozen)
    data = dashboard.build_data(full, None, 4)
    assert data["generated_for"] == "2026-03-10"
    assert panel(data, "today")["state"] == "done"
    assert panel(data, "today") == panel(dashboard.build_data(full, "2026-03-10", 4), "today")


# ---------------------------------------------------------------- last sync


def test_last_import_absent(full: sqlite3.Connection) -> None:
    data = dashboard.build_data(full, TODAY, 4)
    assert data["last_import"] is None
    page = dashboard.render_html(data)
    assert view(page, "today").count("No runs imported yet.") == 1
    assert view(page, "week").count("No runs imported yet.") == 1


def test_last_import_newest_batch_in_athlete_local_time(full: sqlite3.Connection) -> None:
    add_batch(full, "2026-03-13T02:02:00.000Z")  # Thu Mar 12, 7:02 pm in Los Angeles (PDT)
    add_batch(full, "2026-03-15T14:42:00.000Z")  # Sun Mar 15, 7:42 am
    data = dashboard.build_data(full, TODAY, 4)
    assert data["last_import"] == {
        "at": "2026-03-15T14:42:00.000Z",
        "label": "Last run imported today, 7:42 am",
    }
    page = dashboard.render_html(data)
    assert "Last run imported today, 7:42 am" in view(page, "today")
    assert "Last run imported today, 7:42 am" in view(page, "week")
    later = dashboard.build_data(full, "2026-03-17", 4)
    assert later["last_import"]["label"] == "Last run imported Mar 15, 7:42 am"


def test_last_import_crossing_midnight_uses_the_local_date(full: sqlite3.Connection) -> None:
    add_batch(full, "2026-03-15T06:02:00.000Z")  # Sat Mar 14, 11:02 pm local, Sunday in UTC
    label = dashboard.build_data(full, TODAY, 4)["last_import"]["label"]
    assert label == "Last run imported Mar 14, 11:02 pm"
    with full:
        full.execute("UPDATE athlete SET timezone = 'Europe/London' WHERE id = 1")
    label = dashboard.build_data(full, TODAY, 4)["last_import"]["label"]
    assert label == "Last run imported today, 6:02 am"


# ---------------------------------------------------------------- this week


def test_this_week_days_monday_first_with_states(full: sqlite3.Connection) -> None:
    week = panel(dashboard.build_data(full, TODAY, 4), "this_week")
    assert week["status"] == "ok" and (week["start"], week["end"]) == ("2026-03-09", TODAY)
    rows = week["rows"]
    assert [r["date"] for r in rows] == [f"2026-03-{d:02d}" for d in range(9, 16)]
    plan = week_plan(full)
    by_date = {s["date"]: s for s in plan["sessions"]}
    assert [r["kind"] for r in rows] == [by_date[r["date"]]["kind"] for r in rows]
    assert [r["details"] for r in rows] == [by_date[r["date"]]["details"] for r in rows]
    # Mon rest, Tue run (done), Wed rest, Thu run, Fri rest, Sat run, Sun long (done, today).
    assert [r["state"] for r in rows] == ["rest", "done", "rest", "past", "rest", "past", "done"]
    assert [r["done_meters"] for r in rows] == [None, 6000, None, None, None, None, 12000]
    assert not any(r["future"] for r in rows)
    runs = [s["distance_meters"] for s in plan["sessions"] if s["kind"] not in ("rest", "strength")]
    assert week["planned_meters"] == sum(runs)
    assert week["done_meters"] == 18000
    assert week["left"] == [] and week["left_meters"] is None and week["up_next"] is None
    assert (week["week"], week["of_weeks"], week["phase"]) == (
        plan["week"],
        plan["of_weeks"],
        plan["phase"],
    )


def test_this_week_midweek_future_days_and_runs_left(full: sqlite3.Connection) -> None:
    week = panel(dashboard.build_data(full, "2026-03-11", 4), "this_week")  # a Wednesday
    rows = week["rows"]
    assert [r["future"] for r in rows] == [False, False, False, True, True, True, True]
    assert [r["state"] for r in rows] == [
        "rest",
        "done",
        "rest",
        "planned",
        "rest",
        "planned",
        "planned",
    ]
    assert [r["done_meters"] for r in rows] == [None, 6000, None, None, None, None, None]
    assert [d["date"] for d in week["left"]] == ["2026-03-12", "2026-03-14", "2026-03-15"]
    assert week["left_meters"] == sum(d["planned_meters"] for d in week["left"])
    assert week["up_next"] == "2026-03-12"


def test_this_week_today_not_done_is_still_left(full: sqlite3.Connection) -> None:
    week = panel(dashboard.build_data(full, "2026-03-14", 4), "this_week")  # Saturday, no run
    assert week["rows"][5]["state"] == "today"
    assert [d["date"] for d in week["left"]] == ["2026-03-14", "2026-03-15"]
    assert week["up_next"] == "2026-03-15"  # the first run day after today


def test_this_week_two_runs_one_day(full: sqlite3.Connection) -> None:
    add_run(full, "2026-03-11T01:00:00.000Z", 3000)  # 6pm Tuesday in Los Angeles: same local day
    rows = panel(dashboard.build_data(full, TODAY, 4), "this_week")["rows"]
    assert rows[1]["done_meters"] == 9000 and rows[1]["run_count"] == 2


def test_this_week_without_a_plan(conn: sqlite3.Connection) -> None:
    add_run(conn, "2026-03-10T16:00:00.000Z", 6000)
    week = panel(dashboard.build_data(conn, TODAY, 4), "this_week")
    assert week["status"] == "no_data" and week["start"] == "2026-03-09" and week["end"] == TODAY
    # The days are still there with what was done; only the planned parts are missing.
    assert [r["date"] for r in week["rows"]] == [f"2026-03-{d:02d}" for d in range(9, 16)]
    assert [r["done_meters"] for r in week["rows"]] == [None, 6000, None, None, None, None, None]
    assert all(r["kind"] is None and r["planned_meters"] is None for r in week["rows"])
    assert week["planned_meters"] is None and week["done_meters"] == 6000
    assert week["left"] == [] and week["up_next"] is None


def test_no_plan_run_today_shows_as_done(conn: sqlite3.Connection) -> None:
    add_run(conn, "2026-03-15T16:00:00.000Z", 8047)  # 5.0 mi, today
    data = dashboard.build_data(conn, TODAY, 4)
    today = panel(data, "today")
    assert today["status"] == "no_data" and today["state"] == "done"
    assert today["done_meters"] == 8047
    page = dashboard.render_html(data)
    hero = section(page, "today")
    assert "No accepted plan yet." in hero and "Done · 5.0 mi" in hero
    week = view(page, "week")
    assert "No accepted plan yet." in week and week.count("<li ") == 7
    sunday = week.split("<li ")[7]
    assert "Done · 5.0 mi" in sunday and "Sun" in sunday


def test_week_outside_the_plan_still_shows_runs(full: sqlite3.Connection) -> None:
    add_run(full, "2026-02-17T16:00:00.000Z", 4828)  # 3.0 mi, the week before plan week 1
    page = dashboard.render_html(dashboard.build_data(full, "2026-02-18", 1))
    assert "This week is not in the accepted plan." in view(page, "week")
    assert "Done · 3.0 mi" in view(page, "week")
    assert "3.0" in section(page, "this_week")


def test_this_week_race_week(full: sqlite3.Connection) -> None:
    data = dashboard.build_data(full, SETUP.race_date, 1)  # race day, a Sunday
    rows = panel(data, "this_week")["rows"]
    race = rows[6]
    assert (race["date"], race["kind"], race["planned_meters"]) == ("2026-06-14", "race", 42195)
    assert panel(data, "where")["weeks_to_race"] == 1
    page = dashboard.render_html(data)
    assert "Race week" in section(page, "where") and "26.2" in view(page, "week")


# ---------------------------------------------------------------- where you are


def test_where_weeks_to_race_and_plan_week(full: sqlite3.Connection) -> None:
    where = panel(dashboard.build_data(full, TODAY, 4), "where")
    plan = week_plan(full)
    # Same count as the plan brief: race week included (Mar 9 week ... Jun 8 week = 14 weeks).
    assert where["weeks_to_race"] == 14
    assert (where["week"], where["of_weeks"], where["phase"]) == (
        plan["week"],
        plan["of_weeks"],
        plan["phase"],
    )
    assert where["race_name"] == SETUP.race_name and where["race_date"] == SETUP.race_date
    text = section(dashboard.render_html(dashboard.build_data(full, TODAY, 4)), "where")
    assert "14" in text and "weeks to race" in text
    assert f"Week {plan['week']} of {plan['of_weeks']}" in text


def test_where_after_the_race(full: sqlite3.Connection) -> None:
    data = dashboard.build_data(full, "2026-06-20", 1)
    where = panel(data, "where")
    assert where["weeks_to_race"] is None and where["status"] == "ok"
    assert "Race day was Jun 14, 2026." in section(dashboard.render_html(data), "where")


def test_where_with_a_race_but_no_plan(conn: sqlite3.Connection) -> None:
    db.save_race_setup(conn, SETUP)
    data = dashboard.build_data(conn, TODAY, 4)
    assert panel(data, "where")["weeks_to_race"] == 14 and panel(data, "where")["week"] is None
    text = section(dashboard.render_html(data), "where")
    assert "weeks to race" in text and "No accepted plan yet." in text


# ---------------------------------------------------------------- weekly distance and milestones


def test_weekly_distance_matches_rollups_and_plan(full: sqlite3.Connection) -> None:
    data = dashboard.build_data(full, TODAY, 4)
    rows = panel(data, "weekly_distance")["rows"]
    assert [r["date"] for r in rows] == ["2026-02-16", "2026-02-23", "2026-03-02", "2026-03-09"]
    assert [r["value"] for r in rows] == [None, None, 23000, 18000]
    plan = db.load_accepted_plan(full)
    assert plan is not None
    targets = {w["start_date"]: w["target_distance_meters"] for w in plan["weeks"]}
    assert rows[0]["target"] is None  # before the plan starts
    assert [r["target"] for r in rows[1:]] == [targets[r["date"]] for r in rows[1:]]
    assert panel(data, "weekly_distance")["status"] == "ok"


def test_weekly_chart_render(full: sqlite3.Connection) -> None:
    page = dashboard.render_html(dashboard.build_data(full, TODAY, 4))
    chart = section(page, "weekly_distance")
    assert chart.count("<title>Week of ") == 4  # one hover text per bar
    assert "<title>Week of Mar 2: 14.3 mi, plan " in chart
    assert "<title>Week of Feb 16: no runs logged</title>" in chart
    assert 'role="img"' in chart and "aria-labelledby" in chart
    assert "Show as a table" in chart
    assert "<td>Mar 2</td>" in chart and "<td>14.3</td>" in chart
    assert f"<td>Feb 16</td><td>{dashboard.MISSING}</td><td>{dashboard.MISSING}</td>" in chart
    assert "<td>0.0</td>" not in chart
    assert "Done</span>" in chart and "Plan</span>" in chart


def test_today_hides_later_runs(full: sqlite3.Connection) -> None:
    data = dashboard.build_data(full, "2026-03-09", 2)
    tiles = {t["id"]: t for t in panel(data, "milestones")["tiles"]}
    assert tiles["runs_logged"]["value"] == 2
    assert [r["value"] for r in panel(data, "weekly_distance")["rows"]] == [23000, None]


def test_milestones(full: sqlite3.Connection) -> None:
    data = dashboard.build_data(full, TODAY, 4)
    tiles = {t["id"]: t for t in panel(data, "milestones")["tiles"]}
    assert tiles["longest_run"]["value"] == 15000
    assert tiles["biggest_week"]["value"] == 23000
    assert tiles["biggest_week"]["detail"] == "week of Mar 2"
    assert tiles["days_to_race"]["detail"] == "Test <City> Marathon, Jun 14, 2026"
    assert tiles["runs_logged"]["value"] == 4
    assert tiles["days_to_race"]["value"] == 91
    text = section(dashboard.render_html(data), "milestones")
    assert "Longest run so far" in text and "9.3 mi" in text


def test_km_display_follows_the_athlete_unit(full: sqlite3.Connection) -> None:
    with full:
        full.execute("UPDATE athlete SET units = 'km' WHERE id = 1")
    data = dashboard.build_data(full, TODAY, 4)
    assert data["unit"] == "km"
    page = dashboard.render_html(data)
    assert "23.0 km" in page and "Weekly kilometres" in page


# ---------------------------------------------------------------- the page


def test_page_has_three_css_tabs_today_first(full: sqlite3.Connection) -> None:
    page = dashboard.render_html(dashboard.build_data(full, TODAY, 4))
    assert '<input type="radio" name="view" id="v-today" checked>' in page
    assert '<input type="radio" name="view" id="v-week">' in page
    assert '<input type="radio" name="view" id="v-block">' in page
    assert '<label for="v-block">Block</label>' in page
    assert (
        '<label for="v-today">Today</label>' in page and '<label for="v-week">Week</label>' in page
    )
    today, week = view(page, "today"), view(page, "week")
    for panel_id in TODAY_SHOWN:
        assert f'id="{panel_id}"' in today
    # Weekly miles and milestones stay on Today only.
    assert "Weekly miles" not in week and "Milestones" not in week
    assert 'id="week_days"' in week


def test_page_is_self_contained_and_escaped(full: sqlite3.Connection) -> None:
    edit_week(full, {0: {"details": 'Long & easy <img src="x"> "loop"'}})
    page = dashboard.render_html(dashboard.build_data(full, TODAY, 4))
    low = page.lower()
    for banned in ("http", "<script", "<link", "<img", "@import", "url(", "onclick", "onload"):
        assert banned not in low
    assert "Test &lt;City&gt; Marathon" in page and "<City>" not in page
    assert "Long &amp; easy &lt;img src=&quot;x&quot;&gt; &quot;loop&quot;" in page
    assert page.startswith("<!doctype html>") and "Nothing on this page is uploaded." in page


def test_week_view_rows(full: sqlite3.Connection) -> None:
    page = dashboard.render_html(dashboard.build_data(full, "2026-03-11", 4))
    week = view(page, "week")
    assert week.count("<li ") == 7
    assert week.count("Up next") == 1
    up_next = week.split("Up next")[0].rsplit("<li ", 1)[1]
    assert "Thu" in up_next and "12" in up_next  # the first run day after today
    assert "Done · 3.7 mi" in week
    assert week.count('class="pill sm"') == 1 and ">Today</span>" in week  # rest day today
    assert "mi left this week" in week
    left = sum(
        d["planned_meters"]
        for d in panel(dashboard.build_data(full, "2026-03-11", 4), "this_week")["left"]
    )
    assert f"{left / 1609.344:.1f} mi left this week" in week


def test_week_view_done_today_shows_done_not_today_pill(full: sqlite3.Connection) -> None:
    week = view(dashboard.render_html(dashboard.build_data(full, TODAY, 4)), "week")
    assert "Done · 7.5 mi" in week and ">Today</span>" not in week
    assert "Up next" not in week  # nothing left this week


def test_past_run_day_with_nothing_logged_looks_different(full: sqlite3.Connection) -> None:
    page = dashboard.render_html(dashboard.build_data(full, TODAY, 4))  # Thu and Sat: nothing
    strip = section(page, "this_week")
    assert strip.count("g-past") == 2 and "g-plan" not in strip
    assert strip.count(", nothing logged</span>") == 2
    week = view(page, "week")
    thursday = week.split("<li ")[4]
    assert "g-past" in thursday and f'<span class="muted">{dashboard.MISSING}</span>' in thursday
    future = view(dashboard.render_html(dashboard.build_data(full, "2026-03-11", 4)), "week")
    assert "g-past" not in future and "nothing logged" not in future


def test_strip_glyphs_and_text_alternatives(full: sqlite3.Connection) -> None:
    edit_week(full, {3: {"kind": "strength"}})
    page = dashboard.render_html(dashboard.build_data(full, "2026-03-11", 4))
    strip = section(page, "this_week").split('<div class="strip">')[1].split("</div></div>")[0]
    assert strip.count('class="day') == 7 and strip.count('class="day is-today"') == 1
    for glyph in ("g-done", "g-gym", "g-rest", "g-plan"):
        assert glyph in strip
    assert 'aria-hidden="true"' in strip and 'class="sr"' in strip
    assert "Tue Mar 10: Easy run" in strip or "Tue Mar 10: Workout" in strip


def test_no_zero_for_missing_and_no_judgement_words(full: sqlite3.Connection) -> None:
    for day in ("2026-03-14", "2026-03-11", "2026-03-09", TODAY):
        page = dashboard.render_html(dashboard.build_data(full, day, 4))
        assert "0.0 mi" not in page and ">0.0<" not in page
        low = page.lower()
        for word in JUDGEMENT:
            assert word not in low, (day, word)
    monday = dashboard.render_html(dashboard.build_data(full, "2026-03-09", 4))
    ring = section(monday, "week_head")
    assert f">{dashboard.MISSING}</text>" in ring  # nothing run yet this week: a dash, not 0


# ---------------------------------------------------------------- CLI


def test_cli_writes_the_file(tmp_db: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["init-db", "--timezone", "America/Los_Angeles"]).exit_code == 0
    out = tmp_path / "nested" / "dashboard.html"
    fits = str(tmp_path / "fits")  # never the real exports folder
    r = runner.invoke(
        app, ["dashboard", "--out", str(out), "--weeks", "3", "--today", TODAY, "--fit-dir", fits]
    )
    assert r.exit_code == 0, r.output
    got = json.loads(r.output)
    assert set(got) == {"path", "panels", "weeks", "generated_for"}
    assert got["path"] == str(out) and got["weeks"] == 3 and got["generated_for"] == TODAY
    assert [p["id"] for p in got["panels"]] == SHOWN
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")
    bad = ["dashboard", "--out", str(out), "--weeks", "0", "--fit-dir", fits]
    assert runner.invoke(app, bad).exit_code == 1


# ---------------------------------------------------------------- body checks on weekly miles


def weekly_chart(data: dict[str, Any]) -> str:
    return section(dashboard.render_html(data), "weekly_distance")


def weekly_svg(data: dict[str, Any]) -> str:
    return weekly_chart(data).split('class="chart">')[1].split("</svg>")[0]


def test_weekly_distance_marks_weeks_with_yellow_or_red(full: sqlite3.Connection) -> None:
    for day, status in [
        ("2026-02-10", "red"),  # before the window
        ("2026-02-17", "green"),  # green is not marked
        ("2026-03-03", "yellow"),
        ("2026-03-05", "yellow"),
        ("2026-03-08", "red"),  # Sunday: still the week of 03-02
        ("2026-03-12", "red"),
        ("2026-03-20", "red"),  # after today
    ]:
        body.add_entry(full, body.BodyEntry(date=day, status=status, location="left <shin>"))
    data = dashboard.build_data(full, TODAY, 4)
    weekly = panel(data, "weekly_distance")
    assert [r["body"] for r in weekly["rows"]] == [{}, {}, {"yellow": 2, "red": 1}, {"red": 1}]
    assert all(
        "body" not in r
        for r in panel(dashboard.build_data(full, TODAY, 4, None, True), "long_run")["rows"]
    )

    chart = weekly_chart(data)
    svg = weekly_svg(data)
    assert svg.count('<path class="st-yellow"') == 1  # one mark per week and status
    assert svg.count('<rect class="st-red"') == 2
    assert "st-green" not in chart
    assert "<g><title>2 yellow, 1 red body checks, week of Mar 2</title>" in chart
    assert "<g><title>1 red body check, week of Mar 9</title>" in chart
    assert "body checks: 2 yellow, 1 red</title>" in chart  # also in that week's bar text
    assert "yellow or red body check" in chart.split("</title>")[0]  # the chart's description
    assert "Yellow body check</span>" in chart and "Red body check</span>" in chart
    assert "<th>Yellow or red body checks</th>" in chart
    assert "<td>2 yellow, 1 red</td></tr>" in chart and "<td>1 red</td></tr>" in chart
    assert "shin" not in chart  # where it is stays in the body log
    low = chart.lower()
    for word in ("should", "rest", "injur", "see a ", "careful", "risk"):
        assert word not in low


def test_marks_keep_a_fixed_row_per_status(full: sqlite3.Connection) -> None:
    body.add_entry(full, body.BodyEntry(date="2026-03-12", status="red"))  # red only, week 03-09
    one = weekly_svg(dashboard.build_data(full, TODAY, 4))
    body.add_entry(full, body.BodyEntry(date="2026-03-03", status="yellow"))
    body.add_entry(full, body.BodyEntry(date="2026-03-04", status="red"))
    two = weekly_svg(dashboard.build_data(full, TODAY, 4))
    assert dashboard.MARKED_STATUSES == ("yellow", "red")

    def red_ys(chart: str) -> list[str]:
        return [m.split('y="')[1].split('"')[0] for m in chart.split('<rect class="st-red"')[1:]]

    # A status keeps its row whether or not the other status has marks.
    assert set(red_ys(one)) == set(red_ys(two)) and len(set(red_ys(two))) == 1


def test_weekly_distance_without_yellow_or_red_has_no_marks(full: sqlite3.Connection) -> None:
    body.add_entry(full, body.BodyEntry(date="2026-03-12", status="green"))
    data = dashboard.build_data(full, TODAY, 4)
    assert all(r["body"] == {} for r in panel(data, "weekly_distance")["rows"])
    chart = weekly_chart(data)
    assert 'class="st-' not in chart and "body check" not in chart


# ---------------------------------------------------------------- data kept but not shown


def with_extra(*args: Any) -> dict[str, Any]:
    return dashboard.build_data(*args, include_extra=True)


def test_extra_is_off_by_default_and_fit_files_are_not_read(
    full: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("FIT files decoded for data the page does not show")

    monkeypatch.setattr(hr_pace, "report", boom)
    assert dashboard.build_data(full, TODAY, 4, tmp_path)["extra"] == []


def test_hidden_panels_are_not_rendered(full: sqlite3.Connection, tmp_path: Path) -> None:
    data = with_extra(full, TODAY, 4, tmp_path)
    assert [p["id"] for p in data["extra"]] == NOT_SHOWN
    page = dashboard.render_html(data)
    for panel_id in NOT_SHOWN:
        assert f'id="{panel_id}"' not in page
    assert "Carbs per hour" not in page and "Long-run build-up" not in page


def test_long_run_is_longest_per_week_vs_planned_long(full: sqlite3.Connection) -> None:
    rows = panel(with_extra(full, TODAY, 4), "long_run")["rows"]
    assert [r["value"] for r in rows] == [None, None, 15000, 12000]
    week = next(s for s in week_plan(full)["sessions"] if s["kind"] == "long")
    assert rows[3]["target"] == week["distance_meters"]


def test_fueling_only_plots_entries_with_a_rate(full: sqlite3.Connection) -> None:
    fuel = panel(with_extra(full, TODAY, 4), "fueling")
    assert fuel["rows"] == [{"date": "2026-03-08", "value": 40.0, "target": None}]
    assert "1 of 2" in fuel["note"]


def test_fueling_counts_every_entry_in_the_window(conn: sqlite3.Connection) -> None:
    """More entries than `fuel show` lists at once: none dropped, later ones don't crowd out."""
    for _ in range(2):
        fueling.add_entry(
            conn, fueling.FuelEntry(date="2026-03-08", duration_seconds=5400, carbs_grams=60)
        )
    for _ in range(fueling.MAX_LAST):
        fueling.add_entry(
            conn, fueling.FuelEntry(date="2026-04-01", duration_seconds=3600, carbs_grams=30)
        )
    fuel = panel(with_extra(conn, TODAY, 4), "fueling")
    assert [r["date"] for r in fuel["rows"]] == ["2026-03-08", "2026-03-08"]
    assert "2 of 2" in fuel["note"]
    later = panel(with_extra(conn, "2026-04-05", 8), "fueling")
    assert len(later["rows"]) == fueling.MAX_LAST + 2
    assert later["rows"][0]["date"] == "2026-03-08"


def test_fueling_logged_without_rate_is_no_data(conn: sqlite3.Connection) -> None:
    fueling.add_entry(conn, fueling.FuelEntry(date="2026-03-15", stomach=4))
    fuel = panel(with_extra(conn, TODAY, 4), "fueling")
    assert fuel["status"] == "no_data" and fuel["rows"] == []


def tuesday_run(folder: Path, weeks_ago: int, hr: int | None) -> None:
    """A synthetic 20-minute steady run at 2.6 m/s on the Tuesday `weeks_ago` weeks before TODAY's
    week (07:00 Los Angeles)."""
    start = datetime(2026, 3, 10, 14, 0, tzinfo=UTC) - timedelta(days=7 * weeks_ago)
    write_run(folder, f"run-{weeks_ago}.fit", start, [(1200, 2.6, hr)])


def test_hr_panel_no_folder_or_no_files(conn: sqlite3.Connection, tmp_path: Path) -> None:
    for folder in (None, tmp_path / "missing", tmp_path):
        hr = panel(with_extra(conn, TODAY, 4, folder), "hr_at_pace")
        assert hr["status"] == "no_data" and hr["band"] is None and hr["trend"] is None
        assert all(r["value"] is None for r in hr["rows"])  # missing, never zero


def test_hr_panel_runs_but_none_usable(conn: sqlite3.Connection, tmp_path: Path) -> None:
    tuesday_run(tmp_path, 0, None)  # no heart rate recorded
    hr = panel(with_extra(conn, TODAY, 4, tmp_path), "hr_at_pace")
    assert hr["status"] == "no_data"
    assert hr["note"].startswith("0 of 1 runs")


def test_hr_panel_weekly_means_band_and_gaps(conn: sqlite3.Connection, tmp_path: Path) -> None:
    tuesday_run(tmp_path, 0, 150)
    tuesday_run(tmp_path, 1, 152)
    tuesday_run(tmp_path, 3, 156)  # the week in between has no run
    hr = panel(with_extra(conn, TODAY, 4, tmp_path), "hr_at_pace")
    report = hr_pace.report(tmp_path, "America/Los_Angeles", TODAY, 4)
    assert hr["status"] == "ok" and hr["band"] == "10:05-10:35/mi"
    assert [r["value"] for r in hr["rows"]] == [w["mean_hr"] for w in report["weeks"]]
    assert [r["value"] for r in hr["rows"]] == [156.0, None, 152.0, 150.0]
    assert hr["trend"] is None  # four weeks cannot hold two periods
    assert hr["note"].startswith("3 of 3 runs")


def test_hr_panel_trend_sentence_with_enough_weeks(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    for weeks_ago, bpm in ((0, 150), (1, 150), (3, 150), (4, 156), (5, 156), (7, 156)):
        tuesday_run(tmp_path, weeks_ago, bpm)
    expected = (
        "HR at your usual pace (10:05-10:35/mi): 150 bpm over the last 4 weeks, 156 the 4 before"
    )
    assert panel(with_extra(conn, TODAY, 8, tmp_path), "hr_at_pace")["trend"] == expected


def test_body_panel_entries_in_the_window(conn: sqlite3.Connection) -> None:
    assert panel(with_extra(conn, TODAY, 4), "body_log")["rows"] == []
    entries = [
        ("2026-02-10", "red", "old entry"),  # before the 4-week window
        ("2026-02-17", "green", None),
        ("2026-03-03", "yellow", "left calf"),
        ("2026-03-03", "yellow", "right hip"),
        ("2026-03-12", "red", "shin"),
        ("2026-03-20", "green", "after today"),
    ]
    for day, status, where in entries:
        body.add_entry(conn, body.BodyEntry(date=day, status=status, location=where))
    log = panel(with_extra(conn, TODAY, 4), "body_log")
    assert log["status"] == "ok" and log["start"] == "2026-02-16" and log["end"] == TODAY
    assert [(r["date"], r["status"], r["location"]) for r in log["rows"]] == entries[1:5]


# ---------------------------------------------------------------- block view


def test_block_lists_every_plan_week_by_phase(full: sqlite3.Connection) -> None:
    data = dashboard.build_data(full, TODAY, 4)
    block = panel(data, "block")
    plan = db.load_accepted_plan(full)
    assert plan is not None
    rows = [r for g in block["phases"] for r in g["weeks"]]
    assert [r["start"] for r in rows] == [w["start_date"] for w in plan["weeks"]]
    # consecutive weeks of one phase share a group; groups follow plan order
    assert [g["phase"] for g in block["phases"]] == [
        p
        for i, p in enumerate(w["phase"] for w in plan["weeks"])
        if i == 0 or plan["weeks"][i - 1]["phase"] != p
    ]
    by_start = {r["start"]: r for r in rows}
    assert by_start["2026-03-02"]["state"] == "past"
    assert by_start["2026-03-02"]["done_meters"] == 23000
    assert by_start["2026-03-02"]["longest_meters"] == 15000
    current = by_start["2026-03-09"]
    assert current["state"] == "current" and current["done_meters"] == 18000
    assert by_start["2026-02-23"]["done_meters"] is None  # nothing logged: missing, not zero
    ahead = [r for r in rows if r["start"] > TODAY]
    assert ahead and all(r["state"] == "ahead" and r["done_meters"] is None for r in ahead)
    for r, w in zip(rows, plan["weeks"], strict=True):
        runs = [s for s in w["sessions"] if s["kind"] not in ("rest", "strength")]
        assert r["planned_meters"] == sum(s["distance_meters"] for s in runs)


def test_block_planned_total_follows_sessions_after_a_cut(full: sqlite3.Connection) -> None:
    edit_week(full, {0: {"distance_meters": 5000}})  # long run lowered; target left as it was
    row = next(
        r
        for g in panel(dashboard.build_data(full, TODAY, 4), "block")["phases"]
        for r in g["weeks"]
        if r["start"] == "2026-03-09"
    )
    plan = db.load_accepted_plan(full)
    assert plan is not None
    week = next(w for w in plan["weeks"] if w["start_date"] == "2026-03-09")
    runs = [s for s in week["sessions"] if s["kind"] not in ("rest", "strength")]
    assert row["planned_meters"] == sum(s["distance_meters"] for s in runs)
    assert row["planned_long_meters"] == 5000


def test_block_view_page(full: sqlite3.Connection) -> None:
    body.add_entry(full, body.BodyEntry(date="2026-03-10", status="yellow", location="calf"))
    page = dashboard.render_html(dashboard.build_data(full, TODAY, 4))
    block = view(page, "block")
    assert "Training block" in block and 'class="wkrow now"' in block
    assert block.count('class="wkrow') == len(db.load_accepted_plan(full)["weeks"])  # type: ignore[index]
    assert "Base" in block and "Taper" in block and "Race week" in block
    assert "14.3 mi done<br>" in block and "11.2 mi done so far" in block
    assert "so far" in block and "yellow" in block
    assert not any(word in block.lower() for word in JUDGEMENT)
