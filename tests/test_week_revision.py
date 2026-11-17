from __future__ import annotations

import copy
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest

from mara_coach import coros, slack_bot, tools
from mara_coach.agent import CoachReply
from mara_trainer import db, reports
from mara_trainer.plan_rules import validate_schedule
from tests.test_slack_bot import SYDNEY, FakeCoach, SharedDB, SlackFake, _dm

EXAMPLE = Path(__file__).parents[1] / "examples" / "sample-plan.json"


def _plan() -> dict[str, Any]:
    raw = json.loads(EXAMPLE.read_text())
    check = reports.check_plan(raw, None)
    schedule = validate_schedule(raw, check.setup, check.brief)
    return {"setup": check.setup.to_dict(), "brief_date": check.brief_date, **schedule}


def _week(plan: dict[str, Any], index: int = 1) -> tuple[str, str, list[dict[str, Any]]]:
    """A week's start, the day before it, and its sessions in propose_week form."""
    start = plan["weeks"][index]["start_date"]
    week = reports.plan_week(plan, start)
    assert week is not None
    sessions = [
        {k: s[k] for k in ("date", "kind", "distance_meters", "details")} for s in week["sessions"]
    ]
    before = plan["weeks"][index - 1]["start_date"]
    return start, before, sessions


def _strength(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    return sessions[0]  # the sample plan has no strength day; any session's details will do


def test_new_details_change_only_that_week() -> None:
    plan = _plan()
    start, today, sessions = _week(plan)
    _strength(sessions)["details"] = "Goblet squat 3x8, RDL 3x8, stop 2-3 reps short of failure."
    before = copy.deepcopy(plan)
    revised, problems = reports.revise_week(plan, start, sessions, today, None)
    assert problems == [] and revised is not None
    assert plan == before  # the input plan is untouched
    assert revised["weeks"][0] == plan["weeks"][0] and revised["weeks"][2:] == plan["weeks"][2:]
    week = reports.plan_week(revised, start)
    assert week is not None
    assert "Goblet squat" in _strength(week["sessions"])["details"]


def test_guardrails_still_apply() -> None:
    plan = _plan()
    start, today, sessions = _week(plan)
    run = next(s for s in sessions if s["kind"] == "easy")
    run["distance_meters"] += 1609  # would raise the weekly total
    revised, problems = reports.revise_week(plan, start, sessions, today, None)
    assert revised is None and problems


@pytest.mark.parametrize(
    "edit, expected",
    [
        (lambda s: s.pop(), "exactly once"),
        (lambda s: s[0].update(date="2020-01-01"), "isn't in the week"),
    ],
)
def test_sessions_must_fill_the_week(edit: Any, expected: str) -> None:
    plan = _plan()
    start, today, sessions = _week(plan)
    edit(sessions)
    revised, problems = reports.revise_week(plan, start, sessions, today, None)
    assert revised is None and expected in problems[0]


def test_passed_days_are_locked_but_the_rest_of_the_week_can_change() -> None:
    plan = _plan()
    start, _, sessions = _week(plan)
    sessions[0]["details"] = "Changed."
    tuesday = sessions[1]["date"]
    revised, problems = reports.revise_week(plan, start, sessions, tuesday, None)
    assert revised is None and "already passed" in problems[0]
    start, _, sessions = _week(plan)
    sessions[1]["details"] = "Changed."
    revised, problems = reports.revise_week(plan, start, sessions, tuesday, None)
    assert problems == [] and revised is not None


def _ill(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**s, "kind": "rest", "distance_meters": 0, "details": "Rest."}
        if s["kind"] != "race"
        else s
        for s in sessions
    ]


def test_cut_can_rest_the_week() -> None:
    plan = _plan()
    start, today, sessions = _week(plan)
    revised, problems = reports.revise_week(plan, start, _ill(sessions), today, None, cut=True)
    assert problems == [] and revised is not None
    assert reports.plan_week(revised, start)["sessions"][0]["kind"] == "rest"  # type: ignore[index]
    later, _, more = _week(revised, 2)
    again, problems = reports.revise_week(revised, later, more, start, None)
    assert problems == [] and again is not None  # one cut week doesn't block later weeks


