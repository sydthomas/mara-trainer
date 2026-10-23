"""Workout file imports: FIT (default), GPX, Apple Health export.

Ported from legacy apps/api/src/fit.ts (decodeFit) and packages/db/src/imports.ts (dedup):
- exact fingerprint = sha256(JSON [UTC start ISO with ms, meters, seconds]) -> already present,
  skipped
- near match (start ±60 s, distance ±max(100 m, 2%), duration ±max(60 s, 2%)) across ALL sources
  -> a *candidate* the user must confirm. Nothing is ever auto-deleted or silently merged.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .dates import date_in_timezone, iso_utc, js_round, parse_instant

MAX_FIT_BYTES = 5 * 1024 * 1024
MAX_FIT_SESSIONS = 20


class ImportError_(ValueError):  # noqa: N801 - avoid shadowing builtin ImportError
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}{': ' + detail if detail else ''}")


@dataclass
class Draft:
    source: str
    title: str
    started_at: str  # ISO UTC with ms, e.g. 2026-01-01T07:00:00.000Z
    distance_meters: int
    duration_seconds: int
    fingerprint: str
    rpe: None = None

    @property
    def key(self) -> str:
        return self.fingerprint[:12]


@dataclass
class DecodedFile:
    source: str
    file_hash: str
    drafts: list[Draft]
    warnings: list[str] = field(default_factory=list)
    check_ins: dict[str, dict[str, float]] = field(default_factory=dict)  # date -> metrics


def fingerprint(started_at_iso: str, meters: int, seconds: int) -> str:
    payload = json.dumps([started_at_iso, meters, seconds], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def make_draft(source: str, start: datetime, meters: float, seconds: float, now: datetime) -> Draft:
    m, s = js_round(meters), js_round(seconds)
    if not (0 < m <= 500000) or not (0 < s <= 604800) or start > now:
        raise ImportError_("RUNNING_SESSION_INVALID_METRICS", f"{iso_utc(start)} {m} m {s} s")
    started = iso_utc(start)
    return Draft(
        source, f"Imported run · {started[:10]}", started, m, s, fingerprint(started, m, s)
    )


def _dedupe_within_file(drafts: list[Draft], warnings: list[str]) -> list[Draft]:
    seen: set[str] = set()
    out = []
    for i, d in enumerate(drafts):
        if d.fingerprint in seen:
            warnings.append(
                f"Session {i + 1} repeated an identical running session and was skipped."
            )
            continue
        seen.add(d.fingerprint)
        out.append(d)
    return out


# ------------------------------------------------------------------ FIT


def decode_fit(data: bytes, now: datetime | None = None) -> DecodedFile:
    from garmin_fit_sdk import Decoder, Stream

    now = now or datetime.now(UTC)
    if len(data) > MAX_FIT_BYTES:
        raise ImportError_("FIT_TOO_LARGE")
    try:
        checker = Decoder(Stream.from_byte_array(bytearray(data)))
        if not checker.is_fit() or not checker.check_integrity():
            raise ImportError_("INVALID_FIT_FILE")
        # check_integrity() consumes the stream, so read with a fresh decoder.
        decoder = Decoder(Stream.from_byte_array(bytearray(data)))
        messages, errors = decoder.read(merge_heart_rates=False)
    except ImportError_:
        raise
    except Exception as exc:  # the SDK raises assorted errors on garbage input
        raise ImportError_("INVALID_FIT_FILE") from exc
    if errors:
        raise ImportError_("FIT_DECODE_FAILED", str(errors[0]))
    sessions = messages.get("session_mesgs", [])
    if len(sessions) > MAX_FIT_SESSIONS:
        raise ImportError_("TOO_MANY_FIT_SESSIONS")
    warnings: list[str] = []
    drafts: list[Draft] = []
    for i, s in enumerate(sessions):
        if s.get("sport") != "running":
            warnings.append(f"Session {i + 1} was skipped because it is not a running session.")
            continue
        start = s.get("start_time")
        dist = s.get("total_distance")
        dur = s.get("total_timer_time", s.get("total_elapsed_time"))
        if (
            not isinstance(start, datetime)
            or not isinstance(dist, (int, float))
            or not isinstance(dur, (int, float))
        ):
            raise ImportError_("FIT_RUNNING_SESSION_MISSING_METRICS")
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        drafts.append(make_draft("fit", start, dist, dur, now))
    drafts = _dedupe_within_file(drafts, warnings)
    if not drafts:
        raise ImportError_("NO_SUPPORTED_RUNNING_SESSIONS")
    return DecodedFile("fit", hashlib.sha256(data).hexdigest(), drafts, warnings)


# ------------------------------------------------------------------ GPX


def decode_gpx(data: bytes, now: datetime | None = None) -> DecodedFile:
    import gpxpy

    now = now or datetime.now(UTC)
    try:
        gpx = gpxpy.parse(data.decode("utf-8"))
    except Exception as exc:
        raise ImportError_("INVALID_GPX_FILE") from exc
    warnings: list[str] = []
    drafts: list[Draft] = []
    for i, track in enumerate(gpx.tracks):
        kind = (track.type or "running").lower()
        if "run" not in kind:
            warnings.append(f"Track {i + 1} skipped: type {track.type!r} is not running.")
            continue
        bounds = track.get_time_bounds()
        if bounds.start_time is None or bounds.end_time is None:
            raise ImportError_("GPX_TRACK_MISSING_TIMES", f"track {i + 1}")
        start = (
            bounds.start_time if bounds.start_time.tzinfo else bounds.start_time.replace(tzinfo=UTC)
        )
        moving = track.get_moving_data()
        seconds = (
            moving.moving_time
            if moving and moving.moving_time
            else (bounds.end_time - bounds.start_time).total_seconds()
        )
        drafts.append(make_draft("gpx", start, track.length_2d(), seconds, now))
    drafts = _dedupe_within_file(drafts, warnings)
    if not drafts:
        raise ImportError_("NO_SUPPORTED_RUNNING_SESSIONS")
    return DecodedFile("gpx", hashlib.sha256(data).hexdigest(), drafts, warnings)


# ------------------------------------------------------------------ Apple Health

_HK_DT = "%Y-%m-%d %H:%M:%S %z"
_DIST_UNITS = {"km": 1000.0, "m": 1.0, "mi": 1609.344}
_DUR_UNITS = {"min": 60.0, "s": 1.0, "hr": 3600.0, "h": 3600.0}
_HRV = "HKQuantityTypeIdentifierHeartRateVariabilitySDNN"
_RHR = "HKQuantityTypeIdentifierRestingHeartRate"


def decode_apple_health(
    data: bytes, timezone: str = "UTC", now: datetime | None = None
) -> DecodedFile:
    """Stream-parse export.xml (or export.zip): running workouts + daily HRV / resting HR.

    Sleep is not imported yet (needs interval merging); it stays missing, never zero.
    """
    now = now or datetime.now(UTC)
    stream: io.BufferedIOBase | Any
    if data[:2] == b"PK":
        zf = zipfile.ZipFile(io.BytesIO(data))
        name = next((n for n in zf.namelist() if n.endswith("export.xml")), None)
        if name is None:
            raise ImportError_("APPLE_HEALTH_EXPORT_XML_NOT_FOUND")
        stream = zf.open(name)
    else:
        stream = io.BytesIO(data)
    drafts: list[Draft] = []
    warnings: list[str] = []
    daily: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    try:
        for _, el in ET.iterparse(stream, events=("end",)):
            if el.tag == "Workout":
                if el.get("workoutActivityType") == "HKWorkoutActivityTypeRunning":
                    drafts.append(_hk_workout(el, now))
                el.clear()
            elif el.tag == "Record" and el.get("type") in (_HRV, _RHR):
                start = datetime.strptime(el.get("startDate", ""), _HK_DT)
                metric = "hrv_ms" if el.get("type") == _HRV else "resting_hr"
                daily[date_in_timezone(start, timezone)][metric].append(float(el.get("value", 0)))
                el.clear()
    except ET.ParseError as exc:
        raise ImportError_("INVALID_APPLE_HEALTH_EXPORT") from exc
    drafts = _dedupe_within_file(drafts, warnings)
    check_ins = {
        d: {k: round(sum(v) / len(v), 2) for k, v in metrics.items() if v}
        for d, metrics in daily.items()
    }
    if not drafts and not check_ins:
        raise ImportError_("NO_SUPPORTED_RUNNING_SESSIONS")
    return DecodedFile(
        "apple_health", hashlib.sha256(data).hexdigest(), drafts, warnings, check_ins
    )


def _hk_workout(el: ET.Element, now: datetime) -> Draft:
    start = datetime.strptime(el.get("startDate", ""), _HK_DT)
    dur = float(el.get("duration", 0)) * _DUR_UNITS.get(el.get("durationUnit", "min"), 60.0)
    meters: float | None = None
    if el.get("totalDistance"):
        meters = float(el.get("totalDistance", 0)) * _DIST_UNITS.get(
            el.get("totalDistanceUnit", "km"), 1000.0
        )
    else:  # newer exports: <WorkoutStatistics type="...DistanceWalkingRunning" sum=... unit=...>
        for stat in el.iter("WorkoutStatistics"):
            if stat.get("type") == "HKQuantityTypeIdentifierDistanceWalkingRunning":
                meters = float(stat.get("sum", 0)) * _DIST_UNITS.get(stat.get("unit", "km"), 1000.0)
    if meters is None:
        raise ImportError_("APPLE_HEALTH_WORKOUT_MISSING_DISTANCE", el.get("startDate", ""))
    return make_draft("apple_health", start, meters, dur, now)


# ------------------------------------------------------------------ dispatch + dedup + commit


def decode_file(path: Path, timezone: str = "UTC", now: datetime | None = None) -> DecodedFile:
    data = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix == ".fit":
        return decode_fit(data, now)
    if suffix == ".gpx":
        return decode_gpx(data, now)
    if suffix in (".xml", ".zip"):
        return decode_apple_health(data, timezone, now)
    raise ImportError_("UNSUPPORTED_FILE_TYPE", suffix or path.name)


@dataclass
class Candidate:
    draft: Draft
    exact_duplicate: bool
    possible_duplicates: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.draft.key,
            **asdict(self.draft),
            "exact_duplicate": self.exact_duplicate,
            "possible_duplicates": self.possible_duplicates,
        }


def find_candidates(conn: sqlite3.Connection, draft: Draft) -> Candidate:
    exact = conn.execute(
        "SELECT 1 FROM activities WHERE fingerprint = ? LIMIT 1", (draft.fingerprint,)
    ).fetchone()
    if exact:
        return Candidate(draft, True, [])
    start = parse_instant(draft.started_at)
    lo, hi = iso_utc(start - timedelta(seconds=60)), iso_utc(start + timedelta(seconds=60))
    d, s = draft.distance_meters, draft.duration_seconds
    rows = conn.execute(
        "SELECT id, source, title, started_at, distance_meters, duration_seconds FROM activities "
        "WHERE started_at BETWEEN ? AND ? AND abs(distance_meters - ?) <= max(100, ? * 0.02) "
        "AND abs(duration_seconds - ?) <= max(60, ? * 0.02) ORDER BY started_at LIMIT 10",
        (lo, hi, d, d, s, s),
    ).fetchall()
    return Candidate(draft, False, [dict(r) for r in rows])


@dataclass
class ImportResult:
    imported: int = 0
    already_present: int = 0
    needs_confirmation: list[str] = field(default_factory=list)
    check_in_days: int = 0
    replayed_file: bool = False


def preview(conn: sqlite3.Connection, decoded: DecodedFile) -> list[Candidate]:
    return [find_candidates(conn, d) for d in decoded.drafts]


def commit(
    conn: sqlite3.Connection,
    decoded: DecodedFile,
    filename: str,
    allow_duplicate_keys: frozenset[str] = frozenset(),
) -> ImportResult:
    """All-or-nothing: if any near-duplicate isn't explicitly allowed, nothing is written."""
    result = ImportResult()
    prior = conn.execute(
        "SELECT result FROM import_batches WHERE file_hash = ?", (decoded.file_hash,)
    ).fetchone()
    candidates = preview(conn, decoded)
    result.needs_confirmation = [
        c.draft.key
        for c in candidates
        if not c.exact_duplicate
        and c.possible_duplicates
        and c.draft.key not in allow_duplicate_keys
    ]
    if result.needs_confirmation:
        return result
    with conn:
        for c in candidates:
            if c.exact_duplicate:
                result.already_present += 1
                continue
            d = c.draft
            cur = conn.execute(
                "INSERT OR IGNORE INTO activities(source, source_id, fingerprint, title, "
                "started_at,"
                " distance_meters, duration_seconds, rpe) VALUES (?,?,?,?,?,?,?,NULL)",
                (
                    d.source,
                    f"{d.source}:{d.fingerprint}",
                    d.fingerprint,
                    d.title,
                    d.started_at,
                    d.distance_meters,
                    d.duration_seconds,
                ),
            )
            if cur.rowcount:
                result.imported += 1
            else:
                result.already_present += 1
        for day, metrics in decoded.check_ins.items():
            conn.execute(
                "INSERT INTO daily_check_ins(date, hrv_ms, resting_hr, source) VALUES (?,?,?,?) "
                "ON CONFLICT(date) DO UPDATE SET hrv_ms=coalesce(excluded.hrv_ms, hrv_ms), "
                "resting_hr=coalesce(excluded.resting_hr, resting_hr)",
                (day, metrics.get("hrv_ms"), metrics.get("resting_hr"), decoded.source),
            )
            result.check_in_days += 1
        result.replayed_file = prior is not None
        conn.execute(
            "INSERT INTO import_batches(file_hash, filename, source, result) VALUES (?,?,?,?) "
            "ON CONFLICT(file_hash) DO UPDATE SET result=excluded.result",
            (decoded.file_hash, filename[:160], decoded.source, json.dumps(asdict(result))),
        )
    return result
