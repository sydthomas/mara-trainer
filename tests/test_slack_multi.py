"""Several things in one Slack message: a body entry and a fueling entry waiting on one yes, and
the coach's sync and weather actions. Fake Slack, fake coach / fake API, synthetic data only."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from types import SimpleNamespace as NS
from typing import Any

import pytest

from mara_coach import agent, coros, slack_bot
from mara_coach.slack_bot import SlackCoach
from mara_trainer import body, db, fueling, reports

from .test_slack_body import PLAN, YELLOW, MultiCoach, _entries, _la
from .test_slack_bot import (
    PROPOSAL,
    SYDNEY,
    Clock,
    ProposingCoach,
    SharedDB,
    SlackFake,
    _debrief_bot,
    _dm,
    _sat,
)
from .test_week_revision import FakePush, WeekCoach, _strength
from .test_week_revision import _bot as _week_bot
from .test_week_revision import _proposal as _week_proposal

NIGGLE = {**YELLOW, "date": "2026-11-07"}
BOTH = {"propose_fuel_entry": PROPOSAL, "propose_body_entry": NIGGLE}
MESSAGE = "10 mi in 1:30, 7 chews, and my left shin is sore, 3, no swelling, yellow"


def _both_bot(*answers: str) -> tuple[Any, SharedDB, SlackFake, MultiCoach]:
    shared, client = SharedDB(), SlackFake()
    db.save_plan(shared.conn, PLAN)
    coach = MultiCoach(list(answers), BOTH)
    bot = _debrief_bot(coach, shared, _la(7, 11))  # long-run day, debrief open
    reports.mark_notified(shared.conn, slack_bot.DEBRIEF_KIND, "2026-11-07")
    bot.handle(_dm(MESSAGE), client)
    return bot, shared, client, coach


def _fuel_count(shared: SharedDB) -> int:
    return int(fueling.list_entries(shared.conn)["count"])


def test_body_and_fuel_proposed_together_are_both_shown_and_one_yes_saves_both() -> None:
    bot, shared, client, coach = _both_bot("Run and shin noted. **Save it?**", "Nice work.")
    shown = client.posted[-1]["text"]
    assert "*1. Body log entry* 2026-11-07: yellow, left shin" in shown
    assert "*2. Fueling entry* 2026-11-07, 10.00 mi, 1:30:00, during: 7 chews" in shown
    assert "27 g carbs (18 g per hour)" in shown and "stomach 5/5" in shown
    assert shown.endswith(slack_bot.BOTH_QUESTION) and "Save it?" not in shown
    assert "One thing at a time" not in shown
    convo = bot.conversations["D1"]
    assert isinstance(convo.pending, body.BodyEntry)
    assert [type(item) for item in convo.more] == [fueling.FuelEntry]
    bot.handle(_dm("yes"), client)
    assert len(_entries(shared)) == 1 and _fuel_count(shared) == 1
    texts = [post["text"] for post in client.posted]
    assert any(text.startswith("Saved to your body log") for text in texts)
    assert any(text.startswith("Saved to your fuel log") for text in texts)
    assert texts[-1] == "Nice work."  # the fueling save's coach follow-up comes last
    assert convo.pending is None and convo.more == []


@pytest.mark.parametrize(("reply", "niggles", "fuel"), [("yes 1", 1, 0), ("Yes, 2.", 0, 1)])
def test_yes_with_a_number_saves_only_that_entry(reply: str, niggles: int, fuel: int) -> None:
    bot, shared, client, _ = _both_bot("Noted. Save it?", "Nice work.")
    bot.handle(_dm(reply), client)
    assert len(_entries(shared)) == niggles and _fuel_count(shared) == fuel
    convo = bot.conversations["D1"]
    assert convo.pending is None and convo.more == []
    bot.handle(_dm("yes"), client)  # nothing is waiting any more: this goes to the coach
    assert len(_entries(shared)) == niggles and _fuel_count(shared) == fuel


def test_no_saves_neither_entry() -> None:
    bot, shared, client, _ = _both_bot("Noted. Save it?")
    bot.handle(_dm("no"), client)
    assert client.posted[-1]["text"] == slack_bot.CANCELLED
    assert _entries(shared) == [] and _fuel_count(shared) == 0
    convo = bot.conversations["D1"]
    assert convo.pending is None and convo.more == []


@pytest.mark.parametrize("reply", ["both", "yes both", "Yes, to both!"])
def test_both_is_a_yes_while_two_entries_wait(reply: str) -> None:
    bot, shared, client, _ = _both_bot("Noted. Save it?", "Nice work.")
    bot.handle(_dm(reply), client)
    assert len(_entries(shared)) == 1 and _fuel_count(shared) == 1


def test_a_duplicate_question_replaces_both_waiting_entries(tmp_path: Any) -> None:
    """A sync that asks about a possible duplicate takes over the yes: the entries shown
    earlier must not be saved by it, and its no is the duplicate question's no."""
    bot, shared, client, _ = _both_bot("Noted. Save it?")
    path = tmp_path / "1.fit"
    path.write_bytes(b"")
    pending = coros.Pending(path, ("k",), ("Sat 11/7: 10.00 mi, 1:30:00",))
    bot.sync = lambda conn, now: coros.SyncReport("2026-11-02", "2026-11-08", pending=[pending])
    bot.handle(_dm("sync my runs"), client)
    convo = bot.conversations["D1"]
    assert isinstance(convo.pending, slack_bot.DuplicateRuns) and convo.more == []
    bot.handle(_dm("no"), client)
    assert client.posted[-1]["text"] == slack_bot.SKIPPED
    assert _entries(shared) == [] and _fuel_count(shared) == 0
    closed = shared.conn.execute(
        "SELECT 1 FROM notifications WHERE kind = ?", (slack_bot.DEBRIEF_CLOSED,)
    ).fetchone()
    assert closed is None


