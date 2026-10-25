from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from mara_coach import profile, sources, tools
from mara_coach.cli import app
from mara_coach.tools import ToolError
from mara_trainer import db
from mara_trainer.plan_rules import RaceSetup

runner = CliRunner()
EXAMPLE = Path(__file__).parents[1] / "examples" / "sample-plan.json"
EXPECTED = {
    "plan_brief",
    "summary",
    "validate_plan",
    "show_plan",
    "get_profile",
    "fuel_log",
    "search_sources",
    "allowed_domains",
    "read_knowledge",
}


def _setup() -> RaceSetup:
    return RaceSetup.from_dict(json.loads(EXAMPLE.read_text())["setup"])


def _source(slug: str, **overrides: Any) -> sources.Source:
    raw: dict[str, Any] = {
        "slug": slug,
        "title": "Synthetic fueling stand",
        "author": "Example Society",
        "kind": "position_stand",
        "url": f"https://{slug}.example.org/x",
        "topics": ["fueling"],
        "license": "open_access",
        "trust_reason": "Peer-reviewed consensus statement.",
    }
    raw.update(overrides)
    return sources.parse_source(raw)


def test_registry_is_complete_and_well_formed() -> None:
    specs = tools.tool_specs()
    assert {s["name"] for s in specs} == EXPECTED
    for spec in specs:
        assert set(spec) == {"name", "description", "input_schema"}
        assert spec["description"].strip()
        schema = spec["input_schema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema.get("required", [])) <= set(schema["properties"])
    json.dumps(specs)  # JSON-serializable as-is for an API tool list


def test_no_tool_mentions_strava() -> None:
    assert "strava" not in json.dumps(tools.tool_specs()).lower()


def test_unknown_tool_and_bad_args(conn: sqlite3.Connection) -> None:
    with pytest.raises(ToolError, match="unknown tool"):
        tools.call_tool(conn, "delete_everything", {})
    with pytest.raises(ToolError, match="unexpected"):
        tools.call_tool(conn, "show_plan", {"x": 1})
    with pytest.raises(ToolError, match="weeks"):
        tools.call_tool(conn, "summary", {"weeks": "two"})
    with pytest.raises(ToolError, match="weeks"):
        tools.call_tool(conn, "summary", {"weeks": 500})
    with pytest.raises(ToolError, match="weeks"):
        tools.call_tool(conn, "summary", {"weeks": True})
    with pytest.raises(ToolError, match="topic"):
        tools.call_tool(conn, "search_sources", {"topic": "vibes"})
    with pytest.raises(ToolError, match="path"):
        tools.call_tool(conn, "validate_plan", {})


def test_plan_brief_matches_core(conn: sqlite3.Connection) -> None:
    with pytest.raises(ToolError, match="no race set"):
        tools.call_tool(conn, "plan_brief", {"today": "2027-01-04"})
    db.save_race_setup(conn, _setup())
    got = tools.call_tool(conn, "plan_brief", {"today": "2027-01-04"})
    assert got["brief_date"] == "2027-01-04" and len(got["weeks"]) == 16
    assert got["feasibility_problems"] == []
    assert got["weeks"][0]["target_distance_meters"] > 0
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        tools.call_tool(conn, "plan_brief", {"today": "next week"})
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        tools.call_tool(conn, "plan_brief", {"today": "2027-01-04\n"})


def test_summary_keeps_missing_as_null(conn: sqlite3.Connection) -> None:
    got = tools.call_tool(conn, "summary", {"week": "last", "weeks": 2})
    assert got["summary"]["run_count"] == 0
    assert got["summary"]["average_sleep_hours"] is None
    assert len(got["weekly"]) == 2 and got["has_accepted_plan"] is False


def test_summary_needs_initialized_db() -> None:
    c = db.connect(":memory:")
    db.migrate(c)
    with pytest.raises(ToolError, match="init-db"):
        tools.summary(c)


def test_validate_plan_never_accepts(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "PLAN_DIRS", (EXAMPLE.parent, tmp_path))
    ok = tools.call_tool(conn, "validate_plan", {"path": str(EXAMPLE)})
    assert ok["ok"] is True and ok["violations"] == [] and ok["weeks"] == 16
    raw = json.loads(EXAMPLE.read_text())
    raw["weeks"][0]["sessions"][1]["distance_meters"] = 999
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(raw))
    got = tools.call_tool(conn, "validate_plan", {"path": str(bad)})
    assert got["ok"] is False and got["violations"][0]["week"] == 1
    assert db.load_accepted_plan(conn) is None
    with pytest.raises(ToolError, match=".json"):
        tools.call_tool(conn, "validate_plan", {"path": str(tmp_path / "plan.txt")})
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "secret.env")
    (tmp_path / "secret.env").write_text("TOKEN=x")
    with pytest.raises(ToolError, match=".json"):
        tools.call_tool(conn, "validate_plan", {"path": str(link)})
    with pytest.raises(ToolError, match="not found"):
        tools.call_tool(conn, "validate_plan", {"path": str(tmp_path / "missing.json")})


