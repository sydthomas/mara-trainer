from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest

from mara_coach import slack_bot, tools
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


def _bot(coach: FakeCoach, shared: SharedDB, today: str) -> slack_bot.SlackCoach:
    bot = slack_bot.SlackCoach(SYDNEY, coach_factory=lambda _: coach, connect=shared)
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
    assert any("Saved Week" in m["text"] and "push my week" in m["text"] for m in client.posted)
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
