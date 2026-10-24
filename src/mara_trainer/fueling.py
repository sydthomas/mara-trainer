"""Fueling log: practice race-day fueling on long runs.

Each entry records what the runner ate before a run (and how long before), what they took during
it (gels, chews, drink), roughly how many carbs, 1-5 ratings for stomach (1 = bad, 5 = no
issues) and energy, and effort (RPE 1-10). The `debrief` skill fills it after long runs. The
only number computed here is carbs per hour, from the runner's own carb total and the run's
duration.

By design there are no calorie, body-weight or daily-intake fields: the log is for gut training
and race-day planning, not food tracking. Missing stays missing: unset fields are null and
carbs per hour is null unless both carbs and duration are known.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

TEXT_MAX = 500
NOTES_MAX = 2000
MAX_LAST = 200

FIELDS = (
    "date",
    "activity_id",
    "distance_meters",
    "duration_seconds",
    "pre_run",
    "pre_run_minutes",
    "during",
    "carbs_grams",
    "fluid_ml",
    "stomach",
    "energy",
    "effort",
    "notes",
)


class FuelError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class FuelEntry:
    date: str
    activity_id: int | None = None
    distance_meters: int | None = None
    duration_seconds: int | None = None
    pre_run: str | None = None
    pre_run_minutes: int | None = None
    during: str | None = None
    carbs_grams: float | None = None
    fluid_ml: float | None = None
    stomach: int | None = None
    energy: int | None = None
    effort: int | None = None
    notes: str | None = None

    def problems(self) -> list[str]:
        out: list[str] = []
        try:
            date.fromisoformat(self.date)
        except ValueError:
            out.append(f"date must be YYYY-MM-DD, got {self.date!r}")
        for key, low, high in (
            ("distance_meters", 1, 500000),
            ("duration_seconds", 1, 86400),
            ("pre_run_minutes", 0, 720),
            ("carbs_grams", 0, 1000),
            ("fluid_ml", 0, 10000),
            ("stomach", 1, 5),
            ("energy", 1, 5),
            ("effort", 1, 10),
        ):
            value = getattr(self, key)
            if value is not None and not low <= value <= high:
                out.append(f"{key} must be {low}-{high}")
        for key, limit in (("pre_run", TEXT_MAX), ("during", TEXT_MAX), ("notes", NOTES_MAX)):
            value = getattr(self, key)
            if value is not None and len(value) > limit:
                out.append(f"{key} must be at most {limit} chars")
        if all(getattr(self, k) is None for k in FIELDS if k != "date"):
            out.append("give at least one detail besides the date")
        return out


def carbs_per_hour(carbs_grams: float | None, duration_seconds: int | None) -> float | None:
    """Grams of carbohydrate per hour, or None unless both inputs are known."""
    if carbs_grams is None or not duration_seconds:
        return None
    return round(carbs_grams * 3600 / duration_seconds, 1)


def _clean(text: str | None) -> str | None:
    return (text.strip() or None) if text is not None else None


def add_entry(conn: sqlite3.Connection, entry: FuelEntry) -> dict[str, Any]:
    """Validate and save one entry. With `activity_id`, missing distance/duration come from the
    imported activity. Returns the saved entry."""
    entry = FuelEntry(
        **{
            **entry.__dict__,
            "pre_run": _clean(entry.pre_run),
            "during": _clean(entry.during),
            "notes": _clean(entry.notes),
        }
    )
    problems = entry.problems()
    values = dict(entry.__dict__)
    if entry.activity_id is not None:
        row = conn.execute(
            "SELECT distance_meters, duration_seconds FROM activities WHERE id = ?",
            (entry.activity_id,),
        ).fetchone()
        if row is None:
            problems.append(f"no activity with id {entry.activity_id}")
        else:
            for key in ("distance_meters", "duration_seconds"):
                if values[key] is None:
                    values[key] = row[key]
    if problems:
        raise FuelError(problems)
    with conn:
        cur = conn.execute(
            f"INSERT INTO fuel_log ({', '.join(FIELDS)}) VALUES ({', '.join('?' * len(FIELDS))})",
            [values[k] for k in FIELDS],
        )
    saved = get_entry(conn, int(cur.lastrowid or 0))
    assert saved is not None
    return saved


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = {"id": row["id"], **{k: row[k] for k in FIELDS}}
    data["carbs_per_hour"] = carbs_per_hour(row["carbs_grams"], row["duration_seconds"])
    data["created_at"] = row["created_at"]
    return data


def get_entry(conn: sqlite3.Connection, entry_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM fuel_log WHERE id = ?", (entry_id,)).fetchone()
    return _row_to_dict(row) if row else None


def delete_entry(conn: sqlite3.Connection, entry_id: int) -> bool:
    with conn:
        cur = conn.execute("DELETE FROM fuel_log WHERE id = ?", (entry_id,))
    return cur.rowcount > 0


def list_entries(
    conn: sqlite3.Connection, last: int = 10, since: str | None = None
) -> dict[str, Any]:
    """Newest-first entries plus coverage counts (how many entries have each measure)."""
    if not 1 <= last <= MAX_LAST:
        raise FuelError([f"last must be 1-{MAX_LAST}"])
    if since is not None:
        try:
            date.fromisoformat(since)
        except ValueError as exc:
            raise FuelError([f"since must be YYYY-MM-DD, got {since!r}"]) from exc
    rows = conn.execute(
        "SELECT * FROM fuel_log WHERE (? IS NULL OR date >= ?) ORDER BY date DESC, id DESC LIMIT ?",
        (since, since, last),
    ).fetchall()
    entries = [_row_to_dict(r) for r in rows]
    rates = [e["carbs_per_hour"] for e in entries if e["carbs_per_hour"] is not None]
    return {
        "count": len(entries),
        "coverage": {
            key: sum(1 for e in entries if e[key] is not None)
            for key in (
                "carbs_grams",
                "duration_seconds",
                "carbs_per_hour",
                "stomach",
                "energy",
                "effort",
            )
        },
        "carbs_per_hour_range": [min(rates), max(rates)] if rates else None,
        "entries": entries,
    }
