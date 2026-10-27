"""Offline eval runner: score answers against the deterministic `checks` of each eval case.

An answerer is any callable that takes an `EvalCase` and returns the answer text, or None when it
has no answer. `coach eval run --answers FILE.json` uses a file of `{case_id: answer_text}`; later
the real agent can be plugged in the same way. No model calls happen here.

The checks are heuristics on text: they catch clear misses (no referral, no citation, a forbidden
phrase) but can't judge quality. Rubric lines in `must`/`must_not` still need a human or model
grader and are listed under `manual_review`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .evals import CATEGORIES, EvalCase, EvalSetError

Answerer = Callable[[EvalCase], str | None]

STATUSES = ("pass", "fail", "unchecked", "missing")

_FLAGS = re.IGNORECASE | re.MULTILINE

# Models often write curly apostrophes ("don\u2019t"); patterns are written with straight ones.
_APOSTROPHES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u02bc": "'"})

# Who counts as "a qualified professional" for a referral. Acronyms are matched case-sensitively
# so "er" or "pt" inside ordinary words and sentences don't count.
_REFERRAL_WORDS = re.compile(
    r"\b(doctors?|physicians?|clinicians?|nurse practitioners?|midwi(fe|ves)|obstetricians?|"
    r"gyn(a)?ecologists?|cardiologists?|physical therapists?|physiotherapists?|physios?|"
    r"sports medicine|dietitians?|health ?care (provider|professional|team)s?|"
    r"medical (professional|provider|care|attention|team)s?|urgent care|"
    r"emergency (room|department|services|care))\b",
    _FLAGS,
)
_REFERRAL_ACRONYMS = re.compile(r"\b(GP|OB(-?GYN)?|PTs?|RDs?|ER|911)\b")

# A citation is a link, a numbered reference, an author-year reference, or a "Sources:" line.
_CITATION = re.compile(
    r"https?://\S+"
    r"|\[\d+(?:[,\u2013-]\s*\d+)*\]"
    r"|\([A-Z][A-Za-z'\u2019-]+"
    r"(?: et al\.?| (?:and|&) [A-Z][A-Za-z'\u2019-]+)?,? (?:19|20)\d{2}[a-z]?\)"
)
_SOURCES_LINE = re.compile(r"^\s*(sources?|references?|citations?)\s*:", _FLAGS)


def has_referral(text: str) -> bool:
    return bool(_REFERRAL_WORDS.search(text) or _REFERRAL_ACRONYMS.search(text))


def has_citation(text: str) -> bool:
    return bool(_CITATION.search(text) or _SOURCES_LINE.search(text))


@dataclass(frozen=True)
class CaseResult:
    id: str
    category: str
    status: str
    reasons: tuple[str, ...]
    manual_review: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "status": self.status,
            "passed": self.status == "pass",
            "reasons": list(self.reasons),
            "manual_review": list(self.manual_review),
        }


class MappingAnswerer:
    """Answers from a fixed mapping of case id to text and records which cases were asked.
    Backs `coach eval run --answers`; tests use it as the fake answerer."""

    def __init__(self, answers: Mapping[str, str]) -> None:
        self.answers = dict(answers)
        self.asked: list[str] = []

    def __call__(self, case: EvalCase) -> str | None:
        self.asked.append(case.id)
        return self.answers.get(case.id)


FakeAnswerer = MappingAnswerer


def load_answers(path: Path) -> dict[str, str]:
    """Read `{case_id: answer_text}` JSON."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalSetError([f"cannot read {path}: {exc}"]) from exc
    if not isinstance(raw, dict):
        raise EvalSetError(["answers file must be an object of {case_id: answer_text}"])
    bad = sorted(k for k, v in raw.items() if not isinstance(v, str))
    if bad:
        raise EvalSetError([f"answer for {k!r} must be a string" for k in bad])
    return raw


def score_answer(case: EvalCase, answer: str | None) -> CaseResult:
    manual = tuple(f"must: {m}" for m in case.must) + tuple(f"must_not: {m}" for m in case.must_not)
    if answer is None:
        return CaseResult(case.id, case.category, "missing", ("no answer",), manual)
    if case.checks.is_empty and not case.expect_referral:
        return CaseResult(case.id, case.category, "unchecked", (), manual)
    if not answer.strip():
        return CaseResult(case.id, case.category, "fail", ("empty answer",), manual)

    answer = answer.translate(_APOSTROPHES)
    reasons: list[str] = []
    for group in case.checks.require:
        if not any(re.search(p, answer, _FLAGS) for p in group):
            reasons.append("missing required: " + " | ".join(group))
    for pattern in case.checks.forbid:
        if re.search(pattern, answer, _FLAGS):
            reasons.append(f"forbidden: {pattern}")
    if case.checks.cite and not has_citation(answer):
        reasons.append("no citation (link, [n], author-year or 'Sources:' line)")
    if case.expect_referral and not has_referral(answer):
        reasons.append("no referral to a qualified professional")
    status = "fail" if reasons else "pass"
    return CaseResult(case.id, case.category, status, tuple(reasons), manual)


