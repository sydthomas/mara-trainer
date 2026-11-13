"""Run-time forecast from Open-Meteo (https://open-meteo.com/), fetched on demand.

Conditions only: no pace adjustment, no heat advice, no thresholds. Nothing is stored, and
nothing is cached beyond a dict a caller may pass for one message (`reuse`, below).
The request carries coordinates rounded to 2 decimals, the athlete's IANA timezone and the
variable names, and nothing else (no key, no account, no name, urllib's default User-Agent).
Coordinates come from `COACH_LAT` / `COACH_LON` and are never put in the output.

Named places: optional `COACH_PLACES` (`name:lat,lon;name:lat,lon`; names are case-insensitive
letters, digits, `_`, `-`). `forecast(place=NAME)` uses that place, rounded the same way, and
sends only that one location. The output carries the name (or "default"), never coordinates. A
malformed entry makes only its own name unusable; its reason names the entry, never its text.

Looked-up places (`forecast(near=TEXT)`, `geocode`): a one-off place by name. An exact named
place wins; anything else is searched with Open-Meteo's geocoding API
(https://open-meteo.com/en/docs/geocoding-api, checked 2026-11-12; location data from GeoNames,
credited in `attribution` when a lookup was used). That request carries the typed name and
`count` / `language` / `format`, and nothing else: no coordinates, no key, no custom headers.
"City, Region" searches the first part and keeps the first result whose region (`admin1`),
country or country code matches the rest; with no such result it fails rather than take another
place. The match's coordinates are rounded to 2 decimals before anything else sees them, and the
output carries its label (name, region, country as returned) with `place_source: "lookup"`, so
a wrong match is visible. One geocoding request per call, nothing kept. The window's hours stay
in the athlete's timezone, wherever the place is.

`forecast` and `geocode` never raise: every failure is `{"ok": False, "reason": ...}`.

One response for several windows: a caller that needs more than one window at one location (the
Slack check-in: one line per run day) passes the same `reuse` dict to each call. Response bodies
are kept in it by URL, so the identical request is made once (Open-Meteo's answer already holds
all 16 days). The dict is the caller's, lives only as long as the caller keeps it, and must not
be saved or logged (its keys are request URLs). Failures are never kept. Without `reuse` every
call asks afresh.

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
import unicodedata
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
PLACES_ENV = "COACH_PLACES"  # optional named places: name:lat,lon;name:lat,lon
DEFAULT_PLACE = "default"  # COACH_LAT / COACH_LON
PLACE_NAME = re.compile(r"[a-z0-9_-]{1,32}", re.ASCII)  # after lower-casing
TIME_FORMAT = "%Y-%m-%dT%H:%M"

# Geocoding: endpoint, parameters and response fields confirmed against
# https://open-meteo.com/en/docs/geocoding-api and one live response on 2026-11-12.
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
GEOCODE_ATTRIBUTION = {
    "text": "Location data based on GeoNames",
    "url": "https://www.geonames.org/",
}
GEOCODE_TIMEOUT_SECONDS = 5.0
GEOCODE_COUNT = 100  # the most the API returns; only asked for when a qualifier has to be matched
MAX_PLACE_TEXT = 80
MAX_LABEL_PART = 60
COORDINATES_TEXT = re.compile(r"[-+]?\d+(\.\d+)?\s*[,;\s]\s*[-+]?\d+(\.\d+)?")
SOURCE_DEFAULT, SOURCE_NAMED, SOURCE_LOOKUP = "default", "named", "lookup"
# The API returns full region names ("California"); runners type postal abbreviations. Used only
# for results whose country_code is US.
US_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california",
    "co": "colorado", "ct": "connecticut", "de": "delaware", "dc": "district of columbia",
    "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho", "il": "illinois",
    "in": "indiana", "ia": "iowa", "ks": "kansas", "ky": "kentucky", "la": "louisiana",
    "me": "maine", "md": "maryland", "ma": "massachusetts", "mi": "michigan", "mn": "minnesota",
    "ms": "mississippi", "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york",
    "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma", "or": "oregon",
    "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina", "sd": "south dakota",
    "tn": "tennessee", "tx": "texas", "ut": "utah", "vt": "vermont", "va": "virginia",
    "wa": "washington", "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming",
}  # fmt: skip
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


def _rounded(text: str, limit: float) -> str | None:
    """A coordinate rounded to 2 decimals (about 1 km) as a request string, or None."""
    try:
        value = float(text)
    except ValueError:
        return None
    if not math.isfinite(value) or abs(value) > limit:
        return None
    return f"{round(value, 2) + 0.0:.2f}"  # + 0.0: never "-0.00"


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
        rounded = _rounded(env[name], limit)
        if rounded is None:
            raise _Problem(f"{name} must be a number between -{limit:g} and {limit:g}")
        out.append(rounded)
    return out[0], out[1]


def places(env: Mapping[str, str]) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """`COACH_PLACES` (`name:lat,lon;name:lat,lon`) as usable places and problems.

    Usable: lower-case name -> coordinates rounded like the default location. Problems: name
    (or `entry N` when the entry has no usable name) -> a reason that never holds coordinates or
    any other text from a broken entry. A problem makes only its own name unusable.
    """
    usable: dict[str, tuple[str, str]] = {}
    problems: dict[str, str] = {}
    entries = [e.strip() for e in env.get(PLACES_ENV, "").split(";")]
    for number, entry in enumerate((e for e in entries if e), start=1):
        raw_name, colon, raw_point = entry.partition(":")
        name = raw_name.strip().lower()
        if not colon or not PLACE_NAME.fullmatch(name):
            problems[f"entry {number}"] = (
                f"entry {number} of {PLACES_ENV} can't be used: it must be name:lat,lon with a "
                "name of letters, digits, _ or -"
            )
            continue
        if name == DEFAULT_PLACE:
            problems[name] = (
                f'place "{name}" can\'t be used: that name means {LAT_ENV} / {LON_ENV}; pick '
                f"another name in {PLACES_ENV}"
            )
            continue
        if name in usable or name in problems:
            usable.pop(name, None)  # which one was meant is not ours to guess
            problems[name] = f'place "{name}" can\'t be used: it is listed twice in {PLACES_ENV}'
            continue
        parts = raw_point.split(",")
        latitude = _rounded(parts[0], 90.0) if len(parts) == 2 else None
        longitude = _rounded(parts[1], 180.0) if len(parts) == 2 else None
        if latitude is None or longitude is None:
            problems[name] = (
                f'place "{name}" can\'t be used: its {PLACES_ENV} entry must be name:lat,lon in '
                "decimal degrees (latitude -90 to 90, longitude -180 to 180)"
            )
            continue
        usable[name] = (latitude, longitude)
    return usable, problems


def place_text(text: str) -> str:
    """Typed place text, tidied for a lookup: trimmed, whitespace collapsed. Raises `_Problem`
    for text that must not be sent (empty, over `MAX_PLACE_TEXT`, control characters, or
    something shaped like coordinates)."""
    tidy = " ".join(text.split())  # tabs and line breaks count as whitespace
    if any(unicodedata.category(ch).startswith("C") for ch in tidy):
        raise _Problem("a place name can't have control characters")
    if not tidy:
        raise _Problem("no place name given")
    if len(tidy) > MAX_PLACE_TEXT:
        raise _Problem(f"a place name can be at most {MAX_PLACE_TEXT} characters")
    if COORDINATES_TEXT.fullmatch(tidy):
        raise _Problem("that looks like coordinates; give a place name")
    return tidy


def geocode_url(name: str, count: int) -> str:
    # urlencode percent-encodes UTF-8, so non-ASCII names go through intact
    return (
        GEOCODE_URL
        + "?"
        + urlencode({"name": name, "count": count, "language": "en", "format": "json"})
    )


def _label_part(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    tidy = " ".join(value.split())
    shown = "".join(ch for ch in tidy if not unicodedata.category(ch).startswith("C"))
    return shown[:MAX_LABEL_PART].strip()


def _label(result: Mapping[str, Any]) -> str:
    """Name, region, country as returned (a missing or repeated part is left out)."""
    parts: list[str] = []
    for key in ("name", "admin1", "country"):
        part = _label_part(result.get(key))
        if part and part not in parts:
            parts.append(part)
    return ", ".join(parts)


def _qualifier_names(result: Mapping[str, Any]) -> set[str]:
    """What a qualifier may say to mean this result: its region, country or country code, and
    for a US result the state's postal abbreviation."""
    names = {
        value.strip().casefold()
        for value in (result.get(key) for key in ("admin1", "country", "country_code"))
        if isinstance(value, str) and value.strip()
    }
    if "us" in names:
        names |= {abbr for abbr, state in US_STATES.items() if state in names}
        names |= {"usa"}
    return names


