"""Fixed-time Slack messages queued on Slack's servers, so they arrive while the laptop sleeps.

While the bot is awake it keeps the next `HORIZON_DAYS` of fallbacks queued with
`chat.scheduleMessage` (recorded in `scheduled_messages`):
- the long-run debrief ping at `PING_HOUR` on plan long-run days (static text, never cancelled),
- a generic sync reminder at `SYNC_HOUR` on plan Sundays,
- a short weekly note at `WEEKLY_HOUR` on Sundays: next week's runs from the plan, and that the
  full recap follows when the laptop wakes.
The sync and weekly fallbacks are "live-replaceable": if the bot is awake `LEAD` before their
post time, it cancels them and sends the live version (real counts, the coach's recap).
Plan changes are reconciled: stale queued messages are deleted and re-queued with new text.
Rows are only touched while their post time is more than `LEAD` away, so nothing races Slack.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from mara_trainer import db, reports

log = logging.getLogger(__name__)

DEBRIEF_KIND = "long_run_debrief"
SYNC_KIND = "sync_nudge"
WEEKLY_KIND = "weekly_checkin"
PING_HOUR = 10  # athlete-local, Sydney's choice
SYNC_HOUR = 17  # Sunday, athlete-local: an hour before the weekly check-in
WEEKLY_HOUR = 18  # Sunday, athlete-local, Sydney's choice
HORIZON_DAYS = 7
LEAD = timedelta(minutes=5)  # cancel live-replaceable fallbacks this long before they post
LIVE_REPLACEABLE = (SYNC_KIND, WEEKLY_KIND)
MI_METERS = 1609.344
RUN_KINDS = ("easy", "workout", "long")
SYNC_FALLBACK = (
    "Check-in at 6pm. If you haven't synced this week's runs, say \"sync my runs\" in Claude "
    "Code first so the recap has your data."
)


def ping_text(miles: float) -> str:
    return (
        f"Long run day: {miles:g} mi on the plan. How'd it go? When you have a minute, tell me "
        "the distance and time, effort (1-10), what you ate before and how long before, what "
        "you took during, and how your stomach and energy held up."
    )


def weekly_fallback_text(next_week: dict[str, Any] | None) -> str:
    lines = [
        "Sunday check-in. My laptop is asleep, so your full recap comes when it wakes (until 10pm)."
    ]
    if next_week:
        runs = [s for s in next_week["sessions"] if s["kind"] in RUN_KINDS]
        strength = [s["weekday"][:3] for s in next_week["sessions"] if s["kind"] == "strength"]
        lines.append(f"Next week (week {next_week['week']}):")
        lines += [
            f"• {s['weekday'][:3]} {s['date'][5:]}: {s['miles']:g} mi {s['kind']}" for s in runs
        ]
        if strength:
            lines.append("Strength: " + ", ".join(strength))
    return "\n".join(lines)


class ScheduleClient(Protocol):
    def chat_scheduleMessage(self, **kwargs: Any) -> Any: ...  # noqa: N802 (Slack SDK name)
    def chat_deleteScheduledMessage(self, **kwargs: Any) -> Any: ...  # noqa: N802


@dataclass(frozen=True)
class Wanted:
    kind: str
    date: str
    post_at: datetime  # UTC
    text: str


def _at(day: date, hour: int, tz: str) -> datetime:
    return datetime.combine(day, time(hour), ZoneInfo(tz)).astimezone(UTC)


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def wanted(conn: sqlite3.Connection, now: datetime) -> dict[tuple[str, str], Wanted]:
    """Fallbacks that should be queued: the next HORIZON_DAYS, post time more than LEAD away."""
    tz = db.athlete_timezone(conn)
    plan = db.load_accepted_plan(conn)
    today = now.astimezone(ZoneInfo(tz)).date()
    out: dict[tuple[str, str], Wanted] = {}
    for offset in range(HORIZON_DAYS + 1):
        day = today + timedelta(days=offset)
        iso = day.isoformat()
        items: list[Wanted] = []
        session = reports.planned_session(plan, iso)
        if session and session["kind"] == "long":
            miles = round(session["distance_meters"] / MI_METERS, 1)
            items.append(Wanted(DEBRIEF_KIND, iso, _at(day, PING_HOUR, tz), ping_text(miles)))
        if day.weekday() == 6:
            this_week = reports.plan_week(plan, iso)
            next_week = reports.plan_week(plan, (day + timedelta(days=1)).isoformat())
            if this_week:
                items.append(Wanted(SYNC_KIND, iso, _at(day, SYNC_HOUR, tz), SYNC_FALLBACK))
            if this_week or next_week:
                text = weekly_fallback_text(next_week)
                items.append(Wanted(WEEKLY_KIND, iso, _at(day, WEEKLY_HOUR, tz), text))
        for item in items:
            if item.post_at > now + LEAD:
                out[(item.kind, item.date)] = item
    return out


def _rows(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    cur = conn.execute("SELECT * FROM scheduled_messages")
    names = [d[0] for d in cur.description]
    rows = [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
    return {(r["kind"], r["date"]): r for r in rows}


def _set(conn: sqlite3.Connection, w: Wanted, channel: str, slack_id: str, status: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO scheduled_messages(kind, date, post_at, channel, slack_id, text, status) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(kind, date) DO UPDATE SET post_at=excluded.post_at,"
            " channel=excluded.channel, slack_id=excluded.slack_id, text=excluded.text,"
            " status=excluded.status, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
            (w.kind, w.date, _iso(w.post_at), channel, slack_id, w.text, status),
        )


def _status(conn: sqlite3.Connection, kind: str, day: str, status: str) -> None:
    with conn:
        conn.execute(
            "UPDATE scheduled_messages SET status=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', "
            "'now') WHERE kind=? AND date=?",
            (status, kind, day),
        )


def _delete(client: ScheduleClient, row: dict[str, Any]) -> bool:
    try:
        client.chat_deleteScheduledMessage(
            channel=row["channel"], scheduled_message_id=row["slack_id"]
        )
    except Exception:
        log.warning("couldn't delete scheduled %s for %s", row["kind"], row["date"])
        return False
    return True


def reconcile(conn: sqlite3.Connection, client: ScheduleClient, channel: str, now: datetime) -> int:
    """Queue missing fallbacks, replace changed ones, delete ones the plan no longer has.
    Returns the number of Slack calls made."""
    want, have, calls = wanted(conn, now), _rows(conn), 0
    for key, row in have.items():
        post_at = datetime.fromisoformat(row["post_at"].replace("Z", "+00:00"))
        if row["status"] == "scheduled" and post_at <= now:
            _status(conn, *key, "sent")
        elif row["status"] == "scheduled" and post_at > now + LEAD:
            w = want.get(key)
            if w is None or w.text != row["text"] or _iso(w.post_at) != row["post_at"]:
                calls += 1
                if _delete(client, row):
                    _status(conn, *key, "cancelled")
    have = _rows(conn)
    for key, w in want.items():
        existing = have.get(key)
        if existing is not None and existing["status"] in ("scheduled", "sent", "cancelled_live"):
            continue
        calls += 1
        try:
            resp = client.chat_scheduleMessage(
                channel=channel, post_at=int(w.post_at.timestamp()), text=w.text
            )
        except Exception:
            log.warning("couldn't schedule %s for %s", w.kind, w.date)
            continue
        _set(conn, w, channel, str(resp["scheduled_message_id"]), "scheduled")
    return calls


def cancel_for_live(conn: sqlite3.Connection, client: ScheduleClient, now: datetime) -> list[str]:
    """Awake shortly before a live-replaceable fallback posts: cancel it, so the live version is
    the only one. Returns the kinds cancelled."""
    done = []
    for (kind, day), row in _rows(conn).items():
        if kind not in LIVE_REPLACEABLE or row["status"] != "scheduled":
            continue
        post_at = datetime.fromisoformat(row["post_at"].replace("Z", "+00:00"))
        if now < post_at <= now + LEAD and _delete(client, row):
            _status(conn, kind, day, "cancelled_live")
            done.append(kind)
    return done


def queued(conn: sqlite3.Connection, kind: str, day: str) -> bool:
    """True if Slack has (or will) post this fallback, so the live check shouldn't send too."""
    row = conn.execute(
        "SELECT status FROM scheduled_messages WHERE kind=? AND date=?", (kind, day)
    ).fetchone()
    return row is not None and row[0] in ("scheduled", "sent")
