from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mara_trainer import hr_pace
from mara_trainer.cli import app

from .fit_builder import build_fit, steady_records

# Synthetic inputs only. 2.6 m/s = 10:19/mi (auto band 10:05-10:35); 3.0 m/s = 8:56/mi (8:40-9:10).
TZ = "America/Los_Angeles"
EASY = 2.6
FAST = 3.0
TODAY = "2026-03-08"  # a Sunday; its week is Mon 2026-03-02 .. Sun 2026-03-08
RUN_START = datetime(2026, 3, 3, 15, 0, tzinfo=UTC)  # Tue 07:00 local


def write_run(
    folder: Path,
    name: str,
    start: datetime,
    parts: list[tuple[int, float | None, int | None]],
    *,
    enhanced_speed: bool = False,
    sport: str = "running",
) -> None:
    """parts: consecutive (seconds, speed m/s, heart rate) blocks, one record per second."""
    records: list[dict[str, object]] = []
    offset, distance = 0, 0.0
    for seconds, speed, hr in parts:
        block = steady_records(start, seconds, speed, hr, offset=offset, distance=distance)
        records += block
        offset += seconds
        distance += (speed or 0.0) * seconds
    session: dict[str, object] = {
        "start": start,
        "meters": max(distance, 1.0),
        "seconds": offset,
        "sport": sport,
    }
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(build_fit([session], records, enhanced_speed=enhanced_speed))


def run_report(folder: Path, **kwargs: object) -> dict:  # type: ignore[type-arg]
    return hr_pace.report(folder, TZ, TODAY, **kwargs)  # type: ignore[arg-type]


def test_steady_run_in_band(tmp_path: Path) -> None:
    write_run(tmp_path, "a.fit", RUN_START, [(1800, EASY, 140)])
    out = run_report(tmp_path, weeks=2)
    assert out["band"]["chosen_by"] == "most_used_pace"
    # Centred on the pace she ran (10:19 -> 10:20), not on a fixed 10:00/10:30 grid.
    assert (out["band"]["pace_min"], out["band"]["pace_max"]) == ("10:05/mi", "10:35/mi")
    assert out["band"]["steady_seconds_in_band"] == 1200
    (row,) = out["runs"]
    assert row["status"] == "used"
    assert row["date"] == "2026-03-03" and row["week_start"] == "2026-03-02"
    assert row["seconds_in_band"] == 1200  # 30 min minus the 10 min warm-up
    assert row["mean_hr"] == 140.0
    assert row["mean_pace"] == "10:19/mi"
    assert row["mean_pace_seconds_per_km"] == pytest.approx(1000 / EASY, abs=0.1)
    assert [w["week_start"] for w in out["weeks"]] == ["2026-02-23", "2026-03-02"]
    assert out["weeks"][0]["mean_hr"] is None and out["weeks"][0]["runs_used"] == 0
    assert out["weeks"][1]["mean_hr"] == 140.0 and out["weeks"][1]["seconds_in_band"] == 1200
    cov = out["coverage"]
    assert (cov["files_read"], cov["runs_in_window"], cov["runs_used"]) == (1, 1, 1)
    assert cov["runs_without_hr"] == 0 and cov["runs_without_time_in_band"] == 0
    assert {"wrist_hr_noise", "conditions_vary", "trend_not_fitness_score"} <= set(out["caveats"])
    assert out["definition"]["kind"] == "descriptive_defaults_not_coaching_rules"


def test_enhanced_speed_field_is_read(tmp_path: Path) -> None:
    write_run(tmp_path, "a.fit", RUN_START, [(1800, EASY, 150)], enhanced_speed=True)
    assert run_report(tmp_path)["runs"][0]["mean_hr"] == 150.0