def test_fuel_line_shows_every_field_a_yes_would_save() -> None:
    entry = fueling.FuelEntry(
        date="2026-11-07",
        distance_meters=16093,
        duration_seconds=5400,
        pre_run="oatmeal",
        pre_run_minutes=90,
        during="2 gels",
        carbs_grams=45,
        fluid_ml=500,
        stomach=4,
        energy=3,
        effort=6,
        notes="windy",
    )
    assert slack_bot.fuel_line(entry) == (
        "2026-11-07, 10.00 mi, 1:30:00, before: oatmeal (90 min before), during: 2 gels, "
        "45 g carbs (30 g per hour), 500 ml fluid, stomach 4/5, energy 3/5, effort 6/10, "
        "notes: windy"
    )
    assert slack_bot.fuel_line(fueling.FuelEntry(date="2026-11-07")) == "2026-11-07"


def test_idle_reset_drops_both_waiting_entries() -> None:
    bot, shared, client, coach = _both_bot("Noted. Save it?", "Hello again.")
    bot.clock.now += slack_bot.IDLE_RESET + 1
    bot.handle(_dm("yes"), client)  # long after: nothing waits, the coach gets it
    assert _entries(shared) == [] and _fuel_count(shared) == 0
    assert bot.conversations["D1"].more == []


def test_both_waiting_entries_survive_a_restart() -> None:
    _, shared, client, _ = _both_bot("Noted. Save it?")
    coach = MultiCoach(["Nice work."])
    restarted = _debrief_bot(coach, shared, _la(7, 11))
    restarted.handle(_dm("yes"), client)
    assert len(_entries(shared)) == 1 and _fuel_count(shared) == 1


def test_yes_with_a_number_means_nothing_when_one_thing_waits() -> None:
    shared, client = SharedDB(), SlackFake()
    db.save_plan(shared.conn, PLAN)
    coach = MultiCoach(["Got it.", "Which one?"], {"propose_body_entry": NIGGLE})
    bot = _debrief_bot(coach, shared, _la(7, 8))
    bot.handle(_dm("left shin sore, 3 out of 10, no swelling, yellow"), client)
    bot.handle(_dm("yes 2"), client)  # not a known reply here: the coach gets it
    assert _entries(shared) == [] and client.posted[-1]["text"].startswith("Which one?")
    assert client.posted[-1]["text"].endswith(slack_bot.BODY_QUESTION)  # still open, said in code


