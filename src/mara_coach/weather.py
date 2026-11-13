"""Run-time forecast from Open-Meteo (https://open-meteo.com/), fetched on demand.

Conditions only: no pace adjustment, no heat advice, no thresholds. Nothing is stored or cached.
The request carries coordinates rounded to 2 decimals, the athlete's IANA timezone and the
variable names, and nothing else (no key, no account, no name, urllib's default User-Agent).
Coordinates come from `COACH_LAT` / `COACH_LON` and are never put in the output.

`forecast` never raises: every failure is `{"ok": False, "reason": ...}`.

Optional second lookup: the US National Weather Service (https://api.weather.gov, flow per
https://www.weather.gov/documentation/services-web-api) for wet-bulb globe temperature (WBGT).
It runs only when `COACH_NWS_CONTACT` is set, because NWS asks clients to identify themselves:
that contact goes in the User-Agent of at most two requests (`/points/{lat},{lon}`, then its
`forecastGridData` URL) with the same rounded coordinates, and nowhere else (never in output,
reasons or logs). The gridpoint `wetBulbGlobeTemperature` layer is in live data (checked
2026-11-12) but not documented, so anything unexpected means `nws: {"ok": false, "reason"}` and
null `wbgt_c`; an NWS failure never changes the Open-Meteo result. NWS data is open data, free to
use; the attribution entry credits it and implies no endorsement.

Hours are asked for as unix timestamps and converted to local time here. Open-Meteo's ISO
timestamps use one fixed UTC offset for the whole range, so they are an hour off after a
daylight-saving change (https://open-meteo.com/en/docs, `timezone` / `timeformat`).

The free Open-Meteo API is for non-commercial use (https://open-meteo.com/en/terms); data is
CC BY 4.0, so every successful result carries `attribution`.
"""

from __future__ import annotations

import http.client
import json
import math
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import date as date_cls
from datetime import datetime
from email.message import Message
from typing import IO, Any
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
TIME_UNIT = "unixtime"
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")

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
    "wbgt_c": "°C, wet-bulb globe temperature from NWS (null when NWS has none)",
    "wbgt_f": "°F (display)",
}
MPH_PER_M_S = 3600 / 1609.344

NWS_POINTS_URL = "https://api.weather.gov/points/"
NWS_GRID_PREFIX = "https://api.weather.gov/gridpoints/"  # the only place the second request goes
NWS_CONTACT_ENV = "COACH_NWS_CONTACT"
NWS_TIMEOUT_SECONDS = 5.0
NWS_WBGT_LAYER = "wetBulbGlobeTemperature"
NWS_WBGT_UNIT = "wmoUnit:degC"
NWS_ATTRIBUTION = {
    "text": (
        "Wet-bulb globe temperature forecast: NOAA National Weather Service "
        "(open data; no endorsement implied)"
    ),
    "url": "https://www.weather.gov/",
}
NWS_NO_CONTACT = "no contact set"
# ISO 8601 durations as NWS writes them (PT1H, PT2H, P1D, P7DT5H). Months and years have no
# fixed length and are refused.
DURATION_PATTERN = re.compile(
    r"P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
    re.ASCII,
)

Fetch = Callable[[str, float], bytes]
NwsFetch = Callable[[str, float, Mapping[str, str]], bytes]


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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The contact header goes to the URL asked for and nowhere else."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None  # urllib then raises HTTPError with the 3xx code


def nws_http_get(url: str, timeout: float, headers: Mapping[str, str]) -> bytes:
    """One HTTPS GET with the given headers. Redirects are not followed."""
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers=dict(headers))
    with opener.open(request, timeout=timeout) as response:
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
            f"location is not set: add {' and '.join(missing)} (decimal degrees) to .env",
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
        out.append(f"{round(value, 2) + 0.0:.2f}")  # + 0.0: never "-0.00"
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
                "timeformat": TIME_UNIT,
                "wind_speed_unit": "ms",
                "forecast_days": FORECAST_DAYS,
            }
        )
    )


