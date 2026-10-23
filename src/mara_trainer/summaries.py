"""Timezone-aware rollups, ported from the legacy `domain.summarize`.

Rules: athlete-local calendar dates decide which day/week a run belongs to; missing values stay
missing (None) and are reported as coverage counts, never imputed as zero.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .dates import date_in_timezone, days_before, js_round, parse_date, parse_instant


@dataclass(frozen=True)
class Activity:
    started_at: str  # ISO-8601 UTC instant
    distance_meters: int
    duration_seconds: int
    rpe: int | None = None


@dataclass(frozen=True)
class CheckIn:
    date: str  # athlete-local YYYY-MM-DD
    sleep_hours: float | None = None
    hrv_ms: float | None = None
    resting_hr: float | None = None


@dataclass(frozen=True)
class Summary:
    start_date: str
    end_date: str
    timezone: str
    run_count: int
    distance_meters: int
    duration_seconds: int
    session_load: int | None  # sum(minutes * RPE) over runs that HAVE an RPE; None if none do
    runs_with_rpe: int
    check_in_days: int
    sleep_days: int
    average_sleep_hours: float | None
    hrv_days: int
    average_hrv_ms: float | None
    resting_hr_days: int
    average_resting_hr: float | None
    longest_run_meters: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def summarize_range(
    activities: list[Activity],
    check_ins: list[CheckIn],
    timezone: str,
    start_date: str,
    end_date: str,
    now: datetime | None = None,
) -> Summary:
    """Summarize runs/check-ins whose athlete-local date is in [start_date, end_date]."""
    now = now or datetime.now(UTC)
    runs = []
    for run in activities:
        instant = parse_instant(run.started_at)
        local = date_in_timezone(instant, timezone)
        if start_date <= local <= end_date and instant <= now:
            runs.append(run)
    checks = [c for c in check_ins if start_date <= c.date <= end_date]
    rated = [r for r in runs if r.rpe is not None]
    sleeps = [c.sleep_hours for c in checks if c.sleep_hours is not None]
    hrvs = [c.hrv_ms for c in checks if c.hrv_ms is not None]
    rhrs = [c.resting_hr for c in checks if c.resting_hr is not None]
    return Summary(
        start_date=start_date,
        end_date=end_date,
        timezone=timezone,
        run_count=len(runs),
        distance_meters=sum(r.distance_meters for r in runs),
        duration_seconds=sum(r.duration_seconds for r in runs),
        session_load=js_round(sum(r.duration_seconds / 60 * (r.rpe or 0) for r in rated))
        if rated
        else None,
        runs_with_rpe=len(rated),
        check_in_days=len(checks),
        sleep_days=len(sleeps),
        average_sleep_hours=_mean(sleeps),
        hrv_days=len(hrvs),
        average_hrv_ms=_mean(hrvs),
        resting_hr_days=len(rhrs),
        average_resting_hr=_mean(rhrs),
        longest_run_meters=max((r.distance_meters for r in runs), default=None),
    )


def summarize(
    activities: list[Activity],
    check_ins: list[CheckIn],
    timezone: str,
    now: datetime | None = None,
) -> Summary:
    """Trailing 7 athlete-local days ending today (legacy behaviour)."""
    now = now or datetime.now(UTC)
    end = date_in_timezone(now, timezone)
    return summarize_range(activities, check_ins, timezone, days_before(end, 6), end, now)


def week_bounds(any_date: str) -> tuple[str, str]:
    """Monday..Sunday calendar week containing `any_date`."""
    d = parse_date(any_date)
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def weekly_rollups(
    activities: list[Activity],
    check_ins: list[CheckIn],
    timezone: str,
    weeks: int,
    now: datetime | None = None,
) -> list[Summary]:
    """The last `weeks` Monday-Sunday weeks (oldest first), including the current partial week."""
    now = now or datetime.now(UTC)
    start, end = week_bounds(date_in_timezone(now, timezone))
    out = []
    for i in range(weeks - 1, -1, -1):
        out.append(
            summarize_range(
                activities,
                check_ins,
                timezone,
                days_before(start, 7 * i),
                days_before(end, 7 * i),
                now,
            )
        )
    return out