# --- the coach's channel actions


def _tool_use(name: str, args: dict[str, Any], id_: str) -> NS:
    return NS(type="tool_use", name=name, input=args, id=id_)


def _resp(stop: str, *blocks: NS) -> NS:
    return NS(stop_reason=stop, content=list(blocks), usage=NS(input_tokens=1, output_tokens=1))


class FakeAPI:
    def __init__(self, *responses: NS) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> NS:
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


FORECAST = {
    "ok": True,
    "place": "default",
    "place_source": "default",
    "hours": [{"time": "2026-11-10T07:00", "temperature_f": 55.0}],
    "attribution": [{"text": "Weather data by Open-Meteo.com", "url": "https://open-meteo.com/"}],
}


def _action_bot(api: FakeAPI, sync: Any, forecast: Any = None) -> tuple[SlackCoach, SlackFake]:
    shared = SharedDB()
    bot = SlackCoach(
        SYDNEY,
        lambda conn: agent.Coach(conn, api, today="2026-11-07", channel="slack"),
        shared,
        Clock(),
        lambda: _la(7, 14),
        sync,
    )
    bot.forecast = forecast or (lambda **window: FORECAST)  # type: ignore[method-assign]
    return bot, SlackFake()


def _tool_results(api: FakeAPI) -> list[str]:
    return [block["content"] for block in api.calls[-1]["messages"][-1]["content"]]


def test_the_coach_can_sync_and_read_the_forecast_in_one_reply() -> None:
    api = FakeAPI(
        _resp(
            "tool_use",
            _tool_use("sync_runs", {}, "t1"),
            _tool_use("get_weather", {"date": "2026-11-10"}, "t2"),
        ),
        _resp("end_turn", NS(type="text", text="Synced 1 run. Tuesday looks like 55°F.")),
    )
    seen: list[dict[str, Any]] = []

    def sync(conn: sqlite3.Connection, now: datetime) -> coros.SyncReport:
        return coros.SyncReport("2026-11-02", "2026-11-08", imported=["Sat 11/7: 5.15 mi"])

    def forecast(**window: Any) -> dict[str, Any]:
        seen.append(window)
        return FORECAST

    bot, client = _action_bot(api, sync, forecast)
    bot.handle(_dm("my shin is sore. sync my run and what's Tuesday's weather"), client)
    synced, weather_result = _tool_results(api)
    assert '"new_runs": ["Sat 11/7: 5.15 mi"]' in synced and '"ok": true' in synced
    assert '"temperature_f": 55.0' in weather_result and "attribution" not in weather_result
    assert seen[0]["date"] == "2026-11-10" and seen[0]["near"] is None
    assert seen[0]["hour"] == slack_bot.DEFAULT_MORNING_HOUR
    answer = client.posted[-1]["text"]
    assert answer.startswith("Synced 1 run.")
    assert answer.endswith("_<https://open-meteo.com/|Weather data by Open-Meteo.com>_")


def test_a_failed_sync_or_forecast_is_reported_to_the_coach_not_raised() -> None:
    api = FakeAPI(
        _resp(
            "tool_use",
            _tool_use("sync_runs", {}, "t1"),
            _tool_use("get_weather", {"date": "2026-11-10", "place": "Nowhereville"}, "t2"),
        ),
        _resp("end_turn", NS(type="text", text="Neither worked just now.")),
    )

    def sync(conn: sqlite3.Connection, now: datetime) -> coros.SyncReport:
        raise RuntimeError("socket closed")

    failed = {"ok": False, "reason": "no place found", "known_places": ["coast"]}
    bot, client = _action_bot(api, sync, lambda **window: failed)
    bot.handle(_dm("sync my run and the weather in Nowhereville please"), client)
    synced, weather_result = _tool_results(api)
    assert '"ok": false' in synced and "socket closed" not in synced
    assert '"reason": "no place found"' in weather_result and "coast" in weather_result
    assert client.posted[-1]["text"] == "Neither worked just now."  # no credit: nothing was read