def test_speed_falls_back_to_distance(tmp_path: Path) -> None:
    records = steady_records(RUN_START, 1800, EASY, 141)
    for r in records:
        r["speed"] = None  # distance still climbs 2.6 m every second
    session: dict[str, object] = {"start": RUN_START, "meters": 4680, "seconds": 1800}
    (tmp_path / "a.fit").write_bytes(build_fit([session], records))
    row = run_report(tmp_path)["runs"][0]
    assert (row["status"], row["seconds_in_band"], row["mean_hr"]) == ("used", 1200, 141.0)


def test_run_without_hr_is_counted_not_averaged(tmp_path: Path) -> None:
    write_run(tmp_path, "a.fit", RUN_START, [(1800, EASY, 140)])
    write_run(tmp_path, "b.fit", datetime(2026, 3, 5, 15, 0, tzinfo=UTC), [(1800, EASY, None)])
    out = run_report(tmp_path)
    by_file = {r["file"]: r for r in out["runs"]}
    assert by_file["b.fit"]["status"] == "no_hr"
    assert by_file["b.fit"]["mean_hr"] is None  # never imputed
    assert by_file["b.fit"]["seconds_in_band"] == 1200
    assert out["coverage"]["runs_without_hr"] == 1 and out["coverage"]["runs_used"] == 1
    week = out["weeks"][-1]
    assert (week["runs_used"], week["mean_hr"], week["seconds_in_band"]) == (1, 140.0, 1200)


def test_warmup_is_excluded(tmp_path: Path) -> None:
    # A high heart rate in the first 10 minutes must not reach the mean.
    write_run(tmp_path, "a.fit", RUN_START, [(600, EASY, 180), (900, EASY, 140)])
    row = run_report(tmp_path)["runs"][0]
    assert (row["seconds_in_band"], row["mean_hr"]) == (900, 140.0)
    # A run that ends inside the warm-up has no time in band.
    write_run(tmp_path, "b.fit", datetime(2026, 3, 4, 15, 0, tzinfo=UTC), [(540, EASY, 150)])
    out = run_report(tmp_path)
    assert {r["file"]: r["status"] for r in out["runs"]}["b.fit"] == "no_time_in_band"
    assert out["coverage"]["runs_without_time_in_band"] == 1


def test_stretch_needs_five_continuous_minutes(tmp_path: Path) -> None:
    # After the warm-up: 4 min easy, a 60 s stop (no records), 4 min easy, then 20 min faster.
    start = RUN_START
    records = steady_records(start, 840, EASY, 140)
    records += steady_records(start, 240, EASY, 140, offset=900, distance=840 * EASY)
    records += steady_records(start, 1200, FAST, 160, offset=1140, distance=1080 * EASY)
    session: dict[str, object] = {"start": start, "meters": 6408, "seconds": 2340}
    (tmp_path / "a.fit").write_bytes(build_fit([session], records))
    out = run_report(tmp_path, pace_min_seconds=600, pace_max_seconds=630)
    assert out["band"]["chosen_by"] == "arguments"
    assert out["runs"][0]["status"] == "no_time_in_band"
    assert out["runs"][0]["seconds_in_band"] == 0 and out["runs"][0]["mean_pace"] is None


def test_band_auto_selection_picks_most_used_pace(tmp_path: Path) -> None:
    thursday = datetime(2026, 3, 5, 15, 0, tzinfo=UTC)
    write_run(tmp_path, "a.fit", RUN_START, [(600, EASY, 130), (600, FAST, 165), (900, EASY, 142)])
    write_run(tmp_path, "b.fit", thursday, [(600, EASY, 130), (900, FAST, 160)])
    out = run_report(tmp_path)
    # After the warm-ups: about 1500 s near the fast pace (over two runs), about 900 s near easy.
    assert out["band"]["pace_min"] == "8:40/mi" and out["band"]["pace_max"] == "9:10/mi"
    write_run(tmp_path, "c.fit", datetime(2026, 3, 6, 15, 0, tzinfo=UTC), [(2400, EASY, 141)])
    out = run_report(tmp_path)
    assert out["band"]["pace_min"] == "10:05/mi"
    assert out["band"]["steady_seconds_in_band"] > 2500
    statuses = {r["file"]: r["status"] for r in out["runs"]}
    assert statuses == {"a.fit": "used", "b.fit": "no_time_in_band", "c.fit": "used"}
    # Override: the same files measured at the faster band, in km.
    out = run_report(tmp_path, pace_min_seconds=320, pace_max_seconds=345, unit="km")
    assert out["band"]["chosen_by"] == "arguments" and out["band"]["pace_min"] == "5:20/km"
    statuses = {r["file"]: r["status"] for r in out["runs"]}
    assert statuses == {"a.fit": "used", "b.fit": "used", "c.fit": "no_time_in_band"}
    assert out["runs"][1]["mean_hr"] == 160.0


