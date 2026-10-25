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
from mara_trainer import db, fueling

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


# ------------------------------------------------------------------ long-run debrief

from datetime import UTC, datetime  # noqa: E402
from types import SimpleNamespace as NS  # noqa: E402

from mara_coach import tools  # noqa: E402
from mara_trainer import reports  # noqa: E402

PROPOSAL = {
    "date": "2026-11-07",
    "distance": 10,
    "unit": "mi",
    "duration": "1:30:00",
    "during": "7 chews",
    "carbs_grams": 27,
    "stomach": 5,
}
PLAN = {
    "weeks": [
        {
            "week": 1,
            "start_date": "2026-11-02",
            "sessions": [
                {"day": 0, "kind": "rest", "distance_meters": 0},
                {"day": 6, "kind": "long", "distance_meters": 16093},
            ],
        }
    ]
}


class _NoClose(sqlite3.Connection):
    def close(self) -> None:  # the bot closes after each message; keep the shared DB open
        pass


class SharedDB:
    """One in-memory DB for the whole test; the bot's close() is a no-op."""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:", factory=_NoClose, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        db.init_db(self.conn, "America/Los_Angeles")

    def __call__(self) -> sqlite3.Connection:
        return self.conn


class ProposingCoach(FakeCoach):
    """Replies like the real agent after calling propose_fuel_entry."""

    def __init__(self, answers: list[str | Exception], propose: dict[str, Any] | None) -> None:
        super().__init__(answers)
        self.propose = propose

    def ask(self, question: str, history: list[dict[str, Any]] | None = None) -> CoachReply:
        reply = super().ask(question, history)
        msgs: list[dict[str, Any]] = [*(history or []), {"role": "user", "content": question}]
        if self.propose is not None:
            block = NS(type="tool_use", name="propose_fuel_entry", input=self.propose, id="t1")
            msgs.append({"role": "assistant", "content": [block]})
            self.propose = None
        return CoachReply(reply.text, "end_turn", messages=msgs)


class SlackFake(FakeClient):
    def conversations_open(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"users": SYDNEY}
        return {"channel": {"id": "D1"}}


def _debrief_bot(coach: FakeCoach, shared: Any, when: datetime, clock: Clock | None = None):  # type: ignore[no-untyped-def]
    return SlackCoach(SYDNEY, lambda conn: coach, shared, clock or Clock(), lambda: when)


def _sat(hour_utc: int) -> datetime:  # 2026-11-07 is a Saturday; LA is UTC-8 then
    return datetime(2026, 11, 7, hour_utc, 0, tzinfo=UTC)


def test_planned_session_maps_dates_to_weekdays() -> None:
    assert reports.planned_session(PLAN, "2026-11-07")["kind"] == "long"  # type: ignore[index]
    assert reports.planned_session(PLAN, "2026-11-08")["kind"] == "rest"  # type: ignore[index]
    assert reports.planned_session(PLAN, "2026-11-09") is None
    assert reports.planned_session(None, "2026-11-07") is None


def test_check_in_sent_once_at_ten_on_long_run_days() -> None:
    shared, client = SharedDB(), SlackFake()
    db.save_plan(shared.conn, PLAN)
    assert not _debrief_bot(FakeCoach([]), shared, _sat(17)).maybe_ping(client)  # 9am LA
    bot = _debrief_bot(FakeCoach([]), shared, _sat(18))  # 10am LA
    assert bot.maybe_ping(client)
    assert client.posted == [{"channel": "D1", "text": slack_bot.ping_text(10.0)}]
    assert not bot.maybe_ping(client)  # once a day, even after a restart:
    assert not _debrief_bot(FakeCoach([]), shared, _sat(20)).maybe_ping(client)
    assert len(client.posted) == 1