def _fetch(fetch: Fetch, url: str) -> bytes:
    timed_out = f"weather request timed out after {TIMEOUT_SECONDS:g} s"
    try:
        return fetch(url, TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        raise _Problem(f"weather request failed: HTTP {exc.code}") from None
    except TimeoutError:
        raise _Problem(timed_out) from None
    except (OSError, http.client.HTTPException) as exc:
        # urlopen wraps a connect timeout in URLError
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            raise _Problem(timed_out) from None
        raise _Problem(f"couldn't reach Open-Meteo ({type(exc).__name__})") from None
    except Exception as exc:  # an injected fetch may fail any way; callers must never see it
        raise _Problem(f"weather request failed ({type(exc).__name__})") from None


def _number(value: Any, what: str = "weather") -> float | None:
    """Missing stays null. Anything that isn't a number is a malformed response."""
    if value is None:
        return None
    problem = _Problem(f"unexpected {what} response: a value is not a number")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise problem
    try:
        number = float(value)
    except OverflowError:  # an integer too large for a float
        raise problem from None
    if not math.isfinite(number):
        raise problem
    return number


def _parse(body: bytes) -> tuple[list[int], dict[str, list[Any]]]:
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):  # JSONDecodeError, UnicodeDecodeError, deep nesting
        raise _Problem("unexpected weather response: not JSON") from None
    if not isinstance(data, dict):
        raise _Problem("unexpected weather response: not a JSON object")
    if data.get("error"):
        raise _Problem("Open-Meteo rejected the request")
    hourly, units = data.get("hourly"), data.get("hourly_units")
    if not isinstance(hourly, dict) or not isinstance(units, dict):
        raise _Problem("unexpected weather response: no hourly data")
    times = hourly.get("time")
    if (
        units.get("time") != TIME_UNIT
        or not isinstance(times, list)
        or not times
        or not all(isinstance(t, int) and not isinstance(t, bool) for t in times)
    ):
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


def _hour_row(
    time: str, index: int, series: dict[str, list[Any]], wbgt: float | None
) -> dict[str, Any]:
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
    row["wbgt_c"] = None if wbgt is None else _round(wbgt, 1)
    row["wbgt_f"] = None if wbgt is None else _round(wbgt * 9 / 5 + 32, 1)
    return row


def _nws_get(fetch: NwsFetch, url: str, headers: Mapping[str, str], not_found: str) -> Any:
    """One NWS request, parsed to its `properties`. Reasons never carry the URL, the
    coordinates or the contact."""
    timed_out = f"NWS request timed out after {NWS_TIMEOUT_SECONDS:g} s"
    try:
        body = fetch(url, NWS_TIMEOUT_SECONDS, headers)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise _Problem(not_found) from None
        raise _Problem(f"NWS request failed: HTTP {exc.code}") from None
    except TimeoutError:
        raise _Problem(timed_out) from None
    except (OSError, http.client.HTTPException) as exc:
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            raise _Problem(timed_out) from None
        raise _Problem(f"couldn't reach NWS ({type(exc).__name__})") from None
    except Exception as exc:
        raise _Problem(f"NWS request failed ({type(exc).__name__})") from None
    try:
        data = json.loads(body)
    except (ValueError, RecursionError, TypeError):
        raise _Problem("unexpected NWS response: not JSON") from None
    properties = data.get("properties") if isinstance(data, dict) else None
    if not isinstance(properties, dict):
        raise _Problem("unexpected NWS response: no properties")
    return properties


def _duration_seconds(text: str) -> int | None:
    """An ISO 8601 duration of fixed length in seconds, or None."""
    match = DURATION_PATTERN.fullmatch(text)
    if match is None or not any(match.groups()) or text.endswith("T"):
        return None
    part = {name: int(value or 0) for name, value in match.groupdict().items()}
    return (
        ((part["weeks"] * 7 + part["days"]) * 24 + part["hours"]) * 3600
        + part["minutes"] * 60
        + part["seconds"]
    )