def test_possible_duplicates_from_a_coach_sync_are_not_imported_and_she_is_told(
    tmp_path: Any,
) -> None:
    api = FakeAPI(
        _resp("tool_use", _tool_use("sync_runs", {}, "t1")),
        _resp("end_turn", NS(type="text", text="One run looks like a copy.")),
    )
    path = tmp_path / "1.fit"
    path.write_bytes(b"")
    pending = coros.Pending(path, ("k",), ("Sat 11/7: 10.00 mi, 1:30:00",))

    def sync(conn: sqlite3.Connection, now: datetime) -> coros.SyncReport:
        return coros.SyncReport("2026-11-02", "2026-11-08", pending=[pending], not_downloaded=2)

    bot, client = _action_bot(api, sync)
    bot.handle(_dm("did my run come through? sync it and tell me my week"), client)
    assert '"possible_duplicates_not_imported": 1' in _tool_results(api)[0]
    answer = client.posted[-1]["text"]
    assert slack_bot.SYNC_DUPLICATES in answer and slack_bot.SYNC_LEFT.format(n=2) in answer
    assert bot.conversations["D1"].pending is None  # reviewing them is her "sync my runs"


def test_without_a_channel_the_coach_is_told_the_actions_are_not_available(
    conn: sqlite3.Connection,
) -> None:
    api = FakeAPI(
        _resp("tool_use", _tool_use("sync_runs", {}, "t1")),
        _resp("end_turn", NS(type="text", text="I can't sync from here.")),
    )
    agent.Coach(conn, api, today="2026-11-07").ask("sync my runs and log a niggle")
    assert "isn't available in this channel" in _tool_results(api)[0]


# --- yes / no with a few more words, through the real handler


def _week_details(shared: SharedDB, start: str) -> str:
    stored = reports.plan_week(db.load_accepted_plan(shared.conn), start)
    assert stored is not None
    return str(_strength(stored["sessions"])["details"])


def test_yes_log_it_saves_a_waiting_body_entry_without_the_coach() -> None:
    shared, client = SharedDB(), SlackFake()
    db.save_plan(shared.conn, PLAN)
    coach = MultiCoach(["Got it. Save it?"], {"propose_body_entry": NIGGLE})
    bot = _debrief_bot(coach, shared, _la(7, 8))
    bot.handle(_dm("left shin sore, 3 out of 10, no swelling, yellow"), client)
    assert _entries(shared) == []  # proposed, not saved
    assert isinstance(bot.conversations["D1"].pending, body.BodyEntry)
    bot.handle(_dm("yes log it"), client)
    [entry] = _entries(shared)
    assert (entry["date"], entry["status"], entry["location"]) == (
        "2026-11-07",
        "yellow",
        "left shin",
    )
    assert len(coach.calls) == 1  # the yes never reached the coach (no answer was left for it)
    assert client.posted[-1]["text"].startswith("Saved to your body log")
    assert bot.conversations["D1"].pending is None


def _week_reply(reply: str) -> tuple[list[str], list[str], str, int]:
    """A waiting week, then `reply`: what was posted, what was pushed, what is stored."""
    shared, proposal, start, today = _week_proposal()
    client, push = SlackFake(), FakePush()
    coach = WeekCoach(["Save this week?"], proposal)
    bot = _week_bot(coach, shared, today, push)
    bot.handle(_dm("all good"), client)
    assert push.days == [] and _week_details(shared, start) != "Leg press 3x10."
    bot.handle(_dm(reply), client)
    assert bot.conversations["D1"].pending is None
    assert push.days in ([], [start])
    return (
        [m["text"] for m in client.posted],
        push.days,
        _week_details(shared, start),
        len(coach.calls),
    )


def test_yes_please_saves_and_pushes_the_week_exactly_as_yes_does() -> None:
    texts, pushed, details, asked = _week_reply("yes please")
    assert details == "Leg press 3x10." and len(pushed) == 1
    assert asked == 1  # only the turn that proposed the week
    assert texts[-3].startswith("Saved Week") and texts[-2] == slack_bot.PUSHING
    assert texts[-1].startswith("On your watch calendar")
    assert (texts, pushed, details, asked) == _week_reply("yes")


