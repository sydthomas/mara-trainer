"""Slack channel for the coach: DMs in, coach answers out, over Socket Mode (no public URL).

- One runner for now: only `SLACK_ALLOWED_USER_ID` gets answers; anyone else gets a short note.
- Setup mode (no `SLACK_ALLOWED_USER_ID` yet): the bot only tells each sender their own member ID,
  so the runner can find it without the Slack profile page. The coach never runs in this mode.
- Each DM keeps a short text-only history (last `MAX_TURNS` exchanges, reset after `IDLE_RESET`
  of quiet), so follow-ups work without resending tool output.
- One message at a time per DM; a placeholder reply is posted, then edited with the answer.
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
from typing import Any, Protocol

from mara_trainer import db

from .agent import Coach, CoachError

log = logging.getLogger(__name__)

MAX_TURNS = 10
IDLE_RESET = 6 * 60 * 60  # seconds
THINKING = "On it, checking your plan and sources..."
NOT_ALLOWED = "Sorry, this coach is private for now."
FAILED = "Sorry, I couldn't answer that just now. Try again in a minute."
REQUIRED_ENV = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_ALLOWED_USER_ID", "ANTHROPIC_API_KEY")
SETUP_ENV = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")


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


class Asker(Protocol):
    def ask(self, question: str, history: list[dict[str, Any]] | None = None) -> Any: ...


def to_mrkdwn(text: str) -> str:
    """Markdown from the coach -> Slack mrkdwn: bold, headings, links, bullets."""
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    return re.sub(r"^(\s*)[-*] ", r"\1• ", text, flags=re.MULTILINE)


@dataclass
class Conversation:
    turns: list[dict[str, Any]] = field(default_factory=list)
    last_seen: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


class SlackCoach:
    def __init__(
        self,
        allowed_user: str,
        coach_factory: Callable[[sqlite3.Connection], Asker] | None = None,
        connect: Callable[[], sqlite3.Connection] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.allowed_user = allowed_user
        self.coach_factory = coach_factory or (lambda conn: Coach(conn))
        self.connect = connect or _connect
        self.clock = clock
        self.conversations: dict[str, Conversation] = {}
        self._guard = threading.Lock()

    def _conversation(self, channel: str) -> Conversation:
        with self._guard:
            convo = self.conversations.setdefault(channel, Conversation())
        now = self.clock()
        if convo.turns and now - convo.last_seen > IDLE_RESET:
            convo.turns = []
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
            placeholder = client.chat_postMessage(channel=channel, text=THINKING)
            answer = self._answer(text, convo)
            client.chat_update(channel=channel, ts=placeholder["ts"], text=to_mrkdwn(answer))

    def _answer(self, text: str, convo: Conversation) -> str:
        conn = self.connect()
        try:
            reply = self.coach_factory(conn).ask(text, history=list(convo.turns))
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
        return answer


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

    SocketModeHandler(app, env["SLACK_APP_TOKEN"]).start()  # type: ignore[no-untyped-call]
