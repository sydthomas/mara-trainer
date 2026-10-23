"""Date helpers. Weekday convention everywhere in this project: 0=Sunday ... 6=Saturday
(the same convention as the legacy TypeScript code, JavaScript's getUTCDay)."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

DAY_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]


def js_round(value: float) -> int:
    """JavaScript Math.round semantics (half rounds toward +infinity), not banker's rounding."""
    return math.floor(value + 0.5)


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def weekday_sun0(d: date) -> int:
    """0=Sunday ... 6=Saturday."""
    return (d.weekday() + 1) % 7


def days_before(value: str, days: int) -> str:
    return (parse_date(value) - timedelta(days=days)).isoformat()


def date_in_timezone(instant: datetime, timezone: str) -> str:
    """Athlete-local calendar date (YYYY-MM-DD) for a UTC instant."""
    if instant.tzinfo is None:
        raise ValueError("instant must be timezone-aware")
    return instant.astimezone(ZoneInfo(timezone)).date().isoformat()


def parse_instant(value: str) -> datetime:
    """Parse an ISO-8601 instant (accepts trailing Z) into an aware UTC datetime."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"instant has no timezone: {value!r}")
    return dt.astimezone(UTC)


def iso_utc(instant: datetime) -> str:
    """Format like JavaScript toISOString(): 2026-01-01T07:00:00.000Z."""
    dt = instant.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_day(value: str | int) -> int:
    if isinstance(value, int):
        day = value
    elif value.strip().isdigit():
        day = int(value)
    else:
        key = value.strip().lower()[:3]
        if key not in DAY_NAMES:
            raise ValueError(f"unknown weekday: {value!r}")
        day = DAY_NAMES.index(key)
    if not 0 <= day <= 6:
        raise ValueError(f"weekday out of range: {value!r}")
    return day