def test_no_check_in_without_long_run_or_late() -> None:
    shared, client = SharedDB(), SlackFake()
    assert not _debrief_bot(FakeCoach([]), shared, _sat(18)).maybe_ping(client)  # no plan
    db.save_plan(shared.conn, PLAN)
    sunday = datetime(2026, 11, 8, 18, tzinfo=UTC)
    assert not _debrief_bot(FakeCoach([]), shared, sunday).maybe_ping(client)  # rest day
    assert not _debrief_bot(FakeCoach([]), shared, _sat(23).replace(day=8, hour=4)).maybe_ping(
        client
    )  # 8pm LA Saturday is too late
    assert client.posted == []


def test_debrief_proposal_saved_only_on_yes() -> None:
    shared, client = SharedDB(), SlackFake()
    db.save_plan(shared.conn, PLAN)
    coach = ProposingCoach(["10 mi in 1:30, 18 g/h. Save it?", "Nice work."], PROPOSAL)
    bot = _debrief_bot(coach, shared, _sat(18))
    bot.maybe_ping(client)
    bot.handle(_dm("did 10 in 1:30, 7 chews, stomach great"), client)
    assert coach.calls[0][0].startswith("(Long-run debrief for 2026-11-07: 10 mi long run")
    assert fueling.list_entries(shared.conn)["count"] == 0  # proposed, not saved
    assert bot.conversations["D1"].pending is not None
    bot.handle(_dm("yes"), client)
    saved = fueling.list_entries(shared.conn)
    assert saved["count"] == 1 and saved["entries"][0]["carbs_grams"] == 27
    assert any("Saved to your fuel log" in m["text"] for m in client.posted)
    assert coach.calls[-1][0] == slack_bot.SAVE_FOLLOW_UP
    convo = bot.conversations["D1"]
    assert convo.pending is None and convo.debrief is None


def test_debrief_no_drops_the_entry() -> None:
    shared, client = SharedDB(), SlackFake()
    coach = ProposingCoach(["Save it?"], PROPOSAL)
    bot = _debrief_bot(coach, shared, _sat(18))
    bot.handle(_dm("10 miles 1:30"), client)
    bot.handle(_dm("no"), client)
    assert fueling.list_entries(shared.conn)["count"] == 0
    assert client.posted[-1]["text"] == slack_bot.CANCELLED


def test_corrections_go_to_the_coach_and_replace_the_proposal() -> None:
    shared, client = SharedDB(), SlackFake()
    coach = ProposingCoach(["Save it?", "Updated. Save it?"], PROPOSAL)
    bot = _debrief_bot(coach, shared, _sat(18))
    bot.handle(_dm("10 miles 1:30"), client)
    coach.propose = {**PROPOSAL, "stomach": 3}
    bot.handle(_dm("actually stomach was a 3"), client)
    assert len(coach.calls) == 2
    pending = bot.conversations["D1"].pending
    assert pending is not None and pending.stomach == 3


@pytest.mark.parametrize(
    "bad",
    [
        {**PROPOSAL, "stomach": 9},
        {**PROPOSAL, "calories": 300},
        {**PROPOSAL, "duration": "ninety minutes"},
    ],
)
def test_invalid_proposals_are_never_pending(bad: dict[str, Any]) -> None:
    assert (
        slack_bot.proposed_entry(
            [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "propose_fuel_entry", "input": bad}],
                }
            ]
        )
        is None
    )


def test_propose_tool_validates_but_never_saves(conn: sqlite3.Connection) -> None:
    got = tools.call_tool(conn, "propose_fuel_entry", PROPOSAL)
    assert got["ok"] and got["saved"] is False and got["carbs_per_hour"] == 18.0
    assert got["entry"]["distance_meters"] == 16093 and got["entry"]["duration_seconds"] == 5400
    assert fueling.list_entries(conn)["count"] == 0
    bad = tools.call_tool(conn, "propose_fuel_entry", {**PROPOSAL, "duration": "1h30"})
    assert bad["ok"] is False and "H:MM:SS" in bad["problems"][0]
    with pytest.raises(tools.ToolError, match="unexpected"):
        tools.call_tool(conn, "propose_fuel_entry", {**PROPOSAL, "weight_kg": 60})
