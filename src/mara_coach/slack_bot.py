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
- Body check: on days the accepted plan has a long run or a quality workout, the bot DMs a
  green / yellow / red check at `BODY_HOUR` athlete-local (queued ahead on Slack; live until
  `BODY_LAST_HOUR` if it wasn't), once a day. A bare "green" is logged in code, no coach call,
  on any day, under the day she sent it (a reply answered late by catch-up is not today's).
  Yellow, red or a described niggle goes to the coach, which asks once for where,
  swelling and pain 0-10 and calls `propose_body_entry` (validates only); the bot shows the
  entry and saves it only on her yes. The saved-entry replies are fixed text: nothing here or
  in the coach rates an entry or clears her to run. Yellow/red entries of the week go into
  the Sunday check-in.
- One yes: a body entry and a fueling entry proposed in the same turn wait together
  (`Conversation.more`); the bot lists both, "yes" saves both, "yes 1" / "yes 2" saves one, "no"
  saves neither. A week is always its own yes: proposed with anything else, only the first of
  those is kept to confirm and the reply says which one wasn't (`ONE_AT_A_TIME`).
- Yes plus a request: while a body or fueling entry waits, an explicit save, then "then", then
  a request ("yes log it then plan the rest of my week", "ok save it, then what's tomorrow",
  "log both then plan my week") saves the waiting entries exactly as a plain yes does (without
  the fueling save's coach follow-up), then sends the request to the coach with a note saying
  what was saved (`yes_and_more`, `saved_note`). It is an allow-list: a bare "yes, ..." never
  counts, and a request with hesitation, entry fields, symptoms, body parts, colours or digits
  means nothing is saved and the whole message goes to the coach. A waiting week never takes
  this: it needs its own yes.
- Not dropped silently: when a waiting body or fueling entry is replaced by a new proposal or
  a sync's duplicate question without a yes or a no, the reply ends with one line in code
  saying it wasn't saved (`NOT_SAVED_BODY`, `NOT_SAVED_FUEL`). A corrected entry of the same
  kind and day is shown for her yes, so it gets no such line. (An `IDLE_RESET` or "yes 1" /
  "yes 2" drops the other entry as before, with no line.)
- Sunday is one conversation: at `WEEKLY_HOUR` athlete-local (late until `WEEKLY_LAST_HOUR`),
  when the week ending or the next one is in the plan, the bot first syncs the week's runs from
  COROS in code (no separate reminder), then the coach writes a recap of the week and a preview
  of the next one, ending with one question. Her reply (through Monday, `WEEKLY_KEY`) is tagged
  as a check-in answer, so the coach writes the week with `propose_week`. One "yes" saves it in
  code and puts its runs on her COROS calendar (`coros.push_week`: workouts from the accepted
  plan only, days that already have a workout skipped and listed); "save only" skips the watch.
  "push my week" sends the coming week anytime, the same way. The live check-in may end with
  one code-built line: heart rate at her usual pace, last 4 weeks vs the 4 before, read from the
  synced FIT files (`hr_pace_line`); without enough weeks of data there is no line.
- Asleep-proof: fixed-time messages are also queued on Slack's servers (`slack_schedule`), so
  they arrive while the laptop sleeps; the live versions replace them when the bot is awake.
  On start, after a sleep, and every `CATCH_UP_EVERY`, the bot answers DMs it missed (last
  `CATCH_UP_WINDOW`) in one reply. A stored cursor of the newest handled message means Slack
  retries and catch-up never answer the same message twice. Debrief context comes from the DB
  (plan, ping record, fuel log), so it survives restarts.
- COROS sync: "sync my runs" (or just "sync") pulls this week's runs in code with `coros`, no
  coach call. Possible duplicates are imported only if she replies yes. A sync or forecast asked
  for inside a longer message goes to the coach, which has the `sync_runs` and `get_weather`
  tools; the bot does the work (`_actions`: the same sync and forecast code), adds the weather
  credit and any sync note in code, and never imports a possible duplicate there.
- Weather: the live Sunday check-in ends with one conditions line per coming run (default
  location, `DEFAULT_MORNING_HOUR`), built in code from `weather.forecast` at send time; a failed
  lookup just leaves the block out, and the week costs one Open-Meteo request (plus two NWS
  requests at most), not one per run. "weather" / "weather at|in|near PLACE" answers for the
  next plan run with no coach call (today's, unless a run is already imported for today),
  restart-proof like the other code-handled phrases. PLACE is a named place if it is exactly
  one, otherwise it is looked up by name (`weather.forecast(near=...)`: one geocoding request
  carrying only the typed name) and the reply shows the matched place's label. Only a message
  that is the whole phrase counts (`weather_place`); anything else goes to the coach. "near
  me" / "in here" mean the default location, and a tail that reads as a time or a generic spot
  ("in the morning", "at 6am tomorrow", "at the track") goes to the coach with no lookup.
  Conditions only, never advice; nothing is stored, and queued fallbacks never carry it.
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

from mara_trainer import body, db, fueling, hr_pace, reports
from mara_trainer.dates import date_in_timezone, parse_instant

from . import coros, slack_schedule, tools, weather
from .agent import Coach, CoachError
from .slack_schedule import (
    BODY_HOUR,
    BODY_KIND,
    BODY_SESSION_KINDS,
    DEBRIEF_KIND,
    PING_HOUR,
    WEEKLY_HOUR,
    WEEKLY_KIND,
    body_ping_text,
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


PUSH_LATER = 'Not on your watch yet. Say "push my week" when you want it there.'
PUSHING = "Putting your runs on your watch..."
PUSH = re.compile(
    r"^\s*(please\s+)?push(\s+my)?(\s+(week|runs|workouts))?"
    r"(\s+to\s+(coros|my\s+watch|the\s+watch))?\b[\s.!]*$",
    re.I,
)
SAVE_ONLY = re.compile(
    r"^\s*(just\s+)?save(\s+it)?\s+only\b[\s.!]*$|^\s*just\s+save(\s+it)?[\s.!]*$", re.I
)
WEEK_QUESTION = (
    "Reply *yes* to save it and put its runs on your watch, *save only* to just save it, or *no*."
)
_SAVE_ASK = re.compile(r"\s*\**\s*Save this week\?\s*\**\s*$", re.I)


def ask_to_save_week(answer: str) -> str:
    """The coach's week write-up, ending with the bot's own yes / save only / no question."""
    return _SAVE_ASK.sub("", answer).rstrip() + "\n\n" + WEEK_QUESTION


WEEKLY_KEY = "weekly_reply"  # Monday of the week her check-in reply should write
SYNC_DUPLICATES = (
    '_Some runs look like ones already imported. Reply "sync my runs" to review them._'
)
SYNC_LEFT = '_{n} run(s) didn\'t download from COROS yet. Reply "sync my runs" later._'
SYNC_FAILED = "Couldn't sync from COROS just now, so the recap may be missing runs."


BODY_LAST_HOUR = 10  # a before-run check: don't send it live after this
GREEN = re.compile(r"^\s*(all\s+)?(green|:large_green_circle:|\U0001F7E2)[\s.!]*$", re.I)
YELLOW_RED = re.compile(
    r"^\s*(yellow|red|:large_yellow_circle:|:red_circle:|\U0001F7E1|\U0001F534)[\s.!]*$", re.I
)
_CIRCLES = {
    ":large_yellow_circle:": "yellow",
    "\U0001f7e1": "yellow",
    ":red_circle:": "red",
    "\U0001f534": "red",
}
GREEN_SAVED = "Logged green for today ({day})."
GREEN_SAVED_EARLIER = "Logged green for {day}, the day you sent it."
ONE_AT_A_TIME = (
    "_One thing at a time: your yes or no here is only for the {kept}. The {dropped} isn't "
    "saved. Ask me for it again after this and I'll bring it back._"
)
BODY_QUESTION = "Reply *yes* to log it, or *no*."
BOTH_QUESTION = (
    "Reply *yes* to save both, *yes 1* or *yes 2* to save just that one, or *no* for neither."
)
YES_BOTH = re.compile(r"^\s*(?:(?:y|yes|yep|yeah|save)[\s,]*)?(?:to\s+)?both\b[\s.!]*$", re.I)
YES_ONE = re.compile(r"^\s*(?:y|yes|yep|yeah|save)[\s,]*(?:#|number\s*)?([12])\b[\s.!]*$", re.I)
_SAVE_IT_ASK = re.compile(r"[ \t]*\**Save it\?\**", re.I)  # anywhere: the bot asks instead
SYNC_ACTION_FAILED = 'COROS didn\'t answer just now. She can send "sync my runs" in a few minutes.'
WEATHER_ACTION_HOURS = 3
BODY_SAVED = "Saved to your body log (entry {id})."
YELLOW_SAVED = (
    BODY_SAVED + " It'll come up at Sunday's check-in, where the plan can change. If the pain "
    "persists, gets worse or comes with swelling, see a qualified professional (a doctor or "
    "physio)."
)
RED_SAVED = (
    BODY_SAVED + " Please see a qualified professional (a doctor or physio) before you run on "
    "it. Want the rest of this week lowered? Reply *lower my week* and I'll write it for you "
    "to approve."
)
_CONTEXT_WORDS = {"before_run": "before a run", "after_run": "after a run", "other": "other"}


def body_line(entry: dict[str, Any]) -> str:
    """One body-log entry as she reported it. Unset fields are left out, never filled in."""
    parts = [f"{entry['date']}: {entry['status']}"]
    if entry.get("location"):
        parts.append(str(entry["location"]))
    if entry.get("swelling") is not None:
        parts.append("swelling: " + ("yes" if entry["swelling"] else "no"))
    if entry.get("pain") is not None:
        parts.append(f"pain {entry['pain']} out of 10")
    if entry.get("context"):
        parts.append(_CONTEXT_WORDS[entry["context"]])
    if entry.get("notes"):
        parts.append(str(entry["notes"]))
    return ", ".join(parts)


FUEL_QUESTION = "Reply *yes* to save it, or *no*."
WEEK_REJECTED = (
    "That week didn't pass the plan checks, so nothing is waiting to be saved now. "
    "Ask me for the week again when you're ready."
)


def still_waiting(convo: Conversation) -> str | None:
    """The open yes/no question again, when a coach reply leaves an earlier proposal waiting.
    Built in code: the coach can't know whether anything was saved."""
    if isinstance(convo.pending, WeekProposal):
        return "The week I proposed earlier isn't saved yet. " + WEEK_QUESTION
    if isinstance(convo.pending, body.BodyEntry):
        if convo.more:
            return "The two entries I showed earlier aren't saved yet. " + BOTH_QUESTION
        return "The body log entry I showed earlier isn't saved yet. " + BODY_QUESTION
    if isinstance(convo.pending, fueling.FuelEntry):
        return "The fueling entry from earlier isn't saved yet. " + FUEL_QUESTION
    return None


def ask_to_log_body(answer: str, entry: body.BodyEntry) -> str:
    """The coach's reply, then the entry and the bot's own yes / no question."""
    return f"{answer.rstrip()}\n\n*Body log entry* {body_line(asdict(entry))}\n{BODY_QUESTION}"


def fuel_line(entry: fueling.FuelEntry) -> str:
    """One proposed fueling entry as she gave it: every field a yes would save. Unset fields
    are left out, never filled in."""
    parts = [entry.date]
    if entry.distance_meters:
        parts.append(f"{entry.distance_meters / tools.MI_METERS:.2f} mi")
    if entry.duration_seconds:
        minutes, seconds = divmod(entry.duration_seconds, 60)
        parts.append(f"{minutes // 60}:{minutes % 60:02d}:{seconds:02d}")
    if entry.pre_run:
        ahead = f" ({entry.pre_run_minutes} min before)" if entry.pre_run_minutes else ""
        parts.append(f"before: {entry.pre_run}{ahead}")
    if entry.during:
        parts.append(f"during: {entry.during}")
    if entry.carbs_grams is not None:
        carbs = f"{entry.carbs_grams:g} g carbs"
        per_hour = fueling.carbs_per_hour(entry.carbs_grams, entry.duration_seconds)
        parts.append(carbs + (f" ({per_hour:g} g per hour)" if per_hour is not None else ""))
    if entry.fluid_ml is not None:
        parts.append(f"{entry.fluid_ml:g} ml fluid")
    for label, value, top in (
        ("stomach", entry.stomach, 5),
        ("energy", entry.energy, 5),
        ("effort", entry.effort, 10),
    ):
        if value is not None:
            parts.append(f"{label} {value}/{top}")
    if entry.notes:
        parts.append(f"notes: {entry.notes}")
    return ", ".join(parts)


def ask_to_log_both(answer: str, niggle: body.BodyEntry, entry: fueling.FuelEntry) -> str:
    """The coach's reply, then both entries and the bot's own question for the pair."""
    return (
        f"{_SAVE_IT_ASK.sub('', answer).rstrip()}\n\n"
        f"*1. Body log entry* {body_line(asdict(niggle))}\n"
        f"*2. Fueling entry* {fuel_line(entry)}\n{BOTH_QUESTION}"
    )


def credit_line(results: list[dict[str, Any]]) -> str:
    """Weather credits as a Markdown line (the coach's reply is converted to Slack's format)."""
    credits: dict[str, str] = {}
    for result in results:
        for credit in result["attribution"]:
            credits.setdefault(credit["text"], credit["url"])
    return "_" + " · ".join(f"[{text}]({url})" for text, url in credits.items()) + "_"


def body_reply(status: str, day: str, text: str) -> str:
    """Her bare yellow / red, tagged so the coach gathers the details and proposes the entry."""
    return (
        f"(Body check for {day}: I'm reporting {status}. Ask me in one message for whatever is "
        "missing of: where it is, any swelling, pain 0-10. Then call propose_body_entry.)"
        f"\n\n{text}"
    )


def niggle_note(items: list[dict[str, Any]]) -> str:
    """Yellow / red entries from the body log for the Sunday check-in; nothing if none."""
    if not items:
        return ""
    lines = "\n".join("- " + body_line(i) for i in items)
    return (
        "\n(My body log this week, as I reported it:\n" + lines + "\nRaise each one: ask how "
        "it is now, and take it into account when you write the coming week. Don't diagnose, "
        "and never treat a pain number as a reason it's fine to run.)"
    )


def weekly_prompt(
    sunday: str,
    synced: str,
    missed: Debrief | None = None,
    niggles: list[dict[str, Any]] | None = None,
) -> str:
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
week's exact sessions from her answer. Don't mention syncing.{_missed_line(missed)}\
{niggle_note(niggles or [])}
Keep it brief and cite sources at the end."""


def _missed_line(missed: Debrief | None) -> str:
    if missed is None:
        return ""
    return (
        f"\nMy {missed.planned_miles:g} mi long run on {missed.date} isn't debriefed yet: in the "
        "same question, also ask how it went (distance and time, effort 1-10, what I ate before "
        "and how long before, what I took during, stomach and energy)."
    )


HR_PACE_DIR = coros.EXPORTS  # the FIT files the sync downloads
HR_PACE_WAIT = 2.0  # seconds the check-in waits for the line once the coach's answer is ready


def hr_pace_line(directory: Path, timezone: str, today: str) -> str:
    """Heart rate at the usual pace as one fixed sentence plus its caveat, or "" when there are
    not enough weeks with data (the common case), no FIT files, or anything goes wrong. Built in
    code from `hr_pace` only: no coach call, no network, no interpretation."""
    try:
        if not directory.is_dir() or not any(directory.glob("*.[fF][iI][tT]")):
            return ""
        line = hr_pace.trend_line(hr_pace.report(directory, timezone, today)["trend"])
    except Exception:  # an optional line must never cost her the check-in
        log.exception("HR-at-pace line failed")
        return ""
    return f"{line}. {hr_pace.TREND_CAVEAT}" if line else ""


def start_hr_pace_line(
    directory: Path, timezone: str, today: str, wait: float | None = None
) -> Callable[[], str]:
    """Start reading the FIT files in the background (while the coach writes the recap) and
    return a function that gives the line. It waits at most `wait` seconds (`HR_PACE_WAIT`) and
    gives "" if the files aren't read by then, so the check-in is never held up."""
    result: list[str] = []
    worker = threading.Thread(
        target=lambda: result.append(hr_pace_line(directory, timezone, today)), daemon=True
    )
    worker.start()

    def line() -> str:
        worker.join(HR_PACE_WAIT if wait is None else wait)
        return result[0] if result else ""

    return line


SYNC = re.compile(r"^\s*(please\s+)?sync(\s+my)?(\s+runs)?(\s+from\s+coros)?\b[\s.!]*$", re.I)
SYNCING = "Syncing your runs from COROS..."
SKIPPED = "Okay, skipped those."
# A yes may carry a few plain words ("yes log it", "ok save it please"); anything longer goes
# to the coach. "yes 1" and "save only" have their own patterns. A trailing "thanks" counts
# only after a plain yes: "ok thanks" is an acknowledgement, not a yes to save.
# "yes and save to coros", "yes push it to my watch": still a plain yes ("save only" is not).
_YES_OBJECT = r"(?:\s+(?:it|that|them|this|the\s+(?:runs|week)))?"
_YES_WATCH = r"(?:\s+(?:to|on|onto)\s+(?:my\s+|the\s+)?(?:coros(?:\s+watch)?|watch))?"
_YES_WATCH_NEEDED = r"\s+(?:to|on|onto)\s+(?:my\s+|the\s+)?(?:coros(?:\s+watch)?|watch)"
# "and" only joins on a verb ("yes and" alone is an unfinished sentence, not a yes); push, put
# and send need the watch named ("yes send" could be the start of anything).
_YES_VERBS = (
    r"please|do\s+it|go\s+ahead|log(?:\s+(?:it|that))?"
    rf"|(?:and\s+)?save{_YES_OBJECT}{_YES_WATCH}"
    rf"|(?:and\s+)?(?:push|put|send){_YES_OBJECT}{_YES_WATCH_NEEDED}"
)
YES = re.compile(
    r"^\s*(?:"
    rf"(?:y|yes|yep|yeah|yup)\b(?:[\s,]+(?:{_YES_VERBS}|thanks|thank\s+you))*"
    rf"|(?:sure|ok|okay|save|save it|log it|do it|go ahead)\b(?:[\s,]+(?:{_YES_VERBS}))*"
    r")[\s.!]*$",
    re.I,
)
NO = re.compile(
    r"^\s*(n|no|nope|cancel|don'?t save|do not save|(?:don'?t|do not) log(?: it)?)\b"
    r"(?:[\s,]+(?:thanks|thank\s+you|don'?t\s+(?:save|log)(?:\s+it)?))*[\s.!]*$",
    re.I,
)
CANCELLED = "Okay, not saved."
# A yes, then more: "yes log it then plan the rest of my week". Only for a waiting body or
# fueling entry (a week is always its own yes). An allow-list, not a guess: the head must name
# the save ("log it", "save that", "log both", optionally after yes / ok and commas), then
# "then" (or "and then", ", then", ". then", "; then"), then her request. A bare "yes, ..."
# never saves: "yes, left ankle" or "yes, it's a 4" is a correction. The request is checked
# again (`_EDIT_WORDS`): hesitation, entry fields, symptoms, body parts, colours or any digit
# mean no save at all. A missed yes costs her one more message; a wrong save, a wrong log.
_YES_THEN = re.compile(
    r"^\s*(?:(?:y|yes|yep|yeah|yup|ok|okay|sure)[\s,]+)?(?:please[\s,]+)?"
    r"(?:log|save)\s+(?:it|that|both)(?:[\s,]+please)?"
    r"\s*(?:[,.;]\s*)?(?:and\s+)?then\b[\s,]*(?P<rest>.*?)\s*$",
    re.I | re.S,
)
_EDIT_WORDS = re.compile(
    r"\b(?:but|change[sd]?|except|instead|actually|wait|fix|edit|not|no|nope|wrong|updated?"
    r"|make\s+it|should\s+be|hold|oops|sorry|correct(?:ion)?|typo|remove|delete|was|were"
    r"|meant|unless|though|hmm+|bad|more\s+like|other"
    r"|pain\w*|swell\w*|swollen|carbs?|gels?|gu|chews?|fluid|drank|drink\w*|ml|stomach"
    r"|energy|effort|date|entry|hurt\w*|sore\w*|ach(?:e|es|ed|ing|y)|injur\w*"
    r"|ibuprofen|advil|tylenol|left|right|side|leg|legs|knee|knees|hip|hips|ankle|ankles"
    r"|foot|feet|calf|calves|shin|shins|green|yellow|red)\b"
    r"|n['’]t\b|\d",
    re.I,
)
NOT_SAVED_BODY = 'Your body entry for {day} wasn\'t saved. Say "log a niggle" (or ask) to redo it.'
NOT_SAVED_FUEL = "Your fueling entry for {day} wasn't saved. Ask me to log it again to redo it."


def yes_and_more(text: str) -> str | None:
    """The request after an explicit save ("yes log it then plan my week" -> "plan my week"),
    or None when the message isn't exactly that shape or the request could touch the entry."""
    match = _YES_THEN.match(text)
    if match is None:
        return None
    rest = match.group("rest")
    if not re.search(r"[A-Za-z]", rest) or _EDIT_WORDS.search(rest):
        return None
    return rest


def entry_name(item: fueling.FuelEntry | body.BodyEntry) -> str:
    kind = "body" if isinstance(item, body.BodyEntry) else "fueling"
    return f"her {kind} entry for {item.date}"


def saved_note(saved: list[str], failed: list[str]) -> str:
    """For the coach, ahead of the request she sent with her yes: what the bot did with it."""
    parts = []
    if saved:
        parts.append("Saved: " + " and ".join(saved) + ".")
    if failed:
        parts.append("Couldn't save " + " and ".join(failed) + "; the app told her why.")
    return "(" + " ".join(parts) + ")"


def not_saved_lines(old: list[Pending], new: list[Pending]) -> list[str]:
    """One line per body or fueling entry that was waiting for her yes and is now replaced
    without being saved or declined. A new entry of the same kind for the same day replaces
    it in view (a correction), so that one isn't reported; another day's entry is not."""
    lines = []
    for item in old:
        if not isinstance(item, body.BodyEntry | fueling.FuelEntry):
            continue
        if any(
            type(other) is type(item) and getattr(other, "date", None) == item.date for other in new
        ):
            continue
        template = NOT_SAVED_BODY if isinstance(item, body.BodyEntry) else NOT_SAVED_FUEL
        lines.append(template.format(day=item.date))
    return lines


SAVE_FOLLOW_UP = (
    "(Saved.) Give me your short coach reply: one or two observations from my fuel log, and "
    "one small thing to try next long run if it's worth changing anything."
)


# --- Weather: conditions only, built in code, fetched at send time, never stored. Queued
# fallbacks (`slack_schedule`) never carry it: a forecast written days ahead would be stale.
DEFAULT_MORNING_HOUR = 7  # athlete-local; the plan has no start times, so forecasts use this hour
WEATHER_KINDS = ("easy", "workout", "long", "race")
WEATHER = re.compile(
    r"^\s*(what(['’]?s|\s+is)\s+the\s+)?weather(\s+for\s+my(\s+next)?\s+run)?"
    r"(\s+(at|in|near)\s+(?P<place>\w[\w ,.'’&;-]*?))?\s*[.!?]*$",
    re.I,
)
MAX_PLACE_WORDS = 6  # longer than this reads as a question for the coach, not a place name
# After at/in/near, some short tails are not place names. These mean the default location:
HERE = frozenset({"me", "here", "my location"})
# ... and these make the message a question for the coach (nothing is looked up): a tail that
# opens with one of NOT_A_NAME_OPENERS, has a time word or a clock time anywhere, is only a
# time of day, or is "the" plus a generic spot. "The Strand" or "Sunrise, Region" stay places.
NOT_A_NAME_OPENERS = frozenset({"my", "our", "a", "an", "this", "that", "next", "about"})
TIME_WORDS = frozenset(
    [
        "morning",
        "afternoon",
        "evening",
        "night",
        "tonight",
        "today",
        "tomorrow",
        "now",
        "later",
        "week",
        "weekend",
        "hour",
        "hours",
        "minute",
        "minutes",
        "am",
        "pm",
        "a.m",
        "p.m",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]
)
TIME_OF_DAY = frozenset({"noon", "midday", "midnight", "sunrise", "sunset", "dawn", "dusk"})
GENERIC_SPOTS = frozenset(
    [
        "track",
        "gym",
        "park",
        "beach",
        "trail",
        "trails",
        "race",
        "start",
        "finish",
        "course",
        "office",
        "lake",
        "river",
        "hill",
        "hills",
        "treadmill",
        "road",
    ]
)
CLOCK_TIME = re.compile(r"\d{1,2}(:\d{2})?([ap]\.?m\.?)?", re.I)  # 6, 6am, 6:30pm; not a postcode
WEATHER_HEADER = "*Forecast for this week's runs* (conditions at a morning start):"
NO_RUN_SOON = f"No run on your plan in the next {weather.FORECAST_DAYS} days, so no forecast."
WEATHER_FAILED = "Couldn't get the forecast just now."
CHECKING_WEATHER = "Checking the forecast..."


def _live_forecast(*, nws: bool = True, **window: Any) -> dict[str, Any]:
    """`weather.forecast` with the weather settings from the environment and nothing else.
    `nws=False` leaves the NWS contact out, so no NWS request is made."""
    names = [weather.LAT_ENV, weather.LON_ENV, weather.PLACES_ENV]
    if nws:
        names.append(weather.NWS_CONTACT_ENV)
    return weather.forecast(env={n: os.environ[n] for n in names if n in os.environ}, **window)


def weather_place(text: str) -> str | None:
    """The place asked for when the whole message is a weather phrase ("" = the default
    location), or None when it isn't one and should go to the coach."""
    match = WEATHER.match(text)
    if match is None:
        return None
    place = match.group("place") or ""
    for entity, char in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&")):  # as Slack sends them
        place = place.replace(entity, char)
    place = " ".join(place.split())
    if (
        len(place) > weather.MAX_PLACE_TEXT
        or len(place.split()) > MAX_PLACE_WORDS
        or weather.COORDINATES_TEXT.fullmatch(place)
        or any(char in place for char in "<>;")
    ):
        return None
    if place.casefold() in HERE:
        return ""
    if place and _not_a_place_name(place):
        return None
    return place


def _not_a_place_name(place: str) -> bool:
    """True for a tail that reads as a time or a generic spot ("the morning", "6am tomorrow",
    "my office", "the track"), so it is never sent to the place lookup."""
    words = [word.strip(".,'’").casefold() for word in place.split()]
    rest = words[1:] if words[0] == "the" else words
    return (
        words[0] in NOT_A_NAME_OPENERS
        or any(word in TIME_WORDS or CLOCK_TIME.fullmatch(word) for word in words)
        or (len(rest) == 1 and rest[0] in TIME_OF_DAY | GENERIC_SPOTS and words[0] == "the")
        or (len(words) == 1 and words[0] in TIME_OF_DAY)
    )


def _mrkdwn(text: str) -> str:
    """Text from outside (a looked-up place label) made safe to show: Slack's three escapes."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def hour_label(hour: int) -> str:
    return f"{hour % 12 or 12}{'am' if hour < 12 else 'pm'}"


def attribution_line(results: list[dict[str, Any]]) -> str:
    """The attribution texts `weather.py` returned, each once, linked."""
    credits: dict[str, str] = {}
    for result in results:
        for credit in result["attribution"]:
            credits.setdefault(credit["text"], credit["url"])
    return "_" + " · ".join(f"<{url}|{text}>" for text, url in credits.items()) + "_"


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
    restore: bool = False


@dataclass(frozen=True)
class DuplicateRuns:
    items: tuple[coros.Pending, ...]


Pending = fueling.FuelEntry | body.BodyEntry | WeekProposal | DuplicateRuns | None


def encode_pending(pending: Pending) -> dict[str, Any] | None:
    """A waiting yes/no proposal as JSON, so it survives a restart."""
    if isinstance(pending, fueling.FuelEntry):
        return {"type": "fuel", **asdict(pending)}
    if isinstance(pending, body.BodyEntry):
        return {"type": "body", **asdict(pending)}
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
        if raw.get("type") == "body":
            return _checked_body(data)
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
    # a fueling entry proposed in the same turn as a body entry (`pending`); one yes saves both
    more: list[Pending] = field(default_factory=list)
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
                args["start_date"],
                args["sessions"],
                args.get("cut", False),
                args.get("focus"),
                args.get("restore", False),
            )
    return None


def _checked_body(args: dict[str, Any]) -> body.BodyEntry | None:
    """A body entry only if every field has the right type and passes validation."""
    try:
        entry = tools.build_body_entry(args)
    except tools.ToolError:
        return None
    return None if entry.problems() else entry


def proposed_body(messages: list[dict[str, Any]]) -> body.BodyEntry | None:
    """The last valid `propose_body_entry` call in these messages, re-checked in code."""
    for message in reversed(messages):
        if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
            continue
        for block in reversed(message["content"]):
            if _get(block, "type") != "tool_use" or _get(block, "name") != "propose_body_entry":
                continue
            args = _get(block, "input")
            if not isinstance(args, dict) or tools.check_args(_BODY_SCHEMA, args):
                return None
            return _checked_body(args)
    return None


_BODY_SCHEMA = next(t.input_schema for t in tools.TOOLS if t.name == "propose_body_entry")
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
        push: Callable[[sqlite3.Connection, str, datetime], coros.PushReport] = coros.push_week,
    ) -> None:
        self.allowed_user = allowed_user
        self.coach_factory = coach_factory or (lambda conn: Coach(conn, channel="slack"))
        self.connect = connect or _connect
        self.clock = clock
        self.now = now
        self.sync = sync
        self.push = push
        self.conversations: dict[str, Conversation] = {}
        self._guard = threading.Lock()

    def _conversation(self, channel: str) -> Conversation:
        with self._guard:
            convo = self.conversations.get(channel)
            if convo is None:
                turns, pending, more = self._saved_conversation(channel)
                convo = Conversation(
                    turns, self.clock(), pending=pending, more=more, channel=channel
                )
                self.conversations[channel] = convo
        now = self.clock()
        if (convo.turns or convo.pending) and now - convo.last_seen > IDLE_RESET:
            convo.turns, convo.pending, convo.more = [], None, []
        if not isinstance(convo.pending, body.BodyEntry):
            convo.more = []  # a second entry only ever waits beside a body entry
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
            if PUSH.match(text):
                self._push(channel, client, self._tomorrow())
                return
            asked_weather = weather_place(text)
            if asked_weather is not None:
                self._weather(channel, client, asked_weather)
                return
            if GREEN.match(text) and self._log_green(channel, client, event.get("ts")):
                return
            if convo.debrief is None:
                convo.debrief = self._open_debrief()
            if isinstance(convo.pending, WeekProposal) and SAVE_ONLY.match(text):
                self._save_week(channel, convo, convo.pending, client, push=False)
                return
            if not isinstance(convo.pending, body.BodyEntry):
                convo.more = []  # a sync's duplicate question (or anything else) replaced it
            one = YES_ONE.match(text) if convo.more else None
            if one:  # "yes 1" / "yes 2": just that entry, the other is dropped
                if one.group(1) == "2":
                    convo.pending = convo.more[0]
                convo.more = []
                self._save(channel, convo, client)
                return
            if convo.pending is not None and (
                YES.match(text) or (convo.more and YES_BOTH.match(text))
            ):
                rest, convo.more = convo.more, []
                self._save(channel, convo, client)
                for item in rest:  # the fueling entry last: its save ends with a coach reply
                    convo.pending = item
                    self._save(channel, convo, client)
                return
            if convo.pending is not None and NO.match(text):
                also_fuel = any(isinstance(item, fueling.FuelEntry) for item in convo.more)
                convo.more = []
                if also_fuel:  # no to both: the debrief is closed, as for a fueling entry alone
                    if convo.debrief is not None:
                        self._mark(DEBRIEF_CLOSED, convo.debrief.date)
                    convo.pending, convo.debrief = None, None
                    client.chat_postMessage(channel=channel, text=CANCELLED)
                    return
                if isinstance(convo.pending, DuplicateRuns):
                    convo.pending = None
                    client.chat_postMessage(channel=channel, text=SKIPPED)
                    return
                if isinstance(convo.pending, WeekProposal | body.BodyEntry):
                    convo.pending = None
                else:
                    if convo.debrief is not None:
                        self._mark(DEBRIEF_CLOSED, convo.debrief.date)
                    convo.pending, convo.debrief = None, None
                client.chat_postMessage(channel=channel, text=CANCELLED)
                return
            if isinstance(convo.pending, body.BodyEntry | fueling.FuelEntry) and (
                request := yes_and_more(text)
            ):
                text = f"{self._save_entries(channel, convo, client)}\n\n{request}"
            question = self._body_tagged(text, event.get("ts"))
            week_start, missed = self._open_weekly()
            if week_start:
                question = weekly_reply(week_start, question) + self._week_niggles(week_start)
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
            elif job["kind"] == "push":  # safe to repeat: days with a workout are skipped
                self._push(channel, client, job["question"], placeholder, tries)
            elif job["kind"] == "weather":  # read-only, so safe to repeat
                self._weather(channel, client, job["question"], placeholder, tries)
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

    def _saved_conversation(
        self, channel: str
    ) -> tuple[list[dict[str, Any]], Pending, list[Pending]]:
        conn = self.connect()
        try:
            raw = _state(conn, TURNS_KEY)
        finally:
            conn.close()
        saved = json.loads(raw) if raw else {}
        fresh = self.now().timestamp() - saved.get("at", 0) <= IDLE_RESET
        if saved.get("channel") != channel or not fresh:
            return [], None, []
        pending = decode_pending(saved.get("pending"))
        beside = isinstance(pending, body.BodyEntry)
        more = [decode_pending(raw) for raw in saved.get("more") or []] if beside else []
        return list(saved["turns"]), pending, [item for item in more if item is not None]

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
            "more": [encode_pending(item) for item in convo.more]
            if isinstance(convo.pending, body.BodyEntry)
            else [],
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
        plan, setup = db.load_accepted_plan(conn), db.load_race_setup(conn)
        try:
            original = None
            if week.restore:  # looked up again at save time: the plan may have changed since
                original = reports.lowered_from(
                    db.load_archived_plans(conn), plan, week.start_date, setup
                )
            return reports.revise_week(
                plan,
                week.start_date,
                week.sessions,
                self.today(conn),
                setup,
                week.cut,
                week.restore,
                original,
            )
        except (reports.ReportError, ValueError) as exc:
            return None, [str(exc)]

    def _tomorrow(self) -> str:
        conn = self.connect()
        try:
            return (date.fromisoformat(self.today(conn)) + timedelta(days=1)).isoformat()
        finally:
            conn.close()

    def _save_week(
        self,
        channel: str,
        convo: Conversation,
        week: WeekProposal,
        client: SlackClient,
        push: bool = True,
    ) -> None:
        conn = self.connect()
        try:
            revised, problems = self._revised_week(conn, week)
            if revised is not None:
                db.save_plan(conn, revised)
                if _state(conn, WEEKLY_KEY) == week.start_date:
                    _delete_state(conn, WEEKLY_KEY)  # the week is written: replies are chat again
        finally:
            conn.close()
        convo.pending = None
        saved = reports.plan_week(revised, week.start_date)
        if saved is None:
            client.chat_postMessage(
                channel=channel, text="Couldn't save that week: " + "; ".join(problems)
            )
            return
        text = f"Saved Week {saved['week']}.\n\n{reports.week_card(saved, week.focus)}"
        if not push:
            client.chat_postMessage(channel=channel, text=f"{text}\n\n{PUSH_LATER}")
            return
        client.chat_postMessage(channel=channel, text=text)
        self._push(channel, client, week.start_date)

    def _push(
        self,
        channel: str,
        client: SlackClient,
        day: str,
        placeholder: str | None = None,
        tries: int = 1,
    ) -> None:
        """Put the plan week containing `day` on her COROS calendar. Only after her yes (or her
        "push my week"); the workouts come from the accepted plan, unchanged."""
        if placeholder is None:
            placeholder = client.chat_postMessage(channel=channel, text=PUSHING)["ts"]
        self._track("push", channel, placeholder, tries, day)
        try:
            conn = self.connect()
            try:
                text = coros.push_text(self.push(conn, day, self.now()))
            except coros.CorosError as exc:
                text = str(exc)
            except Exception:  # network or SDK surprises: keep the bot up
                log.exception("COROS push failed")
                text = 'Couldn\'t reach COROS just now. Say "push my week" to try again.'
            finally:
                conn.close()
            _finish(client, channel, placeholder, text)
        finally:
            self._untrack()

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
                duplicates = DuplicateRuns(tuple(report.pending))
                lost = not_saved_lines([convo.pending, *convo.more], [duplicates])
                convo.pending, convo.more = duplicates, []
                text += "\n\n" + coros.duplicate_question(report.pending)
                if lost:
                    text += "\n\n" + "\n".join(lost)
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
        if isinstance(convo.pending, body.BodyEntry):
            self._save_body(channel, convo, convo.pending, client)
            return
        self._save_fuel(channel, convo, convo.pending, client)

    def _save_entries(self, channel: str, convo: Conversation, client: SlackClient) -> str:
        """Her yes with a request after it: the waiting body / fueling entries are saved the
        way a plain yes saves them (same order, same replies), but the fueling save's coach
        follow-up is skipped, since her request goes to the coach next. Returns the note that
        tells the coach what was saved."""
        items, convo.more = [convo.pending, *convo.more], []
        saved: list[str] = []
        failed: list[str] = []
        for item in items:
            if isinstance(item, body.BodyEntry):
                ok = self._save_body(channel, convo, item, client)
            elif isinstance(item, fueling.FuelEntry):
                ok = self._save_fuel(channel, convo, item, client, follow_up=False)
            else:  # only entries wait beside each other
                continue
            (saved if ok else failed).append(entry_name(item))
        convo.pending = None
        return saved_note(saved, failed)

    def _save_fuel(
        self,
        channel: str,
        convo: Conversation,
        entry: fueling.FuelEntry,
        client: SlackClient,
        follow_up: bool = True,
    ) -> bool:
        conn = self.connect()
        try:
            saved = fueling.add_entry(conn, entry)
        except fueling.FuelError as exc:
            convo.pending = None
            client.chat_postMessage(
                channel=channel, text="Couldn't save that: " + "; ".join(exc.problems)
            )
            return False
        finally:
            conn.close()
        convo.pending, convo.debrief = None, None
        cph = saved["carbs_per_hour"]
        client.chat_postMessage(
            channel=channel,
            text=f"Saved to your fuel log (entry {saved['id']})"
            + (f", {cph:g} g carbs per hour." if cph is not None else "."),
        )
        if follow_up:
            week_start, _ = self._open_weekly()
            question = week_after_debrief(week_start) if week_start else SAVE_FOLLOW_UP
            self._reply(channel, convo, client, question)
        return True

    def _sent_day(self, conn: sqlite3.Connection, ts: Any) -> str:
        """The athlete-local date a DM was sent, from Slack's `ts`: catch-up answers messages
        up to CATCH_UP_WINDOW old, so "today" at handling time can be the wrong day. A missing
        or unusable `ts` (not a time, in the future, older than the window) means today."""
        now = self.now()
        try:
            sent = datetime.fromtimestamp(float(ts), UTC)
        except (TypeError, ValueError, OverflowError, OSError):
            sent = now
        if not timedelta(0) <= now - sent <= timedelta(seconds=CATCH_UP_WINDOW):
            sent = now
        return sent.astimezone(ZoneInfo(db.athlete_timezone(conn))).date().isoformat()

    def _log_green(self, channel: str, client: SlackClient, ts: Any = None) -> bool:
        """Her bare "green": saved in code as the entry for the day she sent it, no coach call,
        no confirmation. False (nothing saved) only when no athlete is set up yet."""
        conn = self.connect()
        try:
            try:
                day, today = self._sent_day(conn, ts), self.today(conn)
            except LookupError:
                return False
            body.add_entry(conn, body.BodyEntry(day, "green"))
        finally:
            conn.close()
        said = GREEN_SAVED if day == today else GREEN_SAVED_EARLIER
        client.chat_postMessage(channel=channel, text=said.format(day=day))
        return True

    def _body_tagged(self, text: str, ts: Any = None) -> str:
        """A bare yellow / red gets the body-check tag for the day she sent it; anything else
        goes to the coach as is."""
        match = YELLOW_RED.match(text)
        if not match:
            return text
        word = match.group(1).lower()
        conn = self.connect()
        try:
            day = self._sent_day(conn, ts)
        except LookupError:
            return text
        finally:
            conn.close()
        return body_reply(_CIRCLES.get(word, word), day, text)

    def _save_body(
        self, channel: str, convo: Conversation, entry: body.BodyEntry, client: SlackClient
    ) -> bool:
        """Her yes to a proposed body entry: saved as she reported it. The reply is fixed text
        chosen by her own status (reported swelling gets the red text); nothing here rates the
        entry or says whether to run."""
        convo.pending = None
        conn = self.connect()
        try:
            saved = body.add_entry(conn, entry)
        except body.BodyError as exc:
            client.chat_postMessage(
                channel=channel, text="Couldn't save that: " + "; ".join(exc.problems)
            )
            return False
        finally:
            conn.close()
        template = {"yellow": YELLOW_SAVED, "red": RED_SAVED}.get(saved["status"], BODY_SAVED)
        if saved["status"] != "green" and saved.get("swelling"):
            template = RED_SAVED  # reported swelling gets the see-someone-first text
        text = template.format(id=saved["id"])
        client.chat_postMessage(channel=channel, text=text)
        if convo.turns:  # so the coach sees what she agreed to and what the bot offered
            convo.turns += [
                {"role": "user", "content": "yes"},
                {"role": "assistant", "content": text},
            ]
            convo.turns = convo.turns[-2 * MAX_TURNS :]
        return True

    def _open_niggles(self, since: str, until: str) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            return body.open_items(conn, since, until)
        finally:
            conn.close()

    def _week_niggles(self, week_start: str) -> str:
        """Yellow / red entries since the Monday before `week_start`, as a note for the coach."""
        monday = (date.fromisoformat(week_start) - timedelta(days=7)).isoformat()
        return niggle_note(self._open_niggles(monday, week_start))

    def maybe_body_ping(self, client: SlackClient) -> bool:
        """Send today's body check if it's due: a long-run or workout day, in the morning
        window, not queued on Slack, not sent yet and nothing logged today. True if sent."""
        conn = self.connect()
        try:
            local = self.now().astimezone(ZoneInfo(db.athlete_timezone(conn)))
            if not BODY_HOUR <= local.hour < BODY_LAST_HOUR:
                return False
            day = local.date().isoformat()
            session = reports.planned_session(db.load_accepted_plan(conn), day)
            if not session or session["kind"] not in BODY_SESSION_KINDS:
                return False
            if slack_schedule.body_logged(conn, day) or slack_schedule.queued(conn, BODY_KIND, day):
                return False  # she already checked in, or Slack has it queued
            sent = conn.execute(
                "SELECT 1 FROM notifications WHERE kind = ? AND date = ?", (BODY_KIND, day)
            ).fetchone()
            if sent:
                return False  # already sent live today
            channel = client.conversations_open(users=self.allowed_user)["channel"]["id"]
            client.chat_postMessage(channel=channel, text=body_ping_text())
            reports.mark_notified(conn, BODY_KIND, day)  # after the post: a failed one retries
            return True
        finally:
            conn.close()

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
                convo.turns, convo.pending, convo.more = [], None, []
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
        hr_line = start_hr_pace_line(HR_PACE_DIR, str(local.tzinfo), sunday.isoformat())
        monday = (sunday - timedelta(days=6)).isoformat()
        missed = self._missed_debrief(monday)
        niggles = self._open_niggles(monday, sunday.isoformat())
        channel = client.conversations_open(users=self.allowed_user)["channel"]["id"]
        convo = self._conversation(channel)
        with convo.lock, self._saving(convo):
            convo.turns, convo.pending, convo.more = [], None, []
            prompt = weekly_prompt(sunday.isoformat(), synced, missed, niggles)
            answer = self._answer(prompt, convo)
            text = WEEKLY_FAILED if answer == FAILED else to_mrkdwn(answer)
            if answer != FAILED and (trend := hr_line()):
                text += "\n\n" + trend
            if answer != FAILED:
                text += self._weekly_weather(coming)
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

    forecast = staticmethod(_live_forecast)  # tests swap in a fake; never the network there

    def _weekly_weather(self, week: dict[str, Any] | None) -> str:
        """The forecast block for the live Sunday check-in, or "" (no runs, no location, any
        failure). It can't stop the check-in and holds it up by one set of requests at most,
        however many runs the week has: nothing here raises, the responses are fetched once and
        reused for every run day, and after the first miss no more requests are made."""
        try:
            return self._conditions_block(week)
        except Exception as exc:  # the type only: a message could hold a URL
            log.warning("weather block skipped (%s)", type(exc).__name__)
            return ""

    def _conditions_block(self, week: dict[str, Any] | None) -> str:
        runs = [s for s in (week or {}).get("sessions", []) if s["kind"] in WEATHER_KINDS]
        if not runs:
            return ""
        conn = self.connect()
        try:
            timezone = db.athlete_timezone(conn)
        finally:
            conn.close()
        lines: list[str] = []
        results: list[dict[str, Any]] = []
        nws = True
        bodies: dict[str, bytes] = {}  # this message only: one request serves every run day
        for run in runs:  # one lookup per run day, in date order
            result = self.forecast(
                timezone=timezone,
                date=run["date"],
                hour=DEFAULT_MORNING_HOUR,
                hours=1,
                now=self.now(),
                nws=nws,
                reuse=bodies,
            )
            if not result.get("ok"):
                break  # past the forecast (so are the later runs) or a failure: stop asking
            nws = nws and bool(result["nws"]["ok"])  # NWS missed once: don't wait on it again
            results.append(result)
            lines.append(
                f"• {run['weekday'][:3]} {run['date'][5:]} morning "
                f"({hour_label(DEFAULT_MORNING_HOUR)}): "
                f"{weather.conditions_text(result['hours'][0])}"
            )
        if not lines:
            return ""
        return "\n\n" + "\n".join([WEATHER_HEADER, *lines, attribution_line(results)])

    def _weather(
        self,
        channel: str,
        client: SlackClient,
        place: str,
        placeholder: str | None = None,
        tries: int = 1,
    ) -> None:
        """Her "weather" / "weather at PLACE" (`place` "" = the default location; otherwise a
        named place or one looked up by name): conditions for the next plan run, answered in
        code with no coach call. Tracked like a sync, so a restart mid-lookup finishes it."""
        if placeholder is None:
            placeholder = client.chat_postMessage(channel=channel, text=CHECKING_WEATHER)["ts"]
        self._track("weather", channel, placeholder, tries, place)
        try:
            try:
                text = self._weather_reply(place or None)
            except Exception as exc:
                log.warning("weather reply failed (%s)", type(exc).__name__)
                text = WEATHER_FAILED
            _finish(client, channel, placeholder, text)
        finally:
            self._untrack()

    def _weather_reply(self, place: str | None) -> str:
        conn = self.connect()
        try:
            timezone = db.athlete_timezone(conn)
            plan = db.load_accepted_plan(conn)
            ran = {
                date_in_timezone(parse_instant(a.started_at), timezone)
                for a in db.load_activities(conn)
            }
        finally:
            conn.close()
        local = self.now().astimezone(ZoneInfo(timezone))
        for offset in range(weather.FORECAST_DAYS):
            day = local.date() + timedelta(days=offset)
            if offset == 0 and day.isoformat() in ran:
                continue  # today's run is already imported: the next one is what she means
            session = reports.planned_session(plan, day.isoformat())
            if session and session["kind"] in WEATHER_KINDS:
                break
        else:
            return NO_RUN_SOON
        hour = DEFAULT_MORNING_HOUR
        when = f"morning ({hour_label(hour)})"
        if day == local.date() and local.hour > hour:  # today's morning has passed: from now
            hour = local.hour
            when = hour_label(hour)
        result = self.forecast(  # near: a named place if it is exactly one, else looked up
            timezone=timezone, date=day.isoformat(), hour=hour, hours=1, near=place, now=self.now()
        )
        if not result.get("ok"):
            reason = _mrkdwn(str(result.get("reason", "no reason given")))
            if result.get("place_source") == weather.SOURCE_LOOKUP:
                known = ", ".join(result.get("known_places") or []) or "none set"
                return f"Couldn't look up that place: {reason}. Named places: {known}."
            return f"Couldn't get the forecast: {reason}."
        miles = round(session["distance_meters"] / tools.MI_METERS, 1)
        where = (
            "" if result["place"] == weather.DEFAULT_PLACE else f" at {_mrkdwn(result['place'])}"
        )
        return (
            f"*{reports.WEEKDAYS[session['day']][:3]} {day.isoformat()[5:]}: {miles:g} mi "
            f"{session['kind']}, {when}{where}*\n"
            f"{weather.conditions_text(result['hours'][0])}\n{attribution_line([result])}"
        )

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

    def _actions(
        self, conn: sqlite3.Connection, notes: list[str], forecasts: list[dict[str, Any]]
    ) -> tools.Actions:
        """The coach's channel actions for one reply (`tools.ACTION_NAMES`): the same read-only
        sync and forecast the bot runs in code. They never raise. A possible duplicate is never
        imported here; `notes` gets the lines she should see, `forecasts` what must be credited."""

        def sync_runs() -> dict[str, Any]:
            try:
                report = self.sync(conn, self.now())
            except coros.CorosError as exc:
                return {"ok": False, "reason": str(exc)}
            except Exception:  # network or SDK surprises: the coach says it didn't run
                log.exception("COROS sync asked for by the coach failed")
                return {"ok": False, "reason": SYNC_ACTION_FAILED}
            if report.pending:
                notes.append(SYNC_DUPLICATES)
            if report.not_downloaded:
                notes.append(SYNC_LEFT.format(n=report.not_downloaded))
            return {
                "ok": True,
                "from": report.start,
                "to": report.end,
                "new_runs": report.imported,
                "already_imported": report.already_present,
                "possible_duplicates_not_imported": len(report.pending),
                "not_downloaded_yet": report.not_downloaded,
                "error": report.error,
                "next": "Call summary for the week's totals.",
            }

        def get_weather(
            date: str, hour: int | None = None, place: str | None = None
        ) -> dict[str, Any]:
            try:
                result = self.forecast(
                    timezone=db.athlete_timezone(conn),
                    date=date,
                    hour=DEFAULT_MORNING_HOUR if hour is None else hour,
                    hours=WEATHER_ACTION_HOURS,
                    near=place or None,
                    now=self.now(),
                )
            except Exception as exc:
                log.warning("forecast asked for by the coach failed (%s)", type(exc).__name__)
                return {"ok": False, "reason": WEATHER_FAILED}
            if not result.get("ok"):
                failed = {"ok": False, "reason": str(result.get("reason", "no reason given"))}
                if result.get("known_places") is not None:
                    failed["known_places"] = result["known_places"]
                return failed
            forecasts.append(result)
            return {
                "ok": True,
                "date": date,
                "place": result["place"],
                "start_hour_assumed": hour is None,
                "hours": result["hours"],
                "note": "Conditions only, local time. The app adds the data credit.",
            }

        return {"sync_runs": sync_runs, "get_weather": get_weather}

    def _answer(self, text: str, convo: Conversation) -> str:
        conn = self.connect()
        history = list(convo.turns)
        notes: list[str] = []  # sync notes for her, added in code
        forecasts: list[dict[str, Any]] = []  # forecasts the coach read: credited in code
        try:
            coach = self.coach_factory(conn)
            if isinstance(coach, Coach):  # fakes in tests have no tools
                coach.actions = self._actions(conn, notes, forecasts)
            reply = coach.ask(text, history=history)
            messages = list(getattr(reply, "messages", []))[len(history) :]
            entry = proposed_entry(messages)
            niggle = proposed_body(messages)
            week = proposed_week(messages)
            week_rejected = week is not None and self._revised_week(conn, week)[0] is None
            if week_rejected:
                week = None
        except CoachError:
            log.warning("coach could not finish a Slack reply")
            return FAILED
        except Exception:  # API/network errors: keep the bot up, say so in Slack
            log.exception("coach failed on a Slack message")
            return FAILED
        finally:
            conn.close()
        answer = reply.text or FAILED
        if answer == FAILED:
            niggle = None  # the bot shows a body entry before it can be saved: not shown, not kept
        # One yes can save one thing. The first of these is kept; she is told about the rest.
        proposals: list[tuple[str, Pending]] = [
            (name, proposal)
            for name, proposal in (
                ("body log entry", niggle),
                ("week", week),
                ("fueling entry", entry),
            )
            if proposal is not None
        ]
        both = niggle is not None and entry is not None  # these two wait together
        lost: list[str] = []  # entries that waited for her yes and are now replaced: said in code
        if proposals:
            new: list[Pending] = [proposals[0][1], *([entry] if both else [])]
            lost = not_saved_lines([convo.pending, *convo.more], new)
            convo.pending, convo.more = new[0], new[1:]
        if answer != FAILED:
            if niggle is not None:
                if week is not None:  # the week isn't the thing being confirmed now
                    answer = _SAVE_ASK.sub("", answer)
                if entry is not None:
                    answer = ask_to_log_both(answer, niggle, entry)
                else:
                    answer = ask_to_log_body(answer, niggle)
            elif week is not None:
                answer = ask_to_save_week(answer)
            dropped = [name for name, item in proposals[1:] if not (both and item is entry)]
            if dropped:
                kept = "body log entry and the fueling entry" if both else proposals[0][0]
                answer += "\n\n" + ONE_AT_A_TIME.format(
                    kept=kept, dropped=" and the ".join(dropped)
                )
            if forecasts:
                answer += "\n\n" + credit_line(forecasts)
            if notes:
                answer += "\n\n" + "\n".join(dict.fromkeys(notes))
        # Said in code and kept out of the coach's history: what is (or isn't) still open.
        tail = ""
        if answer != FAILED and not proposals:
            if week_rejected and isinstance(convo.pending, WeekProposal):
                # A yes must never save an older week under a reply that describes another.
                convo.pending = None
                tail = "\n\n" + WEEK_REJECTED
            elif waiting := still_waiting(convo):
                tail = "\n\n" + waiting
        convo.turns += [
            {"role": "user", "content": text},
            {"role": "assistant", "content": answer},
        ]
        convo.turns = convo.turns[-2 * MAX_TURNS :]
        self._save_turns(convo)
        if lost:
            tail += "\n\n" + "\n".join(lost)
        return answer + tail


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
            checks = (bot.maybe_schedule, bot.maybe_body_ping, bot.maybe_ping, bot.maybe_weekly)
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