def _wbgt_intervals(layer: Any) -> list[tuple[float, float, float | None]]:
    """The layer as (start, end, °C): UTC instants in unix seconds, end exclusive."""
    if layer is None:
        raise _Problem("NWS has no WBGT forecast for this location")
    values = layer.get("values") if isinstance(layer, dict) else None
    if not isinstance(values, list):
        raise _Problem("unexpected NWS response: WBGT has no values")
    if layer.get("uom") != NWS_WBGT_UNIT:
        raise _Problem("unexpected NWS response: WBGT is not in °C")
    bad_time = _Problem("unexpected NWS response: a WBGT time is not a start/duration interval")
    out: list[tuple[float, float, float | None]] = []
    for entry in values:
        valid = entry.get("validTime") if isinstance(entry, dict) else None
        if not isinstance(valid, str) or valid.count("/") != 1:
            raise bad_time
        start_text, duration = valid.split("/")
        seconds = _duration_seconds(duration)
        try:
            begin = datetime.fromisoformat(start_text)
            # a time without an offset is not an instant
            start = None if begin.tzinfo is None else begin.timestamp()
        except (OverflowError, ValueError, OSError):
            raise bad_time from None
        if seconds is None or start is None:
            raise bad_time
        out.append((start, start + seconds, _number(entry.get("value"), "NWS")))
    return out


def _nws_wbgt(
    contact: str, latitude: str, longitude: str, wanted: Sequence[int], fetch: NwsFetch
) -> list[float | None]:
    """WBGT in °C for each wanted instant (unix seconds); null where no interval covers it."""
    if not contact.isascii() or not contact.isprintable():  # never sent, never echoed
        raise _Problem(f"{NWS_CONTACT_ENV} has characters that can't go in a request header")
    headers = {"User-Agent": f"(mara-trainer, {contact})", "Accept": "application/geo+json"}
    # The same rounded coordinates Open-Meteo gets. Trailing zeros are trimmed (38.90 -> 38.9)
    # because NWS answers those with a redirect, and redirects are not followed.
    point = ",".join(c.rstrip("0").rstrip(".") for c in (latitude, longitude))
    grid_url = _nws_get(
        fetch, NWS_POINTS_URL + point, headers, "location is outside NWS coverage (US only)"
    ).get("forecastGridData")
    if not isinstance(grid_url, str) or not grid_url.startswith(NWS_GRID_PREFIX):
        raise _Problem("unexpected NWS response: no forecast grid for this location")
    grid = _nws_get(fetch, grid_url, headers, "NWS has no forecast grid for this location")
    intervals = _wbgt_intervals(grid.get(NWS_WBGT_LAYER))
    values = [
        next((value for start, end, value in intervals if start <= t < end), None) for t in wanted
    ]
    if all(value is None for value in values):
        raise _Problem("NWS has no WBGT forecast for these hours")
    return values


def _wbgt(
    env: Mapping[str, str], latitude: str, longitude: str, wanted: Sequence[int], fetch: NwsFetch
) -> tuple[dict[str, Any], list[float | None]]:
    """The NWS part: its status and one value per wanted hour. Never raises."""
    blank: list[float | None] = [None] * len(wanted)
    contact = env.get(NWS_CONTACT_ENV, "").strip()
    if not contact:
        return {"ok": False, "reason": NWS_NO_CONTACT}, blank
    try:
        values = _nws_wbgt(contact, latitude, longitude, wanted, fetch)
    except _Problem as problem:
        return {"ok": False, "reason": problem.reason}, blank
    except Exception as exc:  # name the type only: a message could hold the URL or the header
        return {"ok": False, "reason": f"NWS lookup failed ({type(exc).__name__})"}, blank
    return {"ok": True, "hours_with_wbgt": sum(v is not None for v in values)}, values


def _local(timestamp: int, zone: ZoneInfo) -> str:
    """A UTC instant as local wall-clock time, with the offset that applies at that instant."""
    try:
        return datetime.fromtimestamp(timestamp, zone).strftime(TIME_FORMAT)
    except (OverflowError, ValueError, OSError):
        raise _Problem("unexpected weather response: a time is out of range") from None