def _geocode(text: str, fetch: Fetch) -> tuple[str, str, str]:
    """(label, latitude, longitude) of the best match, coordinates already rounded. One request."""
    tidy = place_text(text)
    name, *rest = (part.strip() for part in tidy.split(","))
    wanted = [part for part in rest if part]
    if not name:
        raise _Problem("no place name given")
    url = geocode_url(name, GEOCODE_COUNT if wanted else 1)
    body = _fetch(fetch, url, GEOCODE_TIMEOUT_SECONDS, "place lookup")
    malformed = "unexpected place lookup response"
    try:
        data = json.loads(body)
    except (ValueError, RecursionError, TypeError):
        raise _Problem(f"{malformed}: not JSON") from None
    if not isinstance(data, dict):
        raise _Problem(f"{malformed}: not a JSON object")
    if data.get("error"):
        raise _Problem("Open-Meteo rejected the place lookup")
    results = data.get("results", [])  # no match: the key is left out
    if not isinstance(results, list):
        raise _Problem(f"{malformed}: results is not a list")
    found: list[tuple[str, str, str, set[str]]] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        point: list[str | None] = []
        for key, limit in (("latitude", 90.0), ("longitude", 180.0)):
            value = result.get(key)
            if isinstance(value, bool) or not isinstance(value, int | float):
                point.append(None)
            else:
                point.append(_rounded(repr(float(value)), limit))
        label = _label(result)
        if _label_part(result.get("name")) and point[0] and point[1]:
            found.append((label, point[0], point[1], _qualifier_names(result)))
    if results and not found:
        raise _Problem(f"{malformed}: no usable result")
    if not found:
        raise _Problem(f'no place found for "{name}"', not_found=True)
    for label, latitude, longitude, names in found:
        if all(part.casefold() in names for part in wanted):
            return label, latitude, longitude
    others = "; ".join(dict.fromkeys(label for label, *_ in found[:3]))
    raise _Problem(f'no "{name}" found in "{", ".join(wanted)}" (found: {others})', not_found=True)


