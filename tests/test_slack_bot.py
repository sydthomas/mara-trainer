from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from mara_coach import slack_bot
from mara_coach.agent import CoachError, CoachReply
from mara_coach.cli import app
from mara_coach.slack_bot import SlackCoach, to_mrkdwn
from mara_trainer import db

SYDNEY = "U123"


class FakeClient:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:  # noqa: N802
        self.posted.append(kwargs)
        return {"ts": f"ts{len(self.posted)}"}

    def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self.updated.append(kwargs)
        return {}


class FakeCoach:
    def __init__(self, answers: list[str | Exception]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    def ask(self, question: str, history: list[dict[str, Any]] | None = None) -> CoachReply:
        self.calls.append((question, list(history or [])))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return CoachReply(answer, "end_turn")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _dm(text: str, user: str = SYDNEY, **extra: Any) -> dict[str, Any]:
    return {"type": "message", "channel_type": "im", "channel": "D1", "user": user, "text": text}


def _bot(coach: FakeCoach, clock: Clock | None = None) -> SlackCoach:
    def connect() -> sqlite3.Connection:
        conn = db.connect(":memory:")
        db.migrate(conn)
        return conn

    return SlackCoach(SYDNEY, lambda conn: coach, connect, clock or Clock())


def test_answers_sydney_with_placeholder_then_update() -> None:
    coach, client = (
        FakeCoach(["**Nice** run. See [Jeukendrup](https://example.org/x)."]),
        FakeClient(),
    )
    _bot(coach).handle(_dm("How was my fueling?"), client)
    assert client.posted == [{"channel": "D1", "text": slack_bot.THINKING}]
    assert client.updated == [
        {
            "channel": "D1",
            "ts": "ts1",
            "text": "*Nice* run. See <https://example.org/x|Jeukendrup>.",
        }
    ]
    assert coach.calls == [("How was my fueling?", [])]


def test_follow_up_gets_text_history() -> None:
    coach, client = FakeCoach(["First answer", "Second answer"]), FakeClient()
    bot = _bot(coach)
    bot.handle(_dm("Q1"), client)
    bot.handle(_dm("Q2"), client)
    assert coach.calls[1][1] == [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "First answer"},
    ]


def test_history_is_capped_and_resets_after_idle() -> None:
    clock = Clock()
    coach = FakeCoach([f"A{i}" for i in range(slack_bot.MAX_TURNS + 2)])
    bot, client = _bot(coach, clock), FakeClient()
    for i in range(slack_bot.MAX_TURNS + 1):
        bot.handle(_dm(f"Q{i}"), client)
    assert len(bot.conversations["D1"].turns) == 2 * slack_bot.MAX_TURNS
    clock.now += slack_bot.IDLE_RESET + 1
    bot.handle(_dm("fresh"), client)
    assert coach.calls[-1] == ("fresh", [])


@pytest.mark.parametrize(
    "event",
    [
        {**_dm("hi"), "channel_type": "channel"},
        {**_dm("hi"), "subtype": "message_changed"},
        {**_dm("hi"), "bot_id": "B1"},
        _dm("   "),
    ],
)
def test_ignores_non_dm_bot_edits_and_empty(event: dict[str, Any]) -> None:
    coach, client = FakeCoach([]), FakeClient()
    _bot(coach).handle(event, client)
    assert client.posted == [] and coach.calls == []


def test_other_users_get_a_private_note_and_no_coach_call() -> None:
    coach, client = FakeCoach([]), FakeClient()
    _bot(coach).handle(_dm("hi", user="U999"), client)
    assert client.posted == [{"channel": "D1", "text": slack_bot.NOT_ALLOWED}]
    assert coach.calls == []


@pytest.mark.parametrize("error", [CoachError("no answer"), RuntimeError("network down")])
def test_errors_become_a_friendly_reply_without_history(error: Exception) -> None:
    coach, client = FakeCoach([error]), FakeClient()
    bot = _bot(coach)
    bot.handle(_dm("Q"), client)
    assert client.updated[0]["text"] == slack_bot.FAILED
    assert bot.conversations["D1"].turns == []


def test_to_mrkdwn_headings_and_bullets() -> None:
    assert to_mrkdwn("## Plan\n- one\n  - two") == "*Plan*\n• one\n  • two"


def test_missing_env_names_only() -> None:
    env = {"SLACK_BOT_TOKEN": "x", "SLACK_APP_TOKEN": " ", "ANTHROPIC_API_KEY": "y"}
    assert slack_bot.missing_env(env) == ["SLACK_APP_TOKEN", "SLACK_ALLOWED_USER_ID"]


def test_cli_slack_run_fails_without_settings(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mara_coach.cli.load_env", lambda: None)  # never read the real .env
    for name in slack_bot.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    r = CliRunner().invoke(app, ["slack", "run"])
    assert r.exit_code == 1
    assert json.loads(r.output)["missing"] == list(slack_bot.SETUP_ENV)  # setup mode needs less


def test_setup_mode_replies_with_own_member_id_only() -> None:
    client = FakeClient()
    slack_bot.handle_setup(_dm("hi", user="U777"), client)
    assert client.posted == [{"channel": "D1", "text": slack_bot.setup_reply("U777")}]
    assert "U777" in client.posted[0]["text"]
    slack_bot.handle_setup({**_dm("hi"), "channel_type": "channel"}, client)
    slack_bot.handle_setup({**_dm("hi"), "bot_id": "B1"}, client)
    assert len(client.posted) == 1


def test_setup_mode_switch() -> None:
    assert slack_bot.setup_mode({}) and slack_bot.setup_mode({"SLACK_ALLOWED_USER_ID": " "})
    assert not slack_bot.setup_mode({"SLACK_ALLOWED_USER_ID": SYDNEY})