def _forecast(
    timezone: str,
    date: str | None,
    hour: int | None,
    hours: int,
    env: Mapping[str, str],
    fetch: Fetch,
    nws_fetch: NwsFetch,
    now: datetime | None,
) -> dict[str, Any]:
    try:
        zone = ZoneInfo(timezone)
    except (KeyError, ValueError, OSError):
        raise _Problem(f"unknown IANA timezone: {timezone}") from None
    local_now = (now or _now(zone)).astimezone(zone)
    bad_date = _Problem(f"--date must be YYYY-MM-DD, got {date!r}")
    if date is not None and not DATE_PATTERN.fullmatch(date):  # fromisoformat takes week dates too
        raise bad_date
    try:
        day = local_now.date() if date is None else date_cls.fromisoformat(date)
    except ValueError:
        raise bad_date from None
    start_hour = local_now.hour if hour is None else hour
    if not 0 <= start_hour <= 23:
        raise _Problem("--hour must be 0-23")
    if not 1 <= hours <= MAX_HOURS:
        raise _Problem(f"--hours must be 1-{MAX_HOURS}")
    # The window is `hours` real hours from the local start, so it stays right across a
    # daylight-saving change.
    try:
        start = int(datetime(day.year, day.month, day.day, start_hour, tzinfo=zone).timestamp())
        wanted = [start + 3600 * i for i in range(hours)]
        labels = [datetime.fromtimestamp(t, zone).strftime(TIME_FORMAT) for t in wanted]
    except (OverflowError, ValueError, OSError):
        raise _Problem("--date is out of range") from None
    latitude, longitude = _coordinates(env)

    times, series = _parse(_fetch(fetch, request_url(latitude, longitude, timezone)))
    index = {t: i for i, t in enumerate(times)}
    # Open-Meteo pads the end of the range with nulls: the forecast ends at the last hour
    # that has any value.
    with_data = [t for t, i in index.items() if any(v[i] is not None for v in series.values())]
    if not with_data:
        raise _Problem("unexpected weather response: no values")
    end = max(with_data)
    first, last = _local(min(times), zone), _local(end, zone)
    if any(t not in index or t > end for t in wanted):
        raise _Problem(
            f"{labels[0]} to {labels[-1]} is outside the forecast, which covers {first} to {last}",
            forecast_from=first,
            forecast_through=last,
        )
    # Open-Meteo is settled before NWS is asked: a failed forecast sends nothing to NWS, and
    # hours are matched to NWS intervals as instants (the same unix seconds), not wall-clock text.
    nws, wbgt = _wbgt(env, latitude, longitude, wanted, nws_fetch)
    return {
        "ok": True,
        "date": day.isoformat(),
        "start_hour": start_hour,
        "timezone": timezone,
        "hours": [
            _hour_row(label, index[t], series, value)
            for t, label, value in zip(wanted, labels, wbgt, strict=True)
        ],
        "units": UNITS,
        "forecast_through": last,
        "nws": nws,
        "attribution": [ATTRIBUTION, NWS_ATTRIBUTION] if nws["ok"] else [ATTRIBUTION],
    }


def forecast(
    *,
    timezone: str,
    date: str | None = None,
    hour: int | None = None,
    hours: int = 3,
    env: Mapping[str, str],
    fetch: Fetch | None = None,
    nws_fetch: NwsFetch | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Hourly conditions for `hours` hours from `hour` on `date`, local to `timezone`.

    Defaults: today and the current local hour. `env` supplies `COACH_LAT` / `COACH_LON` and,
    optionally, `COACH_NWS_CONTACT` (adds `wbgt_c` from NWS; see the module docstring).
    Returns `{"ok": True, "hours": [...], ...}` or `{"ok": False, "reason": ...}`; never raises.
    """
    try:
        return _forecast(
            timezone, date, hour, hours, env, fetch or http_get, nws_fetch or nws_http_get, now
        )
    except _Problem as problem:
        return {"ok": False, "reason": problem.reason, **problem.extra}
    except Exception as exc:  # the promise is "never raises": name the type, nothing else
        return {"ok": False, "reason": f"weather lookup failed ({type(exc).__name__})"}
