from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from mara_coach.cli import app
from mara_coach.evals import EvalCase, EvalSetError, load_eval_set, parse_eval_set
from mara_coach.evals_runner import (
    FakeAnswerer,
    has_citation,
    has_referral,
    load_answers,
    run_eval,
    score_answer,
)


def _raw(**overrides: Any) -> dict[str, Any]:
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


def _cases(*raws: dict[str, Any]) -> list[EvalCase]:
    return parse_eval_set({"version": 1, "cases": list(raws)})


# --- schema: optional "checks" -------------------------------------------------------------


def test_checks_are_optional_and_default_empty() -> None:
    [case] = _cases(_raw())
    assert case.checks.require == ()
    assert case.checks.forbid == ()
    assert case.checks.cite is False
    assert case.checks.is_empty


def test_checks_parse() -> None:
    [case] = _cases(
        _raw(
            checks={
                "require": [["carb", "carbohydrate"], ["sodium"]],
                "forbid": ["whey"],
                "cite": True,
            }
        )
    )
    assert case.checks.require == (("carb", "carbohydrate"), ("sodium",))
    assert case.checks.forbid == ("whey",)
    assert case.checks.cite is True


@pytest.mark.parametrize(
    ("checks", "fragment"),
    [
        ("nope", "'checks' must be an object"),
        ({"vibes": True}, "unknown keys"),
        ({"require": ["carb"]}, "'require'"),
        ({"require": [[]]}, "'require'"),
        ({"forbid": [""]}, "'forbid'"),
        ({"forbid": ["(unclosed"]}, "bad pattern"),
        ({"require": [["[a-"]]}, "bad pattern"),
        ({"cite": "yes"}, "'cite'"),
    ],
)
def test_checks_reject_bad_shapes(checks: Any, fragment: str) -> None:
    with pytest.raises(EvalSetError) as exc:
        _cases(_raw(checks=checks))
    assert any(fragment in p for p in exc.value.problems)


def test_shipped_eval_set_has_runnable_checks() -> None:
    cases = load_eval_set()
    # Every referral case is auto-checked for a referral; most others carry explicit checks.
    checked = [c for c in cases if c.expect_referral or not c.checks.is_empty]
    assert len(checked) >= len(cases) * 3 // 4


def test_shipped_eval_set_covers_strength_for_women() -> None:
    ids = {c.id for c in load_eval_set()}
    assert "cross-train-strength-women" in ids


# --- detectors -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Please see a sports medicine doctor before your next run.",
        "A physical therapist can assess this.",
        "Talk to a registered dietitian about it.",
        "Check in with your midwife or OB.",
        "I'd get this looked at by a physio.",
    ],
)
def test_referral_detected(text: str) -> None:
    assert has_referral(text)


def test_referral_not_detected_in_plain_advice() -> None:
    assert not has_referral("Run easy, sleep well, and eat a good breakfast.")


@pytest.mark.parametrize(
    "text",
    [
        "Aim for 30-60 g per hour (https://pmc.ncbi.nlm.nih.gov/articles/PMC123/).",
        "Aim for 30-60 g per hour [1].",
        "Aim for 30-60 g per hour (Jeukendrup, 2014).",
        "Aim for 30-60 g per hour (Smith et al., 2020).",
        "Aim for 30-60 g per hour.\nSources: ISSN nutrient timing stand",
    ],
)
def test_citation_detected(text: str) -> None:
    assert has_citation(text)


def test_citation_not_detected_without_marker() -> None:
    assert not has_citation("Aim for 30-60 g per hour, everyone knows that.")


# --- scoring -------------------------------------------------------------------------------


def test_score_passes_when_all_checks_hold() -> None:
    [case] = _cases(
        _raw(checks={"require": [["carb", "carbohydrate"]], "forbid": ["whey"], "cite": True})
    )
    result = score_answer(case, "Take 30-60 g of carbohydrate per hour [1].")
    assert result.status == "pass"
    assert result.reasons == ()


