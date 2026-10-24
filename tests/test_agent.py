from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest
from typer.testing import CliRunner

from mara_coach import agent, sources
from mara_coach.cli import app
from mara_trainer import fueling


def _text(t: str) -> NS:
    return NS(type="text", text=t)


def _tool_use(name: str, args: Any, id_: str = "tu_1") -> NS:
    return NS(type="tool_use", name=name, input=args, id=id_)


def _resp(stop: str, *blocks: NS) -> NS:
    return NS(stop_reason=stop, content=list(blocks), usage=NS(input_tokens=100, output_tokens=20))


class FakeAPI:
    """Scripted stand-in for client.beta.messages: returns the queued responses in order."""

    def __init__(self, *responses: NS) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> NS:
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


def test_plain_answer_and_request_shape(conn: sqlite3.Connection) -> None:
    api = FakeAPI(_resp("end_turn", NS(type="thinking", thinking=""), _text("Run easy today.")))
    reply = agent.Coach(conn, api, today="2026-11-03").ask("What should I run?")
    assert reply.text == "Run easy today." and reply.stop_reason == "end_turn"
    call = api.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"] == {"effort": "medium"}
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert "thinking" not in call and "temperature" not in call
    assert call["messages"][0]["content"].startswith("(Today is 2026-11-03.)")
    assert reply.usage == {"input_tokens": 100, "output_tokens": 20}


def test_tool_loop_runs_real_tools(conn: sqlite3.Connection) -> None:
    fueling.add_entry(
        conn, fueling.FuelEntry(date="2026-10-24", carbs_grams=27, duration_seconds=5391)
    )
    api = FakeAPI(
        _resp("tool_use", _tool_use("fuel_log", {"last": 5}), _tool_use("nope", {}, "tu_2")),
        _resp("end_turn", _text("You took about 18 g per hour.")),
    )
    reply = agent.Coach(conn, api).ask("How was my fueling?")
    assert reply.tools_used == ["fuel_log", "nope"]
    results = api.calls[1]["messages"][-1]["content"]
    good = json.loads(results[0]["content"])
    assert good["entries"][0]["carbs_per_hour"] == 18.0 and "is_error" not in results[0]
    assert results[1]["is_error"] is True and "unknown tool" in results[1]["content"]
    assert results[1]["tool_use_id"] == "tu_2"


def test_bad_tool_args_are_errors_not_crashes(conn: sqlite3.Connection) -> None:
    api = FakeAPI(
        _resp("tool_use", _tool_use("fuel_log", {"last": 0})),
        _resp("end_turn", _text("ok")),
    )
    agent.Coach(conn, api).ask("x")
    assert api.calls[1]["messages"][-1]["content"][0]["is_error"] is True


def test_refusal_and_pause_turn(conn: sqlite3.Connection) -> None:
    api = FakeAPI(
        _resp("pause_turn", NS(type="server_tool_use", name="web_fetch", id="s1", input={})),
        _resp("refusal"),
    )
    reply = agent.Coach(conn, api).ask("x")
    assert reply.stop_reason == "refusal" and "can't help" in reply.text
    assert reply.tools_used == ["web_fetch"]
    # pause_turn re-sends without adding a user message
    assert api.calls[1]["messages"][-1]["role"] == "assistant"


def test_step_limit(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "MAX_STEPS", 2)
    api = FakeAPI(*[_resp("tool_use", _tool_use("get_profile", {})) for _ in range(2)])
    with pytest.raises(agent.CoachError, match="2 steps"):
        agent.Coach(conn, api).ask("x")


def test_web_fetch_only_with_approved_domains(conn: sqlite3.Connection) -> None:
    names = [t["name"] for t in agent.Coach(conn, FakeAPI()).tool_definitions()]
    assert "web_fetch" not in names and "fuel_log" in names
    sources.add_source(
        conn,
        sources.parse_source(
            {
                "slug": "example-fueling-2020",
                "title": "Synthetic fueling stand",
                "author": "Example Society",
                "kind": "position_stand",
                "url": "https://journal.example.org/x",
                "topics": ["fueling"],
                "license": "open_access",
                "trust_reason": "Synthetic test source",
            }
        ),
    )
    sources.approve(conn, ["example-fueling-2020"], by="Sydney")
    fetch = [t for t in agent.Coach(conn, FakeAPI()).tool_definitions() if t["name"] == "web_fetch"]
    assert fetch[0]["allowed_domains"] == ["journal.example.org"]


def test_history_is_kept(conn: sqlite3.Connection) -> None:
    api = FakeAPI(_resp("end_turn", _text("a")), _resp("end_turn", _text("b")))
    coach = agent.Coach(conn, api)
    first = coach.ask("one")
    coach.ask("two", history=first.messages)
    assert [m["role"] for m in api.calls[1]["messages"]] == ["user", "assistant", "user"]


def test_system_prompt_rules() -> None:
    p = agent.SYSTEM_PROMPT
    for phrase in (
        "No approved source covers this",
        "qualified professional",
        "Strava",
        "dietitian",
    ):
        assert phrase in p


def test_cli_ask_without_key(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("mara_coach.cli.load_env", lambda: None)
    r = CliRunner().invoke(app, ["ask", "hi"])
    assert r.exit_code == 1 and "ANTHROPIC_API_KEY" in r.output


def test_cli_ask_with_fake(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    fake = FakeAPI(_resp("end_turn", _text("Easy 3 today.")))
    monkeypatch.setattr(agent, "_default_messages_api", lambda: fake)
    r = CliRunner().invoke(app, ["init-db", "--timezone", "America/Los_Angeles"])
    r = CliRunner().invoke(app, ["ask", "What today?"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["answer"] == "Easy 3 today."