def paced_run(folder: Path, name: str, paces: list[float], hr: int = 140) -> None:
    """One record per second at the given paces (seconds per mile), starting at RUN_START."""
    records: list[dict[str, object]] = []
    distance = 0.0
    for i, pace in enumerate(paces):
        speed = hr_pace.MI / pace
        distance += speed
        when = RUN_START + timedelta(seconds=i + 1)
        records.append({"time": when, "hr": hr, "speed": speed, "distance": distance})
    session: dict[str, object] = {"start": RUN_START, "meters": distance, "seconds": len(paces)}
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(build_fit([session], records))


@pytest.mark.parametrize("usual_pace", [600, 630, 607])
def test_auto_band_is_centred_on_a_pace_that_wobbles(tmp_path: Path, usual_pace: int) -> None:
    # A slow +/-4 s/mile wobble around the usual pace, also when that pace is a round 10:00 or
    # 10:30: the whole run after the warm-up is one steady stretch.
    paced_run(tmp_path, "a.fit", [usual_pace + 4 * math.sin(i / 120) for i in range(3600)])
    out = run_report(tmp_path)
    centre = round(usual_pace / 5) * 5
    assert out["band"]["pace_min_seconds_per_km"] == pytest.approx(
        (centre - 15) / 1.609344, abs=0.1
    )
    assert out["band"]["pace_max_seconds_per_km"] == pytest.approx(
        (centre + 15) / 1.609344, abs=0.1
    )
    (row,) = out["runs"]
    assert (row["status"], row["seconds_in_band"], row["mean_hr"]) == ("used", 3000, 140.0)


def test_short_excursion_does_not_end_a_stretch(tmp_path: Path) -> None:
    # After the warm-up: 4 min at 10:15, N seconds at 11:40, 4 min at 10:15. A 10 s slow patch
    # holds the 30 s mean pace outside 10:00-10:30 for 29 s (from its 6th second on).
    def seconds_in_band(folder: Path, outside: int) -> int:
        paced_run(folder, "a.fit", [615.0] * 840 + [700.0] * outside + [615.0] * 240)
        out = run_report(folder, pace_min_seconds=600, pace_max_seconds=630)
        return int(out["runs"][0]["seconds_in_band"])

    # One stretch: 490 s after the warm-up minus the 29 s outside, which are not counted.
    assert seconds_in_band(tmp_path / "short", 10) == 461
    assert seconds_in_band(tmp_path / "long", 40) == 0  # two 4-minute pieces, neither counts


def test_smoothing_window_is_thirty_seconds(tmp_path: Path) -> None:
    # 5 min easy after the warm-up, then a step to the faster speed. With a 30 s trailing mean
    # the pace leaves 10:00-10:30 on the 7th faster second and enters 8:30-9:00 on the 29th.
    write_run(tmp_path, "a.fit", RUN_START, [(900, EASY, 140), (600, FAST, 160)])
    easy = run_report(tmp_path, pace_min_seconds=600, pace_max_seconds=630)["runs"][0]
    assert easy["seconds_in_band"] == 306
    assert easy["mean_hr"] == round((300 * 140 + 6 * 160) / 306, 1)
    fast = run_report(tmp_path, pace_min_seconds=510, pace_max_seconds=540)["runs"][0]
    assert (fast["seconds_in_band"], fast["mean_hr"]) == (572, 160.0)