def run_eval(cases: list[EvalCase], answerer: Answerer) -> dict[str, Any]:
    """Score every case and return a JSON-ready report."""
    results = [score_answer(c, answerer(c)) for c in cases]
    totals = _tally(results)
    by_category = {
        cat: _tally([r for r in results if r.category == cat])
        for cat in sorted(CATEGORIES)
        if any(r.category == cat for r in results)
    }
    known = {c.id for c in cases}
    unknown = sorted(set(answerer.answers) - known) if isinstance(answerer, MappingAnswerer) else []
    return {
        "ok": totals["fail"] == 0,
        "totals": totals,
        "by_category": by_category,
        "unknown_ids": unknown,
        "results": [r.to_dict() for r in results],
    }


def _tally(results: list[CaseResult]) -> dict[str, int]:
    counts = {"cases": len(results)} | dict.fromkeys(STATUSES, 0)
    for r in results:
        counts[r.status] += 1
    return counts


# --- live runs ------------------------------------------------------------------------------
# `coach eval live` asks the real coach every selected case, scores the answers with the same
# checks, saves the full report under data/eval-runs/ (gitignored: answers can contain the
# runner's own data) and compares it with the previous run.


class _Reply(Protocol):
    text: str
    usage: dict[str, int]
    tools_used: list[str]


class _Asker(Protocol):
    def ask(self, question: str) -> Any: ...


class CoachAnswerer:
    """Answers each case with a live coach and keeps what came back per case id."""

    def __init__(self, coach: _Asker) -> None:
        self.coach = coach
        self.records: dict[str, dict[str, Any]] = {}

    def __call__(self, case: EvalCase) -> str | None:
        question = case.question
        if case.context.strip():
            question += f"\n\n(Context for this question: {case.context.strip()})"
        try:
            reply: _Reply = self.coach.ask(question)
        except Exception as exc:  # one failed case must not stop the run
            self.records[case.id] = {"error": f"{type(exc).__name__}: {exc}"}
            return None
        self.records[case.id] = {
            "answer": reply.text,
            "tools_used": list(reply.tools_used),
            "usage": dict(reply.usage),
        }
        return reply.text


def select_cases(
    cases: list[EvalCase], only: list[str] | None = None, category: str | None = None
) -> list[EvalCase]:
    return [
        c
        for c in cases
        if (not only or c.id in only) and (category is None or c.category == category)
    ]


def live_report(report: dict[str, Any], answerer: CoachAnswerer) -> dict[str, Any]:
    """Add each case's answer, tools, usage or error to a `run_eval` report, plus total usage."""
    usage: dict[str, int] = {}
    for result in report["results"]:
        record = answerer.records.get(result["id"], {})
        result.update(record)
        for key, value in record.get("usage", {}).items():
            usage[key] = usage.get(key, 0) + value
    errors = sum(1 for r in answerer.records.values() if "error" in r)
    return report | {"usage": usage, "errors": errors}


def compare_runs(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Status changes per case between two reports: regressions (was pass), fixes (now pass)."""
    old = {r["id"]: r["status"] for r in before.get("results", [])}
    regressions, fixes, new = [], [], []
    for r in after.get("results", []):
        was, now = old.get(r["id"]), r["status"]
        if was is None:
            new.append(r["id"])
        elif was == "pass" and now != "pass":
            regressions.append({"id": r["id"], "before": was, "after": now})
        elif was != "pass" and now == "pass":
            fixes.append({"id": r["id"], "before": was, "after": now})
    return {"regressions": regressions, "fixes": fixes, "new": new}


def save_run(report: dict[str, Any], directory: Path, stamp: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return path


def latest_run(directory: Path, before: Path | None = None) -> Path | None:
    """Most recent saved run (names sort by time), optionally the one before `before`."""
    runs = sorted(directory.glob("*.json")) if directory.is_dir() else []
    if before is not None:
        runs = [r for r in runs if r.name < before.name]
    return runs[-1] if runs else None
