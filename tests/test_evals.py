from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from mara_coach.evals import CATEGORIES, DEFAULT_PATH, EvalSetError, load_eval_set, parse_eval_set


def _case(**overrides: Any) -> dict[str, Any]:
    case: dict[str, Any] = {
        "id": "fuel-long-run",
        "category": "fueling",
        "question": "What should I eat during an 18 mile run?",
        "context": "",
        "must": ["Gives a carbohydrate-per-hour range with a cited source"],
        "must_not": [],
        "expect_referral": False,
    }
    case.update(overrides)
    return case


def test_shipped_eval_set_loads() -> None:
    cases = load_eval_set()
    assert len(cases) >= 20
    assert {c.category for c in cases} == CATEGORIES
    assert sum(c.expect_referral for c in cases) >= 5


def test_shipped_ids_are_unique() -> None:
    counts = Counter(c.id for c in load_eval_set())
    assert [i for i, n in counts.items() if n > 1] == []


def test_default_path_points_at_repo_file() -> None:
    assert DEFAULT_PATH.name == "coach-questions.json"
    assert DEFAULT_PATH.exists()


def test_load_from_file(tmp_path: Path) -> None:
    f = tmp_path / "evals.json"
    f.write_text(json.dumps({"version": 1, "cases": [_case()]}), encoding="utf-8")
    [case] = load_eval_set(f)
    assert case.id == "fuel-long-run"
    assert case.must == ("Gives a carbohydrate-per-hour range with a cited source",)


def test_load_rejects_bad_json(tmp_path: Path) -> None:
    f = tmp_path / "evals.json"
    f.write_text("{not json", encoding="utf-8")
    with pytest.raises(EvalSetError):
        load_eval_set(f)


def test_rejects_wrong_top_level() -> None:
    with pytest.raises(EvalSetError, match="version"):
        parse_eval_set({"version": 2, "cases": []})
    with pytest.raises(EvalSetError, match="cases"):
        parse_eval_set({"version": 1})


def test_reports_every_problem_at_once() -> None:
    raw = {
        "version": 1,
        "cases": [
            _case(),
            _case(),  # duplicate id
            _case(id="bad-cat", category="vibes"),
            _case(id="no-must", must=[]),
            _case(id="Bad Id"),
        ],
    }
    with pytest.raises(EvalSetError) as exc:
        parse_eval_set(raw)
    problems = exc.value.problems
    assert any("duplicate id" in p for p in problems)
    assert any("unknown category" in p for p in problems)
    assert any("'must'" in p for p in problems)
    assert any("id must be" in p for p in problems)


def test_missing_field() -> None:
    case = _case()
    del case["question"]
    with pytest.raises(EvalSetError, match="question"):
        parse_eval_set({"version": 1, "cases": [case]})


def test_safety_cases_must_expect_referral() -> None:
    raw = {"version": 1, "cases": [_case(id="knee", category="safety_referral")]}
    with pytest.raises(EvalSetError, match="expect_referral"):
        parse_eval_set(raw)


def test_strings_must_be_non_empty() -> None:
    raw = {"version": 1, "cases": [_case(question="  ", must=["ok", ""])]}
    with pytest.raises(EvalSetError) as exc:
        parse_eval_set(raw)
    assert len(exc.value.problems) == 2