def test_alternating_speeds_are_smoothed(tmp_path: Path) -> None:
    # Per-second speed flips between 2.4 and 2.8 m/s (9:35 and 11:11 per mile, both far outside
    # the band); the 30 s mean is a constant 2.6 m/s = 10:19/mi.
    records = steady_records(RUN_START, 1800, EASY, 140)
    for i, r in enumerate(records):
        r["speed"] = 2.4 if i % 2 else 2.8
    session: dict[str, object] = {"start": RUN_START, "meters": 4680, "seconds": 1800}
    (tmp_path / "a.fit").write_bytes(build_fit([session], records))
    row = run_report(tmp_path, pace_min_seconds=618, pace_max_seconds=620)["runs"][0]
    assert (row["status"], row["seconds_in_band"], row["mean_pace"]) == ("used", 1200, "10:19/mi")


def test_standing_still_with_records_ends_a_stretch(tmp_path: Path) -> None:
    # After the warm-up: 4 min easy, 60 s at speed 0 (the watch keeps recording), 4 min easy.
    write_run(tmp_path, "a.fit", RUN_START, [(840, EASY, 140), (60, 0.0, 120), (240, EASY, 140)])
    out = run_report(tmp_path, pace_min_seconds=600, pace_max_seconds=630)
    assert out["runs"][0]["status"] == "no_time_in_band"
    assert out["runs"][0]["seconds_in_band"] == 0
    # A 20 s stop is shorter than the excursion allowance but still ends the stretch.
    write_run(tmp_path, "a.fit", RUN_START, [(840, EASY, 140), (20, 0.0, 120), (240, EASY, 140)])
    out = run_report(tmp_path, pace_min_seconds=600, pace_max_seconds=630)
    assert out["runs"][0]["seconds_in_band"] == 0


def test_week_bucketing_across_timezone_boundary(tmp_path: Path) -> None:
    # 2026-03-02T06:30Z is Sunday 22:30 in Los Angeles (previous week) but Monday in UTC.
    write_run(tmp_path, "late.fit", datetime(2026, 3, 2, 6, 30, tzinfo=UTC), [(1800, EASY, 150)])
    write_run(tmp_path, "mon.fit", datetime(2026, 3, 2, 15, 0, tzinfo=UTC), [(1800, EASY, 140)])
    out = run_report(tmp_path, weeks=2)
    by_file = {r["file"]: r for r in out["runs"]}
    assert (by_file["late.fit"]["date"], by_file["late.fit"]["week_start"]) == (
        "2026-03-01",
        "2026-02-23",
    )
    assert by_file["mon.fit"]["week_start"] == "2026-03-02"
    assert [w["mean_hr"] for w in out["weeks"]] == [150.0, 140.0]
    assert [(r["weeks_with_data"], r["mean_hr"]) for r in out["rolling_4_week"]] == [
        (1, 150.0),
        (2, 145.0),
    ]
    # The same instants in UTC land in one week.
    utc = hr_pace.report(tmp_path, "UTC", TODAY, weeks=2)
    assert [w["runs_used"] for w in utc["weeks"]] == [0, 2]


def test_rolling_mean_skips_weeks_without_data(tmp_path: Path) -> None:
    for name, day, hr in (("w1.fit", 3, 150), ("w3.fit", 17, 144), ("w6.fit", 7, 138)):
        month = 4 if name == "w6.fit" else 3
        write_run(tmp_path, name, datetime(2026, month, day, 15, 0, tzinfo=UTC), [(1800, EASY, hr)])
    out = hr_pace.report(tmp_path, TZ, "2026-04-12", weeks=6)
    assert [w["mean_hr"] for w in out["weeks"]] == [150.0, None, 144.0, None, None, 138.0]
    rolling = [(r["weeks_with_data"], r["mean_hr"]) for r in out["rolling_4_week"]]
    assert rolling == [(1, 150.0), (1, 150.0), (2, 147.0), (2, 147.0), (1, 144.0), (2, 141.0)]


