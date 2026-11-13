"""Run-time forecast from Open-Meteo (https://open-meteo.com/), fetched on demand.

Conditions only: no pace adjustment, no heat advice, no thresholds. Nothing is stored or cached.
The request carries coordinates rounded to 2 decimals, the athlete's IANA timezone and the
variable names, and nothing else (no key, no account, no name, urllib's default User-Agent).
Coordinates come from `COACH_LAT` / `COACH_LON` and are never put in the output.

`forecast` never raises: every failure is `{"ok": False, "reason": ...}`.

The free Open-Meteo API is for non-commercial use (https://open-meteo.com/en/terms); data is
CC BY 4.0, so every successful result carries `attribution`.
"""

from __future__ import annotations

import http.client
import json
import math
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from mara_trainer.dates import js_round

API_URL = "https://api.open-meteo.com/v1/forecast"
ATTRIBUTION = {"text": "Weather data by Open-Meteo.com", "url": "https://open-meteo.com/"}
TIMEOUT_SECONDS = 10.0
FORECAST_DAYS = 16  # the most the forecast API serves
MAX_HOURS = 24
LAT_ENV, LON_ENV = "COACH_LAT", "COACH_LON"
TIME_FORMAT = "%Y-%m-%dT%H:%M"

# Open-Meteo hourly variable -> (output key, unit the API must report for it).
# Names confirmed against https://open-meteo.com/en/docs on 2026-11-12.
VARIABLES: dict[str, tuple[str, str]] = {
    "temperature_2m": ("temperature_c", "°C"),
    "relative_humidity_2m": ("relative_humidity_pct", "%"),
    "dew_point_2m": ("dew_point_c", "°C"),
    "apparent_temperature": ("apparent_temperature_c", "°C"),
    "precipitation_probability": ("precipitation_probability_pct", "%"),
    "precipitation": ("precipitation_mm", "mm"),
    "wind_speed_10m": ("wind_speed_m_s", "m/s"),
    "wind_gusts_10m": ("wind_gusts_m_s", "m/s"),
    "uv_index": ("uv_index", ""),
}
UNITS = {
    "time": "local time in `timezone`, start of the hour",
    "temperature_c": "°C",
    "temperature_f": "°F (display)",
    "relative_humidity_pct": "%",
    "dew_point_c": "°C",
    "dew_point_f": "°F (display)",
    "apparent_temperature_c": "°C",
    "apparent_temperature_f": "°F (display)",
    "precipitation_probability_pct": "%",
    "precipitation_mm": "mm in the preceding hour",
    "wind_speed_m_s": "m/s",
    "wind_speed_mph": "mph (display)",
    "wind_gusts_m_s": "m/s",
    "wind_gusts_mph": "mph (display)",
    "uv_index": "index",
}
MPH_PER_M_S = 3600 / 1609.344

Fetch = Callable[[str, float], bytes]


