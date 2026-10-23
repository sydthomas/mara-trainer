"""Eval set for the coach: synthetic questions plus what a good answer must and must not do.

The file is hand-written JSON (`evals/coach-questions.json`). This module loads and checks its
shape. `must` and `must_not` are rubric lines for a human (or later a model) grader. The optional
`checks` object holds what code can verify deterministically: `require` (groups of regexes, one
per group must match), `forbid` (regexes that must not match) and `cite` (the answer must carry a
citation). Referral cases are always checked for a referral. Scoring lives in `evals_runner`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "evals" / "coach-questions.json"

CATEGORIES = frozenset(
    {
        "plan",
        "fueling",
        "daily_nutrition",
        "race_week",
        "gear",
        "recovery",
        "cross_training",
        "safety_referral",
        "data_rules",
    }
)

_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_FIELDS = ("id", "category", "question", "context", "must", "must_not", "expect_referral")
_CHECK_KEYS = frozenset({"require", "forbid", "cite"})


class EvalSetError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Checks:
    """Deterministic checks. Patterns are case-insensitive Python regexes."""

    require: tuple[tuple[str, ...], ...] = ()
    forbid: tuple[str, ...] = ()
    cite: bool = False

    @property
    def is_empty(self) -> bool:
        return not (self.require or self.forbid or self.cite)


@dataclass(frozen=True)
class EvalCase:
    id: str
    category: str
    question: str
    context: str
    must: tuple[str, ...]
    must_not: tuple[str, ...]
    expect_referral: bool
    checks: Checks = field(default_factory=Checks)


def load_eval_set(path: Path = DEFAULT_PATH) -> list[EvalCase]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalSetError([f"cannot read {path}: {exc}"]) from exc
    return parse_eval_set(raw)


def parse_eval_set(raw: Any) -> list[EvalCase]:
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise EvalSetError(["top level must be an object with version 1"])
    items = raw.get("cases")
    if not isinstance(items, list):
        raise EvalSetError(["'cases' must be a list"])

    problems: list[str] = []
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        label = f"case {i}"
        if not isinstance(item, dict):
            problems.append(f"{label}: must be an object")
            continue
        missing = [f for f in _FIELDS if f not in item]
        if missing:
            problems.append(f"{label}: missing {', '.join(missing)}")
            continue
        case_problems = _check_case(item)
        case_id = item["id"]
        if isinstance(case_id, str):
            label = f"case {case_id!r}"
            if case_id in seen:
                case_problems.append("duplicate id")
            seen.add(case_id)
        if case_problems:
            problems.extend(f"{label}: {p}" for p in case_problems)
            continue
        cases.append(
            EvalCase(
                id=item["id"],
                category=item["category"],
                question=item["question"],
                context=item["context"],
                must=tuple(item["must"]),
                must_not=tuple(item["must_not"]),
                expect_referral=item["expect_referral"],
                checks=_to_checks(item.get("checks")),
            )
        )
    if problems:
        raise EvalSetError(problems)
    return cases


def _check_case(item: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if not isinstance(item["id"], str) or not _ID.match(item["id"]):
        problems.append("id must be lowercase-kebab-case")
    if item["category"] not in CATEGORIES:
        problems.append(f"unknown category {item['category']!r}")
    if not _non_empty_str(item["question"]):
        problems.append("'question' must be a non-empty string")
    if not isinstance(item["context"], str):
        problems.append("'context' must be a string")
    for key, allow_empty in (("must", False), ("must_not", True)):
        value = item[key]
        if not isinstance(value, list) or (not value and not allow_empty):
            problems.append(f"'{key}' must be a {'' if allow_empty else 'non-empty '}list")
        elif not all(_non_empty_str(v) for v in value):
            problems.append(f"'{key}' entries must be non-empty strings")
    if not isinstance(item["expect_referral"], bool):
        problems.append("'expect_referral' must be true or false")
    elif item["category"] == "safety_referral" and not item["expect_referral"]:
        problems.append("safety_referral cases need expect_referral: true")
    if "checks" in item:
        problems.extend(_check_checks(item["checks"]))
    return problems


def _check_checks(checks: Any) -> list[str]:
    if not isinstance(checks, dict):
        return ["'checks' must be an object"]
    problems: list[str] = []
    unknown = sorted(set(checks) - _CHECK_KEYS)
    if unknown:
        problems.append(f"'checks' has unknown keys {', '.join(unknown)}")
    require = checks.get("require", [])
    if not isinstance(require, list) or not all(
        isinstance(g, list) and g and all(_non_empty_str(p) for p in g) for g in require
    ):
        problems.append("'require' must be a list of non-empty lists of patterns")
    else:
        problems.extend(_bad_patterns(p for g in require for p in g))
    forbid = checks.get("forbid", [])
    if not isinstance(forbid, list) or not all(_non_empty_str(p) for p in forbid):
        problems.append("'forbid' must be a list of non-empty patterns")
    else:
        problems.extend(_bad_patterns(forbid))
    if not isinstance(checks.get("cite", False), bool):
        problems.append("'cite' must be true or false")
    return problems


def _bad_patterns(patterns: Any) -> list[str]:
    problems: list[str] = []
    for p in patterns:
        try:
            re.compile(p)
        except re.error as exc:
            problems.append(f"bad pattern {p!r}: {exc}")
    return problems


def _to_checks(raw: Any) -> Checks:
    if raw is None:
        return Checks()
    return Checks(
        require=tuple(tuple(g) for g in raw.get("require", [])),
        forbid=tuple(raw.get("forbid", [])),
        cite=raw.get("cite", False),
    )


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
