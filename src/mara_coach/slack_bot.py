"""Slack channel for the coach: DMs in, coach answers out, over Socket Mode (no public URL).

- One runner for now: only `SLACK_ALLOWED_USER_ID` gets answers; anyone else gets a short note.
- Setup mode (no `SLACK_ALLOWED_USER_ID` yet): the bot only tells each sender their own member ID,
  so the runner can find it without the Slack profile page. The coach never runs in this mode.
- Each DM keeps a short text-only history (last `MAX_TURNS` exchanges, reset after `IDLE_RESET`
  of quiet), so follow-ups work without resending tool output.
- One message at a time per DM; a placeholder reply is posted, then the answer comes as a new
  message (Slack doesn't notify on edits) and the placeholder is deleted.
- Long-run debrief: on days the accepted plan has a long run, the bot DMs a check-in at
  `PING_HOUR` athlete-local time (or later that day if it was off, until `LAST_PING_HOUR`), at
  most once a day (`notifications` table). The coach gathers the details and calls
  `propose_fuel_entry`, which only validates. The entry is saved here, in code, only when the
  runner replies yes; "no" drops it. The AI never writes the log itself.
- Sunday is one conversation: at `WEEKLY_HOUR` athlete-local (late until `WEEKLY_LAST_HOUR`),
  when the week ending or the next one is in the plan, the bot first syncs the week's runs from
  COROS in code (no separate reminder), then the coach writes a recap of the week and a preview
  of the next one, ending with one question. Her reply (through Monday, `WEEKLY_KEY`) is tagged
  as a check-in answer, so the coach writes the week with `propose_week`; the bot saves it in
  code on yes.
- Asleep-proof: fixed-time messages are also queued on Slack's servers (`slack_schedule`), so
  they arrive while the laptop sleeps; the live versions replace them when the bot is awake.
  On start, after a sleep, and every `CATCH_UP_EVERY`, the bot answers DMs it missed (last
  `CATCH_UP_WINDOW`) in one reply. A stored cursor of the newest handled message means Slack
  retries and catch-up never answer the same message twice. Debrief context comes from the DB
  (plan, ping record, fuel log), so it survives restarts.
- COROS sync: "sync my runs" (or just "sync") pulls this week's runs in code with `coros`, no
  coach call. Possible duplicates are imported only if she replies yes.
- Restart-proof replies: a reply in progress is recorded in `slack_state` (`INFLIGHT_KEY`) until
  it's answered. On start, `resume` finishes it and removes its stuck placeholder (at most
  `MAX_TRIES` attempts, so a reply that crashes the bot can't loop), or says it was lost if it's
  older than `CATCH_UP_WINDOW`. The DM's text history and any yes/no question still waiting
  (`pending`) are saved too (`TURNS_KEY`), so a restart within `IDLE_RESET` keeps the
  conversation and her "yes" still saves.
- Socket watchdog: the Slack client can retry broken sessions forever while the process looks
  healthy, so after `STUCK_AFTER` of repeating socket errors the bot exits and launchd restarts it.
- Tokens come from the environment (`.env`); this module never reads, prints or logs them.
- No Strava data: the coach's tools don't have any.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from mara_trainer import db, fueling, reports

from . import coros, slack_schedule, tools
from .agent import Coach, CoachError
from .slack_schedule import (
    DEBRIEF_KIND,
    PING_HOUR,
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
INFLIGHT_KEY = "inflight_reply"  # the reply being written, so a restart can finish it
TURNS_KEY = "dm_turns"  # text history, so a restart keeps the conversation
MAX_TRIES = 2
LOST = "Sorry, I lost this one when I restarted. Send it again?"
ANSWERED_BELOW = "(answered below)"
CURSOR_KEY = "last_handled_ts"  # newest DM handled, so retries and catch-up never answer twice
CATCH_UP_WINDOW = 36 * 60 * 60  # seconds: older missed DMs are stale
CATCH_UP_EVERY = 15 * 60  # seconds between routine catch-up checks
WOKE_GAP = 3 * 60  # seconds: a loop gap this long means the laptop slept
OFFLINE_NOTE = "(Sent while I was offline, oldest first:)"
STUCK_AFTER = 5 * 60  # seconds of repeating socket errors before the bot exits to restart
QUIET_RESET = 10 * 60  # seconds without a socket error that end a run of them


@dataclass
class SocketWatch:
    """Spots the Socket Mode client looping on broken sessions, which it can do for days."""

    first: float | None = None
    last: float | None = None

    def stuck(self, now: float) -> bool:
        """Record one socket error; True once errors have kept coming for `STUCK_AFTER`."""
        if self.first is None or self.last is None or now - self.last > QUIET_RESET:
            self.first = now
        self.last = now
        return now - self.first >= STUCK_AFTER


def exit_when_stuck(
    watch: SocketWatch, clock: Callable[[], float], leave: Callable[[int], None]
) -> Callable[[Exception], None]:
    """Socket error listener: a stuck client leaves, and launchd starts a fresh process."""

    def on_error(error: Exception) -> None:
        if watch.stuck(clock()):
            log.error("Socket Mode stuck (%s); exiting so launchd restarts", type(error).__name__)
            leave(1)

    return on_error


PUSH_WEEK = 'Say "push my week" in Claude Code to put it on your watch.'
WATCH_STALE = (
    "If this week is already on your watch, change or remove those workouts in the COROS app; "
    "I can't move them."
)


WEEKLY_KEY = "weekly_reply"  # Monday of the week her check-in reply should write
SYNC_DUPLICATES = (
    '_Some runs look like ones already imported. Reply "sync my runs" to review them._'
)
SYNC_LEFT = '_{n} run(s) didn\'t download from COROS yet. Reply "sync my runs" later._'
SYNC_FAILED = "Couldn't sync from COROS just now, so the recap may be missing runs."


def weekly_prompt(sunday: str, synced: str, missed: Debrief | None = None) -> str:
    day = date.fromisoformat(sunday)
    monday = (day - timedelta(days=6)).isoformat()
    next_monday = (day + timedelta(days=1)).isoformat()
    return f"""(Weekly check-in, Sunday {sunday}. The week ending today ran {monday} to {sunday}; \
the next week starts {next_monday}. {synced})