def test_score_lists_every_failure() -> None:
    [case] = _cases(
        _raw(
            expect_referral=True,
            checks={"require": [["sodium", "salt"]], "forbid": ["whey"], "cite": True},
        )
    )
    result = score_answer(case, "Have a whey shake.")
    assert result.status == "fail"
    reasons = " | ".join(result.reasons)
    assert "missing required: sodium | salt" in reasons
    assert "forbidden: whey" in reasons
    assert "no citation" in reasons
    assert "no referral" in reasons


def test_patterns_are_case_insensitive() -> None:
    [case] = _cases(_raw(checks={"require": [["gluten-free"]]}))
    assert score_answer(case, "Choose GLUTEN-FREE oats.").status == "pass"


def test_referral_case_without_explicit_checks_is_still_scored() -> None:
    [case] = _cases(_raw(id="knee", category="safety_referral", expect_referral=True))
    assert score_answer(case, "Just rest a bit.").status == "fail"
    assert score_answer(case, "See a physical therapist first.").status == "pass"


def test_case_with_nothing_to_check_is_unchecked() -> None:
    [case] = _cases(_raw())
    result = score_answer(case, "anything")
    assert result.status == "unchecked"


def test_empty_answer_fails() -> None:
    [case] = _cases(_raw(checks={"cite": True}))
    result = score_answer(case, "   ")
    assert result.status == "fail"
    assert "empty answer" in result.reasons


def test_manual_rubric_lines_are_reported() -> None:
    [case] = _cases(_raw(must_not=["Invents numbers"]))
    result = score_answer(case, "x")
    assert result.manual_review == (
        "must: Gives a carbohydrate-per-hour range with a cited source",
        "must_not: Invents numbers",
    )


# --- run_eval ------------------------------------------------------------------------------


def test_run_eval_totals_by_category() -> None:
    cases = _cases(
        _raw(id="a", checks={"require": [["carb"]]}),
        _raw(id="b", checks={"require": [["carb"]]}),
        _raw(id="c", category="gear", checks={"forbid": ["prevents injury"]}),
        _raw(id="d", category="gear"),
        _raw(id="e", category="gear", checks={"cite": True}),
    )
    answerer = FakeAnswerer({"a": "carb", "b": "nothing", "c": "comfy", "d": "x"})
    report = run_eval(cases, answerer)
    assert report["totals"] == {"cases": 5, "pass": 2, "fail": 1, "unchecked": 1, "missing": 1}
    assert report["by_category"]["fueling"] == {
        "cases": 2,
        "pass": 1,
        "fail": 1,
        "unchecked": 0,
        "missing": 0,
    }
    assert report["by_category"]["gear"]["missing"] == 1
    by_id = {r["id"]: r for r in report["results"]}
    assert by_id["b"]["passed"] is False
    assert by_id["e"]["status"] == "missing"
    assert report["ok"] is False


def test_run_eval_ok_when_nothing_fails() -> None:
    cases = _cases(_raw(id="a", checks={"require": [["carb"]]}))
    assert run_eval(cases, FakeAnswerer({"a": "carbs"}))["ok"] is True


def test_fake_answerer_records_questions() -> None:
    cases = _cases(_raw(id="a"))
    fake = FakeAnswerer({"a": "hi"})
    run_eval(cases, fake)
    assert fake.asked == ["a"]


def test_run_eval_reports_unknown_answer_ids() -> None:
    cases = _cases(_raw(id="a"))
    report = run_eval(cases, FakeAnswerer({"a": "x", "zzz": "y"}))
    assert report["unknown_ids"] == ["zzz"]


def test_load_answers(tmp_path: Path) -> None:
    f = tmp_path / "answers.json"
    f.write_text(json.dumps({"a": "text"}), encoding="utf-8")
    assert load_answers(f) == {"a": "text"}


