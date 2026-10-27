from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from mara_coach import slack_bot, slack_schedule
from mara_trainer import db

from .test_slack_bot import PLAN, SYDNEY, FakeCoach, SharedDB, SlackFake, _debrief_bot

# PLAN: week of Mon 2026-11-02, long run Sat 11-07 (10 mi), rest Sun 11-08. LA is UTC-8 then.


class ScheduleFake(SlackFake):
    def __init__(self, history: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.queued: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []
        self.fail_delete = False
        self.history = history or []

    def chat_scheduleMessage(self, **kwargs: Any) -> dict[str, Any]:  # noqa: N802
        sid = f"Q{len(self.queued) + len(self.deleted) + 1}"
        self.queued[sid] = kwargs
        return {"scheduled_message_id": sid}

    def chat_deleteScheduledMessage(self, **kwargs: Any) -> dict[str, Any]:  # noqa: N802
        if self.fail_delete:
            raise RuntimeError("too late to delete")
        self.queued.pop(kwargs["scheduled_message_id"])
        self.deleted.append(kwargs["scheduled_message_id"])
        return {}

    def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        oldest = float(kwargs["oldest"])
        return {"messages": [m for m in reversed(self.history) if float(m["ts"]) > oldest]}


def _utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


def _queued_texts(client: ScheduleFake) -> dict[int, str]:
    return {q["post_at"]: q["text"] for q in client.queued.values()}


def test_wanted_covers_the_next_seven_days() -> None:
    shared = SharedDB()
    db.save_plan(shared.conn, PLAN)
    want = slack_schedule.wanted(shared.conn, _utc(1, 20))  # Sun 11-01 noon LA
    assert sorted(want) == [
        ("long_run_debrief", "2026-11-07"),
        ("sync_nudge", "2026-11-08"),  # 11-01's week isn't in the plan: no sync that day
        ("weekly_checkin", "2026-11-01"),
        ("weekly_checkin", "2026-11-08"),
    ]
    assert want[("long_run_debrief", "2026-11-07")].post_at == _utc(7, 18)  # 10am LA
    assert want[("long_run_debrief", "2026-11-07")].text == slack_schedule.ping_text(10.0)
    preview = want[("weekly_checkin", "2026-11-01")].text
    assert "full recap comes when it wakes" in preview and "Sat 11-07: 10 mi long" in preview
    assert "Next week" not in want[("weekly_checkin", "2026-11-08")].text  # plan ends
    # within LEAD of post time: left alone
    near = slack_schedule.wanted(shared.conn, _utc(7, 17, 56))
    assert ("long_run_debrief", "2026-11-07") not in near


def test_reconcile_is_idempotent_and_follows_plan_changes() -> None:
    shared, client = SharedDB(), ScheduleFake()
    db.save_plan(shared.conn, PLAN)
    bot = _debrief_bot(FakeCoach([]), shared, _utc(1, 20))
    assert bot.maybe_schedule(client) == 4 and len(client.queued) == 4
    assert bot.maybe_schedule(client) == 0  # nothing changed: no Slack calls
    changed = json.loads(json.dumps(PLAN))
    changed["weeks"][0]["sessions"][1]["distance_meters"] = 19312  # 12 mi long run
    db.save_plan(shared.conn, changed)
    # the ping and the 11-01 preview both mention the long run: replace both (2 deletes + 2 new)
    assert bot.maybe_schedule(client) == 4
    texts = list(_queued_texts(client).values())
    assert slack_schedule.ping_text(12.0) in texts and slack_schedule.ping_text(10.0) not in texts
    assert any("Sat 11-07: 12 mi long" in t for t in texts)
    assert len(client.queued) == 4 and len(client.deleted) == 2


def test_live_replaces_the_sync_fallback_when_awake() -> None:
    shared, client = SharedDB(), ScheduleFake()
    db.save_plan(shared.conn, PLAN)
    _debrief_bot(FakeCoach([]), shared, _utc(1, 20)).maybe_schedule(client)
    awake = _debrief_bot(FakeCoach([]), shared, _utc(9, 0, 56))  # Sun 11-08 4:56pm LA
    awake.maybe_schedule(client)
    assert slack_schedule.SYNC_FALLBACK not in _queued_texts(client).values()
    live = _debrief_bot(FakeCoach([]), shared, _utc(9, 1))  # 5pm LA
    assert live.maybe_sync_nudge(client)
    assert client.posted[-1]["text"] == slack_bot.sync_nudge_text(0, 1)


def test_no_double_nudge_when_the_fallback_could_not_be_cancelled() -> None:
    shared, client = SharedDB(), ScheduleFake()
    db.save_plan(shared.conn, PLAN)
    _debrief_bot(FakeCoach([]), shared, _utc(1, 20)).maybe_schedule(client)
    client.fail_delete = True
    _debrief_bot(FakeCoach([]), shared, _utc(9, 0, 56)).maybe_schedule(client)
    assert not _debrief_bot(FakeCoach([]), shared, _utc(9, 1)).maybe_sync_nudge(client)
    assert client.posted == []  # Slack posts the queued fallback instead


def test_queued_ping_is_not_sent_live_and_still_opens_the_debrief() -> None:
    shared, client = SharedDB(), ScheduleFake()
    db.save_plan(shared.conn, PLAN)
    _debrief_bot(FakeCoach([]), shared, _utc(1, 20)).maybe_schedule(client)
    coach = FakeCoach(["Nice work. What did you eat before?"])
    bot = _debrief_bot(coach, shared, _utc(7, 20))  # Sat 11-07 noon LA, after Slack's 10am ping
    assert not bot.maybe_ping(client)  # Slack already sent it
    bot.handle(_dm("Did 10 in 1:40", "100.1"), client)
    assert coach.calls[0][0].startswith("(Long-run debrief for 2026-11-07: 10 mi long run")


def test_duplicate_events_are_answered_once() -> None:
    shared, client = SharedDB(), ScheduleFake()
    coach = FakeCoach(["Hi!"])
    bot = _debrief_bot(coach, shared, _utc(3, 20))
    bot.handle(_dm("hello", "200.1"), client)
    bot.handle(_dm("hello", "200.1"), client)  # Slack retry of the same event
    assert len(coach.calls) == 1


def test_catch_up_answers_missed_dms_once_and_starts_fresh_on_first_run() -> None:
    old = [_msg("from long ago", "50.0")]
    shared, client = SharedDB(), ScheduleFake(history=old)
    coach = FakeCoach(["Answer to both."])
    now = _utc(3, 20)
    bot = _debrief_bot(coach, shared, now)
    assert bot.catch_up(client) == 0  # first run: record the cursor, answer nothing
    t = now.timestamp()
    client.history += [  # arrive while the laptop sleeps
        _msg("ran 5 easy", f"{t + 600:.6f}"),
        {"ts": f"{t + 700:.6f}", "bot_id": "B1", "text": "bot msg"},
        _msg("legs feel heavy", f"{t + 900:.6f}"),
    ]
    bot = _debrief_bot(coach, shared, _utc(3, 22))  # wakes two hours later
    assert bot.catch_up(client) == 2
    question = coach.calls[0][0]
    assert question.startswith(slack_bot.OFFLINE_NOTE)
    assert "- ran 5 easy\n- legs feel heavy" in question
    assert bot.catch_up(client) == 0  # handled: never answered twice
    bot.handle(_dm("legs feel heavy", f"{t + 900:.6f}"), client)  # late Slack retry
    assert len(coach.calls) == 1


def _dm(text: str, ts: str) -> dict[str, Any]:
    return {"channel_type": "im", "channel": "D1", "user": SYDNEY, "text": text, "ts": ts}


def _msg(text: str, ts: str) -> dict[str, Any]:
    return {"user": SYDNEY, "text": text, "ts": ts}