def test_window_duplicates_and_other_files(tmp_path: Path) -> None:
    write_run(tmp_path, "a.fit", RUN_START, [(1800, EASY, 140)])
    write_run(tmp_path, "a-copy.fit", RUN_START, [(1800, EASY, 140)])
    write_run(tmp_path, "old.fit", datetime(2025, 3, 3, 15, 0, tzinfo=UTC), [(1800, EASY, 140)])
    write_run(tmp_path, "ride.fit", RUN_START, [(1800, 6.0, 120)], sport="cycling")
    (tmp_path / "broken.fit").write_bytes(b"not a fit file")
    (tmp_path / "notes.txt").write_text("ignored")
    cov = run_report(tmp_path)["coverage"]
    assert cov == {
        "files_read": 4,
        "files_unreadable": 1,
        "files_without_running": 1,
        "runs_outside_window": 1,
        "duplicate_runs_skipped": 1,
        "runs_in_window": 1,
        "runs_used": 1,
        "runs_without_hr": 0,
        "runs_without_time_in_band": 0,
    }


@pytest.mark.parametrize("make_dir", [True, False])
def test_empty_or_missing_directory(tmp_path: Path, make_dir: bool) -> None:
    folder = tmp_path / "coros"
    if make_dir:
        folder.mkdir()
    out = run_report(folder, weeks=3)
    assert out["band"]["chosen_by"] == "none" and out["band"]["pace_min"] is None
    assert out["runs"] == [] and len(out["weeks"]) == 3
    assert all(w["mean_hr"] is None and w["mean_pace"] is None for w in out["weeks"])
    assert all(r["mean_hr"] is None for r in out["rolling_4_week"])
    assert set(out["coverage"].values()) == {0}


def test_bad_arguments(tmp_path: Path) -> None:
    for kwargs in (
        {"weeks": 0},
        {"unit": "furlong"},
        {"pace_min_seconds": 600},
        {"pace_min_seconds": 630, "pace_max_seconds": 600},
    ):
        with pytest.raises(ValueError):
            run_report(tmp_path, **kwargs)
    assert hr_pace.parse_pace("10:05") == 605
    for bad in ("10", "10:5", "10:75", "a:30"):
        with pytest.raises(ValueError):
            hr_pace.parse_pace(bad)


def test_cli_prints_json(tmp_path: Path, tmp_db: Path) -> None:
    runner = CliRunner()
    folder = tmp_path / "fits"
    args = ["hr-pace", "--dir", str(folder), "--today", TODAY, "--weeks", "2"]
    assert runner.invoke(app, args).exit_code == 1  # no DB yet: no athlete timezone
    assert runner.invoke(app, ["init-db", "--timezone", TZ]).exit_code == 0
    write_run(folder, "a.fit", RUN_START, [(1800, EASY, 140)])
    r = runner.invoke(app, args)
    assert r.exit_code == 0
    data = json.loads(r.output)
    assert data["window"]["timezone"] == TZ and data["weeks"][-1]["mean_hr"] == 140.0
    r = runner.invoke(app, [*args, "--pace-min", "8:30", "--pace-max", "9:00"])
    assert json.loads(r.output)["runs"][0]["status"] == "no_time_in_band"
    assert runner.invoke(app, [*args, "--pace-min", "8:30"]).exit_code == 1
    assert runner.invoke(app, [*args, "--pace-min", "fast", "--pace-max", "9:00"]).exit_code == 1


# ------------------------------------------------------------------ band width