def test_without_cut_the_week_must_hit_its_total() -> None:
    plan = _plan()
    start, today, sessions = _week(plan)
    revised, problems = reports.revise_week(plan, start, _ill(sessions), today, None)
    assert revised is None and problems


def test_cut_cannot_add_running() -> None:
    plan = _plan()
    start, today, sessions = _week(plan)
    run = next(s for s in sessions if s["kind"] == "easy")
    long_run = next(s for s in sessions if s["kind"] == "long")
    run["distance_meters"] += 1609
    long_run["distance_meters"] -= 5000
    revised, problems = reports.revise_week(plan, start, sessions, today, None, cut=True)
    assert revised is None and "can't add distance" in problems[0]


def test_cut_must_lower_the_total() -> None:
    plan = _plan()
    start, today, sessions = _week(plan)
    revised, problems = reports.revise_week(plan, start, sessions, today, None, cut=True)
    assert revised is None and "lower the week" in problems[0]


def _lowered(plan: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    """The plan with week 2 rested (a cut), that week as it was, its start and the day before."""
    start, today, sessions = _week(plan)
    lowered, problems = reports.revise_week(plan, start, _ill(sessions), today, None, cut=True)
    assert problems == [] and lowered is not None
    return lowered, plan["weeks"][1], start, today


def test_restore_gives_back_up_to_the_original_week() -> None:
    plan = _plan()
    lowered, original, start, today = _lowered(plan)
    _, _, full = _week(plan)
    back, problems = reports.revise_week(
        lowered, start, full, today, None, restore=True, original_week=original
    )
    assert problems == [] and back is not None
    assert reports.plan_week(back, start) == reports.plan_week(plan, start)
    _, _, part = _week(plan)
    for s in part:
        if s["kind"] == "long":
            s.update(kind="rest", distance_meters=0, details="Rest.")
    was_workout = copy.deepcopy(original)
    next(s for s in was_workout["sessions"] if s["kind"] == "easy")["kind"] = "workout"
    for before in (original, was_workout):  # a workout may come back as an easy run
        back, problems = reports.revise_week(
            lowered, start, part, today, None, restore=True, original_week=before
        )
        assert problems == [] and back is not None  # less than the original is fine


def test_restore_cannot_go_above_the_original_week() -> None:
    plan = _plan()
    lowered, original, start, today = _lowered(plan)
    _, _, more = _week(plan)
    run = next(s for s in more if s["kind"] == "easy")
    run["distance_meters"] += 1609
    back, problems = reports.revise_week(
        lowered, start, more, today, None, restore=True, original_week=original
    )
    assert back is None and "can't add distance" in problems[0]
    _, _, swapped = _week(plan)
    next(s for s in swapped if s["kind"] == "easy")["kind"] = "workout"
    back, problems = reports.revise_week(
        lowered, start, swapped, today, None, restore=True, original_week=original
    )
    assert back is None and any("into workout" in p for p in problems)


def test_restore_must_give_something_back() -> None:
    plan = _plan()
    lowered, original, start, today = _lowered(plan)
    _, _, sessions = _week(lowered)
    back, problems = reports.revise_week(
        lowered, start, sessions, today, None, restore=True, original_week=original
    )
    assert back is None and problems == [
        "a restore must give something back; use cut to lower further"
    ]


def test_restore_needs_a_lowered_week() -> None:
    plan = _plan()
    start, today, sessions = _week(plan)
    back, problems = reports.revise_week(plan, start, sessions, today, None, restore=True)
    assert back is None and problems == [
        "this week was never lowered, so there is nothing to give back"
    ]


def test_restore_leaves_passed_days_as_they_are_now() -> None:
    plan = _plan()
    lowered, original, start, _ = _lowered(plan)
    _, _, full = _week(plan)
    running = [s["date"] for s in full if s["distance_meters"]]
    back, problems = reports.revise_week(
        lowered, start, full, running[1], None, restore=True, original_week=original
    )
    assert back is None and "already passed" in problems[0]  # the rested day stays rested
    _, _, ahead = _week(plan)
    for s in ahead:
        if s["date"] < running[1]:
            s.update(kind="rest", distance_meters=0, details="Rest.")
    back, problems = reports.revise_week(
        lowered, start, ahead, running[1], None, restore=True, original_week=original
    )
    assert problems == [] and back is not None


def test_cut_and_restore_together_is_an_error() -> None:
    plan = _plan()
    start, today, sessions = _week(plan)
    with pytest.raises(reports.ReportError, match="together"):
        reports.revise_week(plan, start, sessions, today, None, cut=True, restore=True)


def _half(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Week 2's sessions with the long run rested: a first, partial cut."""
    _, _, sessions = _week(plan)
    return [
        {**s, "kind": "rest", "distance_meters": 0, "details": "Rest."}
        if s["kind"] == "long"
        else s
        for s in sessions
    ]


def test_original_week_is_found_in_the_archived_plans(conn: sqlite3.Connection) -> None:
    plan = _plan()
    start, today, sessions = _week(plan)
    db.save_plan(conn, {**plan, "weeks": plan["weeks"][2:]})  # an older plan without this week
    db.save_plan(conn, plan)
    assert reports.lowered_from(db.load_archived_plans(conn), plan, start) is None  # never lowered
    _strength(sessions)["details"] = "Leg press 3x10."
    rewritten, _ = reports.revise_week(plan, start, sessions, today, None)
    assert rewritten is not None
    db.save_plan(conn, rewritten)
    assert reports.lowered_from(db.load_archived_plans(conn), rewritten, start) is None
    once, _ = reports.revise_week(rewritten, start, _half(rewritten), today, None, cut=True)
    assert once is not None
    db.save_plan(conn, once)
    found = reports.lowered_from(db.load_archived_plans(conn), once, start)
    assert found is not None and found["sessions"] == rewritten["weeks"][1]["sessions"]  # newest
    twice, _ = reports.revise_week(once, start, _ill(sessions), today, None, cut=True)
    assert twice is not None
    db.save_plan(conn, twice)
    found = reports.lowered_from(db.load_archived_plans(conn), twice, start)
    assert found is not None and found["sessions"] == rewritten["weeks"][1]["sessions"]
    later = plan["weeks"][2]["start_date"]
    assert reports.lowered_from(db.load_archived_plans(conn), twice, later) is None
    assert reports.lowered_from(db.load_archived_plans(conn), twice, "2000-01-03") is None


def test_archived_plans_load_newest_first(conn: sqlite3.Connection) -> None:
    assert db.load_archived_plans(conn) == []
    for name in ("first", "second", "third"):
        db.save_plan(conn, {"name": name, "weeks": []})
    assert [p["name"] for p in db.load_archived_plans(conn)] == ["second", "first"]


def test_week_card_lists_each_day() -> None:
    plan = _plan()
    start, _, _ = _week(plan)
    week = reports.plan_week(plan, start)
    assert week is not None
    card = reports.week_card(week, "Easy miles.").splitlines()
    assert card[0].startswith("Week 2 of") and card[1] == "Focus: Easy miles."
    assert len(card) == 9


def test_week_must_be_in_the_plan() -> None:
    plan = _plan()
    _, today, sessions = _week(plan)
    revised, problems = reports.revise_week(plan, "2000-01-03", sessions, today, None)
    assert revised is None and "isn't in the accepted plan" in problems[0]


def test_tool_checks_but_never_saves(conn: sqlite3.Connection, monkeypatch: Any) -> None:
    plan = _plan()
    db.save_plan(conn, plan)
    start, today, sessions = _week(plan)
    _strength(sessions)["details"] = "Leg press 3x10."
    monkeypatch.setattr(reports, "athlete_today", lambda _: today)
    out = tools.call_tool(conn, "propose_week", {"start_date": start, "sessions": sessions})
    assert out["ok"] is True and out["saved"] is False
    assert _strength(out["week"]["sessions"])["details"] == "Leg press 3x10."
    stored = reports.plan_week(db.load_accepted_plan(conn), start)
    assert stored is not None and _strength(stored["sessions"])["details"] != "Leg press 3x10."


class WeekCoach(FakeCoach):
    """Replies like the real agent after calling propose_week."""

    def __init__(self, answers: list[str | Exception], propose: dict[str, Any]) -> None:
        super().__init__(answers)
        self.propose: dict[str, Any] | None = propose

    def ask(self, question: str, history: list[dict[str, Any]] | None = None) -> CoachReply:
        reply = super().ask(question, history)
        msgs: list[dict[str, Any]] = [*(history or []), {"role": "user", "content": question}]
        if self.propose is not None:
            block = NS(type="tool_use", name="propose_week", input=self.propose, id="t1")
            msgs.append({"role": "assistant", "content": [block]})
            self.propose = None
        return CoachReply(reply.text, "end_turn", messages=msgs)


class FakePush:
    def __init__(self, error: Exception | None = None) -> None:
        self.days: list[str] = []
        self.error = error

    def __call__(self, conn: sqlite3.Connection, day: str, now: Any) -> coros.PushReport:
        self.days.append(day)
        if self.error:
            raise self.error
        return coros.PushReport(3, sent=["Mon 11/16: Easy 3 mi"])


def _bot(
    coach: FakeCoach, shared: SharedDB, today: str, push: FakePush | None = None
) -> slack_bot.SlackCoach:
    bot = slack_bot.SlackCoach(
        SYDNEY, coach_factory=lambda _: coach, connect=shared, push=push or FakePush()
    )
    bot.today = lambda conn: today  # type: ignore[method-assign]
    return bot


def _proposal() -> tuple[SharedDB, dict[str, Any], str, str]:
    shared, plan = SharedDB(), _plan()
    db.save_plan(shared.conn, plan)
    start, today, sessions = _week(plan)
    _strength(sessions)["details"] = "Leg press 3x10."
    return shared, {"start_date": start, "sessions": sessions}, start, today


def test_slack_saves_the_week_only_on_yes() -> None:
    shared, proposal, start, today = _proposal()
    client = SlackFake()
    bot = _bot(WeekCoach(["Here's your week. Save this week?"], proposal), shared, today)
    bot.handle(_dm("nothing special this week"), client)
    stored = reports.plan_week(db.load_accepted_plan(shared.conn), start)
    assert stored is not None and _strength(stored["sessions"])["details"] != "Leg press 3x10."
    bot.handle(_dm("yes"), client)
    stored = reports.plan_week(db.load_accepted_plan(shared.conn), start)
    assert stored is not None and _strength(stored["sessions"])["details"] == "Leg press 3x10."
    assert bot.conversations["D1"].pending is None
    assert any(m["text"].startswith("Saved Week") for m in client.posted)
    rows = shared.conn.execute("SELECT status FROM training_plans ORDER BY id").fetchall()
    assert [r["status"] for r in rows] == ["archived", "accepted"]  # old plan kept


def test_slack_no_drops_the_week() -> None:
    shared, proposal, start, today = _proposal()
    client = SlackFake()
    bot = _bot(WeekCoach(["Save this week?"], proposal), shared, today)
    bot.handle(_dm("all good"), client)
    bot.handle(_dm("no"), client)
    stored = reports.plan_week(db.load_accepted_plan(shared.conn), start)
    assert stored is not None and _strength(stored["sessions"])["details"] != "Leg press 3x10."
    assert client.posted[-1]["text"] == slack_bot.CANCELLED


def test_invalid_week_is_never_pending() -> None:
    shared, proposal, _, today = _proposal()
    proposal["sessions"].pop()
    bot = _bot(WeekCoach(["Save this week?"], proposal), shared, today)
    bot.handle(_dm("all good"), SlackFake())
    assert bot.conversations["D1"].pending is None


def _lowered_db() -> tuple[SharedDB, dict[str, Any], str, str]:
    """A DB where week 2 was lowered to all rest, and a proposal that gives the week back."""
    shared, plan = SharedDB(), _plan()
    db.save_plan(shared.conn, plan)
    lowered, _, start, today = _lowered(plan)
    db.save_plan(shared.conn, lowered)
    _, _, sessions = _week(plan)
    return shared, {"start_date": start, "sessions": sessions, "restore": True}, start, today


def test_tool_restores_only_a_lowered_week(monkeypatch: Any) -> None:
    shared, proposal, start, today = _lowered_db()
    monkeypatch.setattr(reports, "athlete_today", lambda _: today)
    out = tools.call_tool(shared.conn, "propose_week", proposal)
    assert out["ok"] is True and out["saved"] is False
    assert sum(s["distance_meters"] for s in out["week"]["sessions"]) > 0
    stored = reports.plan_week(db.load_accepted_plan(shared.conn), start)
    assert stored is not None and not any(s["distance_meters"] for s in stored["sessions"])
    with pytest.raises(tools.ToolError, match="together"):
        tools.call_tool(shared.conn, "propose_week", {**proposal, "cut": True})
    db.save_plan(shared.conn, _plan())  # the full week again: nothing left to give back
    out = tools.call_tool(shared.conn, "propose_week", proposal)
    assert out["ok"] is False and "never lowered" in out["problems"][0]


def test_slack_restore_saves_on_yes_and_survives_a_restart() -> None:
    shared, proposal, start, today = _lowered_db()
    client, push = SlackFake(), FakePush()
    bot = _bot(WeekCoach(["These runs come back. Save this week?"], proposal), shared, today, push)
    bot.handle(_dm("the sports doctor said I can run again, easy and pain-free"), client)
    pending = bot.conversations["D1"].pending
    assert isinstance(pending, slack_bot.WeekProposal) and pending.restore and not pending.cut
    raw = json.loads(json.dumps(slack_bot.encode_pending(pending)))
    assert raw["restore"] is True and slack_bot.decode_pending(raw) == pending
    stored = reports.plan_week(db.load_accepted_plan(shared.conn), start)
    assert stored is not None and not any(s["distance_meters"] for s in stored["sessions"])
    bot = _bot(FakeCoach([]), shared, today, push)  # a restart: the waiting week is reloaded
    bot.handle(_dm("yes"), client)
    stored = reports.plan_week(db.load_accepted_plan(shared.conn), start)
    assert stored is not None
    assert [s["distance_meters"] for s in stored["sessions"]] == [
        s["distance_meters"] for s in proposal["sessions"]
    ]
    assert push.days == [start]  # the watch push is the same as for any other week


def test_slack_refuses_a_restore_above_the_original() -> None:
    shared, proposal, start, today = _lowered_db()
    next(s for s in proposal["sessions"] if s["kind"] == "easy")["distance_meters"] += 1609
    client = SlackFake()
    bot = _bot(WeekCoach(["Save this week?"], proposal), shared, today)
    bot.handle(_dm("the doctor said I can run again"), client)
    assert bot.conversations["D1"].pending is None
    bot.handle(_dm("yes"), client)
    stored = reports.plan_week(db.load_accepted_plan(shared.conn), start)
    assert stored is not None and not any(s["distance_meters"] for s in stored["sessions"])


def test_restore_is_rechecked_when_saved() -> None:
    shared, proposal, start, today = _lowered_db()
    client = SlackFake()
    bot = _bot(WeekCoach(["Save this week?"], proposal), shared, today)
    bot.handle(_dm("the doctor said I can run again"), client)
    running = [s["date"] for s in proposal["sessions"] if s["distance_meters"]]
    bot.today = lambda conn: running[1]  # type: ignore[method-assign]  # a rested day has passed
    bot.handle(_dm("yes"), client)
    stored = reports.plan_week(db.load_accepted_plan(shared.conn), start)
    assert stored is not None and not any(s["distance_meters"] for s in stored["sessions"])
    assert "Couldn't save" in client.posted[-1]["text"]


def test_week_is_rechecked_when_saved() -> None:
    shared, proposal, start, today = _proposal()
    client = SlackFake()
    bot = _bot(WeekCoach(["Save this week?"], proposal), shared, today)
    bot.handle(_dm("all good"), client)
    tuesday = proposal["sessions"][1]["date"]
    bot.today = lambda conn: tuesday  # type: ignore[method-assign]  # Monday has now passed
    bot.handle(_dm("yes"), client)
    stored = reports.plan_week(db.load_accepted_plan(shared.conn), start)
    assert stored is not None and _strength(stored["sessions"])["details"] != "Leg press 3x10."
    assert "Couldn't save" in client.posted[-1]["text"]


def test_saving_the_week_ends_the_check_in_reply() -> None:
    shared, proposal, start, today = _proposal()
    shared.conn.execute(
        "INSERT INTO slack_state(key, value) VALUES (?, ?)", (slack_bot.WEEKLY_KEY, start)
    )
    coach = WeekCoach(["Save this week?", "Sure"], proposal)
    bot, client = _bot(coach, shared, today), SlackFake()
    bot.handle(_dm("nothing special"), client)
    assert coach.calls[0][0] == slack_bot.weekly_reply(start, "nothing special")
    bot.handle(_dm("yes"), client)
    bot.handle(_dm("thanks"), client)
    assert coach.calls[1][0] == "thanks"  # week saved: back to plain chat


def test_week_proposal_ends_with_the_bots_question() -> None:
    shared, proposal, _, today = _proposal()
    client = SlackFake()
    bot = _bot(WeekCoach(["Here's your week.\n\n**Save this week?**"], proposal), shared, today)
    bot.handle(_dm("all good"), client)
    assert client.posted[-1]["text"] == "Here's your week.\n\n" + slack_bot.WEEK_QUESTION


def test_one_yes_saves_and_puts_the_week_on_the_watch() -> None:
    shared, proposal, start, today = _proposal()
    client, push = SlackFake(), FakePush()
    bot = _bot(WeekCoach(["Save this week?"], proposal), shared, today, push)
    bot.handle(_dm("all good"), client)
    assert push.days == []  # nothing sent before her yes
    bot.handle(_dm("yes"), client)
    assert push.days == [start]
    texts = [m["text"] for m in client.posted]
    assert texts[-3].startswith("Saved Week") and texts[-2] == slack_bot.PUSHING
    assert texts[-1].startswith("On your watch calendar (Week 3):")
    assert client.deleted[-1]["ts"] == f"ts{len(client.posted) - 1}"  # placeholder removed


@pytest.mark.parametrize("text", ["save only", "Just save it", "save it only"])
def test_save_only_skips_the_watch(text: str) -> None:
    shared, proposal, start, today = _proposal()
    client, push = SlackFake(), FakePush()
    bot = _bot(WeekCoach(["Save this week?"], proposal), shared, today, push)
    bot.handle(_dm("all good"), client)
    bot.handle(_dm(text), client)
    stored = reports.plan_week(db.load_accepted_plan(shared.conn), start)
    assert stored is not None and _strength(stored["sessions"])["details"] == "Leg press 3x10."
    assert push.days == [] and client.posted[-1]["text"].endswith(slack_bot.PUSH_LATER)


def test_push_my_week_sends_the_coming_week_without_the_coach() -> None:
    shared, _, _, today = _proposal()
    client, push, coach = SlackFake(), FakePush(), FakeCoach([])
    _bot(coach, shared, today, push).handle(_dm("Push my week to my watch"), client)
    assert push.days == [(date.fromisoformat(today) + timedelta(days=1)).isoformat()]
    assert coach.calls == [] and client.posted[0]["text"] == slack_bot.PUSHING
    assert not slack_bot.PUSH.match("should I push the pace on my long run?")


def test_push_errors_become_a_reply() -> None:
    shared, _, _, today = _proposal()
    client = SlackFake()
    push = FakePush(coros.SignInNeeded())
    _bot(FakeCoach([]), shared, today, push).handle(_dm("push my week"), client)
    assert client.posted[-1]["text"] == coros.SIGN_IN_AGAIN


def test_resume_reruns_a_cut_off_push() -> None:
    shared, _, start, today = _proposal()
    job = {
        "kind": "push",
        "channel": "D1",
        "placeholder": "P1",
        "tries": 1,
        "question": start,
        "at": datetime.now(UTC).timestamp(),
    }
    shared.conn.execute(
        "INSERT INTO slack_state(key, value) VALUES (?, ?)",
        (slack_bot.INFLIGHT_KEY, json.dumps(job)),
    )
    client, push = SlackFake(), FakePush()
    assert _bot(FakeCoach([]), shared, today, push).resume(client)
    assert push.days == [start] and client.deleted == [{"channel": "D1", "ts": "P1"}]


def test_restore_cannot_go_back_to_a_week_that_no_longer_fits_the_plan() -> None:
    plan = _plan()
    lowered, original, start, today = _lowered(plan)
    bigger = copy.deepcopy(original)  # e.g. from an older plan with a higher weekly target
    next(s for s in bigger["sessions"] if s["kind"] == "easy")["distance_meters"] += 5000
    _, _, full = _week(plan)
    back, problems = reports.revise_week(
        lowered, start, full, today, None, restore=True, original_week=bigger
    )
    assert back is None and "no longer fits the plan" in problems[0]
