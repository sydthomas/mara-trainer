"""Source gaps: a private backlog of questions no approved source covers.

When the coach has to say "No approved source covers this", it also logs the topic here, so
Sydney can see which sources are worth finding next. Rows hold a short topic label and a one-line
paraphrase in our own words (no health detail beyond the topic); they never leave the DB.
One open or researching row per topic: asking again counts the ask and keeps the first summary.
Asks within `REPEAT_MINUTES` of the last one are the same question (a follow-up turn), so they
move `last_asked` but don't add to `asks`.

Ranking (`score`) is a descriptive default for ordering a to-do list, not a coaching rule, so it
needs no source. It is code-computed and every point comes with a plain reason:
- asks, counted in full when the topic was last asked within `RECENT_DAYS` (judged on the
  runner's local date), otherwise at half weight (only the latest ask time is stored);
- that ask count doubled for a safety topic (injury, illness, health);
- `RACE_BONUS` when the gap's category matters near the race (`RACE_CATEGORIES`: race fueling
  and race-day kit) and the race is within `RACE_WINDOW_DAYS`;
- `RESEARCH_BONUS` while the gap is being researched.

No LLM here: the coach calls `log_gap` through its tool layer.
"""

from __future__ import annotations

import math
import re
import sqlite3
from datetime import UTC, date, datetime
from typing import Any

from . import db
from .dates import date_in_timezone, parse_instant

CATEGORIES = ("training", "fueling", "recovery", "gear", "health", "other")
STATUSES = ("open", "researching", "closed")
TOPIC_MAX = 60
SUMMARY_MAX = 200
REASON_MAX = 500

RECENT_DAYS = 28
OLD_ASK_WEIGHT = 0.5
SAFETY_FACTOR = 2
RACE_CATEGORIES = frozenset({"fueling", "gear"})
RACE_WINDOW_DAYS = 28
RACE_BONUS = 2
RESEARCH_BONUS = 1
REPEAT_MINUTES = 30

_NON_SLUG = re.compile(r"[^a-z0-9]+")


