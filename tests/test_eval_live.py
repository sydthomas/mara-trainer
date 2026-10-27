from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from mara_coach.evals import EvalCase, parse_eval_set
from mara_coach.evals_runner import (
    CoachAnswerer,
    compare_runs,
    latest_run,
    live_report,
    run_eval,
    save_run,
    select_cases,
)


def _case(case_id: str, category: str = "fueling", **extra: Any) -> dict[str, Any]:
    return {
        "id": case_id,
        "category": category,
        "question": f"Question {case_id}?",
        "context": extra.pop("context", ""),
        "must": ["Answers the question"],
        "must_not": [],
        "expect_referral": False,
        "checks": {"require": [["carb"]]},
        **extra,
    }


CASES: list[EvalCase] = parse_eval_set(
    {
        "version": 1,
        "cases": [
            _case("a", context="Synthetic runner, no race result."),
            _case("b"),
            _case("c", category="gear"),
        ],
    }
)


@dataclass
class Reply:
    text: str
    usage: dict[str, int] = field(default_factory=dict)
    tools_used: list[str] = field(default_factory=list)


class FakeCoach:
    """Answers by case id found in the question; raises for ids in `fail`."""

    def __init__(self, answers: dict[str, str], fail: tuple[str, ...] = ()) -> None:
        self.answers, self.fail, self.questions = answers, fail, []  # type: ignore[var-annotated]

    def ask(self, question: str) -> Reply:
        self.questions.append(question)
        case_id = question.split()[1].rstrip("?")
        if case_id in self.fail:
            raise RuntimeError("API down")
        return Reply(self.answers[case_id], {"input_tokens": 100, "output_tokens": 20}, ["summary"])


def test_coach_answerer_sends_context_and_records_usage_and_errors() -> None:
    coach = FakeCoach({"a": "30-60 g carb per hour", "c": "no idea"}, fail=("b",))
    answerer = CoachAnswerer(coach)
    report = live_report(run_eval(CASES, answerer), answerer)
    assert (
        coach.questions[0]
        == "Question a?\n\n(Context for this question: Synthetic runner, no race result.)"
    )
    assert coach.questions[1] == "Question b?"  # no context, no note
    by_id = {r["id"]: r for r in report["results"]}
    assert by_id["a"]["status"] == "pass" and by_id["a"]["answer"] == "30-60 g carb per hour"
    assert by_id["a"]["tools_used"] == ["summary"]
    assert by_id["b"]["status"] == "missing" and by_id["b"]["error"] == "RuntimeError: API down"
    assert by_id["c"]["status"] == "fail"
    assert report["usage"] == {"input_tokens": 200, "output_tokens": 40}
    assert report["errors"] == 1


def test_select_cases_by_id_and_category() -> None:
    assert [c.id for c in select_cases(CASES)] == ["a", "b", "c"]
    assert [c.id for c in select_cases(CASES, only=["c", "a"])] == ["a", "c"]
    assert [c.id for c in select_cases(CASES, category="gear")] == ["c"]
    assert select_cases(CASES, only=["zzz"]) == []


def test_compare_runs_flags_regressions_and_fixes() -> None:
    before = {"results": [{"id": "a", "status": "pass"}, {"id": "b", "status": "fail"}]}
    after = {
        "results": [
            {"id": "a", "status": "fail"},
            {"id": "b", "status": "pass"},
            {"id": "c", "status": "pass"},
        ]
    }
    diff = compare_runs(before, after)
    assert diff["regressions"] == [{"id": "a", "before": "pass", "after": "fail"}]
    assert diff["fixes"] == [{"id": "b", "before": "fail", "after": "pass"}]
    assert diff["new"] == ["c"]


def test_save_and_find_latest_run(tmp_path: Path) -> None:
    assert latest_run(tmp_path) is None
    first = save_run({"results": []}, tmp_path, stamp="20261026-090000")
    second = save_run({"results": [{"id": "a"}]}, tmp_path, stamp="20261026-100000")
    assert first.name == "20261026-090000.json"
    assert latest_run(tmp_path) == second
    assert latest_run(tmp_path, before=second) == first
    assert json.loads(second.read_text())["results"] == [{"id": "a"}]


def test_cli_eval_live_saves_and_compares(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from mara_coach import cli

    evals_file = tmp_path / "evals.json"
    evals_file.write_text(json.dumps({"version": 1, "cases": [_case("a"), _case("b")]}))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "load_env", lambda: None)
    answers = {"a": "carb 30-60 g/h", "b": "carb too"}
    monkeypatch.setattr(cli.agent, "Coach", lambda conn, **_: FakeCoach(answers))
    out = tmp_path / "runs"
    args = ["eval", "live", "--evals", str(evals_file), "--out", str(out)]

    first = CliRunner().invoke(cli.app, args)
    assert first.exit_code == 0, first.output
    summary = json.loads(first.output)
    assert summary["totals"]["pass"] == 2 and summary["compared_with"] is None
    assert "answer" not in first.output  # answers stay in the saved file only
    saved = json.loads(Path(summary["saved"]).read_text())
    assert saved["results"][0]["answer"] == "carb 30-60 g/h"

    answers["b"] = "no idea"
    for old in out.glob("*.json"):  # same-second runs: give the first an older name
        old.rename(out / "20000101-000000.json")
    second = CliRunner().invoke(cli.app, args + ["--only", "b"])
    assert second.exit_code == 3
    changes = json.loads(second.output)["changes"]
    assert changes["regressions"] == [{"id": "b", "before": "pass", "after": "fail"}]


def test_cli_eval_live_needs_key_and_matching_cases(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from mara_coach import cli

    monkeypatch.setattr(cli, "load_env", lambda: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = CliRunner().invoke(cli.app, ["eval", "live", "--only", "nope"])
    assert r.exit_code == 1 and "no cases match" in r.output
    r = CliRunner().invoke(cli.app, ["eval", "live", "--category", "fueling"])
    assert r.exit_code == 1 and "ANTHROPIC_API_KEY" in r.output
