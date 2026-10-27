"""Slack channel for the coach: DMs in, coach answers out, over Socket Mode (no public URL).

- One runner for now: only `SLACK_ALLOWED_USER_ID` gets answers; anyone else gets a short note.
- Setup mode (no `SLACK_ALLOWED_USER_ID` yet): the bot only tells each sender their own member ID,
  so the runner can find it without the Slack profile page. The coach never runs in this mode.
- Each DM keeps a short text-only history (last `MAX_TURNS` exchanges, reset after `IDLE_RESET`
  of quiet), so follow-ups work without resending tool output.
- One message at a time per DM; a placeholder reply is posted, then edited with the answer.
- Long-run debrief: on days the accepted plan has a long run, the bot DMs a check-in at
  `PING_HOUR` athlete-local time (or later that day if it was off, until `LAST_PING_HOUR`), at
  most once a day (`notifications` table). The coach gathers the details and calls
  `propose_fuel_entry`, which only validates. The entry is saved here, in code, only when the
  runner replies yes; "no" drops it. The AI never writes the log itself.
- Weekly check-in: Sundays at `WEEKLY_HOUR` athlete-local (late until `WEEKLY_LAST_HOUR`), when
  the week ending or the next one is in the plan. The coach writes a recap of the week and a
  preview of the next one; her reply continues the conversation.
- Asleep-proof: fixed-time messages are also queued on Slack's servers (`slack_schedule`), so
  they arrive while the laptop sleeps; the live versions replace them when the bot is awake.
  On start, after a sleep, and every `CATCH_UP_EVERY`, the bot answers DMs it missed (last
  `CATCH_UP_WINDOW`) in one reply. A stored cursor of the newest handled message means Slack
  retries and catch-up never answer the same message twice. Debrief context comes from the DB
  (plan, ping record, fuel log), so it survives restarts.
- Tokens come from the environment (`.env`); this module never reads, prints or logs them.
- No Strava data: the coach's tools don't have any.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from mara_trainer import db, fueling, reports

from . import slack_schedule, tools
from .agent import Coach, CoachError
from .slack_schedule import (
    DEBRIEF_KIND,
    PING_HOUR,
    RUN_KINDS,
    SYNC_HOUR,
    SYNC_KIND,
    WEEKLY_HOUR,
    WEEKLY_KIND,
    ping_text,
)

log = logging.getLogger(__name__)

MAX_TURNS = 10
IDLE_RESET = 6 * 60 * 60  # seconds
THINKING = "On it, checking your plan and sources..."
NOT_ALLOWED = "Sorry, this coach is private for now."
FAILED = "Sorry, I couldn't answer that just now. Try again in a minute."
REQUIRED_ENV = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_ALLOWED_USER_ID", "ANTHROPIC_API_KEY")
SETUP_ENV = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")

LAST_PING_HOUR = 20  # don't send a late check-in after this
DEBRIEF_TTL = 36 * 60 * 60  # seconds a check-in stays open
CHECK_EVERY = 60  # seconds between schedule checks
WEEKLY_LAST_HOUR = 22
WEEKLY_FAILED = (
    'I couldn\'t put together your weekly check-in just now. Message me "weekly check-in" '
    "anytime and I'll try again."
)

DEBRIEF_CLOSED = "long_run_debrief_closed"  # she said no to saving: don't reopen that day
CURSOR_KEY = "last_handled_ts"  # newest DM handled, so retries and catch-up never answer twice
CATCH_UP_WINDOW = 36 * 60 * 60  # seconds: older missed DMs are stale
CATCH_UP_EVERY = 15 * 60  # seconds between routine catch-up checks
WOKE_GAP = 3 * 60  # seconds: a loop gap this long means the laptop slept
OFFLINE_NOTE = "(Sent while I was offline, oldest first:)"


def sync_nudge_text(imported: int, planned: int) -> str:
    return (
        f"Your runs this week: {imported} of {planned} planned are imported. Before your 6pm "
        'check-in, say "sync my runs" in Claude Code so the recap has your data.'
    )


COROS_NUDGE = '\n\n_Want this week on your watch? In Claude Code, say "push my week to COROS"._'


def weekly_prompt(sunday: str) -> str:
    day = date.fromisoformat(sunday)
    monday = (day - timedelta(days=6)).isoformat()
    next_monday = (day + timedelta(days=1)).isoformat()
    return f"""(Weekly check-in, Sunday {sunday}. The week ending today ran {monday} to {sunday}; \
the next week starts {next_monday}.)

