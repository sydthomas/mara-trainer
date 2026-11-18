"""Runs logged by hand (source `manual`): days the watch didn't record ("forgot my watch").

- A hand-logged run has a date, a distance and a duration. It has no clock time, so it is stored
  as starting at 00:00 athlete-local on its date: it lands on that day in every rollup and
  counts as soon as it's saved (rollups leave out runs that start after "now").
- Duration is required. The schema needs one, and an estimate would quietly skew time totals
  and pace, so the coach asks once instead of guessing.
- The date is today or earlier (athlete-local), never a future day.
- Dedup follows `imports` with one difference: every run on the same athlete-local day (any
  source) is a possible duplicate the runner must confirm, because a hand-logged distance and
  time rarely fall inside the file-import tolerances of the watch's copy. That includes an
  identical hand-logged run: two real runs of the same distance and time on one day happen, so
  it is never a silent replay. A confirmed run starts n seconds after 00:00 (n = hand-logged
  runs already that day, moved on if taken), so its fingerprint and source_id are its own. The
  reverse case (the watch file imported after a hand-logged run) is flagged by
  `imports.find_candidates` with the same day rule. Nothing is merged or deleted automatically.
- No import batch is written: "last import" stays about files from the watch.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import db, imports
from .dates import date_in_timezone, iso_utc, parse_instant

NOTES_MAX = 2000
MAX_METERS = 500000
MAX_SECONDS = 604800
TITLE = "Logged by hand · {date}"


class ManualRunError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class ManualRun:
    date: str  # athlete-local YYYY-MM-DD
    distance_meters: int
    duration_seconds: int
    notes: str | None = None

    def problems(self, today: str) -> list[str]:
        out: list[str] = []
        if not _is_day(self.date):
            out.append(f"date must be YYYY-MM-DD, got {self.date!r}")
        elif self.date > today:
            out.append(f"date can't be in the future (today is {today})")
        if not _whole(self.distance_meters) or not 0 < self.distance_meters <= MAX_METERS:
            out.append(f"distance must be more than 0 and at most {MAX_METERS} m")
        if not _whole(self.duration_seconds) or not 0 < self.duration_seconds <= MAX_SECONDS:
            out.append(f"duration must be more than 0 and at most {MAX_SECONDS} s")
        if self.notes is not None and len(self.notes) > NOTES_MAX:
            out.append(f"notes must be at most {NOTES_MAX} chars")
        return out


def _is_day(value: Any) -> bool:
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _clean(text: str | None) -> str | None:
    return (text.strip() or None) if text is not None else None


def _today(conn: sqlite3.Connection) -> str:
    return date_in_timezone(datetime.now(UTC), db.athlete_timezone(conn))


def draft(run: ManualRun, timezone: str, offset_seconds: int = 0) -> imports.Draft:
    """The run as an import draft: 00:00 athlete-local on its date (plus `offset_seconds`),
    fingerprinted as files are."""
    start = datetime.combine(date.fromisoformat(run.date), time.min, ZoneInfo(timezone))
    started = iso_utc(start + timedelta(seconds=offset_seconds))
    return imports.Draft(
        "manual",
        TITLE.format(date=run.date),
        started,
        run.distance_meters,
        run.duration_seconds,
        imports.fingerprint(started, run.distance_meters, run.duration_seconds),
    )


def check(conn: sqlite3.Connection, run: ManualRun, today: str | None = None) -> dict[str, Any]:
    """Validate a run and look for duplicates. Never saves."""
    run = ManualRun(run.date, run.distance_meters, run.duration_seconds, _clean(run.notes))
    problems = run.problems(today or _today(conn))
    if problems:
        return {"ok": False, "problems": problems}
    tz = db.athlete_timezone(conn)
    candidate = imports.find_candidates(conn, draft(run, tz))
    return {
        "ok": True,
        "problems": [],
        "entry": {k: v for k, v in run.__dict__.items() if v is not None},
        # each with its athlete-local `date` added
        "possible_duplicates": [_dated(row, tz) for row in candidate.possible_duplicates],
    }


def _dated(row: dict[str, Any], tz: str) -> dict[str, Any]:
    return {**row, "date": date_in_timezone(parse_instant(row["started_at"]), tz)}


def add_run(
    conn: sqlite3.Connection,
    run: ManualRun,
    allow_duplicate: bool = False,
    today: str | None = None,
) -> dict[str, Any]:
    """Validate, check for duplicates, then save. `status` is one of:
    - `saved` (with `activity`),
    - `needs_confirmation`: another run that day, an identical one too (`possible_duplicates`),
      nothing written; call again with `allow_duplicate=True` only if the runner says it's a
      different run.
    Raises ManualRunError for an invalid entry."""
    run = ManualRun(run.date, run.distance_meters, run.duration_seconds, _clean(run.notes))
    problems = run.problems(today or _today(conn))
    if problems:
        raise ManualRunError(problems)
    tz = db.athlete_timezone(conn)
    candidate = imports.find_candidates(conn, draft(run, tz))
    if candidate.possible_duplicates and not allow_duplicate:
        return {
            "status": "needs_confirmation",
            "entry": run.__dict__,
            "possible_duplicates": [_dated(row, tz) for row in candidate.possible_duplicates],
        }
    offset = sum(1 for row in candidate.possible_duplicates if row["source"] == "manual")
    d = draft(run, tz, offset)
    while conn.execute(
        "SELECT 1 FROM activities WHERE source = 'manual' AND source_id = ?",
        (f"manual:{d.fingerprint}",),
    ).fetchone():  # a start left free by a deleted run, or a later one taken: move on
        offset += 1
        d = draft(run, tz, offset)
    with conn:
        cur = conn.execute(
            "INSERT INTO activities(source, source_id, fingerprint, title, started_at, "
            "distance_meters, duration_seconds, rpe, notes) VALUES (?,?,?,?,?,?,?,NULL,?)",
            (
                d.source,
                f"{d.source}:{d.fingerprint}",
                d.fingerprint,
                d.title,
                d.started_at,
                d.distance_meters,
                d.duration_seconds,
                run.notes or "",
            ),
        )
    return {"status": "saved", "activity": {"id": cur.lastrowid, **run.__dict__}}


def delete_run(conn: sqlite3.Connection, activity_id: int) -> bool:
    """Delete one hand-logged run (a typo, or the watch's copy turned up). Runs imported from a
    file are never deleted here."""
    with conn:
        cur = conn.execute(
            "DELETE FROM activities WHERE id = ? AND source = 'manual'", (activity_id,)
        )
    return cur.rowcount > 0