def test_band_width_widens_the_automatic_band(tmp_path: Path) -> None:
    write_run(tmp_path, "a.fit", RUN_START, [(1800, EASY, 140)])
    default = run_report(tmp_path, weeks=2)
    assert default["definition"]["band_width_seconds_per_mile"] == 30
    wide = run_report(tmp_path, weeks=2, band_width_seconds_per_mile=60)
    assert (wide["band"]["pace_min"], wide["band"]["pace_max"]) == ("9:50/mi", "10:50/mi")
    assert wide["band"]["chosen_by"] == "most_used_pace"
    assert wide["definition"]["band_width_seconds_per_mile"] == 60
    assert run_report(tmp_path, weeks=2, band_width_seconds_per_mile=30)["band"] == default["band"]


def test_band_width_bad_values(tmp_path: Path) -> None:
    for width in (0, -30, 4, 32, 125):
        with pytest.raises(ValueError, match="band-width"):
            run_report(tmp_path, band_width_seconds_per_mile=width)
    with pytest.raises(ValueError, match="not both"):
        run_report(
            tmp_path, pace_min_seconds=600, pace_max_seconds=630, band_width_seconds_per_mile=60
        )


def test_cli_band_width(tmp_path: Path, tmp_db: Path) -> None:
    runner = CliRunner()
    folder = tmp_path / "fits"
    assert runner.invoke(app, ["init-db", "--timezone", TZ]).exit_code == 0
    write_run(folder, "a.fit", RUN_START, [(1800, EASY, 140)])
    args = ["hr-pace", "--dir", str(folder), "--today", TODAY, "--weeks", "2"]
    data = json.loads(runner.invoke(app, [*args, "--band-width", "60"]).output)
    assert data["band"]["pace_max"] == "10:50/mi"
    assert data["trend"]["enough_data"] is False
    assert runner.invoke(app, [*args, "--band-width", "7"]).exit_code == 1


# ------------------------------------------------------------------ trend summary


def fake_report(hrs: list[float | None], chosen_by: str = "most_used_pace") -> dict:  # type: ignore[type-arg]
    """Only the parts `trend_summary` reads: weekly rows (oldest first) and the band."""
    first = datetime(2026, 1, 5)  # a Monday
    weeks = []
    for i, hr in enumerate(hrs):
        monday = first + timedelta(days=7 * i)
        weeks.append(
            {
                "week_start": monday.date().isoformat(),
                "week_end": (monday + timedelta(days=6)).date().isoformat(),
                "runs_used": 0 if hr is None else 2,
                "mean_hr": hr,
            }
        )
    has_band = chosen_by != "none"
    band = {
        "chosen_by": chosen_by,
        "unit": "mi",
        "pace_min": "8:35/mi" if has_band else None,
        "pace_max": "9:05/mi" if has_band else None,
    }
    return {"weeks": weeks, "band": band}


def test_trend_exactly_at_the_minimum_weeks() -> None:
    # 3 of 4 in each period: the smallest case that counts.
    trend = hr_pace.trend_summary(fake_report([155, None, 156, 154, 151, 152, None, 153.4]))
    assert trend["enough_data"] is True and trend["reason"] is None
    assert trend["latest"]["mean_hr"] == 152.1 and trend["previous"]["mean_hr"] == 155.0
    assert trend["latest"]["weeks_with_data"] == trend["previous"]["weeks_with_data"] == 3
    assert (trend["weeks_with_data"], trend["runs_used"]) == (6, 12)
    assert (trend["period_weeks"], trend["min_weeks_with_data"]) == (4, 3)
    assert trend["band"] == {
        "chosen_by": "most_used_pace",
        "unit": "mi",
        "pace_min": "8:35/mi",
        "pace_max": "9:05/mi",
    }
    assert (trend["previous"]["start"], trend["previous"]["end"]) == ("2026-01-05", "2026-02-01")
    assert (trend["latest"]["start"], trend["latest"]["end"]) == ("2026-02-02", "2026-03-01")
    assert hr_pace.trend_line(trend) == (
        "HR at your usual pace (8:35-9:05/mi): 152 bpm over the last 4 weeks, 155 the 4 before"
    )
    # No word about what the difference means.
    assert not {"fitter", "fatigue", "improv", "better", "worse"} & set(str(trend).lower().split())