def test_show_plan_and_profile(conn: sqlite3.Connection) -> None:
    assert tools.call_tool(conn, "show_plan", {}) == {"plan": None}
    db.save_plan(conn, {"weeks": []})
    assert tools.call_tool(conn, "show_plan", {}) == {"plan": {"weeks": []}}
    assert tools.call_tool(conn, "get_profile", {})["profile"]["diet"] is None
    profile.update_profile(conn, {"diet": "Vegetarian", "allergies": []})
    got = tools.call_tool(conn, "get_profile", {})["profile"]
    assert got["diet"] == "Vegetarian" and got["allergies"] == []


def test_search_sources_returns_approved_only(conn: sqlite3.Connection) -> None:
    empty = tools.call_tool(conn, "search_sources", {"topic": "fueling"})
    assert empty["count"] == 0 and "no approved source" in empty["message"].lower()
    sources.add_many(
        conn,
        [
            _source("aa", female_evidence="mostly_male"),
            _source("bb", female_evidence="female_specific"),
            _source("cc"),
            _source("dd", topics=["gear"]),
        ],
    )
    sources.approve(conn, ["aa", "bb", "dd"], by="Sydney")
    got = tools.call_tool(conn, "search_sources", {"topic": "fueling"})
    assert [s["slug"] for s in got["sources"]] == ["aa", "bb"]
    assert all(s["status"] == "approved" for s in got["sources"])
    women = tools.call_tool(conn, "search_sources", {"topic": "fueling", "women_tested": True})
    assert [s["slug"] for s in women["sources"]] == ["bb"]
    assert tools.call_tool(conn, "search_sources", {})["count"] == 3
    assert tools.call_tool(conn, "allowed_domains", {}) == {
        "allowed_domains": ["aa.example.org", "bb.example.org", "dd.example.org"]
    }


def test_cli_tools_list_and_call(tmp_db: Path) -> None:
    runner.invoke(app, ["init-db"])
    r = runner.invoke(app, ["tools", "list"])
    assert r.exit_code == 0, r.output
    assert {t["name"] for t in json.loads(r.output)["tools"]} == EXPECTED
    r = runner.invoke(app, ["tools", "call", "get_profile"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["profile"]["notes"] is None
    r = runner.invoke(app, ["tools", "call", "summary", "--args", '{"weeks": 1}'])
    assert r.exit_code == 0, r.output
    assert len(json.loads(r.output)["weekly"]) == 1


def test_cli_tools_call_errors(tmp_db: Path) -> None:
    runner.invoke(app, ["init-db"])
    assert runner.invoke(app, ["tools", "call", "nope"]).exit_code == 1
    assert runner.invoke(app, ["tools", "call", "summary", "--args", "{bad"]).exit_code == 1
    assert runner.invoke(app, ["tools", "call", "summary", "--args", "[1]"]).exit_code == 1
    assert runner.invoke(app, ["tools", "call", "plan_brief"]).exit_code == 1


def test_validate_plan_only_reads_plan_dirs(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plans = tmp_path / "plans"
    plans.mkdir()
    outside = tmp_path / "elsewhere.json"
    outside.write_text(EXAMPLE.read_text())
    monkeypatch.setattr(tools, "PLAN_DIRS", (plans,))
    with pytest.raises(ToolError, match="plans/ or examples/"):
        tools.call_tool(conn, "validate_plan", {"path": str(outside)})
    with pytest.raises(ToolError, match="plans/ or examples/"):
        tools.call_tool(conn, "validate_plan", {"path": str(plans / ".." / "elsewhere.json")})
    escape = plans / "link.json"
    escape.symlink_to(outside)
    with pytest.raises(ToolError, match="plans/ or examples/"):
        tools.call_tool(conn, "validate_plan", {"path": str(escape)})
    (plans / "draft.json").write_text(EXAMPLE.read_text())
    assert tools.call_tool(conn, "validate_plan", {"path": str(plans / "draft.json")})["ok"]


def test_plan_dirs_default_to_repo_folders() -> None:
    assert [d.name for d in tools.PLAN_DIRS] == ["plans", "examples"]
    assert (tools.PLAN_DIRS[1] / "sample-plan.json").is_file()