def test_ok_thanks_after_a_follow_up_does_not_save_or_push_the_waiting_week() -> None:
    shared, proposal, start, today = _week_proposal()
    client, push = SlackFake(), FakePush()
    coach = WeekCoach(["Save this week?", "Thursday is easy on purpose.", "Anytime."], proposal)
    bot = _week_bot(coach, shared, today, push)
    bot.handle(_dm("all good"), client)
    assert isinstance(bot.conversations["D1"].pending, slack_bot.WeekProposal)
    bot.handle(_dm("why is Thursday so short?"), client)
    assert coach.calls[1][0] == "why is Thursday so short?"
    bot.handle(_dm("ok thanks"), client)
    assert coach.calls[2][0] == "ok thanks"  # thanks for the answer, not a yes to the week
    assert client.posted[-1]["text"].startswith("Anytime.")
    assert client.posted[-1]["text"].endswith(slack_bot.WEEK_QUESTION)  # says it is still open
    assert "isn't saved yet" in client.posted[-1]["text"]
    assert push.days == [] and _week_details(shared, start) != "Leg press 3x10."
    assert not any(m["text"].startswith("Saved Week") for m in client.posted)
    rows = shared.conn.execute("SELECT status FROM training_plans").fetchall()
    assert [r["status"] for r in rows] == ["accepted"]  # nothing archived, nothing new
    assert isinstance(bot.conversations["D1"].pending, slack_bot.WeekProposal)  # still waiting
    bot.handle(_dm("yes"), client)  # and a plain yes still saves and pushes it
    assert push.days == [start] and _week_details(shared, start) == "Leg press 3x10."


def test_no_thanks_drops_a_waiting_fueling_entry() -> None:
    shared, client = SharedDB(), SlackFake()
    coach = ProposingCoach(["10 mi in 1:30. Save it?", "Yes to what?"], PROPOSAL)
    bot = _debrief_bot(coach, shared, _sat(18))
    bot.handle(_dm("10 miles 1:30, 7 chews"), client)
    assert bot.conversations["D1"].pending is not None
    bot.handle(_dm("no thanks"), client)
    assert _fuel_count(shared) == 0 and len(coach.calls) == 1
    assert client.posted[-1]["text"] == slack_bot.CANCELLED
    assert bot.conversations["D1"].pending is None
    bot.handle(_dm("yes"), client)  # nothing waits any more: a late yes saves nothing
    assert _fuel_count(shared) == 0


@pytest.mark.parametrize(
    "reply",
    ["yes and save to coros", "Yes, push it to my watch!", "yes and put them on my COROS watch"],
)
def test_yes_with_watch_words_saves_and_pushes_the_week_as_yes_does(reply: str) -> None:
    assert _week_reply(reply) == _week_reply("yes")


@pytest.mark.parametrize(
    "reply",
    [
        "yes save only",
        "yes and no watch",
        "yes but not on coros",
        "yes and add a mile",
        "yes and",
        "ok and",
        "yes and...",
        "yes add",
        "ok send",
        "sure sync",
        "yes sync them",
        "yes send it",
        "yes put",
        "yes and don't push to coros",
        "yes, save it, not to the watch",
        "yes send it to my coach",
        "save it to my notes",
    ],
)
def test_yes_with_other_words_is_not_a_plain_yes(reply: str) -> None:
    assert not slack_bot.YES.match(reply)


def test_ok_save_to_coros_is_a_yes() -> None:
    assert slack_bot.YES.match("ok save to coros") and slack_bot.YES.match("yes and save it")


def test_the_reminder_points_at_the_earlier_proposal_and_stays_out_of_the_history() -> None:
    shared, proposal, start, today = _week_proposal()
    client, push = SlackFake(), FakePush()
    coach = WeekCoach(["Save this week?", "Thursday is easy on purpose."], proposal)
    bot = _week_bot(coach, shared, today, push)
    bot.handle(_dm("all good"), client)
    bot.handle(_dm("why is Thursday so short?"), client)
    shown = client.posted[-1]["text"]
    assert "proposed earlier isn't saved yet" in shown and shown.count("isn't saved yet") == 1
    turns = bot.conversations["D1"].turns
    assert turns[-1]["content"] == "Thursday is easy on purpose."  # the coach never sees it


