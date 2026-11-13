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
from mara_trainer import db, fueling, imports

SYDNEY = "U123"


class FakeClient:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []
        self.fail_chat_delete = False

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:  # noqa: N802
        self.posted.append(kwargs)
        return {"ts": f"ts{len(self.posted)}"}

    def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self.updated.append(kwargs)
        return {}

    def chat_delete(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail_chat_delete:
            raise RuntimeError("cant_delete_message")
        self.deleted.append(kwargs)
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


def test_answer_is_a_new_message_and_the_placeholder_goes() -> None:
    """A new message, not an edit: Slack doesn't notify on edits."""
    coach, client = (
        FakeCoach(["**Nice** run. See [Jeukendrup](https://example.org/x)."]),
        FakeClient(),
    )
    _bot(coach).handle(_dm("How was my fueling?"), client)
    assert client.posted == [
        {"channel": "D1", "text": slack_bot.THINKING},
        {"channel": "D1", "text": "*Nice* run. See <https://example.org/x|Jeukendrup>."},
    ]
    assert client.deleted == [{"channel": "D1", "ts": "ts1"}]
    assert client.updated == []
    assert coach.calls == [("How was my fueling?", [])]


def test_placeholder_points_to_the_answer_if_it_cant_be_deleted() -> None:
    client = FakeClient()
    client.fail_chat_delete = True
    _bot(FakeCoach(["A"])).handle(_dm("Q"), client)
    assert client.posted[-1]["text"] == "A"
    assert client.updated == [{"channel": "D1", "ts": "ts1", "text": slack_bot.ANSWERED_BELOW}]


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
    assert client.posted[-1]["text"] == slack_bot.FAILED
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


# ------------------------------------------------------------------ socket watchdog


def test_socket_watch_trips_only_after_errors_keep_coming() -> None:
    watch = slack_bot.SocketWatch()
    assert not watch.stuck(1000.0)
    assert not watch.stuck(1000.0 + slack_bot.STUCK_AFTER - 1)
    assert watch.stuck(1000.0 + slack_bot.STUCK_AFTER)


def test_socket_watch_quiet_gap_starts_over() -> None:
    watch = slack_bot.SocketWatch()
    assert not watch.stuck(1000.0)
    later = 1000.0 + slack_bot.STUCK_AFTER + slack_bot.QUIET_RESET + 1  # e.g. the laptop slept
    assert not watch.stuck(later)
    assert not watch.stuck(later + 1)


def test_exit_when_stuck_exits_so_launchd_restarts() -> None:
    clock = iter([1000.0, 1000.0 + slack_bot.STUCK_AFTER])
    exits: list[int] = []
    on_error = slack_bot.exit_when_stuck(slack_bot.SocketWatch(), lambda: next(clock), exits.append)
    on_error(BrokenPipeError())
    assert exits == []
    on_error(BrokenPipeError())
    assert exits == [1]


# ------------------------------------------------------------------ long-run debrief

from datetime import UTC, datetime, timedelta  # noqa: E402
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


def _no_new_runs(conn: sqlite3.Connection, now: datetime) -> Any:
    from mara_coach import coros

    return coros.SyncReport("2026-11-02", "2026-11-08")


def _debrief_bot(
    coach: FakeCoach,
    shared: Any,
    when: datetime,
    clock: Clock | None = None,
    sync: Any = _no_new_runs,
):  # type: ignore[no-untyped-def]
    return SlackCoach(SYDNEY, lambda conn: coach, shared, clock or Clock(), lambda: when, sync)


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


# ------------------------------------------------------------------ Sunday weekly check-in


def _sun(hour_utc: int, day: int = 8) -> datetime:  # 2026-11-08 is a Sunday; LA is UTC-8
    return datetime(2026, 11, day, hour_utc, 0, tzinfo=UTC)


def test_weekly_check_in_sunday_six_pm_once() -> None:
    shared, client = SharedDB(), SlackFake()
    db.save_plan(shared.conn, PLAN)
    coach = FakeCoach(["**Last week** 10 mi long run. **This week** easy."])
    assert not _debrief_bot(coach, shared, _sun(1, 9)).maybe_weekly(client)  # Sun 5pm LA
    bot = _debrief_bot(coach, shared, _sun(2, 9))  # Sun 6pm LA (Mon 02:00 UTC)
    assert bot.maybe_weekly(client)
    assert coach.calls[0][0].startswith("(Weekly check-in, Sunday 2026-11-08.")
    assert "plan_week for 2026-11-09" in coach.calls[0][0]
    assert client.posted == [  # week 1 is the last plan week: nothing coming to push
        {"channel": "D1", "text": "*Last week* 10 mi long run. *This week* easy."}
    ]
    assert not bot.maybe_weekly(client)  # once per Sunday
    assert bot.conversations["D1"].turns[-1]["role"] == "assistant"  # her reply continues it


def test_weekly_check_in_skips_weeks_outside_the_plan_and_other_days() -> None:
    shared, client = SharedDB(), SlackFake()
    coach = FakeCoach([])
    assert not _debrief_bot(coach, shared, _sun(2, 9)).maybe_weekly(client)  # no plan
    db.save_plan(shared.conn, PLAN)
    far = datetime(2027, 1, 11, 2, tzinfo=UTC)  # a Sunday 6pm LA, weeks after the plan
    assert not _debrief_bot(coach, shared, far).maybe_weekly(client)
    saturday = _sat(2).replace(day=8)  # Sat 6pm LA
    assert not _debrief_bot(coach, shared, saturday).maybe_weekly(client)
    assert client.posted == [] and coach.calls == []


def test_weekly_check_in_preview_before_the_plan_starts() -> None:
    shared, client = SharedDB(), SlackFake()
    db.save_plan(shared.conn, PLAN)  # starts Monday 2026-11-02
    coach = FakeCoach(["Week 1 preview"])
    sunday_before = datetime(2026, 11, 2, 2, tzinfo=UTC)  # Sun 2026-11-01 6pm LA
    assert _debrief_bot(coach, shared, sunday_before).maybe_weekly(client)
    assert client.posted[-1]["text"] == "Week 1 preview"  # no "push in Claude Code" nudge
    assert (
        shared.conn.execute(
            "SELECT value FROM slack_state WHERE key = ?", (slack_bot.WEEKLY_KEY,)
        ).fetchone()[0]
        == "2026-11-02"
    )  # her reply writes week 1


def test_weekly_check_in_failure_posts_a_note_once() -> None:
    shared, client = SharedDB(), SlackFake()
    db.save_plan(shared.conn, PLAN)
    coach = FakeCoach([RuntimeError("API down")])
    bot = _debrief_bot(coach, shared, _sun(2, 9))
    assert bot.maybe_weekly(client)
    assert client.posted[-1]["text"] == slack_bot.WEEKLY_FAILED
    assert not bot.maybe_weekly(client)  # no retry loop


def test_plan_week_tool(conn: sqlite3.Connection) -> None:
    db.save_plan(conn, PLAN)
    week = tools.call_tool(conn, "plan_week", {"date": "2026-11-05"})
    assert week["week"] == 1 and week["of_weeks"] == 1
    assert [s["weekday"] for s in week["sessions"]] == ["Saturday", "Sunday"]
    assert week["sessions"][0] == {
        "date": "2026-11-07",
        "weekday": "Saturday",
        "kind": "long",
        "distance_meters": 16093,
        "miles": 10.0,
        "details": None,
    }
    assert tools.call_tool(conn, "plan_week", {"date": "2027-01-01"})["week"] is None


# ------------------------------------------------------------------ Sunday: sync, then recap


def _import_run(conn: sqlite3.Connection, start: datetime) -> None:
    now = start + timedelta(hours=2)
    draft = imports.make_draft("fit", start, 16093, 5400, now)
    imports.commit(conn, imports.DecodedFile("fit", draft.fingerprint, [draft]), "x.fit")


def test_weekly_check_in_syncs_runs_first() -> None:
    """No 5pm reminder: the check-in pulls the week's runs itself, then recaps them."""
    from mara_coach import coros

    shared, client, order = SharedDB(), SlackFake(), []
    db.save_plan(shared.conn, PLAN)

    def sync(conn: sqlite3.Connection, now: datetime) -> coros.SyncReport:
        order.append("sync")
        _import_run(conn, datetime(2026, 11, 7, 16, 0, tzinfo=UTC))
        return coros.SyncReport("2026-11-02", "2026-11-08", imported=["Sat 11/7: 10.00 mi"])

    class OrderedCoach(FakeCoach):
        def ask(self, question: str, history: list[dict[str, Any]] | None = None) -> CoachReply:
            order.append("coach")
            return super().ask(question, history)

    coach = OrderedCoach(["Recap"])
    assert _debrief_bot(coach, shared, _sun(2, 9), sync=sync).maybe_weekly(client)
    assert order == ["sync", "coach"]
    assert "Runs synced from COROS just now: 1 new." in coach.calls[0][0]
    assert [m["text"] for m in client.posted] == ["Recap"]  # one message, no sync report


@pytest.mark.parametrize(
    ("error", "note"),
    [
        ("sign_in", "_" + "COROS needs you to sign in again"),
        ("crash", slack_bot.SYNC_FAILED),
    ],
)
def test_weekly_check_in_goes_out_when_sync_fails(error: str, note: str) -> None:
    from mara_coach import coros

    shared, client = SharedDB(), SlackFake()
    db.save_plan(shared.conn, PLAN)

    def sync(conn: sqlite3.Connection, now: datetime) -> coros.SyncReport:
        raise coros.SignInNeeded if error == "sign_in" else RuntimeError("network")

    coach = FakeCoach(["Recap"])
    assert _debrief_bot(coach, shared, _sun(2, 9), sync=sync).maybe_weekly(client)
    assert "COROS sync failed" in coach.calls[0][0]
    text = client.posted[-1]["text"]
    assert text.startswith("Recap\n\n") and note in text


def test_weekly_check_in_points_to_duplicates_and_leftovers(tmp_path: Path) -> None:
    from mara_coach import coros

    shared, client = SharedDB(), SlackFake()
    db.save_plan(shared.conn, PLAN)
    pending = coros.Pending(tmp_path / "1.fit", ("k",), ("Sat 11/7",))
    report = coros.SyncReport("2026-11-02", "2026-11-08", pending=[pending], not_downloaded=2)
    bot = _debrief_bot(FakeCoach(["Recap"]), shared, _sun(2, 9), sync=lambda c, n: report)
    assert bot.maybe_weekly(client)
    text = client.posted[-1]["text"]
    assert slack_bot.SYNC_DUPLICATES in text and "2 run(s) didn't download" in text
    assert bot.conversations["D1"].pending is None  # her answer isn't taken as a yes to them


def test_reply_to_the_check_in_asks_for_a_proposal_through_monday() -> None:
    shared, client = SharedDB(), SlackFake()
    db.save_plan(shared.conn, PLAN)
    sunday_before = datetime(2026, 11, 2, 2, tzinfo=UTC)  # Sun 2026-11-01 6pm LA
    coach = FakeCoach(["Week 1 preview", "Here's the week", "Sure"])
    assert _debrief_bot(coach, shared, sunday_before).maybe_weekly(client)
    monday = datetime(2026, 11, 2, 17, tzinfo=UTC)  # Mon 9am LA: still her check-in reply
    _debrief_bot(coach, shared, monday).handle({**_dm("Busy Thursday"), "ts": "1"}, client)
    assert coach.calls[1][0] == slack_bot.weekly_reply("2026-11-02", "Busy Thursday")
    assert "propose_week" in coach.calls[1][0]
    tuesday = datetime(2026, 11, 3, 17, tzinfo=UTC)
    _debrief_bot(coach, shared, tuesday).handle({**_dm("Thanks"), "ts": "2"}, client)
    assert coach.calls[2][0] == "Thanks"  # Tuesday: plain chat again


TWO_WEEKS = {
    "weeks": [
        *PLAN["weeks"],
        {
            "week": 2,
            "start_date": "2026-11-09",
            "sessions": [
                {"day": 0, "kind": "rest", "distance_meters": 0},
                {"day": 6, "kind": "long", "distance_meters": 19312},
            ],
        },
    ]
}


def test_check_in_asks_how_the_week_felt() -> None:
    shared = SharedDB()
    db.save_plan(shared.conn, PLAN)
    _import_run(shared.conn, datetime(2026, 11, 7, 16, 0, tzinfo=UTC))
    fueling.add_entry(shared.conn, fueling.FuelEntry("2026-11-07", distance_meters=16093))
    coach = FakeCoach(["Recap"])
    _debrief_bot(coach, shared, _sun(2, 9)).maybe_weekly(SlackFake())
    assert "how did last week feel" in coach.calls[0][0]
    assert "isn't debriefed yet" not in coach.calls[0][0]  # Saturday is in the fuel log


def test_missed_saturday_debrief_folds_into_sunday() -> None:
    """No debrief Saturday: the check-in asks for it, her reply logs it (yes), then the week."""
    shared, client = SharedDB(), SlackFake()
    db.save_plan(shared.conn, TWO_WEEKS)
    coach = ProposingCoach(["Recap. How'd the week feel, and Saturday's 10?"], None)
    sunday = _debrief_bot(coach, shared, _sun(2, 9))  # Sun 6pm LA
    assert sunday.maybe_weekly(client)
    assert "My 10 mi long run on 2026-11-07 isn't debriefed yet" in coach.calls[0][0]
    later = _sun(4, 9)  # Sun 8pm LA
    coach.answers += ["Here's the entry. Save it?", "Nice fueling. Here's your week."]
    coach.propose = PROPOSAL
    bot = _debrief_bot(coach, shared, later)
    bot.handle({**_dm("Tired legs. Did 10 in 1:30, 7 chews"), "ts": "1"}, client)
    assert "propose the fuel entry first" in coach.calls[1][0]
    assert "Long-run debrief for 2026-11-07" in coach.calls[1][0]
    bot.handle({**_dm("yes"), "ts": "2"}, client)
    assert shared.conn.execute("SELECT count(*) FROM fuel_log").fetchone()[0] == 1
    assert coach.calls[2][0] == slack_bot.week_after_debrief("2026-11-09")


def test_declined_debrief_is_not_asked_again_on_sunday() -> None:
    shared = SharedDB()
    db.save_plan(shared.conn, TWO_WEEKS)
    db_conn = shared.conn
    from mara_trainer import reports as _reports

    _reports.mark_notified(db_conn, slack_bot.DEBRIEF_CLOSED, "2026-11-07")
    coach = FakeCoach(["Recap"])
    _debrief_bot(coach, shared, _sun(2, 9)).maybe_weekly(SlackFake())
    assert "isn't debriefed yet" not in coach.calls[0][0]


# ------------------------------------------------------------------ COROS sync from Slack

from mara_coach import coros  # noqa: E402


def _sync_bot(shared: Any, sync: Any) -> SlackCoach:
    return SlackCoach(SYDNEY, lambda conn: FakeCoach([]), shared, Clock(), lambda: _sun(4), sync)


def test_sync_runs_in_code_without_the_coach() -> None:
    shared, client, seen = SharedDB(), FakeClient(), []

    def sync(conn: sqlite3.Connection, now: datetime) -> coros.SyncReport:
        seen.append(now)
        _import_run(conn, datetime(2026, 11, 7, 16, 0, tzinfo=UTC))
        return coros.SyncReport("2026-11-02", "2026-11-08", imported=["Sat 11/7: 10.00 mi"])

    _sync_bot(shared, sync).handle(_dm("Sync my runs!"), client)
    assert seen == [_sun(4)]
    assert client.posted[0] == {"channel": "D1", "text": slack_bot.SYNCING}
    assert client.deleted == [{"channel": "D1", "ts": "ts1"}]
    text = client.posted[1]["text"]
    assert "Sat 11/7: 10.00 mi" in text and "This week: 1 runs, 10.00 mi" in text


@pytest.mark.parametrize("text", ["sync", "sync runs", "please sync my runs from COROS"])
def test_sync_phrases(text: str) -> None:
    assert slack_bot.SYNC.match(text)
    assert not slack_bot.SYNC.match("should I sync my runs before the long run?")


def test_sync_errors_become_a_reply() -> None:
    client = FakeClient()

    def sync(conn: sqlite3.Connection, now: datetime) -> coros.SyncReport:
        raise coros.SignInNeeded

    _sync_bot(SharedDB(), sync).handle(_dm("sync"), client)
    assert client.posted[-1]["text"] == coros.SIGN_IN_AGAIN


def _dup_report(tmp_path: Path) -> coros.SyncReport:
    path = tmp_path / "1.fit"
    path.write_bytes(b"")
    pending = coros.Pending(path, ("k",), ("Sat 11/7: 10.00 mi, 1:30:00",))
    return coros.SyncReport("2026-11-02", "2026-11-08", pending=[pending])


def test_duplicates_import_only_on_yes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    confirmed: list[Any] = []
    monkeypatch.setattr(coros, "import_confirmed", lambda conn, items: confirmed.append(items) or 1)
    shared, client = SharedDB(), FakeClient()
    bot = _sync_bot(shared, lambda conn, now: _dup_report(tmp_path))
    bot.handle({**_dm("sync"), "ts": "1"}, client)
    assert "Reply *yes*" in client.posted[-1]["text"]
    bot.handle({**_dm("yes"), "ts": "2"}, client)
    assert len(confirmed) == 1 and client.posted[-1]["text"] == "Imported 1 more run(s)."
    bot.handle({**_dm("sync"), "ts": "3"}, client)
    bot.handle({**_dm("no"), "ts": "4"}, client)
    assert len(confirmed) == 1 and client.posted[-1]["text"] == slack_bot.SKIPPED


# ------------------------------------------------------------------ restart-proof replies

NOON = _sun(20)  # Sun 2026-11-08 noon LA


def _leftover(shared: SharedDB, **job: Any) -> None:
    """What a bot killed mid-reply leaves behind."""
    job = {
        "kind": "answer",
        "channel": "D1",
        "placeholder": "ts9",
        "tries": 1,
        "question": "Q",
        "at": NOON.timestamp(),
        **job,
    }
    slack_bot._set_state(shared.conn, slack_bot.INFLIGHT_KEY, json.dumps(job))


def _inflight(shared: SharedDB) -> dict[str, Any] | None:
    raw = slack_bot._state(shared.conn, slack_bot.INFLIGHT_KEY)
    return None if raw is None else json.loads(raw)


def test_a_reply_is_recorded_while_it_is_written_then_cleared() -> None:
    shared, seen = SharedDB(), []

    class Peek(FakeCoach):
        def ask(self, question: str, history: list[dict[str, Any]] | None = None) -> CoachReply:
            seen.append(_inflight(shared))
            return super().ask(question, history)

    _debrief_bot(Peek(["A"]), shared, NOON).handle(_dm("How's my week?"), FakeClient())
    assert seen[0] is not None
    assert (seen[0]["question"], seen[0]["placeholder"], seen[0]["tries"]) == (
        "How's my week?",
        "ts1",
        1,
    )
    assert _inflight(shared) is None


def test_resume_finishes_a_cut_off_reply_and_removes_its_placeholder() -> None:
    shared, client, coach = SharedDB(), FakeClient(), FakeCoach(["Here's your week."])
    _leftover(shared)
    bot = _debrief_bot(coach, shared, NOON)
    assert bot.resume(client)
    assert coach.calls == [("Q", [])]
    assert client.posted == [{"channel": "D1", "text": "Here's your week."}]  # no new placeholder
    assert client.deleted == [{"channel": "D1", "ts": "ts9"}]
    assert _inflight(shared) is None and not bot.resume(client)


@pytest.mark.parametrize(
    "job",
    [{"tries": slack_bot.MAX_TRIES}, {"at": NOON.timestamp() - slack_bot.CATCH_UP_WINDOW - 1}],
)
def test_resume_gives_up_after_max_tries_or_when_stale(job: dict[str, Any]) -> None:
    shared, client, coach = SharedDB(), FakeClient(), FakeCoach([])
    _leftover(shared, **job)
    assert _debrief_bot(coach, shared, NOON).resume(client)
    assert coach.calls == []
    assert client.posted == [{"channel": "D1", "text": slack_bot.LOST}]
    assert client.deleted == [{"channel": "D1", "ts": "ts9"}]
    assert _inflight(shared) is None


def test_resume_reruns_a_cut_off_sync() -> None:
    shared, client = SharedDB(), FakeClient()
    _leftover(shared, kind="sync", question="")
    report = coros.SyncReport("2026-11-02", "2026-11-08", already_present=4)
    bot = SlackCoach(
        SYDNEY, lambda conn: FakeCoach([]), shared, Clock(), lambda: NOON, lambda conn, now: report
    )
    assert bot.resume(client)
    assert "all already imported" in client.posted[0]["text"]
    assert client.deleted == [{"channel": "D1", "ts": "ts9"}]
    assert _inflight(shared) is None


def test_history_survives_a_restart_until_idle_reset() -> None:
    shared = SharedDB()
    _debrief_bot(FakeCoach(["A1"]), shared, NOON).handle(_dm("Q1"), FakeClient())
    after_restart = FakeCoach(["A2"])
    _debrief_bot(after_restart, shared, NOON + timedelta(minutes=5)).handle(_dm("Q2"), FakeClient())
    assert after_restart.calls[0][1] == [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
    ]
    later = FakeCoach(["A3"])
    idle = NOON + timedelta(minutes=5, seconds=slack_bot.IDLE_RESET + 1)
    _debrief_bot(later, shared, idle).handle(_dm("Q3"), FakeClient())
    assert later.calls[0][1] == []


def test_debrief_yes_still_saves_after_a_restart() -> None:
    shared = SharedDB()
    proposing = ProposingCoach(["Here's the entry. Save it?"], PROPOSAL)
    _debrief_bot(proposing, shared, NOON).handle(_dm("10 mi, 1:30, 7 chews"), FakeClient())
    client = FakeClient()
    _debrief_bot(FakeCoach(["Nice work."]), shared, NOON).handle(_dm("yes"), client)  # restarted
    assert shared.conn.execute("SELECT count(*) FROM fuel_log").fetchone()[0] == 1
    assert client.posted[0]["text"].startswith("Saved to your fuel log")


def test_duplicate_yes_still_imports_after_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    confirmed: list[Any] = []
    monkeypatch.setattr(coros, "import_confirmed", lambda conn, items: confirmed.append(items) or 1)
    shared = SharedDB()
    _sync_bot(shared, lambda conn, now: _dup_report(tmp_path)).handle(_dm("sync"), FakeClient())
    client = FakeClient()
    _sync_bot(shared, lambda conn, now: _dup_report(tmp_path)).handle(_dm("yes"), client)
    assert [i.path for i in confirmed[0]] == [tmp_path / "1.fit"]
    assert client.posted[-1]["text"] == "Imported 1 more run(s)."


def test_pending_round_trips_and_bad_data_is_dropped() -> None:
    week = slack_bot.WeekProposal("2026-11-09", [{"day": 6, "kind": "long"}], focus="fueling")
    entry = fueling.FuelEntry("2026-11-07", distance_meters=16093, carbs_grams=27.0)
    for pending in (week, entry):
        raw = json.loads(json.dumps(slack_bot.encode_pending(pending)))
        assert slack_bot.decode_pending(raw) == pending
    assert slack_bot.decode_pending({"type": "fuel", "date": "2026-11-07", "bogus": 1}) is None
    assert slack_bot.decode_pending({"type": "fuel", "date": "Saturday"}) is None
    assert slack_bot.decode_pending(None) is None