class _Problem(Exception):
    """A failure with a reason that is safe to show (no coordinates, no URL)."""

    def __init__(self, reason: str, **extra: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.extra = extra


def http_get(url: str, timeout: float) -> bytes:
    """One plain HTTPS GET. No custom headers: nothing identifying leaves the machine."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        body: bytes = response.read()
    return body


def _now(timezone: ZoneInfo) -> datetime:
    return datetime.now(timezone)


def _round(value: float, digits: int) -> float:
    scale = float(10**digits)
    return js_round(value * scale) / scale


def _coordinates(env: Mapping[str, str]) -> tuple[str, str]:
    """Rounded to 2 decimals (about 1 km) as request strings. Values never appear in errors."""
    missing = [name for name in (LAT_ENV, LON_ENV) if not env.get(name, "").strip()]
    if missing:
        raise _Problem(
            f"location is not set: add {' and '.join(missing)} to .env (see .env.example)",
            missing=missing,
        )
    out = []
    for name, limit in ((LAT_ENV, 90.0), (LON_ENV, 180.0)):
        try:
            value = float(env[name])
        except ValueError:
            value = math.nan
        if not math.isfinite(value) or abs(value) > limit:
            raise _Problem(f"{name} must be a number between -{limit:g} and {limit:g}")
        out.append(f"{round(value, 2):.2f}")
    return out[0], out[1]


def request_url(latitude: str, longitude: str, timezone: str) -> str:
    return (
        API_URL
        + "?"
        + urlencode(
            {
                "latitude": latitude,
                "longitude": longitude,
                "hourly": ",".join(VARIABLES),
                "timezone": timezone,
                "wind_speed_unit": "ms",
                "forecast_days": FORECAST_DAYS,
            }
        )
    )


def _fetch(fetch: Fetch, url: str) -> bytes:
    try:
        return fetch(url, TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        raise _Problem(f"weather request failed: HTTP {exc.code}") from None
    except TimeoutError:
        raise _Problem(f"weather request timed out after {TIMEOUT_SECONDS:g} s") from None
    except (OSError, http.client.HTTPException) as exc:
        raise _Problem(f"couldn't reach Open-Meteo ({type(exc).__name__})") from None
    except Exception as exc:  # an injected fetch may fail any way; callers must never see it
        raise _Problem(f"weather request failed ({type(exc).__name__})") from None


def _number(value: Any) -> float | None:
    """Missing stays null. Anything that isn't a number is a malformed response."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise _Problem("unexpected weather response: a value is not a number")
    return float(value)


def _parse(body: bytes) -> tuple[list[str], dict[str, list[Any]]]:
    try:
        data = json.loads(body)
    except ValueError:  # JSONDecodeError and UnicodeDecodeError
        raise _Problem("unexpected weather response: not JSON") from None
    if not isinstance(data, dict):
        raise _Problem("unexpected weather response: not a JSON object")
    if data.get("error"):
        raise _Problem("Open-Meteo rejected the request")
    hourly, units = data.get("hourly"), data.get("hourly_units")
    if not isinstance(hourly, dict) or not isinstance(units, dict):
        raise _Problem("unexpected weather response: no hourly data")
    times = hourly.get("time")
    if not isinstance(times, list) or not times or not all(isinstance(t, str) for t in times):
        raise _Problem("unexpected weather response: no hourly times")
    series: dict[str, list[Any]] = {}
    for name, (_, unit) in VARIABLES.items():
        values = hourly.get(name)
        if not isinstance(values, list) or len(values) != len(times):
            raise _Problem(f"unexpected weather response: {name} is missing or incomplete")
        if units.get(name) != unit:
            raise _Problem(f"unexpected weather response: {name} is not in the unit asked for")
        series[name] = values
    return times, series


def _hour_row(time: str, index: int, series: dict[str, list[Any]]) -> dict[str, Any]:
    row: dict[str, Any] = {"time": time}
    for name, (key, unit) in VARIABLES.items():
        value = _number(series[name][index])
        if unit == "%":
            row[key] = None if value is None else js_round(value)
            continue
        row[key] = None if value is None else _round(value, 2 if unit == "m/s" else 1)
        if unit == "°C":
            row[key.removesuffix("_c") + "_f"] = (
                None if value is None else _round(value * 9 / 5 + 32, 1)
            )
        elif unit == "m/s":
            row[key.removesuffix("_m_s") + "_mph"] = (
                None if value is None else _round(value * MPH_PER_M_S, 1)
            )
    return row


def _forecast(
    timezone: str,
    date: str | None,
    hour: int | None,
    hours: int,
    env: Mapping[str, str],
    fetch: Fetch,
    now: datetime | None,
) -> dict[str, Any]:
    try:
        zone = ZoneInfo(timezone)
    except (KeyError, ValueError, OSError):
        raise _Problem(f"unknown IANA timezone: {timezone}") from None
    local_now = (now or _now(zone)).astimezone(zone)
    try:
        day = local_now.date() if date is None else date_cls.fromisoformat(date)
    except ValueError:
        raise _Problem(f"--date must be YYYY-MM-DD, got {date!r}") from None
    if date is not None and len(date) != 10:
        raise _Problem(f"--date must be YYYY-MM-DD, got {date!r}")
    start_hour = local_now.hour if hour is None else hour
    if not 0 <= start_hour <= 23:
        raise _Problem("--hour must be 0-23")
    if not 1 <= hours <= MAX_HOURS:
        raise _Problem(f"--hours must be 1-{MAX_HOURS}")
    latitude, longitude = _coordinates(env)

    times, series = _parse(_fetch(fetch, request_url(latitude, longitude, timezone)))
    index = {t: i for i, t in enumerate(times)}
    start = datetime(day.year, day.month, day.day, start_hour)
    wanted = [(start + timedelta(hours=i)).strftime(TIME_FORMAT) for i in range(hours)]
    first, last = min(times), max(times)
    if any(t not in index for t in wanted):
        raise _Problem(
            f"{wanted[0]} to {wanted[-1]} is outside the forecast, which covers {first} to {last}",
            forecast_from=first,
            forecast_through=last,
        )
    return {
        "ok": True,
        "date": day.isoformat(),
        "start_hour": start_hour,
        "timezone": timezone,
        "hours": [_hour_row(t, index[t], series) for t in wanted],
        "units": UNITS,
        "forecast_through": last,
        "attribution": ATTRIBUTION,
    }


def forecast(
    *,
    timezone: str,
    date: str | None = None,
    hour: int | None = None,
    hours: int = 3,
    env: Mapping[str, str],
    fetch: Fetch | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Hourly conditions for `hours` hours from `hour` on `date`, local to `timezone`.

    Defaults: today and the current local hour. `env` supplies `COACH_LAT` / `COACH_LON`.
    Returns `{"ok": True, "hours": [...], ...}` or `{"ok": False, "reason": ...}`; never raises.
    """
    try:
        return _forecast(timezone, date, hour, hours, env, fetch or http_get, now)
    except _Problem as problem:
        return {"ok": False, "reason": problem.reason, **problem.extra}
