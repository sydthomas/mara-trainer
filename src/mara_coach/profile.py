"""Runner profile: diet, allergies, shoe preferences, injury history and notes, in the runner's
own words. The coach reads it to fit fueling and gear advice; it never infers or fills fields.

Missing stays missing: an unset field is null. For list fields an empty list means the runner
said "none", which is different from null (not asked yet). Updates are partial: only the given
fields change, and `None` clears a field back to null.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

TEXT_FIELDS = ("diet", "injury_history", "notes")
LIST_FIELDS = ("allergies", "shoe_likes", "shoe_dislikes", "current_shoes")
FIELDS = (
    "diet",
    "allergies",
    "shoe_likes",
    "shoe_dislikes",
    "current_shoes",
    "injury_history",
    "notes",
)
TEXT_MAX = 2000
ITEM_MAX = 100
ITEMS_MAX = 30


class ProfileError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def parse_updates(raw: dict[str, Any]) -> dict[str, str | list[str] | None]:
    """Validate and normalize a partial update. Raises ProfileError listing every problem."""
    if not raw:
        raise ProfileError(["give at least one field to set or clear"])
    problems: list[str] = []
    unknown = sorted(set(raw) - set(FIELDS))
    if unknown:
        problems.append(f"unknown fields {', '.join(unknown)}; known: {', '.join(FIELDS)}")
    out: dict[str, str | list[str] | None] = {}
    for key in (k for k in FIELDS if k in raw):
        value = raw[key]
        if value is None:
            out[key] = None
        elif key in TEXT_FIELDS:
            if not isinstance(value, str) or len(value.strip()) > TEXT_MAX:
                problems.append(f"{key} must be a string of at most {TEXT_MAX} chars (or null)")
            else:
                out[key] = value.strip() or None
        elif not isinstance(value, list) or len(value) > ITEMS_MAX:
            problems.append(f"{key} must be a list of at most {ITEMS_MAX} items (or null)")
        elif not all(isinstance(v, str) and 1 <= len(v.strip()) <= ITEM_MAX for v in value):
            problems.append(f"{key} items must be non-empty strings of at most {ITEM_MAX} chars")
        else:
            out[key] = list(dict.fromkeys(v.strip() for v in value))
    if problems:
        raise ProfileError(problems)
    return out


def get_profile(conn: sqlite3.Connection) -> dict[str, Any]:
    """Every field (null when unset) plus `updated_at` (null if the profile was never set)."""
    row = conn.execute(f"SELECT {', '.join(FIELDS)}, updated_at FROM athlete_profile").fetchone()
    if row is None:
        return {**dict.fromkeys(FIELDS), "updated_at": None}
    data: dict[str, Any] = {}
    for key in FIELDS:
        value = row[key]
        data[key] = json.loads(value) if key in LIST_FIELDS and value is not None else value
    data["updated_at"] = row["updated_at"]
    return data


def update_profile(conn: sqlite3.Connection, raw: dict[str, Any]) -> dict[str, Any]:
    """Apply a partial update and return the full profile."""
    updates = parse_updates(raw)
    keys = list(updates)
    values = [
        json.dumps(v) if k in LIST_FIELDS and v is not None else v for k, v in updates.items()
    ]
    sets = ", ".join(f"{k} = excluded.{k}" for k in keys)
    with conn:
        conn.execute(
            f"INSERT INTO athlete_profile(id, {', '.join(keys)}) "
            f"VALUES (1, {', '.join('?' * len(keys))}) ON CONFLICT(id) DO UPDATE SET {sets}, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            values,
        )
    return get_profile(conn)
