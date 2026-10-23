"""Timezone-aware rollups; missing stays missing (None), never zero."""

from __future__ import annotations

from datetime import UTC, datetime

from mara_trainer.dates import date_in_timezone
from mara_trainer.summaries import (
    Activity,
    CheckIn,
    summarize,
    summarize_range,
    week_bounds,
    weekly_rollups,
)

LA = "America/Los_Angeles"
NOW = datetime(2026, 3, 15, 20, 0, tzinfo=UTC)  # Sun 2026-03-15 13:00 PDT


def test_date_in_timezone_crosses_midnight() -> None:
    # 2026-03-10 05:30Z is still 2026-03-09 in Los Angeles
    assert date_in_timezone(datetime(2026, 3, 10, 5, 30, tzinfo=UTC), LA) == "2026-03-09"
    assert date_in_timezone(datetime(2026, 3, 10, 5, 30, tzinfo=UTC), "UTC") == "2026-03-10"


def test_trailing_window_uses_local_dates() -> None:
    acts = [
        Activity("2026-03-09T06:30:00.000Z", 5000, 1800),  # local 03-08 23:30 -> outside
        Activity("2026-03-09T15:00:00.000Z", 8000, 2700),  # local 03-09 -> inside
        Activity("2026-03-15T19:00:00.000Z", 10000, 3600),  # today, before now -> inside
        Activity("2026-03-15T21:00:00.000Z", 3000, 1000),  # later today (future) -> outside
    ]
    s = summarize(acts, [], LA, NOW)
    assert (s.start_date, s.end_date) == ("2026-03-09", "2026-03-15")
    assert s.run_count == 2
    assert s.distance_meters == 18000
    assert s.duration_seconds == 6300
    assert s.longest_run_meters == 10000


def test_same_instant_different_timezone_changes_bucket() -> None:
    acts = [Activity("2026-03-09T06:30:00.000Z", 5000, 1800)]
    assert summarize(acts, [], LA, NOW).run_count == 0
    assert summarize(acts, [], "UTC", NOW).run_count == 1


def test_missing_rpe_is_not_zero() -> None:
    acts = [Activity("2026-03-14T16:00:00.000Z", 5000, 1800)]
    s = summarize(acts, [], LA, NOW)
    assert s.session_load is None and s.runs_with_rpe == 0


def test_session_load_only_counts_rated_runs() -> None:
    acts = [
        Activity("2026-03-14T16:00:00.000Z", 5000, 1800, rpe=4),  # 30 min * 4 = 120
        Activity("2026-03-13T16:00:00.000Z", 5000, 1800, rpe=None),
    ]
    s = summarize(acts, [], LA, NOW)
    assert s.session_load == 120 and s.runs_with_rpe == 1 and s.run_count == 2


def test_missing_checkin_metrics_stay_none_and_report_coverage() -> None:
    checks = [
        CheckIn("2026-03-14", sleep_hours=7.0),
        CheckIn("2026-03-13", sleep_hours=None, hrv_ms=50.0),
        CheckIn("2026-03-12", sleep_hours=8.0, resting_hr=52.0),
        CheckIn("2026-03-01", sleep_hours=3.0),  # outside window
    ]
    s = summarize([], checks, LA, NOW)
    assert s.check_in_days == 3
    assert s.sleep_days == 2 and s.average_sleep_hours == 7.5
    assert s.hrv_days == 1 and s.average_hrv_ms == 50.0
    assert s.resting_hr_days == 1 and s.average_resting_hr == 52.0


def test_empty_week_is_null_not_zero_for_averages() -> None:
    s = summarize([], [], LA, NOW)
    assert s.run_count == 0 and s.distance_meters == 0
    assert s.average_sleep_hours is None and s.longest_run_meters is None
    assert s.average_hrv_ms is None and s.session_load is None


def test_week_bounds_monday_to_sunday() -> None:
    assert week_bounds("2026-03-15") == ("2026-03-09", "2026-03-15")
    assert week_bounds("2026-03-16") == ("2026-03-16", "2026-03-22")


def test_weekly_rollups_oldest_first_and_local_buckets() -> None:
    acts = [
        # Sunday night local (Mar 8 22:00 PDT = Mar 9 05:00Z) belongs to the week of Mar 2
        Activity("2026-03-09T05:00:00.000Z", 20000, 7200),
        Activity("2026-03-10T14:00:00.000Z", 8000, 2700),
    ]
    weeks = weekly_rollups(acts, [], LA, 3, NOW)
    assert [w.start_date for w in weeks] == ["2026-02-23", "2026-03-02", "2026-03-09"]
    assert [w.distance_meters for w in weeks] == [0, 20000, 8000]
    assert weeks[0].longest_run_meters is None


def test_dst_transition_week() -> None:
    # DST starts 2026-03-08 in the US. A run at 2026-03-08T07:30Z is 23:30 PST on 03-07.
    acts = [Activity("2026-03-08T07:30:00.000Z", 5000, 1500)]
    s = summarize_range(acts, [], LA, "2026-03-08", "2026-03-14", NOW)
    assert s.run_count == 0
    s2 = summarize_range(acts, [], LA, "2026-03-07", "2026-03-07", NOW)
    assert s2.run_count == 1
