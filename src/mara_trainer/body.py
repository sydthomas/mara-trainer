"""Body check / niggle log: how the runner says their body feels.

Each entry is the runner's own self-report: a status (green, yellow or red), optionally when it
was noted (before a run, after a run, other), where it is (free text), whether there is swelling
and pain 0-10. Several entries per day are fine.

This module stores and lists; it does not judge. Nothing here classifies an entry, changes the
status the runner gave, or says whether to run: red-flag rules wait on approved sources. Missing
stays missing: unset fields are null (swelling False and pain 0 are real answers, not missing).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

STATUSES = ("green", "yellow", "red")
CONTEXTS = ("before_run", "after_run", "other")
LOCATION_MAX = 200
NOTES_MAX = 2000
MAX_LAST = 200

FIELDS = ("date", "status", "context", "location", "swelling", "pain", "notes")


class BodyError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class BodyEntry:
    date: str
    status: str
    context: str | None = None
    location: str | None = None
    swelling: bool | None = None
    pain: int | None = None
    notes: str | None = None

    def problems(self) -> list[str]:
        out: list[str] = []
        if not _is_day(self.date):
            out.append(f"date must be YYYY-MM-DD, got {self.date!r}")
        if self.status not in STATUSES:
            out.append(f"status must be one of {', '.join(STATUSES)}")
        if self.context is not None and self.context not in CONTEXTS:
            out.append(f"context must be one of {', '.join(CONTEXTS)}")
        if self.pain is not None and not 0 <= self.pain <= 10:
            out.append("pain must be 0-10")
        for key, limit in (("location", LOCATION_MAX), ("notes", NOTES_MAX)):
            value = getattr(self, key)
            if value is not None and len(value) > limit:
                out.append(f"{key} must be at most {limit} chars")
        return out


def _is_day(value: str) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _clean(text: str | None) -> str | None:
    return (text.strip() or None) if text is not None else None


def add_entry(conn: sqlite3.Connection, entry: BodyEntry) -> dict[str, Any]:
    """Validate and save one entry exactly as reported. Returns the saved entry."""
    values = {**entry.__dict__, "location": _clean(entry.location), "notes": _clean(entry.notes)}
    problems = BodyEntry(**values).problems()
    if problems:
        raise BodyError(problems)
    if values["swelling"] is not None:
        values["swelling"] = int(values["swelling"])
    with conn:
        cur = conn.execute(
            f"INSERT INTO niggle_log ({', '.join(FIELDS)}) VALUES ({', '.join('?' * len(FIELDS))})",
            [values[k] for k in FIELDS],
        )
    saved = get_entry(conn, int(cur.lastrowid or 0))
    assert saved is not None
    return saved


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = {"id": row["id"], **{k: row[k] for k in FIELDS}}
    if data["swelling"] is not None:
        data["swelling"] = bool(data["swelling"])
    data["created_at"] = row["created_at"]
    return data


def get_entry(conn: sqlite3.Connection, entry_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM niggle_log WHERE id = ?", (entry_id,)).fetchone()
    return _row_to_dict(row) if row else None


def delete_entry(conn: sqlite3.Connection, entry_id: int) -> bool:
    with conn:
        cur = conn.execute("DELETE FROM niggle_log WHERE id = ?", (entry_id,))
    return cur.rowcount > 0


def list_entries(
    conn: sqlite3.Connection, last: int = 10, since: str | None = None
) -> dict[str, Any]:
    """Newest-first entries, counts by reported status and coverage counts (how many entries
    have each optional detail)."""
    if not 1 <= last <= MAX_LAST:
        raise BodyError([f"last must be 1-{MAX_LAST}"])
    if since is not None and not _is_day(since):
        raise BodyError([f"since must be YYYY-MM-DD, got {since!r}"])
    rows = conn.execute(
        "SELECT * FROM niggle_log WHERE (? IS NULL OR date >= ?) "
        "ORDER BY date DESC, id DESC LIMIT ?",
        (since, since, last),
    ).fetchall()
    entries = [_row_to_dict(r) for r in rows]
    return {
        "count": len(entries),
        "by_status": {s: sum(1 for e in entries if e["status"] == s) for s in STATUSES},
        "coverage": {
            key: sum(1 for e in entries if e[key] is not None)
            for key in ("context", "location", "swelling", "pain")
        },
        "entries": entries,
    }


def open_items(
    conn: sqlite3.Connection, since: str, until: str | None = None
) -> list[dict[str, Any]]:
    """Entries the runner marked yellow or red from `since` through `until` (both inclusive,
    athlete-local dates; no `until` = everything after), oldest first. A plain filter on the
    reported status: nothing is re-rated here."""
    for label, value in (("since", since), ("until", until)):
        if value is not None and not _is_day(value):
            raise BodyError([f"{label} must be YYYY-MM-DD, got {value!r}"])
    rows = conn.execute(
        "SELECT * FROM niggle_log WHERE status IN ('yellow', 'red') AND date >= ? "
        "AND (? IS NULL OR date <= ?) ORDER BY date, id",
        (since, until, until),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]