# --- a yes with a request after it ("yes log it then plan the rest of my week")


@pytest.mark.parametrize(
    ("text", "wanted"),
    [
        (
            "yes log it then we need to plan the rest of the runs for this week",
            "we need to plan the rest of the runs for this week",
        ),
        ("ok save it, then what's tomorrow", "what's tomorrow"),
        ("log both then plan my week", "plan my week"),
        ("Yes, log it, and then what's my long run?", "what's my long run?"),
        ("yep save that. then plan my week", "plan my week"),
        ("save it; then sync my runs", "sync my runs"),
    ],
)
def test_yes_and_more_finds_the_request_after_an_explicit_save(text: str, wanted: str) -> None:
    assert slack_bot.yes_and_more(text) == wanted


# Corrections a strict review found a deny-list let through, then edits and plain answers.
CORRECTIONS = [
    "yes, red",
    "yes, left ankle",
    "yes, I meant the left calf",
    "ok, it's a 4",
    "yes, more like 5",
    "yes, it's the other leg",
    "yes, though it's my right side",
    "yes, it's green now",
    "yes, sunday",
    "yes, Gu at mile 6",
    "yes; I drank 500ml",
    "yes, and it hurts more today",
    "yes, also my hip hurts",
    "yes and my other knee too",
    "yes, and I took ibuprofen",
    "yes, hmm",
    "yes, unless that's bad",
    "yes. Is that bad?",
]


@pytest.mark.parametrize(
    "text",
    [
        *CORRECTIONS,
        "yes",
        "yes log it",
        "no",
        "no thanks, and plan my week",
        "yes but change pain to 2",
        "yes and change pain to 2",
        "yes, actually it was the right shin",
        "yes then make it a 4",
        "yes then plan my week",  # no save verb
        "yes and also what should I eat tonight",  # no "then"
        "yep save it. plan my week",  # no "then"
        "ok; plus sync my runs",
        "log it and plan my week",
        "log it then",
        "log it then ...",
        "log it then it hurts more today",
        "save it then my left knee is sore",
        "log it then make it yellow",
        "log it then I took 2 gels",
        "log it then actually wait",
        "yes, 1 and then plan my week",
        "ok, thanks. what's next week?",
        "sounds good, then plan my week",
    ],
)
def test_yes_and_more_is_none_for_corrections_edits_and_plain_answers(text: str) -> None:
    assert slack_bot.yes_and_more(text) is None


@pytest.mark.parametrize("text", CORRECTIONS)
def test_a_correction_after_yes_never_saves_the_waiting_entry(text: str) -> None:
    shared, client = SharedDB(), SlackFake()
    db.save_plan(shared.conn, PLAN)
    coach = MultiCoach(["Got it. Save it?", "Noted."], {"propose_body_entry": NIGGLE})
    bot = _debrief_bot(coach, shared, _la(7, 8))
    bot.handle(_dm("left shin sore, 3 out of 10, no swelling, yellow"), client)
    bot.handle(_dm(text), client)
    assert _entries(shared) == []
    assert coach.calls[-1][0] == text
    assert isinstance(bot.conversations["D1"].pending, body.BodyEntry)


def _waiting_body_then_week() -> tuple[Any, SharedDB, SlackFake, MultiCoach, str]:
    """A body entry waits for her yes; the coach's next turn proposes a week."""
    shared, proposal, _start, today = _week_proposal()
    client = SlackFake()
    coach = MultiCoach(
        ["Got it. Save it?", "Here's the rest of your week. Save this week?"],
        {"propose_body_entry": {**NIGGLE, "date": today}},
        {"propose_week": proposal},
    )
    bot = _week_bot(coach, shared, today)
    bot.handle(_dm("left shin sore, 3 out of 10, no swelling, yellow"), client)
    assert isinstance(bot.conversations["D1"].pending, body.BodyEntry)
    return bot, shared, client, coach, today