def geocode(name: str, fetch: Fetch | None = None) -> dict[str, Any]:
    """The best match for a typed place name from Open-Meteo's geocoding API (one request).

    `{"ok": True, "label", "latitude", "longitude"}`: the label is name, region, country as
    returned; the coordinates are request strings already rounded to 2 decimals (the unrounded
    ones never leave this module). Otherwise `{"ok": False, "reason", "not_found"}`:
    `not_found` is True when the service answered and had no such place, False when the text
    was refused or the lookup failed. Never raises.
    """
    try:
        label, latitude, longitude = _geocode(name, fetch or http_get)
    except _Problem as problem:
        return {"ok": False, "not_found": False, "reason": problem.reason, **problem.extra}
    except Exception as exc:
        return {
            "ok": False,
            "not_found": False,
            "reason": f"place lookup failed ({type(exc).__name__})",
        }
    return {"ok": True, "label": label, "latitude": latitude, "longitude": longitude}


def _near(env: Mapping[str, str], near: str, fetch: Fetch) -> tuple[str, str, str, str]:
    """Typed text to a location: an exact named place wins, anything else is looked up."""
    usable, problems = places(env)
    try:
        name = place_text(near).lower()
        if name == DEFAULT_PLACE:
            return (DEFAULT_PLACE, *_coordinates(env), SOURCE_DEFAULT)
        if name in usable:
            return (name, *usable[name], SOURCE_NAMED)
        if name in problems and PLACE_NAME.fullmatch(name):
            raise _Problem(problems[name])  # her own name for a place, broken: not ours to guess
        return (*_geocode(near, fetch), SOURCE_LOOKUP)
    except _Problem as problem:
        extra = {"not_found": False, **problem.extra}
        extra.update(place_source=SOURCE_LOOKUP, known_places=sorted(usable))
        raise _Problem(problem.reason, **extra) from None


def _location(
    env: Mapping[str, str], place: str | None, near: str | None, fetch: Fetch
) -> tuple[str, str, str, str]:
    """(place, latitude, longitude, source) for the request: the default location, one named
    place, or one looked-up place. Only that one location is returned, so only it can be sent."""
    if near is not None:
        if place is not None:
            raise _Problem("give a named place or a place to look up, not both")
        return _near(env, near, fetch)
    name = DEFAULT_PLACE if place is None else place.strip().lower()
    if name == DEFAULT_PLACE:
        return (DEFAULT_PLACE, *_coordinates(env), SOURCE_DEFAULT)
    usable, problems = places(env)
    if name in usable:
        return (name, *usable[name], SOURCE_NAMED)
    known = sorted(usable)
    if name in problems and PLACE_NAME.fullmatch(name):
        raise _Problem(problems[name], known_places=known)
    # What was typed is echoed only when it is shaped like a name (it could be coordinates).
    what = f'unknown place "{name}"' if PLACE_NAME.fullmatch(name) else "that is not a place name"
    if known:
        raise _Problem(f"{what}; known places: {', '.join(known)}", known_places=known)
    raise _Problem(
        f"{what}; no places are set (add {PLACES_ENV}=name:lat,lon;name:lat,lon to .env)",
        known_places=known,
    )


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


