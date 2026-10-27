from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from typer.testing import CliRunner

from mara_coach import agent, usage
from mara_coach.cli import app

from .test_agent import FakeAPI, _text


def _resp(model: str = "claude-opus-5-5", **tokens: int) -> NS:
    counts = {"input_tokens": 1000, "output_tokens": 500} | tokens
    return NS(
        stop_reason="end_turn", content=[_text("zebra-answer")], usage=NS(**counts), model=model
    )


def test_cost_uses_per_model_prices() -> None:
    # Opus 5.5: 1M input $4, 1M output $20, cache read $0.20, cache write 1.25x input
    tokens = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
    }
    assert usage.cost_usd("claude-opus-5-5", tokens) == pytest.approx(4 + 20 + 0.20 + 5.00)
    assert usage.cost_usd("claude-sonnet-5-5", {"output_tokens": 1_000_000}) == pytest.approx(10)
    assert usage.cost_usd("claude-haiku-5-5", {"input_tokens": 1_000_000}) == pytest.approx(0.10)
    assert usage.cost_usd("some-unknown-model", tokens) is None


def test_coach_logs_one_row_per_answer_without_text(conn: sqlite3.Connection) -> None:
    api = FakeAPI(_resp(cache_read_input_tokens=2000))
    agent.Coach(conn, api, channel="slack").ask("Secret question about my knee?")
    rows = usage.rows(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["channel"] == "slack" and row["model"] == "claude-opus-5-5" and row["steps"] == 1
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_input_tokens"]) == (
        1000,
        500,
        2000,
    )
    assert row["cost_usd"] == pytest.approx((1000 * 4 + 500 * 20 + 2000 * 0.2) / 1e6)
    dump = json.dumps(rows)
    assert "knee" not in dump and "zebra" not in dump  # counts only, never content


def test_served_model_is_priced_when_a_fallback_answers(conn: sqlite3.Connection) -> None:
    agent.Coach(conn, FakeAPI(_resp(model="claude-sonnet-5-5"))).ask("Q?")
    row = usage.rows(conn)[0]
    assert row["model"] == "claude-sonnet-5-5" and row["channel"] == "cli"
    assert row["cost_usd"] == pytest.approx((1000 * 2 + 500 * 10) / 1e6)


def test_report_totals_by_channel_and_projection(conn: sqlite3.Connection) -> None:
    for channel, at in (
        ("cli", "2026-10-20T10:00:00.000Z"),
        ("slack", "2026-10-25T10:00:00.000Z"),
        ("slack", "2026-10-26T10:00:00.000Z"),
    ):
        usage.record(
            conn,
            channel,
            "claude-opus-5-5",
            {"input_tokens": 10_000, "output_tokens": 1_000},
            2,
            at=at,
        )
    report = usage.report(conn, since="2026-10-24", today="2026-10-26")
    each = (10_000 * 4 + 1_000 * 20) / 1e6  # $0.06
    assert report["answers"] == 2
    assert report["cost_usd"] == pytest.approx(2 * each)
    assert report["avg_cost_per_answer_usd"] == pytest.approx(each)
    assert report["by_channel"] == {"slack": {"answers": 2, "cost_usd": pytest.approx(2 * each)}}
    assert report["days"] == 3  # 24th..26th inclusive
    assert report["projected_30_day_usd"] == pytest.approx(2 * each / 3 * 30)
    assert usage.report(conn, since="2027-01-01", today="2027-01-02")["answers"] == 0


def test_cli_usage(tmp_db: Path) -> None:
    runner = CliRunner()
    runner.invoke(app, ["init-db"])
    r = runner.invoke(app, ["usage", "--days", "7"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["answers"] == 0 and data["cost_usd"] == 0
    assert "prices_as_of" in data
