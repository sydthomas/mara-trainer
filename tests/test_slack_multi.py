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
from .test_slack_bot import PROPOSAL, SYDNEY, Clock, SharedDB, SlackFake, _debrief_bot, _dm

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
    assert _entries(shared) == [] and client.posted[-1]["text"] == "Which one?"


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
