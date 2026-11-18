"""A run the watch didn't record, in Slack: proposed by the coach, shown and saved in code on her
yes. Fake Slack, fake coach, synthetic runs only."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from mara_coach import coros, slack_bot
from mara_trainer import imports, manual_runs
from mara_trainer.imports import commit, decode_fit

from .fit_builder import build_fit
from .test_slack_body import YELLOW, MultiCoach
from .test_slack_bot import FakeCoach, SharedDB, SlackFake, _debrief_bot, _dm

TODAY = "2026-11-17"
RUN = {"date": TODAY, "distance": 2, "duration": "20:00", "notes": "forgot my watch"}
LINE = f"{TODAY}: 2.00 mi, 20:00, notes: forgot my watch"


def _la(day: int, hour: int) -> datetime:
    return datetime(2026, 11, day, hour, 0, tzinfo=ZoneInfo("America/Los_Angeles")).astimezone(UTC)


NOW = _la(17, 12)


def _runs(shared: SharedDB) -> list[sqlite3.Row]:
    return shared.conn.execute("SELECT * FROM activities ORDER BY id").fetchall()


def _watch_run(shared: SharedDB, hour_utc: int = 16) -> None:
    start = datetime(2026, 11, 17, hour_utc, 0, tzinfo=UTC)
    data = build_fit([{"start": start, "meters": 3330, "seconds": 1241, "sport": "running"}])
    commit(shared.conn, decode_fit(data, NOW), f"{hour_utc}.fit")


def _propose(shared: SharedDB, args: dict[str, Any] = RUN, *answers: str) -> tuple[Any, Any]:
    coach = MultiCoach(["Nice, a run is a run.", *answers], {"propose_run_entry": args})
    bot, client = _debrief_bot(coach, shared, NOW), SlackFake()
    bot.handle(_dm("did today's run, 2 mi in 20 min, forgot my watch"), client)
    return bot, client


def test_run_is_shown_and_saved_only_on_yes() -> None:
    shared = SharedDB()
    bot, client = _propose(shared)
    shown = client.posted[-1]["text"]
    assert f"*Run to log* {LINE}" in shown and shown.endswith(slack_bot.RUN_QUESTION)
    assert _runs(shared) == []  # proposed, not saved
    assert isinstance(bot.conversations["D1"].pending, slack_bot.RunProposal)
    bot.handle(_dm("yes"), client)
    [row] = _runs(shared)
    assert row["source"] == "manual" and row["distance_meters"] == 3219
    assert row["duration_seconds"] == 1200 and row["notes"] == "forgot my watch"
    assert client.posted[-1]["text"] == slack_bot.RUN_SAVED.format(line=LINE, id=row["id"])
    assert bot.conversations["D1"].pending is None


def test_no_drops_the_run() -> None:
    shared = SharedDB()
    bot, client = _propose(shared)
    bot.handle(_dm("no"), client)
    assert client.posted[-1]["text"] == slack_bot.CANCELLED
    assert _runs(shared) == [] and bot.conversations["D1"].pending is None
    bot.handle(_dm("yes"), client)  # nothing waiting: a yes saves nothing
    assert _runs(shared) == []


def test_pending_run_survives_a_restart() -> None:
    shared = SharedDB()
    _propose(shared)
    client, coach = SlackFake(), FakeCoach([])
    _debrief_bot(coach, shared, _la(17, 13)).handle(_dm("yes"), client)  # restarted
    assert [r["distance_meters"] for r in _runs(shared)] == [3219]
    assert client.posted[0]["text"].startswith("Logged your run")
    assert coach.calls == []


def test_pending_run_round_trips_and_bad_data_is_dropped() -> None:
    proposal = slack_bot.RunProposal(manual_runs.ManualRun(TODAY, 3219, 1200, "x"), (4,))
    raw = json.loads(json.dumps(slack_bot.encode_pending(proposal)))
    assert raw["type"] == "run" and slack_bot.decode_pending(raw) == proposal
    for bad in (
        {"distance_meters": 0},
        {"duration_seconds": "20:00"},
        {"date": "Tuesday"},
        {"shown": ["4"]},
        {"pace": 1},
    ):
        assert slack_bot.decode_pending({**raw, **bad}) is None


def test_a_future_day_is_never_pending() -> None:
    shared = SharedDB()
    bot, client = _propose(shared, {**RUN, "date": "2026-11-18"})
    assert bot.conversations["D1"].pending is None
    assert "*Run to log*" not in client.posted[-1]["text"]
    bot.handle(_dm("yes"), client)
    assert _runs(shared) == []


@pytest.mark.parametrize(
    "bad", [{"date": TODAY, "distance": 2}, {**RUN, "duration": "soon"}, {**RUN, "pace": "9:00"}]
)
def test_incomplete_or_invalid_proposals_are_never_pending(bad: dict[str, Any]) -> None:
    message = {
        "role": "assistant",
        "content": [{"type": "tool_use", "name": "propose_run_entry", "input": bad}],
    }
    assert slack_bot.proposed_run([message]) is None


def test_a_synced_run_that_day_is_shown_and_yes_keeps_both() -> None:
    shared = SharedDB()
    _watch_run(shared)
    bot, client = _propose(shared)
    shown = client.posted[-1]["text"]
    assert "Already logged that day:\n• Tue 11/17: 2.07 mi, 20:41 (watch)" in shown
    assert shown.endswith(slack_bot.RUN_DUPLICATE_QUESTION)
    assert len(_runs(shared)) == 1
    bot.handle(_dm("yes"), client)
    assert [r["source"] for r in _runs(shared)] == ["fit", "manual"]


def test_a_run_logged_after_she_was_shown_is_asked_about_first() -> None:
    shared = SharedDB()
    bot, client = _propose(shared)
    _watch_run(shared)  # a sync lands between the proposal and her yes
    bot.handle(_dm("yes"), client)
    assert len(_runs(shared)) == 1  # not saved yet
    said = client.posted[-1]["text"]
    assert "since I showed you" in said and "(watch)" in said
    assert said.endswith(slack_bot.RUN_DUPLICATE_QUESTION)
    bot.handle(_dm("yes"), client)
    assert len(_runs(shared)) == 2


def test_two_identical_runs_both_save_after_confirmation() -> None:
    shared = SharedDB()
    bot, client = _propose(shared)
    bot.handle(_dm("yes"), client)
    coach = MultiCoach(["Two today, nice."], {"propose_run_entry": RUN})
    bot.coach_factory = lambda conn: coach
    bot.handle(_dm("did a second 2 mi in 20 min this evening, also no watch"), client)
    shown = client.posted[-1]["text"]
    assert "Already logged that day:\n• Tue 11/17: 2.00 mi, 20:00 (logged by hand)" in shown
    assert shown.endswith(slack_bot.RUN_DUPLICATE_QUESTION)
    assert len(_runs(shared)) == 1
    bot.handle(_dm("yes"), client)
    assert [r["distance_meters"] for r in _runs(shared)] == [3219, 3219]
    bot.handle(_dm("yes"), client)  # a repeated yes saves nothing more: pending was cleared
    assert len(_runs(shared)) == 2


def test_a_run_waits_alone() -> None:
    shared = SharedDB()
    coach = MultiCoach(
        ["Got it."], {"propose_body_entry": {**YELLOW, "date": TODAY}, "propose_run_entry": RUN}
    )
    bot, client = _debrief_bot(coach, shared, NOW), SlackFake()
    bot.handle(_dm("did 2 mi without my watch, left shin a bit sore, 3, no swelling"), client)
    shown = client.posted[-1]["text"]
    assert shown.endswith(slack_bot.ONE_AT_A_TIME.format(kept="body log entry", dropped="run"))
    bot.handle(_dm("yes"), client)
    assert _runs(shared) == []  # that yes was for the body entry only


def test_still_waiting_names_the_run() -> None:
    shared = SharedDB()
    bot, client = _propose(shared, RUN, "Saturday is your long run.")
    bot.handle(_dm("what's my long run this week?"), client)
    assert client.posted[-1]["text"].endswith(
        "The run I showed earlier isn't logged yet. " + slack_bot.RUN_QUESTION
    )
    assert _runs(shared) == []


# ------------------------------------------------------------------ the watch file comes later


def test_sync_yes_asks_again_when_a_run_was_logged_by_hand_since(tmp_path: Path) -> None:
    import asyncio
    from datetime import date

    from .test_coros import FakeApi

    shared = SharedDB()
    start = datetime(2026, 11, 17, 16, 0, tzinfo=UTC)
    draft = imports.make_draft("apple_health", start, 3320, 1240, NOW)
    commit(shared.conn, imports.DecodedFile("apple_health", "h" * 64, [draft]), "export.zip")
    fit = build_fit([{"start": start, "meters": 3330, "seconds": 1241, "sport": "running"}])
    api = FakeApi({"9": fit}, [coros.CorosRun("9", 100, TODAY)])

    def sync(conn: sqlite3.Connection, now: datetime) -> coros.SyncReport:
        day = date(2026, 11, 17)
        return asyncio.run(coros.sync_runs(conn, api, day, day, tmp_path))

    bot, client = _debrief_bot(FakeCoach([]), shared, NOW, sync=sync), SlackFake()
    bot.handle(_dm("sync my runs"), client)
    assert "Apple Health copy" in client.posted[-1]["text"]
    manual_runs.add_run(shared.conn, manual_runs.ManualRun(TODAY, 3219, 1200), True, today=TODAY)
    bot.handle(_dm("yes"), client)
    assert len(_runs(shared)) == 2  # not imported: the set of matches changed
    said = client.posted[-1]["text"]
    assert "since I asked" in said and "Tue 11/17: 2.00 mi, 20:00" in said
    assert isinstance(bot.conversations["D1"].pending, slack_bot.DuplicateRuns)
    bot.handle(_dm("yes"), client)
    assert [r["source"] for r in _runs(shared)] == ["apple_health", "manual", "fit"]


def test_sync_duplicate_question_names_the_hand_logged_run_and_survives_restart(
    tmp_path: Path,
) -> None:
    item = coros.Pending(
        tmp_path / "1.fit", ("k",), ("Tue 11/17: 2.07 mi, 20:41",), ("Tue 11/17: 2.00 mi, 20:00",)
    )
    raw = json.loads(json.dumps(slack_bot.encode_pending(slack_bot.DuplicateRuns((item,)))))
    assert slack_bot.decode_pending(raw) == slack_bot.DuplicateRuns((item,))
    old = {**raw, "items": [{k: v for k, v in raw["items"][0].items() if k != "by_hand"}]}
    restored = slack_bot.decode_pending(old)  # saved before this field existed
    assert isinstance(restored, slack_bot.DuplicateRuns) and restored.items[0].by_hand == ()
    assert "logged by hand" in slack_bot.SYNC_DUPLICATES