class GapError(ValueError):
    """Bad input or a missing gap."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_topic(topic: str) -> str:
    """'  Compression Socks!! ' -> 'compression-socks'. Raises GapError if nothing is left."""
    slug = _NON_SLUG.sub("-", topic.lower()).strip("-")
    if not slug:
        raise GapError("topic must contain letters or digits")
    if len(slug) > TOPIC_MAX:
        raise GapError(f"topic must be at most {TOPIC_MAX} characters")
    return slug


def _row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["safety"] = bool(data["safety"])
    return data


def get_gap(conn: sqlite3.Connection, gap_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM source_gaps WHERE id = ?", (gap_id,)).fetchone()
    return _row(row) if row else None


def log_gap(
    conn: sqlite3.Connection,
    topic: str,
    summary: str,
    category: str,
    safety: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    """Log one ask. A new topic makes a row; an open or researching one gets asks + 1 and a new
    last_asked (summary and category stay as first given; safety can only turn on).
    Returns {"created": bool, "gap": row}."""
    slug = normalize_topic(topic)
    text = summary.strip()
    if not text or len(text) > SUMMARY_MAX:
        raise GapError(f"summary must be 1-{SUMMARY_MAX} characters")
    if category not in CATEGORIES:
        raise GapError(f"category must be one of {', '.join(CATEGORIES)}")
    stamp = now or _now()
    # Write lock first (BEGIN IMMEDIATE), then read and upsert: a second writer waits instead
    # of slipping in between, and `created` is known from the read.
    if conn.in_transaction:
        conn.commit()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id FROM source_gaps WHERE topic = ? AND status != 'closed'", (slug,)
        ).fetchone()
        row = conn.execute(
            "INSERT INTO source_gaps(topic, summary, category, safety, first_asked, last_asked) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(topic) WHERE status != 'closed' DO UPDATE SET "
            "asks = asks + (julianday(excluded.last_asked) - julianday(last_asked) "
            "  > ? / 1440.0), "
            "last_asked = excluded.last_asked, safety = max(safety, excluded.safety) "
            "RETURNING id",
            (slug, text, category, int(safety), stamp, stamp, REPEAT_MINUTES),
        ).fetchone()
    gap_id, created = int(row["id"]), existing is None
    gap = get_gap(conn, gap_id)
    assert gap is not None
    return {"created": created, "gap": gap}


def list_gaps(conn: sqlite3.Connection, status: str = "open") -> list[dict[str, Any]]:
    """Gaps with this status ('all' for every row), oldest first."""
    if status != "all" and status not in STATUSES:
        raise GapError(f"status must be one of {', '.join(STATUSES)} or all")
    rows = conn.execute(
        "SELECT * FROM source_gaps WHERE ? = 'all' OR status = ? ORDER BY id", (status, status)
    ).fetchall()
    return [_row(r) for r in rows]


def close_gap(
    conn: sqlite3.Connection, gap_id: int, reason: str, now: str | None = None
) -> dict[str, Any]:
    """Close a gap with a reason (e.g. 'source approved', 'out of scope'). Kept as history."""
    text = reason.strip()
    if not text or len(text) > REASON_MAX:
        raise GapError(f"reason must be 1-{REASON_MAX} characters")
    gap = get_gap(conn, gap_id)
    if gap is None:
        raise GapError(f"no source gap with id {gap_id}")
    if gap["status"] == "closed":
        raise GapError(f"source gap {gap_id} is already closed")
    with conn:
        conn.execute(
            "UPDATE source_gaps SET status = 'closed', closed_reason = ?, closed_at = ? "
            "WHERE id = ?",
            (text, now or _now(), gap_id),
        )
    closed = get_gap(conn, gap_id)
    assert closed is not None
    return closed


def research_gap(conn: sqlite3.Connection, gap_id: int) -> dict[str, Any]:
    """Mark an open gap as being researched (the Scout found or is looking for sources)."""
    gap = get_gap(conn, gap_id)
    if gap is None:
        raise GapError(f"no source gap with id {gap_id}")
    if gap["status"] != "open":
        raise GapError(f"source gap {gap_id} is {gap['status']}, not open")
    with conn:
        conn.execute("UPDATE source_gaps SET status = 'researching' WHERE id = ?", (gap_id,))
    moved = get_gap(conn, gap_id)
    assert moved is not None
    return moved


def phase_info(conn: sqlite3.Connection, today: str) -> dict[str, Any]:
    """What `score` needs: days from `today` to the saved race (null if none) and the runner's
    timezone (ask dates are compared on her local calendar)."""
    setup = db.load_race_setup(conn)
    days = (
        None
        if setup is None
        else (date.fromisoformat(setup.race_date) - date.fromisoformat(today)).days
    )
    return {"days_to_race": days, "timezone": db.athlete_timezone(conn)}


def ask_age(gap: dict[str, Any], today: str, info: dict[str, Any]) -> int:
    """Days from the last ask to `today`, on the runner's local calendar (UTC if unknown)."""
    zone = info.get("timezone") or "UTC"
    asked = date_in_timezone(parse_instant(str(gap["last_asked"])), zone)
    return (date.fromisoformat(today) - date.fromisoformat(asked)).days


def _times(n: int) -> str:
    return "asked once" if n == 1 else f"asked {n} times"


def score(gap: dict[str, Any], today: str, info: dict[str, Any]) -> dict[str, Any]:
    """Rank one gap (see the module docstring). Returns {score, reasons}."""
    asks = int(gap["asks"])
    reasons = [_times(asks)]
    age = ask_age(gap, today, info)
    weighted = float(asks)
    if age > RECENT_DAYS:
        weighted *= OLD_ASK_WEIGHT
        reasons.append(f"last asked {age} days ago, so asks count half")
    if gap["safety"]:
        weighted *= SAFETY_FACTOR
        reasons.append("health topic")
    total = weighted
    days = info.get("days_to_race")
    if gap["category"] in RACE_CATEGORIES and days is not None and 0 <= days <= RACE_WINDOW_DAYS:
        total += RACE_BONUS
        weeks = math.ceil(days / 7)
        reasons.append(
            "race today" if days == 0 else f"race in {weeks} week{'s' if weeks != 1 else ''}"
        )
    if gap["status"] == "researching":
        total += RESEARCH_BONUS
        reasons.append("being researched")
    return {"score": round(total, 2), "reasons": reasons}


def ranked(
    conn: sqlite3.Connection, status: str, today: str, info: dict[str, Any]
) -> list[dict[str, Any]]:
    """`list_gaps` with each row's score and reasons, highest score first (ties: most recent)."""
    rows = [gap | score(gap, today, info) for gap in list_gaps(conn, status)]
    rows.sort(key=lambda g: g["last_asked"], reverse=True)
    rows.sort(key=lambda g: g["score"], reverse=True)
    return rows