@pytest.mark.parametrize(
    "hrs",
    [
        [155, 156, 154, 153, 151, 152, None, None],  # latest period one week short
        [155, None, None, 153, 151, 152, 150, 149],  # previous period one week short
        [None, None, None, None, 151, 152, 150, 149],  # nothing to compare with
        [155, 156, 154, 153, None, None, None, None],
    ],
)
def test_trend_gaps_mean_not_enough_data(hrs: list[float | None]) -> None:
    trend = hr_pace.trend_summary(fake_report(hrs))
    assert trend["enough_data"] is False and trend["reason"] == "too_few_weeks_with_data"
    assert hr_pace.trend_line(trend) is None
    # A period below the minimum has no mean at all (missing, not a mean of one or two weeks).
    for key, weeks in (("previous", hrs[:4]), ("latest", hrs[4:])):
        have = sum(1 for h in weeks if h is not None)
        assert trend[key]["weeks_with_data"] == have
        assert (trend[key]["mean_hr"] is None) == (have < 3)


def test_trend_empty_short_and_no_band() -> None:
    empty = hr_pace.trend_summary(fake_report([None] * 12))
    assert empty["enough_data"] is False and empty["reason"] == "too_few_weeks_with_data"
    assert empty["latest"]["mean_hr"] is None and empty["previous"]["mean_hr"] is None
    assert (empty["weeks_with_data"], empty["runs_used"]) == (0, 0)
    short = hr_pace.trend_summary(fake_report([150] * 7))
    assert short["enough_data"] is False and short["reason"] == "report_shorter_than_two_periods"
    assert short["latest"]["start"] is None
    no_band = hr_pace.trend_summary(fake_report([None] * 8, chosen_by="none"))
    assert no_band["enough_data"] is False and no_band["reason"] == "no_band"
    assert hr_pace.band_label(no_band["band"]) is None
    for trend in (empty, short, no_band):
        assert hr_pace.trend_line(trend) is None


def test_trend_uses_only_the_last_eight_weeks_and_names_a_given_band() -> None:
    hrs: list[float | None] = [190, 190, 190, 190, 160, 160, 160, 160, 150, 150, 150, 150.5]
    trend = hr_pace.trend_summary(fake_report(hrs, chosen_by="arguments"))
    assert trend["previous"]["mean_hr"] == 160.0 and trend["latest"]["mean_hr"] == 150.1
    assert hr_pace.trend_line(trend) == (
        "HR at 8:35-9:05/mi: 150 bpm over the last 4 weeks, 160 the 4 before"
    )


def test_report_carries_the_trend(tmp_path: Path) -> None:
    # Six Tuesdays: three in each 4-week period (weeks of 01-12..02-02 and 02-09..03-02).
    for i, hr in ((0, 150), (1, 150), (3, 150), (4, 156), (5, 156), (7, 156)):
        write_run(tmp_path, f"{i}.fit", RUN_START - timedelta(days=7 * i), [(1200, EASY, hr)])
    out = run_report(tmp_path)
    assert out["trend"] == hr_pace.trend_summary(out)
    assert out["trend"]["enough_data"] is True
    assert (out["trend"]["latest"]["mean_hr"], out["trend"]["previous"]["mean_hr"]) == (150, 156)
    assert out["trend"]["runs_used"] == 6
    assert run_report(tmp_path, weeks=4)["trend"]["reason"] == "report_shorter_than_two_periods"
    assert run_report(tmp_path / "missing")["trend"]["reason"] == "no_band"
