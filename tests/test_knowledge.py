from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mara_coach import knowledge, sources, tools
from mara_coach.cli import app
from mara_coach.knowledge import KnowledgeError

runner = CliRunner()

GOOD = """---
section: fueling-during
title: Fueling during runs
status: draft
approved_by:
approved_on:
content_sha256:
---
# Fueling during runs

## Bottom line for the coach
- Take carbs on runs over about an hour. [@aa]

## What the sources agree on
- Intake around 30-60 g per hour for runs of 1-2.5 hours, in mixed groups. [@aa] [@bb]

## Where they differ
- One source sets a higher ceiling for very long events,
  using glucose plus fructose. [@bb]

## Women-specific notes
- Evidence is mostly from men. [@aa]

## Gaps
- No approved source covers chews vs gels.

## Works cited
- [@aa] Doe, Jane. "A Synthetic Review." *Journal*, vol. 1, 2020, https://aa.example.org/x.
- [@bb] Roe, Rick. "Another Synthetic Review." *Journal*, vol. 2, 2021, https://bb.example.org/x.
"""


def _write(directory: Path, text: str = GOOD, name: str = "fueling-during") -> Path:
    path = directory / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _approve_sources(conn: sqlite3.Connection, *slugs: str) -> None:
    sources.add_many(
        conn,
        [
            sources.parse_source(
                {
                    "slug": slug,
                    "title": "Synthetic",
                    "author": "Example",
                    "kind": "review",
                    "url": f"https://{slug}.example.org/x",
                    "topics": ["fueling"],
                    "license": "cc_by",
                    "trust_reason": "Peer reviewed.",
                }
            )
            for slug in slugs
        ],
    )
    sources.approve(conn, list(slugs), by="Sydney")


def test_good_section_parses_and_passes() -> None:
    section = knowledge.parse(GOOD)
    assert section.name == "fueling-during" and section.state == "draft"
    assert section.cited() == {"aa", "bb"} and section.works_cited() == ["aa", "bb"]
    assert knowledge.check(section, {"aa", "bb"}) == []


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (("using glucose plus fructose. [@bb]", "using glucose plus fructose."), "uncited claim"),
        (("## Gaps", "## Holes"), "headings must be"),
        (("[@aa] [@bb]\n", "[@aa] [@cc]\n"), "missing from Works cited: cc"),
        (("- Evidence is mostly from men. [@aa]", "- Mostly men. [@aa]\n- [@bb] dup"), None),
        (
            ("Take carbs on runs", 'Per the paper, "take in carbohydrate during every run that '),
            "quote too long",
        ),
        (("around 30-60 g per hour", "about 300 kcal per hour"), "calorie numbers"),
    ],
)
def test_check_catches_problems(change: tuple[str, str], problem: str | None) -> None:
    text = GOOD.replace(*change)
    if 'Per the paper, "' in text:
        text = text.replace(
            "over about an hour. [@aa]", 'lasts longer than one hour of time." [@aa]'
        )
    problems = knowledge.check(knowledge.parse(text))
    if problem is None:
        assert problems == []  # an extra cited bullet is fine
    else:
        assert any(problem in p for p in problems), problems


def test_check_flags_unapproved_and_unused_sources() -> None:
    section = knowledge.parse(GOOD)
    assert knowledge.check(section, {"aa"}) == ["cites a source that isn't approved: bb"]
    extra = GOOD + "- [@zz] Extra. *Journal*, 2020.\n"
    assert "in Works cited but never cited: zz" in knowledge.check(knowledge.parse(extra))


def test_parse_rejects_bad_front_matter() -> None:
    with pytest.raises(KnowledgeError, match="front matter"):
        knowledge.parse("# no front matter\n")
    with pytest.raises(KnowledgeError, match="status"):
        knowledge.parse(GOOD.replace("status: draft", "status: final"))
    with pytest.raises(KnowledgeError, match="unknown front matter key"):
        knowledge.parse(GOOD.replace("title:", "author: x\ntitle:"))


def test_load_checks_name_and_section(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeError, match="unknown section"):
        knowledge.load("vibes", tmp_path)
    with pytest.raises(KnowledgeError, match="not been written"):
        knowledge.load("gear", tmp_path)
    _write(tmp_path, name="gear")
    with pytest.raises(KnowledgeError, match="expected 'gear'"):
        knowledge.load("gear", tmp_path)


def test_approve_records_hash_and_edits_reset_it(tmp_path: Path) -> None:
    path = _write(tmp_path)
    with pytest.raises(KnowledgeError, match="isn't approved: bb"):
        knowledge.approve("fueling-during", "Sydney", {"aa"}, tmp_path)
    with pytest.raises(KnowledgeError, match="--by"):
        knowledge.approve("fueling-during", " ", {"aa", "bb"}, tmp_path)
    done = knowledge.approve("fueling-during", "Sydney", {"aa", "bb"}, tmp_path, "2026-10-24")
    assert done.state == "approved"
    reloaded = knowledge.load("fueling-during", tmp_path)
    assert reloaded.state == "approved" and reloaded.approved_by == "Sydney"
    assert reloaded.approved_on == "2026-10-24"
    path.write_text(path.read_text().replace("30-60 g", "40-70 g"))
    assert knowledge.load("fueling-during", tmp_path).state == "changed_since_approval"


def test_read_knowledge_serves_approved_only(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "KNOWLEDGE_DIR", tmp_path)
    _write(tmp_path)
    listing = tools.call_tool(conn, "read_knowledge", {})
    assert [s["section"] for s in listing["sections"]] == ["fueling-during"]
    assert "gear" in listing["not_written"]
    draft = tools.call_tool(conn, "read_knowledge", {"section": "fueling-during"})
    assert draft["state"] == "draft" and "content" not in draft
    with pytest.raises(tools.ToolError, match="not been written"):
        tools.call_tool(conn, "read_knowledge", {"section": "gear"})
    with pytest.raises(tools.ToolError, match="section must be one of"):
        tools.call_tool(conn, "read_knowledge", {"section": "vibes"})

    _approve_sources(conn, "aa", "bb")
    knowledge.approve("fueling-during", "Sydney", {"aa", "bb"}, tmp_path)
    got = tools.call_tool(conn, "read_knowledge", {"section": "fueling-during"})
    assert got["state"] == "approved" and "30-60 g per hour" in got["content"]

    sources.retire(conn, ["bb"])
    stale = tools.call_tool(conn, "read_knowledge", {"section": "fueling-during"})
    assert stale["state"] == "needs_review" and "content" not in stale


def test_cli_check_and_approve(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    monkeypatch.setattr(tools, "KNOWLEDGE_DIR", kdir)
    runner.invoke(app, ["init-db"])
    _write(kdir)
    r = runner.invoke(app, ["knowledge", "check"])
    assert r.exit_code == 3 and "isn't approved" in r.output
    r = runner.invoke(app, ["knowledge", "approve", "fueling-during", "--by", "Sydney"])
    assert r.exit_code == 1
    from mara_trainer import db

    c = db.connect()
    _approve_sources(c, "aa", "bb")
    c.close()
    r = runner.invoke(app, ["knowledge", "check", "fueling-during"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["knowledge", "approve", "fueling-during", "--by", "Sydney"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["knowledge", "list"])
    assert json.loads(r.output)["sections"][0]["state"] == "approved"


def test_repo_sections_pass_against_seed_registry() -> None:
    """Every written section passes the checks and cites only registry slugs."""
    seed = {s.slug for s in sources.load_seed()}
    for section in knowledge.load_all():
        assert knowledge.check(section, seed) == [], section.name
