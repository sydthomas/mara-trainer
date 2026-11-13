"""Progress dashboard: panels come from the existing rollups; missing stays missing; one offline
HTML file. Synthetic data only."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from mara_trainer import body, dashboard, db, fueling, hr_pace
from mara_trainer.cli import app
from mara_trainer.plan_rules import RaceSetup, build_schedule_brief, validate_schedule

from .plan_factory import build_valid_plan
from .test_hr_pace import write_run

TODAY = "2026-03-15"  # a Sunday; its Monday-Sunday week starts 2026-03-09
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


def add_run(conn: sqlite3.Connection, started_at: str, meters: int, seconds: int = 3600) -> None:
    with conn:
        conn.execute(
            "INSERT INTO activities(source, source_id, fingerprint, title, started_at, "
            "distance_meters, duration_seconds) VALUES ('manual', ?, ?, 'Run', ?, ?, ?)",
            (started_at, f"{abs(hash(started_at)):064d}"[:64], started_at, meters, seconds),
        )


@pytest.fixture
def full(conn: sqlite3.Connection) -> sqlite3.Connection:
    db.save_race_setup(conn, SETUP)
    brief = build_schedule_brief(SETUP, PLAN_ANCHOR)
    db.save_plan(conn, validate_schedule(build_valid_plan(SETUP, brief), SETUP, brief))
    add_run(conn, "2026-03-03T16:00:00.000Z", 8000)  # week of 03-02
    add_run(conn, "2026-03-08T16:00:00.000Z", 15000)  # Sunday, still week of 03-02
    add_run(conn, "2026-03-10T16:00:00.000Z", 6000)  # week of 03-09
    add_run(conn, "2026-03-15T16:00:00.000Z", 12000)  # today
    fueling.add_entry(
        conn, fueling.FuelEntry(date="2026-03-08", duration_seconds=5400, carbs_grams=60)
    )
    fueling.add_entry(conn, fueling.FuelEntry(date="2026-03-15", stomach=4))  # no carbs: no rate
    return conn


def panel(data: dict[str, Any], panel_id: str) -> dict[str, Any]:
    return next(p for p in data["panels"] if p["id"] == panel_id)


def test_empty_db_every_panel_says_no_data(conn: sqlite3.Connection) -> None:
    data = dashboard.build_data(conn, TODAY, 4)
    assert data["generated_for"] == TODAY and data["weeks"] == 4 and data["unit"] == "mi"
    assert [p["id"] for p in data["panels"]] == [
        "milestones",
        "weekly_distance",
        "long_run",
        "fueling",
        "hr_at_pace",
        "body_log",
    ]
    assert all(p["status"] == "no_data" for p in data["panels"])
    assert all(t["value"] is None for t in panel(data, "milestones")["tiles"])
    weekly = panel(data, "weekly_distance")["rows"]
    assert len(weekly) == 4
    assert all(r["value"] is None and r["target"] is None for r in weekly)  # missing, not zero
    page = dashboard.render_html(data)
    assert page.count("No data yet") == len(data["panels"])
    assert "<svg" not in page


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


def test_long_run_is_longest_per_week_vs_planned_long(full: sqlite3.Connection) -> None:
    rows = panel(dashboard.build_data(full, TODAY, 4), "long_run")["rows"]
    assert [r["value"] for r in rows] == [None, None, 15000, 12000]
    plan = db.load_accepted_plan(full)
    assert plan is not None
    week = next(w for w in plan["weeks"] if w["start_date"] == "2026-03-09")
    planned = next(s["distance_meters"] for s in week["sessions"] if s["kind"] == "long")
    assert rows[3]["target"] == planned


def test_fueling_only_plots_entries_with_a_rate(full: sqlite3.Connection) -> None:
    fuel = panel(dashboard.build_data(full, TODAY, 4), "fueling")
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
    fuel = panel(dashboard.build_data(conn, TODAY, 4), "fueling")
    assert [r["date"] for r in fuel["rows"]] == ["2026-03-08", "2026-03-08"]
    assert "2 of 2" in fuel["note"]
    later = panel(dashboard.build_data(conn, "2026-04-05", 8), "fueling")
    assert len(later["rows"]) == fueling.MAX_LAST + 2
    assert later["rows"][0]["date"] == "2026-03-08"


def test_fueling_logged_without_rate_is_no_data(conn: sqlite3.Connection) -> None:
    fueling.add_entry(conn, fueling.FuelEntry(date="2026-03-15", stomach=4))
    fuel = panel(dashboard.build_data(conn, TODAY, 4), "fueling")
    assert fuel["status"] == "no_data" and fuel["rows"] == []


def test_milestones(full: sqlite3.Connection) -> None:
    tiles = {t["id"]: t for t in panel(dashboard.build_data(full, TODAY, 4), "milestones")["tiles"]}
    assert tiles["longest_run"]["value"] == 15000
    assert tiles["biggest_week"]["value"] == 23000
    assert tiles["biggest_week"]["detail"] == "week of 2026-03-02"
    assert tiles["runs_logged"]["value"] == 4
    assert tiles["days_to_race"]["value"] == 91


def test_today_hides_later_runs(full: sqlite3.Connection) -> None:
    data = dashboard.build_data(full, "2026-03-09", 2)
    tiles = {t["id"]: t for t in panel(data, "milestones")["tiles"]}
    assert tiles["runs_logged"]["value"] == 2
    assert [r["value"] for r in panel(data, "weekly_distance")["rows"]] == [23000, None]


def test_render_html_is_self_contained_and_escaped(full: sqlite3.Connection) -> None:
    page = dashboard.render_html(dashboard.build_data(full, TODAY, 4))
    for heading in ("Milestones", "Weekly distance", "Long-run build-up", "Carbs per hour"):
        assert f">{heading}</h2>" in page
    low = page.lower()
    for banned in ("http", "<script", "<link", "<img", "@import", "url("):
        assert banned not in low
    assert "Test &lt;City&gt; Marathon" in page and "<City>" not in page
    assert "prefers-color-scheme: dark" in page
    assert "14.3 mi" in page  # 23000 m shown in miles
    assert page.count("<svg") == 3


def test_km_display_follows_the_athlete_unit(full: sqlite3.Connection) -> None:
    with full:
        full.execute("UPDATE athlete SET units = 'km' WHERE id = 1")
    data = dashboard.build_data(full, TODAY, 4)
    assert data["unit"] == "km"
    assert "23.0 km" in dashboard.render_html(data)


def test_bad_arguments(conn: sqlite3.Connection) -> None:
    with pytest.raises(dashboard.DashboardError):
        dashboard.build_data(conn, TODAY, 0)
    with pytest.raises(dashboard.DashboardError):
        dashboard.build_data(conn, "next week", 4)


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
    assert got["path"] == str(out) and got["weeks"] == 3 and got["generated_for"] == TODAY
    assert [p["id"] for p in got["panels"]] == [
        "milestones",
        "weekly_distance",
        "long_run",
        "fueling",
        "hr_at_pace",
        "body_log",
    ]
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")
    bad = ["dashboard", "--out", str(out), "--weeks", "0", "--fit-dir", fits]
    assert runner.invoke(app, bad).exit_code == 1


# ---------------------------------------------------------------- heart rate at pace


def tuesday_run(folder: Path, weeks_ago: int, hr: int | None) -> None:
    """A synthetic 20-minute steady run at 2.6 m/s on the Tuesday `weeks_ago` weeks before TODAY's
    week (07:00 Los Angeles)."""
    start = datetime(2026, 3, 10, 14, 0, tzinfo=UTC) - timedelta(days=7 * weeks_ago)
    write_run(folder, f"run-{weeks_ago}.fit", start, [(1200, 2.6, hr)])


def test_hr_panel_no_folder_or_no_files(conn: sqlite3.Connection, tmp_path: Path) -> None:
    for folder in (None, tmp_path / "missing", tmp_path):
        hr = panel(dashboard.build_data(conn, TODAY, 4, folder), "hr_at_pace")
        assert hr["status"] == "no_data" and hr["band"] is None and hr["trend"] is None
        assert all(r["value"] is None for r in hr["rows"])  # missing, never zero
    page = dashboard.render_html(dashboard.build_data(conn, TODAY, 4, tmp_path))
    section = page.split('id="hr_at_pace"')[1].split("</section>")[0]
    assert "No data yet" in section and "<svg" not in section
    assert hr_pace.TREND_CAVEAT in section


def test_hr_panel_runs_but_none_usable(conn: sqlite3.Connection, tmp_path: Path) -> None:
    tuesday_run(tmp_path, 0, None)  # no heart rate recorded
    hr = panel(dashboard.build_data(conn, TODAY, 4, tmp_path), "hr_at_pace")
    assert hr["status"] == "no_data"
    assert hr["note"].startswith("0 of 1 runs")
    assert "No data yet" in dashboard.render_html(dashboard.build_data(conn, TODAY, 4, tmp_path))


def test_hr_panel_weekly_means_band_and_gaps(conn: sqlite3.Connection, tmp_path: Path) -> None:
    tuesday_run(tmp_path, 0, 150)
    tuesday_run(tmp_path, 1, 152)
    tuesday_run(tmp_path, 3, 156)  # the week in between has no run
    data = dashboard.build_data(conn, TODAY, 4, tmp_path)
    hr = panel(data, "hr_at_pace")
    report = hr_pace.report(tmp_path, "America/Los_Angeles", TODAY, 4)
    assert hr["status"] == "ok" and hr["band"] == "10:05-10:35/mi"
    assert [r["date"] for r in hr["rows"]] == [
        "2026-02-16",
        "2026-02-23",
        "2026-03-02",
        "2026-03-09",
    ]
    assert [r["value"] for r in hr["rows"]] == [w["mean_hr"] for w in report["weeks"]]
    assert [r["value"] for r in hr["rows"]] == [156.0, None, 152.0, 150.0]
    assert hr["trend"] is None  # four weeks cannot hold two periods
    assert hr["note"].startswith("3 of 3 runs")
    section = dashboard.render_html(data).split('id="hr_at_pace"')[1].split("</section>")[0]
    assert "10:05-10:35/mi" in section and hr_pace.TREND_CAVEAT in section
    assert section.count('class="dot"') == 3  # one mark per week with data
    assert section.count('class="line"') == 1  # only the two neighbouring weeks are joined
    assert "<td>2026-02-23</td><td>\N{EN DASH}</td><td>0</td>" in section
    assert "<td>2026-03-09</td><td>150 bpm</td><td>1</td>" in section
    assert "No data yet" not in section and "over the last 4 weeks" not in section


def test_hr_panel_shows_the_trend_sentence_with_enough_weeks(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    for weeks_ago, bpm in ((0, 150), (1, 150), (3, 150), (4, 156), (5, 156), (7, 156)):
        tuesday_run(tmp_path, weeks_ago, bpm)
    data = dashboard.build_data(conn, TODAY, 8, tmp_path)
    expected = (
        "HR at your usual pace (10:05-10:35/mi): 150 bpm over the last 4 weeks, 156 the 4 before"
    )
    assert panel(data, "hr_at_pace")["trend"] == expected
    assert f"{expected}." in dashboard.render_html(data)


# ---------------------------------------------------------------- body log


def test_body_panel_empty(conn: sqlite3.Connection) -> None:
    data = dashboard.build_data(conn, TODAY, 4)
    log = panel(data, "body_log")
    assert log["status"] == "no_data" and log["rows"] == []
    section = dashboard.render_html(data).split('id="body_log"')[1].split("</section>")[0]
    assert "No data yet" in section and "<svg" not in section


def test_body_panel_one_mark_per_entry(conn: sqlite3.Connection) -> None:
    entries = [
        ("2026-02-10", "red", "old entry"),  # before the 4-week window
        ("2026-02-17", "green", None),
        ("2026-03-03", "yellow", "left <calf> & ankle"),
        ("2026-03-03", "yellow", "right hip"),  # same day, same status: still its own mark
        ("2026-03-12", "red", "shin"),
        ("2026-03-20", "green", "after today"),
    ]
    for day, status, where in entries:
        body.add_entry(conn, body.BodyEntry(date=day, status=status, location=where))
    data = dashboard.build_data(conn, TODAY, 4)
    log = panel(data, "body_log")
    assert log["status"] == "ok" and log["start"] == "2026-02-16" and log["end"] == TODAY
    assert [(r["date"], r["status"], r["location"]) for r in log["rows"]] == entries[1:5]
    section = dashboard.render_html(data).split('id="body_log"')[1].split("</section>")[0]
    chart = section.split('<div class="chart">')[1].split("</div>")[0]
    # Shape differs by status (circle, triangle, square), so colour is never the only cue.
    assert chart.count('<circle class="st-green"') == 1
    assert chart.count('<path class="st-yellow"') == 2
    assert chart.count('<rect class="st-red"') == 1
    for label in ("Green", "Yellow", "Red"):
        assert f">{label}</text>" in chart and f"{label}</span>" in section
    assert "<calf>" not in section
    assert "<title>Mar 3: yellow, left &lt;calf&gt; &amp; ankle</title>" in chart
    assert "<td>2026-03-03</td><td>Yellow</td><td>left &lt;calf&gt; &amp; ankle</td>" in section
    assert "<td>2026-02-17</td><td>Green</td><td>\N{EN DASH}</td>" in section  # no location given
    assert "old entry" not in section and "after today" not in section


def test_new_panels_carry_no_advice(conn: sqlite3.Connection, tmp_path: Path) -> None:
    tuesday_run(tmp_path, 0, 150)
    body.add_entry(conn, body.BodyEntry(date="2026-03-12", status="red", pain=7))
    page = dashboard.render_html(dashboard.build_data(conn, TODAY, 4, tmp_path)).lower()
    tail = page.split('id="hr_at_pace"')[1]
    for word in ("fitter", "fatigue", "should", "rest day", "see a ", "improv"):
        assert word not in tail
    assert "<script" not in page and "http" not in page