Write my Sunday check-in as one Slack message:
1. Last week: compare plan_week for {monday} with summary for week {monday} and fuel_log \
since {monday}. If no runs were imported, say the run data isn't in yet (null is no data, not \
zero) and recap from the fuel log and what I told you.
2. This week: plan_week for {next_monday}. List each run (day, miles, effort) and the strength \
days, then the week's focus for its phase (for example easy volume, the workout, hills, speed, \
long-run fueling practice). Take that focus only from approved read_knowledge sections and say \
if none covers something.
3. End with one question: anything coming up this week (travel, soreness, busy days)?
Keep it brief and cite sources at the end."""


YES = re.compile(r"^\s*(y|yes|yep|yeah|yup|sure|ok|okay|save|save it|do it)\b[\s.!]*$", re.I)
NO = re.compile(r"^\s*(n|no|nope|cancel|don'?t save|do not save)\b[\s.!]*$", re.I)
CANCELLED = "Okay, not saved."
SAVE_FOLLOW_UP = (
    "(Saved.) Give me your short coach reply: one or two observations from my fuel log, and "
    "one small thing to try next long run if it's worth changing anything."
)


def setup_reply(user: str) -> str:
    return (
        f"Setup mode. Your Slack member ID is `{user}`. Add `SLACK_ALLOWED_USER_ID={user}` to "
        "`.env`, then restart `coach slack run`."
    )


def handle_setup(event: dict[str, Any], client: SlackClient) -> None:
    """Setup mode: reply to a DM with the sender's own member ID, nothing else."""
    if event.get("channel_type") != "im" or event.get("subtype") or event.get("bot_id"):
        return
    channel, user = event.get("channel"), event.get("user")
    if channel and user:
        client.chat_postMessage(channel=channel, text=setup_reply(user))


class SlackClient(Protocol):
    def chat_postMessage(self, **kwargs: Any) -> Any: ...  # noqa: N802 (Slack SDK name)
    def chat_update(self, **kwargs: Any) -> Any: ...
    def conversations_open(self, **kwargs: Any) -> Any: ...
    def conversations_history(self, **kwargs: Any) -> Any: ...
    def chat_scheduleMessage(self, **kwargs: Any) -> Any: ...  # noqa: N802
    def chat_deleteScheduledMessage(self, **kwargs: Any) -> Any: ...  # noqa: N802


class Asker(Protocol):
    def ask(self, question: str, history: list[dict[str, Any]] | None = None) -> Any: ...


def to_mrkdwn(text: str) -> str:
    """Markdown from the coach -> Slack mrkdwn: bold, headings, links, bullets."""
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    return re.sub(r"^(\s*)[-*] ", r"\1• ", text, flags=re.MULTILINE)


@dataclass
class Debrief:
    date: str
    planned_miles: float
    expires: float


@dataclass
class Conversation:
    turns: list[dict[str, Any]] = field(default_factory=list)
    last_seen: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)
    debrief: Debrief | None = None
    pending: fueling.FuelEntry | None = None  # proposed by the coach, saved only on "yes"


def _get(block: Any, key: str) -> Any:
    return block.get(key) if isinstance(block, dict) else getattr(block, key, None)