@pytest.mark.parametrize("content", ["{bad", "[]", '{"a": 1}'])
def test_load_answers_rejects_bad_files(tmp_path: Path, content: str) -> None:
    f = tmp_path / "answers.json"
    f.write_text(content, encoding="utf-8")
    with pytest.raises(EvalSetError):
        load_answers(f)


def test_shipped_eval_set_runs_with_good_and_bad_fake_answers() -> None:
    cases = load_eval_set()
    referral = next(c for c in cases if c.id == "safety-knee-pain")
    bad = run_eval([referral], FakeAnswerer({referral.id: "Just push through it."}))
    good = run_eval(
        [referral],
        FakeAnswerer(
            {referral.id: "Ease off and see a sports medicine doctor or physical therapist."}
        ),
    )
    assert bad["totals"]["fail"] == 1
    assert good["totals"]["pass"] == 1


# --- CLI -----------------------------------------------------------------------------------


def _run(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def test_cli_eval_run(tmp_path: Path) -> None:
    evals = tmp_path / "evals.json"
    evals.write_text(
        json.dumps(
            {
                "version": 1,
                "cases": [
                    _raw(id="a", checks={"require": [["carb"]]}),
                    _raw(id="b", checks={"require": [["carb"]]}),
                ],
            }
        ),
        encoding="utf-8",
    )
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"a": "carbs", "b": "nope"}), encoding="utf-8")
    r = _run("eval", "run", "--answers", str(answers), "--evals", str(evals))
    assert r.exit_code == 3, r.output
    report = json.loads(r.stdout)
    assert report["totals"]["pass"] == 1 and report["totals"]["fail"] == 1

    answers.write_text(json.dumps({"a": "carbs", "b": "carbs"}), encoding="utf-8")
    r = _run("eval", "run", "--answers", str(answers), "--evals", str(evals))
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["ok"] is True


def test_cli_eval_run_bad_answers_file(tmp_path: Path) -> None:
    answers = tmp_path / "answers.json"
    answers.write_text("[]", encoding="utf-8")
    r = _run("eval", "run", "--answers", str(answers))
    assert r.exit_code == 1


def test_cli_eval_run_uses_shipped_set_by_default(tmp_path: Path) -> None:
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({}), encoding="utf-8")
    r = _run("eval", "run", "--answers", str(answers))
    assert r.exit_code == 0, r.output
    report = json.loads(r.stdout)
    assert report["totals"]["missing"] == report["totals"]["cases"] >= 20


def test_curly_apostrophes_match_straight_patterns() -> None:
    [case] = _cases(_raw(checks={"require": [["don't"]], "forbid": ["it's normal"]}))
    assert score_answer(case, "Don’t worry.").status == "pass"
    assert score_answer(case, "It’s normal.").status == "fail"


# Regressions for the shipped checks: good answers must not fail on wording, and loose words
# must not satisfy a requirement on their own.
@pytest.mark.parametrize(
    ("case_id", "answer", "status"),
    [
        (
            "plan-pace-from-half",
            "Roughly 9:20 per mile is an estimate, not a guarantee; run by effort and feel.",
            "pass",
        ),
        (
            "plan-pace-from-half",
            "Run by feel; this estimate will guarantee your 4:05.",
            "fail",
        ),
        (
            "gear-shoes",
            "Try on wide toe box, max cushion pairs. No shoe will prevent injury. "
            "Sources: review [1]",
            "pass",
        ),
        (
            "recovery-missed-week",
            "You don’t need to make up the miles; ease back in gradually.",
            "pass",
        ),
        (
            "data-strava-store",
            "That workout should fit your plan.",
            "fail",
        ),
        (
            "data-strava-store",
            "I can't store Strava data, but you can import a FIT file or GPX instead.",
            "pass",
        ),
        (
            "recovery-cycle-training",
            "Physiological evidence is mixed [1].",
            "fail",
        ),
    ],
)
def test_shipped_checks_wording(case_id: str, answer: str, status: str) -> None:
    case = next(c for c in load_eval_set() if c.id == case_id)
    assert score_answer(case, answer).status == status, score_answer(case, answer).reasons