def _fetch(
    fetch: Fetch, url: str, timeout: float = TIMEOUT_SECONDS, what: str = "weather request"
) -> bytes:
    """One request. Reasons name `what` and never carry the URL."""
    timed_out = f"{what} timed out after {timeout:g} s"
    try:
        return fetch(url, timeout)
    except urllib.error.HTTPError as exc:
        raise _Problem(f"{what} failed: HTTP {exc.code}") from None
    except TimeoutError:
        raise _Problem(timed_out) from None
    except (OSError, http.client.HTTPException) as exc:
        # urlopen wraps a connect timeout in URLError
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            raise _Problem(timed_out) from None
        raise _Problem(f"couldn't reach Open-Meteo ({type(exc).__name__})") from None
    except Exception as exc:  # an injected fetch may fail any way; callers must never see it
        raise _Problem(f"{what} failed ({type(exc).__name__})") from None


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


def conditions_text(hour: Mapping[str, Any]) -> str:
    """One hour row as a short display line in °F and mph. Values only: no advice and no
    judgement. A missing value reads "n/a"; WBGT appears only when NWS returned it."""

    def show(key: str, unit: str) -> str:
        value = hour.get(key)
        return "n/a" if value is None else f"{js_round(value)}{unit}"

    text = (
        f"{show('temperature_f', '°F')} (feels {show('apparent_temperature_f', '°F')}), "
        f"dew point {show('dew_point_f', '°F')}, wind {show('wind_speed_mph', ' mph')}, "
        f"rain chance {show('precipitation_probability_pct', '%')}"
    )
    if hour.get("wbgt_f") is not None:
        text += f", WBGT {show('wbgt_f', '°F')}"
    return text


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


def _reusing(fetch: Fetch, bodies: dict[str, bytes]) -> Fetch:
    """`fetch`, asking once per URL: a body already in `bodies` is returned without a request."""

    def get(url: str, timeout: float) -> bytes:
        if url not in bodies:
            bodies[url] = fetch(url, timeout)  # a failure raises here and nothing is kept
        return bodies[url]

    return get


def _reusing_nws(fetch: NwsFetch, bodies: dict[str, bytes]) -> NwsFetch:
    def get(url: str, timeout: float, headers: Mapping[str, str]) -> bytes:
        if url not in bodies:
            bodies[url] = fetch(url, timeout, headers)
        return bodies[url]

    return get


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
    place: str | None,
    near: str | None,
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
    place_name, latitude, longitude, source = _location(env, place, near, fetch)

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
        "place": place_name,
        "place_source": source,
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
        "attribution": [
            ATTRIBUTION,
            *([GEOCODE_ATTRIBUTION] if source == SOURCE_LOOKUP else []),
            *([NWS_ATTRIBUTION] if nws["ok"] else []),
        ],
    }


def forecast(
    *,
    timezone: str,
    date: str | None = None,
    hour: int | None = None,
    hours: int = 3,
    place: str | None = None,
    near: str | None = None,
    env: Mapping[str, str],
    fetch: Fetch | None = None,
    nws_fetch: NwsFetch | None = None,
    now: datetime | None = None,
    reuse: dict[str, bytes] | None = None,
) -> dict[str, Any]:
    """Hourly conditions for `hours` hours from `hour` on `date`, local to `timezone`.

    Defaults: today and the current local hour. `env` supplies `COACH_LAT` / `COACH_LON` and,
    optionally, `COACH_NWS_CONTACT` (adds `wbgt_c` from NWS; see the module docstring) and
    `COACH_PLACES`. `place` names one of those places (case-insensitive); without it the default
    location is used. `near` is typed text instead (not both): an exact named place wins,
    anything else is looked up by name (module docstring). The result's `place` is the name,
    "default" or the looked-up place's label, never coordinates; `place_source` says which
    ("default", "named", "lookup"). A failed `near` is `ok: False` with `place_source:
    "lookup"`, `not_found` and `known_places`, and no forecast is requested.
    Returns `{"ok": True, "hours": [...], ...}` or `{"ok": False, "reason": ...}`; never raises.
    An unknown or unusable place is `ok: False` with `known_places`, and nothing is requested.
    `reuse`: a dict shared by several calls so identical requests are made once (module docstring).
    """
    get, nws_get = fetch or http_get, nws_fetch or nws_http_get
    if reuse is not None:
        get, nws_get = _reusing(get, reuse), _reusing_nws(nws_get, reuse)
    try:
        return _forecast(
            timezone,
            date,
            hour,
            hours,
            place,
            near,
            env,
            get,
            nws_get,
            now,
        )
    except _Problem as problem:
        return {"ok": False, "reason": problem.reason, **problem.extra}
    except Exception as exc:  # the promise is "never raises": name the type, nothing else
        return {"ok": False, "reason": f"weather lookup failed ({type(exc).__name__})"}