def test_yes_then_a_request_saves_the_entry_and_sends_the_rest_to_the_coach() -> None:
    """The Slack bug: her yes came with a request, the coach proposed a week, and the body
    entry was replaced without being saved."""
    bot, shared, client, coach, today = _waiting_body_then_week()
    bot.handle(_dm("yes log it then we need to plan the rest of the runs for this week"), client)
    [entry] = _entries(shared)
    assert (entry["date"], entry["status"]) == (today, "yellow")
    texts = [m["text"] for m in client.posted]
    assert texts.count(slack_bot.YELLOW_SAVED.format(id=entry["id"])) == 1
    assert coach.calls[-1][0] == (
        f"(Saved: her body entry for {today}.)\n\n"
        "we need to plan the rest of the runs for this week"
    )
    assert len(coach.calls) == 2  # the save is code; one coach turn for the request
    assert isinstance(bot.conversations["D1"].pending, slack_bot.WeekProposal)
    assert "wasn't saved" not in texts[-1] and slack_bot.WEEK_QUESTION in texts[-1]


def test_yes_with_an_edit_saves_nothing_and_goes_to_the_coach_whole() -> None:
    shared, client = SharedDB(), SlackFake()
    db.save_plan(shared.conn, PLAN)
    coach = MultiCoach(["Got it. Save it?", "Updated."], {"propose_body_entry": NIGGLE})
    bot = _debrief_bot(coach, shared, _la(7, 8))
    bot.handle(_dm("left shin sore, 3 out of 10, no swelling, yellow"), client)
    bot.handle(_dm("yes but change pain to 2"), client)
    assert _entries(shared) == []
    assert coach.calls[-1][0] == "yes but change pain to 2"
    assert isinstance(bot.conversations["D1"].pending, body.BodyEntry)  # still waiting
    assert client.posted[-1]["text"].endswith(slack_bot.BODY_QUESTION)


def test_a_waiting_body_entry_replaced_by_a_week_is_said_in_code() -> None:
    bot, shared, client, _coach, today = _waiting_body_then_week()
    bot.handle(_dm("can you plan the rest of my week"), client)
    assert _entries(shared) == []
    assert isinstance(bot.conversations["D1"].pending, slack_bot.WeekProposal)
    shown = client.posted[-1]["text"]
    assert shown.endswith(slack_bot.NOT_SAVED_BODY.format(day=today))
    assert shown.count("wasn't saved") == 1
    turns = bot.conversations["D1"].turns
    assert "wasn't saved" not in turns[-1]["content"]  # code's note, not the coach's words


def test_a_corrected_body_entry_replaces_the_old_one_without_a_not_saved_line() -> None:
    shared, client = SharedDB(), SlackFake()
    db.save_plan(shared.conn, PLAN)
    coach = MultiCoach(
        ["Got it. Save it?", "Updated."],
        {"propose_body_entry": NIGGLE},
        {"propose_body_entry": {**NIGGLE, "pain": 2}},
    )
    bot = _debrief_bot(coach, shared, _la(7, 8))
    bot.handle(_dm("left shin sore, 3 out of 10, no swelling, yellow"), client)
    bot.handle(_dm("yes but change pain to 2"), client)
    assert "wasn't saved" not in client.posted[-1]["text"]
    pending = bot.conversations["D1"].pending
    assert isinstance(pending, body.BodyEntry) and pending.pain == 2


def test_log_both_then_saves_both_waiting_entries_in_order_then_asks_once() -> None:
    bot, shared, client, coach = _both_bot("Noted. Save it?", "Rice and salmon is a fine pick.")
    calls = len(coach.calls)
    bot.handle(_dm("log both then what should I eat tonight"), client)
    assert len(_entries(shared)) == 1 and _fuel_count(shared) == 1
    texts = [m["text"] for m in client.posted]
    body_at = next(i for i, t in enumerate(texts) if t.startswith("Saved to your body log"))
    fuel_at = next(i for i, t in enumerate(texts) if t.startswith("Saved to your fuel log"))
    assert body_at < fuel_at
    assert len(coach.calls) == calls + 1  # no separate fueling follow-up: her request instead
    assert coach.calls[-1][0] == (
        "(Saved: her body entry for 2026-11-07 and her fueling entry for 2026-11-07.)\n\n"
        "what should I eat tonight"
    )
    assert texts[-1] == "Rice and salmon is a fine pick."
    convo = bot.conversations["D1"]
    assert convo.pending is None and convo.more == [] and convo.debrief is None