def proposed_entry(messages: list[dict[str, Any]]) -> fueling.FuelEntry | None:
    """The last valid `propose_fuel_entry` call in these messages, re-checked in code."""
    for message in reversed(messages):
        if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
            continue
        for block in reversed(message["content"]):
            if _get(block, "type") != "tool_use" or _get(block, "name") != "propose_fuel_entry":
                continue
            args = _get(block, "input")
            if not isinstance(args, dict) or tools.check_args(_PROPOSE_SCHEMA, args):
                return None
            try:
                entry = tools.build_fuel_entry(args)
            except (tools.ToolError, TypeError, ValueError):
                return None
            return None if entry.problems() else entry
    return None


_PROPOSE_SCHEMA = next(t.input_schema for t in tools.TOOLS if t.name == "propose_fuel_entry")


class SlackCoach:
    def __init__(
        self,
        allowed_user: str,
        coach_factory: Callable[[sqlite3.Connection], Asker] | None = None,
        connect: Callable[[], sqlite3.Connection] | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.allowed_user = allowed_user
        self.coach_factory = coach_factory or (lambda conn: Coach(conn, channel="slack"))
        self.connect = connect or _connect
        self.clock = clock
        self.now = now
        self.conversations: dict[str, Conversation] = {}
        self._guard = threading.Lock()

    def _conversation(self, channel: str) -> Conversation:
        with self._guard:
            convo = self.conversations.setdefault(channel, Conversation())
        now = self.clock()
        if (convo.turns or convo.pending) and now - convo.last_seen > IDLE_RESET:
            convo.turns, convo.pending = [], None
        if convo.debrief and now > convo.debrief.expires:
            convo.debrief = None
        convo.last_seen = now
        return convo

    def handle(self, event: dict[str, Any], client: SlackClient) -> None:
        """A `message` event. Answers direct messages from the allowed user only."""
        if event.get("channel_type") != "im" or event.get("subtype") or event.get("bot_id"):
            return
        channel, text = event.get("channel"), (event.get("text") or "").strip()
        if not channel or not text:
            return
        if event.get("user") != self.allowed_user:
            client.chat_postMessage(channel=channel, text=NOT_ALLOWED)
            return
        convo = self._conversation(channel)
        with convo.lock:
            if not self._claim(event.get("ts")):
                return  # already handled (a Slack retry, or answered by catch-up)
            if convo.debrief is None:
                convo.debrief = self._open_debrief()
            if convo.pending is not None and YES.match(text):
                self._save(channel, convo, client)
                return
            if convo.pending is not None and NO.match(text):
                if convo.debrief is not None:
                    self._mark(DEBRIEF_CLOSED, convo.debrief.date)
                convo.pending, convo.debrief = None, None
                client.chat_postMessage(channel=channel, text=CANCELLED)
                return
            question = text
            if convo.debrief:
                question = (
                    f"(Long-run debrief for {convo.debrief.date}: "
                    f"{convo.debrief.planned_miles:g} mi long run on the plan.)\n\n{text}"
                )
            self._reply(channel, convo, client, question)

    def _reply(self, channel: str, convo: Conversation, client: SlackClient, question: str) -> None:
        placeholder = client.chat_postMessage(channel=channel, text=THINKING)
        answer = self._answer(question, convo)
        client.chat_update(channel=channel, ts=placeholder["ts"], text=to_mrkdwn(answer))

    def _save(self, channel: str, convo: Conversation, client: SlackClient) -> None:
        assert convo.pending is not None
        conn = self.connect()
        try:
            saved = fueling.add_entry(conn, convo.pending)
        except fueling.FuelError as exc:
            convo.pending = None
            client.chat_postMessage(
                channel=channel, text="Couldn't save that: " + "; ".join(exc.problems)
            )
            return
        finally:
            conn.close()
        convo.pending, convo.debrief = None, None
        cph = saved["carbs_per_hour"]
        client.chat_postMessage(
            channel=channel,
            text=f"Saved to your fuel log (entry {saved['id']})"
            + (f", {cph:g} g carbs per hour." if cph is not None else "."),
        )
        self._reply(channel, convo, client, SAVE_FOLLOW_UP)

    def maybe_ping(self, client: SlackClient) -> bool:
        """Send today's long-run check-in if it's due. True if one was sent."""
        conn = self.connect()
        try:
            local = self.now().astimezone(ZoneInfo(db.athlete_timezone(conn)))
            if not PING_HOUR <= local.hour < LAST_PING_HOUR:
                return False
            day = local.date().isoformat()
            session = reports.planned_session(db.load_accepted_plan(conn), day)
            if not session or session["kind"] != "long":
                return False
            sent = conn.execute(
                "SELECT 1 FROM notifications WHERE kind = ? AND date = ?", (DEBRIEF_KIND, day)
            ).fetchone()
            if sent or slack_schedule.queued(conn, DEBRIEF_KIND, day):
                return False  # already pinged live, or Slack has it queued
            miles = round(session["distance_meters"] / tools.MI_METERS, 1)
            channel = client.conversations_open(users=self.allowed_user)["channel"]["id"]
            convo = self._conversation(channel)
            with convo.lock:
                client.chat_postMessage(channel=channel, text=ping_text(miles))
                reports.mark_notified(conn, DEBRIEF_KIND, day)
                convo.turns, convo.pending = [], None
                convo.debrief = Debrief(day, miles, self.clock() + DEBRIEF_TTL)
            return True
        finally:
            conn.close()

    def maybe_sync_nudge(self, client: SlackClient) -> bool:
        """Sunday 5pm: if fewer runs are imported this week than the plan had so far, ask her to
        sync before the check-in. Plain text, no API call. True if sent."""
        conn = self.connect()
        try:
            now = self.now()
            local = now.astimezone(ZoneInfo(db.athlete_timezone(conn)))
            if local.weekday() != 6 or local.hour != SYNC_HOUR:
                return False
            sunday = local.date().isoformat()
            week = reports.plan_week(db.load_accepted_plan(conn), sunday)
            if week is None:
                return False
            planned = sum(
                1 for s in week["sessions"] if s["kind"] in RUN_KINDS and s["date"] <= sunday
            )
            imported = reports.summary(conn, week=sunday, now=now)["summary"]["run_count"]
            if slack_schedule.queued(conn, SYNC_KIND, sunday):
                return False  # the fallback couldn't be cancelled; Slack posts it
            if imported >= planned or not reports.mark_notified(conn, SYNC_KIND, sunday):
                return False
        finally:
            conn.close()
        channel = client.conversations_open(users=self.allowed_user)["channel"]["id"]
        client.chat_postMessage(channel=channel, text=sync_nudge_text(imported, planned))
        return True

    def maybe_weekly(self, client: SlackClient) -> bool:
        """Send the Sunday check-in if it's due. True if one was sent (or a failure note)."""
        conn = self.connect()
        try:
            local = self.now().astimezone(ZoneInfo(db.athlete_timezone(conn)))
            if local.weekday() != 6 or not WEEKLY_HOUR <= local.hour < WEEKLY_LAST_HOUR:
                return False
            sunday = local.date()
            plan = db.load_accepted_plan(conn)
            next_monday = (sunday + timedelta(days=1)).isoformat()
            coming = reports.plan_week(plan, next_monday)
            if not (reports.plan_week(plan, sunday.isoformat()) or coming):
                return False
            if not reports.mark_notified(conn, WEEKLY_KIND, sunday.isoformat()):
                return False  # marked before the coach runs, so a failure never retries in a loop
        finally:
            conn.close()
        channel = client.conversations_open(users=self.allowed_user)["channel"]["id"]
        convo = self._conversation(channel)
        with convo.lock:
            convo.turns, convo.pending = [], None
            answer = self._answer(weekly_prompt(sunday.isoformat()), convo)
            text = WEEKLY_FAILED if answer == FAILED else to_mrkdwn(answer)
            if answer != FAILED and coming:
                text += COROS_NUDGE
            client.chat_postMessage(channel=channel, text=text)
        return True

    def maybe_schedule(self, client: SlackClient) -> int:
        """Keep Slack's queue of fallback messages in step with the plan; cancel the ones about
        to be replaced by a live message. Slack calls happen only when something changed."""
        conn = self.connect()
        try:
            now = self.now()
            slack_schedule.cancel_for_live(conn, client, now)
            channel = self._channel(conn, client)
            return slack_schedule.reconcile(conn, client, channel, now)
        finally:
            conn.close()

    def catch_up(self, client: SlackClient) -> int:
        """Answer DMs that arrived while the bot was down (last CATCH_UP_WINDOW), in one reply.
        The first run only records where history ends. Returns how many messages it answered."""
        conn = self.connect()
        try:
            channel = self._channel(conn, client)
            cursor = _state(conn, CURSOR_KEY)
        finally:
            conn.close()
        oldest = self.now().timestamp() - CATCH_UP_WINDOW
        if cursor is not None:
            oldest = max(oldest, float(cursor))
        history = client.conversations_history(channel=channel, oldest=f"{oldest:.6f}", limit=50)
        mine = [
            m
            for m in history.get("messages", [])
            if m.get("user") == self.allowed_user
            and not m.get("subtype")
            and not m.get("bot_id")
            and (m.get("text") or "").strip()
            and (cursor is None or float(m["ts"]) > float(cursor))
        ]
        mine.sort(key=lambda m: float(m["ts"]))
        if cursor is None:  # first run: start from here, don't answer old history
            latest = max([self.now().timestamp()] + [float(m["ts"]) for m in mine])
            self._claim(f"{latest:.6f}")
            return 0
        if not mine:
            return 0
        text = mine[0]["text"].strip()
        if len(mine) > 1:
            text = OFFLINE_NOTE + "\n" + "\n".join("- " + m["text"].strip() for m in mine)
        event = {
            "channel_type": "im",
            "channel": channel,
            "user": self.allowed_user,
            "text": text,
            "ts": mine[-1]["ts"],
        }
        self.handle(event, client)
        return len(mine)

    def _channel(self, conn: sqlite3.Connection, client: SlackClient) -> str:
        channel = _state(conn, "dm_channel")
        if channel is None:
            channel = str(client.conversations_open(users=self.allowed_user)["channel"]["id"])
            _set_state(conn, "dm_channel", channel)
        return channel

    def _claim(self, ts: Any) -> bool:
        """Record `ts` as handled; False if it (or something newer) already was."""
        if not ts:
            return True
        conn = self.connect()
        try:
            cursor = _state(conn, CURSOR_KEY)
            if cursor is not None and float(ts) <= float(cursor):
                return False
            _set_state(conn, CURSOR_KEY, str(ts))
            return True
        finally:
            conn.close()

    def _mark(self, kind: str, day: str) -> None:
        conn = self.connect()
        try:
            reports.mark_notified(conn, kind, day)
        finally:
            conn.close()

    def _open_debrief(self) -> Debrief | None:
        """Today's or yesterday's long-run debrief, if its ping went out (live or by Slack), it's
        within DEBRIEF_TTL, no fuel entry exists for that day, and she didn't decline saving."""
        conn = self.connect()
        try:
            try:
                tz = ZoneInfo(db.athlete_timezone(conn))
            except LookupError:
                return None  # no athlete set up yet: plain chat, no debrief context
            now = self.now()
            plan = db.load_accepted_plan(conn)
            today = now.astimezone(tz).date()
            for day in (today, today - timedelta(days=1)):
                iso = day.isoformat()
                session = reports.planned_session(plan, iso)
                if not session or session["kind"] != "long":
                    continue
                pinged_at = datetime.combine(day, datetime.min.time().replace(hour=PING_HOUR), tz)
                if not pinged_at <= now < pinged_at + timedelta(seconds=DEBRIEF_TTL):
                    continue
                sent = conn.execute(
                    "SELECT 1 FROM notifications WHERE kind = ? AND date = ?", (DEBRIEF_KIND, iso)
                ).fetchone() or slack_schedule.queued(conn, DEBRIEF_KIND, iso)
                closed = conn.execute(
                    "SELECT 1 FROM notifications WHERE kind = ? AND date = ?", (DEBRIEF_CLOSED, iso)
                ).fetchone()
                logged = conn.execute("SELECT 1 FROM fuel_log WHERE date = ?", (iso,)).fetchone()
                if sent and not closed and not logged:
                    miles = round(session["distance_meters"] / tools.MI_METERS, 1)
                    return Debrief(iso, miles, self.clock() + DEBRIEF_TTL)
            return None
        finally:
            conn.close()

    def _answer(self, text: str, convo: Conversation) -> str:
        conn = self.connect()
        history = list(convo.turns)
        try:
            reply = self.coach_factory(conn).ask(text, history=history)
        except CoachError:
            log.warning("coach could not finish a Slack reply")
            return FAILED
        except Exception:  # API/network errors: keep the bot up, say so in Slack
            log.exception("coach failed on a Slack message")
            return FAILED
        finally:
            conn.close()
        answer = reply.text or FAILED
        entry = proposed_entry(list(getattr(reply, "messages", []))[len(history) :])
        if entry is not None:
            convo.pending = entry
        convo.turns += [
            {"role": "user", "content": text},
            {"role": "assistant", "content": answer},
        ]
        convo.turns = convo.turns[-2 * MAX_TURNS :]
        return answer


def _state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM slack_state WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO slack_state(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _connect() -> sqlite3.Connection:
    conn = db.connect()
    db.migrate(conn)
    return conn


def missing_env(env: dict[str, str], names: tuple[str, ...] = REQUIRED_ENV) -> list[str]:
    """Names (never values) of required settings that aren't set."""
    return [name for name in names if not env.get(name, "").strip()]


def setup_mode(env: dict[str, str]) -> bool:
    return not env.get("SLACK_ALLOWED_USER_ID", "").strip()


def run(env: dict[str, str]) -> None:  # pragma: no cover - needs real Slack tokens
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    app = App(token=env["SLACK_BOT_TOKEN"])
    if setup_mode(env):

        @app.event("message")
        def on_setup_message(event: dict[str, Any], client: Any) -> None:
            handle_setup(event, client)

    else:
        bot = SlackCoach(env["SLACK_ALLOWED_USER_ID"].strip())

        @app.event("message")
        def on_message(event: dict[str, Any], client: Any) -> None:
            bot.handle(event, client)

        def check_in_loop() -> None:
            client = cast(SlackClient, app.client)
            checks = (bot.maybe_schedule, bot.maybe_ping, bot.maybe_sync_nudge, bot.maybe_weekly)
            last_tick, last_catch_up = 0.0, 0.0
            while True:
                tick = time.time()  # wall clock: jumps forward when the laptop slept
                if tick - last_tick > WOKE_GAP or tick - last_catch_up > CATCH_UP_EVERY:
                    try:
                        bot.catch_up(client)
                    except Exception:
                        log.exception("catch-up failed")
                    last_catch_up = tick
                last_tick = tick
                for check in checks:
                    try:
                        check(client)
                    except Exception:
                        log.exception("scheduled check-in failed")
                time.sleep(CHECK_EVERY)

        threading.Thread(target=check_in_loop, name="long-run-check-in", daemon=True).start()

    SocketModeHandler(app, env["SLACK_APP_TOKEN"]).start()  # type: ignore[no-untyped-call]