Write my Sunday check-in as one Slack message:
1. Last week: compare plan_week for {monday} with summary for week {monday} and fuel_log \
since {monday}. If no runs were imported, say the run data isn't in yet (null is no data, not \
zero) and recap from the fuel log and what I told you.
2. This week: plan_week for {next_monday}. List each run (day, miles, effort) and the strength \
days, then the week's focus for its phase (for example easy volume, the workout, hills, speed, \
long-run fueling practice). Take that focus only from approved read_knowledge sections and say \
if none covers something.
3. End with one question in two parts: how did last week feel (energy, legs, sleep, anything \
off)? And anything coming up this week (travel, soreness, busy days)? Say you'll write the \
week's exact sessions from her answer. Don't mention syncing.{_missed_line(missed)}
Keep it brief and cite sources at the end."""


def _missed_line(missed: Debrief | None) -> str:
    if missed is None:
        return ""
    return (
        f"\nMy {missed.planned_miles:g} mi long run on {missed.date} isn't debriefed yet: in the "
        "same question, also ask how it went (distance and time, effort 1-10, what I ate before "
        "and how long before, what I took during, stomach and energy)."
    )


SYNC = re.compile(r"^\s*(please\s+)?sync(\s+my)?(\s+runs)?(\s+from\s+coros)?\b[\s.!]*$", re.I)
SYNCING = "Syncing your runs from COROS..."
SKIPPED = "Okay, skipped those."
YES = re.compile(r"^\s*(y|yes|yep|yeah|yup|sure|ok|okay|save|save it|do it)\b[\s.!]*$", re.I)
NO = re.compile(r"^\s*(n|no|nope|cancel|don'?t save|do not save)\b[\s.!]*$", re.I)
CANCELLED = "Okay, not saved."
SAVE_FOLLOW_UP = (
    "(Saved.) Give me your short coach reply: one or two observations from my fuel log, and "
    "one small thing to try next long run if it's worth changing anything."
)


def weekly_reply(week_start: str, text: str) -> str:
    """Her answer to the Sunday check-in, tagged so the coach writes the week as a proposal."""
    return (
        f"(My reply to your Sunday check-in. Write the week of {week_start} from it and call "
        "propose_week, unless I'm asking about something else. If I'm also debriefing a long "
        "run, propose the fuel entry first; the week comes after I say yes to it.)\n\n" + text
    )


def week_after_debrief(week_start: str) -> str:
    """After a debrief saved from the Sunday check-in, move on to writing the week."""
    return (
        "(Saved.) Give me your short coach reply on the long run in a line or two, then write "
        f"the week of {week_start} from what I told you and call propose_week."
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
    def chat_delete(self, **kwargs: Any) -> Any: ...
    def conversations_open(self, **kwargs: Any) -> Any: ...
    def conversations_history(self, **kwargs: Any) -> Any: ...
    def chat_scheduleMessage(self, **kwargs: Any) -> Any: ...  # noqa: N802
    def chat_deleteScheduledMessage(self, **kwargs: Any) -> Any: ...  # noqa: N802


class Asker(Protocol):
    def ask(self, question: str, history: list[dict[str, Any]] | None = None) -> Any: ...


def _finish(client: SlackClient, channel: str, placeholder: str, text: str) -> None:
    """Send the answer as a new message, so Slack notifies her (edits don't), then remove the
    placeholder. If Slack won't delete it, point it at the answer instead."""
    client.chat_postMessage(channel=channel, text=text)
    try:
        client.chat_delete(channel=channel, ts=placeholder)
    except Exception:
        log.warning("couldn't delete a placeholder")
        with suppress(Exception):
            client.chat_update(channel=channel, ts=placeholder, text=ANSWERED_BELOW)


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


@dataclass(frozen=True)
class WeekProposal:
    start_date: str
    sessions: list[dict[str, Any]]
    cut: bool = False
    focus: str | None = None


@dataclass(frozen=True)
class DuplicateRuns:
    items: tuple[coros.Pending, ...]


Pending = fueling.FuelEntry | WeekProposal | DuplicateRuns | None


def encode_pending(pending: Pending) -> dict[str, Any] | None:
    """A waiting yes/no proposal as JSON, so it survives a restart."""
    if isinstance(pending, fueling.FuelEntry):
        return {"type": "fuel", **asdict(pending)}
    if isinstance(pending, WeekProposal):
        return {"type": "week", **asdict(pending)}
    if isinstance(pending, DuplicateRuns):
        items = [{"path": str(i.path), "keys": i.keys, "runs": i.runs} for i in pending.items]
        return {"type": "duplicates", "items": items}
    return None


def decode_pending(raw: dict[str, Any] | None) -> Pending:
    """The reverse of `encode_pending`; anything unreadable is dropped, never guessed."""
    if not raw:
        return None
    data = {k: v for k, v in raw.items() if k != "type"}
    try:
        if raw.get("type") == "fuel":
            entry = fueling.FuelEntry(**data)
            return None if entry.problems() else entry
        if raw.get("type") == "week":
            return WeekProposal(**data)
        if raw.get("type") == "duplicates":
            return DuplicateRuns(
                tuple(
                    coros.Pending(Path(i["path"]), tuple(i["keys"]), tuple(i["runs"]))
                    for i in data["items"]
                )
            )
    except (TypeError, KeyError):
        log.warning("dropped an unreadable saved proposal")
    return None


@dataclass
class Conversation:
    turns: list[dict[str, Any]] = field(default_factory=list)
    last_seen: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)
    debrief: Debrief | None = None
    # proposed by the coach, saved only on "yes"
    pending: Pending = None
    channel: str = ""


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