def test_a_waiting_week_still_needs_its_own_yes() -> None:
    """A week is always its own yes: "yes and push it" is not one, so nothing is saved."""
    shared, proposal, start, today = _week_proposal()
    client, push = SlackFake(), FakePush()
    coach = WeekCoach(["Save this week?", "Say yes to save it."], proposal)
    bot = _week_bot(coach, shared, today, push)
    bot.handle(_dm("all good"), client)
    bot.handle(_dm("yes and push it"), client)
    assert coach.calls[-1][0] == "yes and push it"
    assert push.days == [] and _week_details(shared, start) != "Leg press 3x10."
    assert isinstance(bot.conversations["D1"].pending, slack_bot.WeekProposal)
    assert client.posted[-1]["text"].endswith(slack_bot.WEEK_QUESTION)


def test_yes_then_a_request_still_saves_after_a_restart() -> None:
    shared = SharedDB()
    db.save_plan(shared.conn, PLAN)
    first = MultiCoach(["Got it. Save it?"], {"propose_body_entry": NIGGLE})
    _debrief_bot(first, shared, _la(7, 8)).handle(_dm("left shin, 3, yellow"), SlackFake())
    client, coach = SlackFake(), MultiCoach(["Saturday is 10 mi."])
    restarted = _debrief_bot(coach, shared, _la(7, 9))
    restarted.handle(_dm("yes, log it, then what's my long run?"), client)
    assert len(_entries(shared)) == 1
    assert coach.calls[0][0] == "(Saved: her body entry for 2026-11-07.)\n\nwhat's my long run?"


def test_a_duplicate_question_that_replaces_an_entry_says_so(tmp_path: Any) -> None:
    bot, _shared, client, _ = _both_bot("Noted. Save it?")
    path = tmp_path / "1.fit"
    path.write_bytes(b"")
    pending = coros.Pending(path, ("k",), ("Sat 11/7: 10.00 mi, 1:30:00",))
    bot.sync = lambda conn, now: coros.SyncReport("2026-11-02", "2026-11-08", pending=[pending])
    bot.handle(_dm("sync my runs"), client)
    shown = client.posted[-1]["text"]
    assert slack_bot.NOT_SAVED_BODY.format(day="2026-11-07") in shown
    assert shown.endswith(slack_bot.NOT_SAVED_FUEL.format(day="2026-11-07"))


def test_a_body_entry_for_another_day_is_not_a_correction() -> None:
    shared, client = SharedDB(), SlackFake()
    db.save_plan(shared.conn, PLAN)
    coach = MultiCoach(
        ["Got it. Save it?", "And yesterday's."],
        {"propose_body_entry": NIGGLE},
        {"propose_body_entry": {**NIGGLE, "date": "2026-11-06"}},
    )
    bot = _debrief_bot(coach, shared, _la(7, 8))
    bot.handle(_dm("left shin sore, 3 out of 10, no swelling, yellow"), client)
    bot.handle(_dm("it was sore yesterday too"), client)
    assert slack_bot.NOT_SAVED_BODY.format(day="2026-11-07") in client.posted[-1]["text"]
    pending = bot.conversations["D1"].pending
    assert isinstance(pending, body.BodyEntry) and pending.date == "2026-11-06"


@pytest.mark.parametrize(
    "text",
    [
        "log it then never mind",
        "log it then nevermind",
        "log it then cancel",
        "log it then scratch that",
        "log it then undo",
        "log it then forget it",
        "log it then hang on",
        "log it then on second thought",
        "log it then set it to amber",
        "log it then mark it orange",
        "log it then lower it",
        "log it then raise it",
        "log it then rename it",
        "log it then add a note",
        "log it then make that a niggle",
        "log it then three out of ten",
    ],
)
def test_taking_it_back_or_editing_after_log_it_never_saves(text: str) -> None:
    assert slack_bot.yes_and_more(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "yes log it then we need to plan the rest of the runs for this week",
        "ok save it, then what's tomorrow",
        "log both then plan my week",
    ],
)
def test_the_plain_requests_still_save(text: str) -> None:
    assert slack_bot.yes_and_more(text)