def proposed_week(messages: list[dict[str, Any]]) -> WeekProposal | None:
    """The last well-formed `propose_week` call in these messages. Its rules run in the bot."""
    for message in reversed(messages):
        if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
            continue
        for block in reversed(message["content"]):
            if _get(block, "type") != "tool_use" or _get(block, "name") != "propose_week":
                continue
            args = _get(block, "input")
            if not isinstance(args, dict) or tools.check_args(_WEEK_SCHEMA, args):
                return None
            return WeekProposal(
                args["start_date"], args["sessions"], args.get("cut", False), args.get("focus")
            )
    return None


_PROPOSE_SCHEMA = next(t.input_schema for t in tools.TOOLS if t.name == "propose_fuel_entry")
_WEEK_SCHEMA = next(t.input_schema for t in tools.TOOLS if t.name == "propose_week")


class SlackCoach:
    def __init__(
        self,
        allowed_user: str,
        coach_factory: Callable[[sqlite3.Connection], Asker] | None = None,
        connect: Callable[[], sqlite3.Connection] | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sync: Callable[[sqlite3.Connection, datetime], coros.SyncReport] = coros.sync_week,
    ) -> None:
        self.allowed_user = allowed_user
        self.coach_factory = coach_factory or (lambda conn: Coach(conn, channel="slack"))
        self.connect = connect or _connect
        self.clock = clock
        self.now = now
        self.sync = sync
        self.conversations: dict[str, Conversation] = {}
        self._guard = threading.Lock()

    def _conversation(self, channel: str) -> Conversation:
        with self._guard:
            convo = self.conversations.get(channel)
            if convo is None:
                turns, pending = self._saved_conversation(channel)
                convo = Conversation(turns, self.clock(), pending=pending, channel=channel)
                self.conversations[channel] = convo
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
        with convo.lock, self._saving(convo):
            if not self._claim(event.get("ts")):
                return  # already handled (a Slack retry, or answered by catch-up)
            if SYNC.match(text):
                self._sync(channel, convo, client)
                return
            if convo.debrief is None:
                convo.debrief = self._open_debrief()
            if convo.pending is not None and YES.match(text):
                self._save(channel, convo, client)
                return
            if convo.pending is not None and NO.match(text):
                if isinstance(convo.pending, DuplicateRuns):
                    convo.pending = None
                    client.chat_postMessage(channel=channel, text=SKIPPED)
                    return
                if isinstance(convo.pending, WeekProposal):
                    convo.pending = None
                else:
                    if convo.debrief is not None:
                        self._mark(DEBRIEF_CLOSED, convo.debrief.date)
                    convo.pending, convo.debrief = None, None
                client.chat_postMessage(channel=channel, text=CANCELLED)
                return
            question = text
            week_start, missed = self._open_weekly()
            if week_start:
                question = weekly_reply(week_start, question)
                convo.debrief = convo.debrief or missed
            if convo.debrief:
                question = (
                    f"(Long-run debrief for {convo.debrief.date}: "
                    f"{convo.debrief.planned_miles:g} mi long run on the plan.)\n\n{question}"
                )
            self._reply(channel, convo, client, question)

    def _reply(
        self,
        channel: str,
        convo: Conversation,
        client: SlackClient,
        question: str,
        placeholder: str | None = None,
        tries: int = 1,
    ) -> None:
        if placeholder is None:
            placeholder = client.chat_postMessage(channel=channel, text=THINKING)["ts"]
        self._track("answer", channel, placeholder, tries, question)
        try:
            answer = self._answer(question, convo)
            _finish(client, channel, placeholder, to_mrkdwn(answer))
        finally:
            self._untrack()

    def resume(self, client: SlackClient) -> bool:
        """On start: finish the reply a restart cut off and remove its placeholder. True if so."""
        conn = self.connect()
        try:
            raw = _state(conn, INFLIGHT_KEY)
        finally:
            conn.close()
        if raw is None:
            return False
        job = json.loads(raw)
        channel, placeholder, tries = job["channel"], job["placeholder"], job["tries"] + 1
        convo = self._conversation(channel)
        with convo.lock, self._saving(convo):
            stale = self.now().timestamp() - job["at"] > CATCH_UP_WINDOW
            if stale or tries > MAX_TRIES:
                self._untrack()
                _finish(client, channel, placeholder, LOST)
            elif job["kind"] == "sync":
                self._sync(channel, convo, client, placeholder, tries)
            else:
                self._reply(channel, convo, client, job["question"], placeholder, tries)
        return True

    def _track(self, kind: str, channel: str, placeholder: str, tries: int, question: str) -> None:
        job = {
            "kind": kind,
            "channel": channel,
            "placeholder": placeholder,
            "tries": tries,
            "question": question,
            "at": self.now().timestamp(),
        }
        conn = self.connect()
        try:
            _set_state(conn, INFLIGHT_KEY, json.dumps(job))
        finally:
            conn.close()

    def _untrack(self) -> None:
        conn = self.connect()
        try:
            with conn:
                conn.execute("DELETE FROM slack_state WHERE key = ?", (INFLIGHT_KEY,))
        finally:
            conn.close()

    def _saved_conversation(self, channel: str) -> tuple[list[dict[str, Any]], Pending]:
        conn = self.connect()
        try:
            raw = _state(conn, TURNS_KEY)
        finally:
            conn.close()
        saved = json.loads(raw) if raw else {}
        fresh = self.now().timestamp() - saved.get("at", 0) <= IDLE_RESET
        if saved.get("channel") != channel or not fresh:
            return [], None
        return list(saved["turns"]), decode_pending(saved.get("pending"))

    @contextmanager
    def _saving(self, convo: Conversation) -> Iterator[None]:
        """Save the conversation when the block ends, however it ends."""
        try:
            yield
        finally:
            self._save_turns(convo)

    def _save_turns(self, convo: Conversation) -> None:
        saved = {
            "channel": convo.channel,
            "turns": convo.turns,
            "pending": encode_pending(convo.pending),
            "at": self.now().timestamp(),
        }
        conn = self.connect()
        try:
            _set_state(conn, TURNS_KEY, json.dumps(saved))
        finally:
            conn.close()

    def today(self, conn: sqlite3.Connection) -> str:
        return self.now().astimezone(ZoneInfo(db.athlete_timezone(conn))).date().isoformat()

    def _revised_week(
        self, conn: sqlite3.Connection, week: WeekProposal
    ) -> tuple[dict[str, Any] | None, list[str]]:
        try:
            return reports.revise_week(
                db.load_accepted_plan(conn),
                week.start_date,
                week.sessions,
                self.today(conn),
                db.load_race_setup(conn),
                week.cut,
            )
        except (reports.ReportError, ValueError) as exc:
            return None, [str(exc)]

    def _save_week(
        self, channel: str, convo: Conversation, week: WeekProposal, client: SlackClient
    ) -> None:
        conn = self.connect()
        try:
            revised, problems = self._revised_week(conn, week)
            if revised is not None:
                db.save_plan(conn, revised)
                if _state(conn, WEEKLY_KEY) == week.start_date:
                    _delete_state(conn, WEEKLY_KEY)  # the week is written: replies are chat again
            today = self.today(conn)
        finally:
            conn.close()
        convo.pending = None
        saved = reports.plan_week(revised, week.start_date)
        if saved is None:
            client.chat_postMessage(
                channel=channel, text="Couldn't save that week: " + "; ".join(problems)
            )
            return
        note = WATCH_STALE if week.start_date <= today else PUSH_WEEK
        client.chat_postMessage(
            channel=channel,
            text=f"Saved Week {saved['week']}. {note}\n\n{reports.week_card(saved, week.focus)}",
        )

    def _sync(
        self,
        channel: str,
        convo: Conversation,
        client: SlackClient,
        placeholder: str | None = None,
        tries: int = 1,
    ) -> None:
        """Pull this week's COROS runs in code. A possible duplicate waits for her yes."""
        if placeholder is None:
            placeholder = client.chat_postMessage(channel=channel, text=SYNCING)["ts"]
        self._track("sync", channel, placeholder, tries, "")
        try:
            conn = self.connect()
            try:
                report = self.sync(conn, self.now())
                text = coros.report_text(conn, report)
            except coros.CorosError as exc:
                report, text = None, str(exc)
            except Exception:  # network or SDK surprises: keep the bot up
                log.exception("COROS sync failed")
                report = None
                text = "Couldn't sync from COROS just now. Try again in a few minutes."
            finally:
                conn.close()
            if report is not None and report.pending:
                convo.pending = DuplicateRuns(tuple(report.pending))
                text += "\n\n" + coros.duplicate_question(report.pending)
            _finish(client, channel, placeholder, text)
        finally:
            self._untrack()

    def _save_duplicates(
        self, channel: str, convo: Conversation, runs: DuplicateRuns, client: SlackClient
    ) -> None:
        convo.pending = None
        conn = self.connect()
        try:
            count = coros.import_confirmed(conn, runs.items)
        finally:
            conn.close()
        client.chat_postMessage(channel=channel, text=f"Imported {count} more run(s).")

    def _save(self, channel: str, convo: Conversation, client: SlackClient) -> None:
        assert convo.pending is not None
        if isinstance(convo.pending, DuplicateRuns):
            self._save_duplicates(channel, convo, convo.pending, client)
            return
        if isinstance(convo.pending, WeekProposal):
            self._save_week(channel, convo, convo.pending, client)
            return
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
        week_start, _ = self._open_weekly()
        follow_up = week_after_debrief(week_start) if week_start else SAVE_FOLLOW_UP
        self._reply(channel, convo, client, follow_up)

    def maybe_ping(self, client: SlackClient) -> bool:
        """Send today's long-run check-in if it's due. True if one was sent."""
        conn = self.connect()
        try:
            local = self.now().astimezone(ZoneInfo(db.athlete_timezone(conn)))
            if not PING_HOUR <= local.hour < LAST_PING_HOUR:
                return False
            day = local.date().isoformat()
            session = reports.planned_session(db.load_accepted_plan(conn), day)
            if not session or session["kind"] != "long" or slack_schedule.logged(conn, day):
                return False  # no long run, or she already debriefed it
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
                self._save_turns(convo)
                convo.debrief = Debrief(day, miles, self.clock() + DEBRIEF_TTL)
            return True
        finally:
            conn.close()

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
        synced, note = self._weekly_sync()
        missed = self._missed_debrief((sunday - timedelta(days=6)).isoformat())
        channel = client.conversations_open(users=self.allowed_user)["channel"]["id"]
        convo = self._conversation(channel)
        with convo.lock, self._saving(convo):
            convo.turns, convo.pending = [], None
            answer = self._answer(weekly_prompt(sunday.isoformat(), synced, missed), convo)
            text = WEEKLY_FAILED if answer == FAILED else to_mrkdwn(answer)
            if note:
                text += "\n\n" + note
            if answer != FAILED and coming:
                conn = self.connect()
                try:
                    _set_state(conn, WEEKLY_KEY, next_monday)
                finally:
                    conn.close()
            client.chat_postMessage(channel=channel, text=text)
        return True

    def _weekly_sync(self) -> tuple[str, str | None]:
        """Pull the week's runs before the recap. Returns a line for the coach and an optional
        note for her (sign-in needed, duplicates to review, runs left to download)."""
        conn = self.connect()
        try:
            report = self.sync(conn, self.now())
        except coros.CorosError as exc:
            return "(COROS sync failed: recap from the runs already imported.)", f"_{exc}_"
        except Exception:  # network or SDK surprises: the check-in still goes out
            log.exception("COROS sync before the check-in failed")
            return "(COROS sync failed: recap from the runs already imported.)", SYNC_FAILED
        finally:
            conn.close()
        notes = []
        if report.pending:
            notes.append(SYNC_DUPLICATES)
        if report.not_downloaded:
            notes.append(SYNC_LEFT.format(n=report.not_downloaded))
        synced = f"Runs synced from COROS just now: {len(report.imported)} new."
        return synced, "\n".join(notes) or None

    def _open_weekly(self) -> tuple[str | None, Debrief | None]:
        """The week her check-in reply should write, if the check-in went out this Sunday and
        that week isn't saved yet (open through Monday, so a morning reply still counts), and
        last week's long run if it still needs a debrief."""
        conn = self.connect()
        try:
            week_start = _state(conn, WEEKLY_KEY)
            if week_start is None:
                return None, None
            try:
                today = self.today(conn)
            except LookupError:
                return None, None
            if today > week_start:
                _delete_state(conn, WEEKLY_KEY)
                return None, None
        finally:
            conn.close()
        last_monday = (date.fromisoformat(week_start) - timedelta(days=7)).isoformat()
        return week_start, self._missed_debrief(last_monday)

    def _missed_debrief(self, monday: str) -> Debrief | None:
        """The long run in the week starting `monday` if it's done, not in the fuel log, and she
        didn't decline saving it: the Sunday check-in asks for it."""
        conn = self.connect()
        try:
            week = reports.plan_week(db.load_accepted_plan(conn), monday)
            today = self.today(conn)
            for s in (week or {}).get("sessions", []):
                if (
                    s["kind"] != "long"
                    or s["date"] > today
                    or slack_schedule.logged(conn, s["date"])
                ):
                    continue
                closed = conn.execute(
                    "SELECT 1 FROM notifications WHERE kind = ? AND date = ?",
                    (DEBRIEF_CLOSED, s["date"]),
                ).fetchone()
                if not closed:
                    return Debrief(s["date"], s["miles"], self.clock() + DEBRIEF_TTL)
            return None
        finally:
            conn.close()

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
                if sent and not closed and not slack_schedule.logged(conn, iso):
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
            messages = list(getattr(reply, "messages", []))[len(history) :]
            entry = proposed_entry(messages)
            if entry is not None:
                convo.pending = entry
            week = proposed_week(messages)
            if week is not None and self._revised_week(conn, week)[0] is not None:
                convo.pending = week
        except CoachError:
            log.warning("coach could not finish a Slack reply")
            return FAILED
        except Exception:  # API/network errors: keep the bot up, say so in Slack
            log.exception("coach failed on a Slack message")
            return FAILED
        finally:
            conn.close()
        answer = reply.text or FAILED
        convo.turns += [
            {"role": "user", "content": text},
            {"role": "assistant", "content": answer},
        ]
        convo.turns = convo.turns[-2 * MAX_TURNS :]
        self._save_turns(convo)
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


def _delete_state(conn: sqlite3.Connection, key: str) -> None:
    with conn:
        conn.execute("DELETE FROM slack_state WHERE key = ?", (key,))


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

    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
            try:
                bot.resume(client)  # a reply the last restart cut off
            except Exception:
                log.exception("resume failed")
            checks = (bot.maybe_schedule, bot.maybe_ping, bot.maybe_weekly)
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

    handler = SocketModeHandler(app, env["SLACK_APP_TOKEN"])
    handler.client.on_error_listeners.append(exit_when_stuck(SocketWatch(), time.time, os._exit))
    handler.start()  # type: ignore[no-untyped-call]
