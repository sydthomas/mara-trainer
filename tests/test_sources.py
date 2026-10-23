from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mara_coach import sources
from mara_coach.sources import SourceError


def _raw(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "slug": "example-position-stand",
        "title": "Example position stand on fueling",
        "author": "Example Society",
        "kind": "position_stand",
        "url": "https://www.example.org/stand",
        "topics": ["fueling"],
        "license": "open_access",
        "year": 2020,
        "trust_reason": "Peer-reviewed consensus statement from a professional society.",
        "notes": "",
    }
    raw.update(overrides)
    return raw


def test_parse_derives_domain_without_www() -> None:
    s = sources.parse_source(_raw(url="https://WWW.Example.org/a?b=1"))
    assert s.domain == "example.org"
    assert s.status == "proposed" and s.approved_by is None


def test_book_without_url_has_no_domain() -> None:
    s = sources.parse_source(_raw(kind="book", url=None, license="copyrighted"))
    assert s.domain is None
    assert s.may_summarize is False


@pytest.mark.parametrize(
    ("license_", "allowed"),
    [
        ("open_access", True),
        ("cc_by", True),
        ("public_domain", True),
        ("copyrighted", False),
        ("unknown", False),
    ],
)
def test_may_summarize_follows_license(license_: str, allowed: bool) -> None:
    assert sources.parse_source(_raw(license=license_)).may_summarize is allowed


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"slug": "Not Kebab"}, "slug"),
        ({"kind": "blog"}, "kind"),
        ({"license": "free"}, "license"),
        ({"topics": []}, "topics"),
        ({"topics": ["fueling", "vibes"]}, "vibes"),
        ({"url": "http://example.org"}, "https"),
        ({"url": "https://"}, "url"),
        ({"trust_reason": "  "}, "trust_reason"),
        ({"year": 1800}, "year"),
        ({"notes": "x" * 1001}, "notes"),
        ({"title": ""}, "title"),
    ],
)
def test_parse_rejects_bad_fields(overrides: dict[str, Any], fragment: str) -> None:
    with pytest.raises(SourceError) as exc:
        sources.parse_source(_raw(**overrides))
    assert any(fragment in p for p in exc.value.problems)


def test_parse_rejects_unknown_and_missing_keys() -> None:
    raw = _raw(extra="x")
    del raw["license"]
    with pytest.raises(SourceError) as exc:
        sources.parse_source(raw)
    text = " ".join(exc.value.problems)
    assert "license" in text and "extra" in text


def test_add_list_and_duplicate_slug(conn: sqlite3.Connection) -> None:
    sources.add_source(conn, sources.parse_source(_raw()))
    with pytest.raises(SourceError, match="already exists"):
        sources.add_source(conn, sources.parse_source(_raw()))
    [s] = sources.list_sources(conn)
    assert s.slug == "example-position-stand" and s.topics == ("fueling",)


def test_add_many_is_idempotent_by_slug(conn: sqlite3.Connection) -> None:
    batch = [sources.parse_source(_raw()), sources.parse_source(_raw(slug="other-one"))]
    assert sources.add_many(conn, batch) == {
        "added": ["example-position-stand", "other-one"],
        "skipped": [],
    }
    assert sources.add_many(conn, batch) == {
        "added": [],
        "skipped": ["example-position-stand", "other-one"],
    }


def test_add_many_rejects_duplicate_slugs_in_batch(conn: sqlite3.Connection) -> None:
    batch = [sources.parse_source(_raw()), sources.parse_source(_raw())]
    with pytest.raises(SourceError, match="duplicate"):
        sources.add_many(conn, batch)
    assert sources.list_sources(conn) == []


def test_only_approved_sources_feed_domains(conn: sqlite3.Connection) -> None:
    sources.add_many(
        conn,
        [
            sources.parse_source(_raw()),
            sources.parse_source(_raw(slug="bb", url="https://journals.example.com/x")),
            sources.parse_source(_raw(slug="cc", url="https://example.org/other")),
            sources.parse_source(_raw(slug="book", kind="book", url=None)),
        ],
    )
    assert sources.approved_domains(conn) == []
    result = sources.approve(conn, ["example-position-stand", "cc", "book"], by="Sydney")
    assert result == {"approved": ["example-position-stand", "cc", "book"], "unchanged": []}
    assert sources.approved_domains(conn) == ["example.org"]
    approved = sources.list_sources(conn, status="approved")
    assert {s.slug for s in approved} == {"example-position-stand", "cc", "book"}
    assert all(s.approved_by == "Sydney" and s.approved_at for s in approved)


def test_approve_requires_a_name_and_known_slugs(conn: sqlite3.Connection) -> None:
    sources.add_source(conn, sources.parse_source(_raw()))
    with pytest.raises(SourceError, match="approver"):
        sources.approve(conn, ["example-position-stand"], by="  ")
    with pytest.raises(SourceError, match="nope"):
        sources.approve(conn, ["example-position-stand", "nope"], by="Sydney")
    assert sources.list_sources(conn, status="approved") == []


def test_approve_twice_is_unchanged(conn: sqlite3.Connection) -> None:
    sources.add_source(conn, sources.parse_source(_raw()))
    sources.approve(conn, ["example-position-stand"], by="Sydney")
    again = sources.approve(conn, ["example-position-stand"], by="Someone else")
    assert again == {"approved": [], "unchanged": ["example-position-stand"]}
    [s] = sources.list_sources(conn)
    assert s.approved_by == "Sydney"


def test_retire_removes_domain(conn: sqlite3.Connection) -> None:
    sources.add_source(conn, sources.parse_source(_raw()))
    sources.approve(conn, ["example-position-stand"], by="Sydney")
    assert sources.retire(conn, ["example-position-stand"]) == {
        "retired": ["example-position-stand"]
    }
    assert sources.approved_domains(conn) == []
    [s] = sources.list_sources(conn, status="retired")
    assert s.approved_by is None


def test_list_filters_by_topic(conn: sqlite3.Connection) -> None:
    sources.add_many(
        conn,
        [
            sources.parse_source(_raw()),
            sources.parse_source(_raw(slug="shoes", topics=["gear", "injury"])),
        ],
    )
    assert [s.slug for s in sources.list_sources(conn, topic="injury")] == ["shoes"]
    with pytest.raises(SourceError, match="topic"):
        sources.list_sources(conn, topic="vibes")


def test_db_rejects_approval_without_approver(conn: sqlite3.Connection) -> None:
    sources.add_source(conn, sources.parse_source(_raw()))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE sources SET status='approved'")


def test_load_seed_file(tmp_path: Path) -> None:
    path = tmp_path / "seed.json"
    path.write_text(json.dumps({"version": 1, "sources": [_raw(), _raw(slug="bb", kind="nope")]}))
    with pytest.raises(SourceError) as exc:
        sources.load_seed(path)
    assert exc.value.problems == ["source 'bb': unknown kind 'nope'"]


def test_shipped_seed_file_is_valid_and_unapproved() -> None:
    seed = sources.load_seed()
    assert 30 <= len(seed) <= 60
    assert all(s.status == "proposed" for s in seed)
    assert {t for s in seed for t in s.topics} == sources.TOPICS
    # Books are cited by reference only: never marked as summarizable.
    assert not any(s.may_summarize for s in seed if s.kind == "book")


def test_source_cli_flow(tmp_db: Path, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from mara_coach.cli import app

    runner = CliRunner()

    def run(*args: str):  # type: ignore[no-untyped-def]
        return runner.invoke(app, ["source", *args])

    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"version": 1, "sources": [_raw()]}))
    r = run("import", str(seed))
    assert r.exit_code == 0 and json.loads(r.output)["added"] == ["example-position-stand"]
    r = run(
        "add",
        "--slug",
        "a-book",
        "--title",
        "A Book",
        "--author",
        "A. Coach",
        "--kind",
        "book",
        "--topics",
        "training",
        "--license",
        "copyrighted",
        "--trust-reason",
        "Published coach.",
    )
    assert r.exit_code == 0, r.output
    bad = run(
        "add",
        "--slug",
        "x y",
        "--title",
        "t",
        "--author",
        "a",
        "--kind",
        "blog",
        "--topics",
        "training",
        "--license",
        "copyrighted",
        "--trust-reason",
        "r",
    )
    assert bad.exit_code == 1
    assert json.loads(run("domains").output) == {"allowed_domains": []}
    assert run("approve", "example-position-stand").exit_code != 0  # --by is required
    assert run("approve", "example-position-stand", "--by", "Sydney").exit_code == 0
    assert json.loads(run("domains").output) == {"allowed_domains": ["example.org"]}
    listed = json.loads(run("list", "--status", "proposed").output)
    assert [s["slug"] for s in listed["sources"]] == ["a-book"]
    assert run("retire", "missing-slug").exit_code == 1
    assert run("list", "--topic", "vibes").exit_code == 1


def test_race_organizer_and_course_topic(conn: sqlite3.Connection) -> None:
    s = sources.parse_source(
        _raw(slug="some-race-course", kind="race_organizer", topics=["course"], license="unknown")
    )
    sources.add_source(conn, s)
    assert [r.kind for r in sources.list_sources(conn, topic="course")] == ["race_organizer"]


def test_migration_keeps_existing_rows(tmp_path: Path) -> None:
    from mara_trainer import db

    path = tmp_path / "old.db"
    c = db.connect(path)
    c.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT)")
    for name, sql in db.migration_files():
        if name >= "003":
            break
        c.executescript(sql)
        c.execute("INSERT INTO schema_migrations(name) VALUES (?)", (name,))
    c.execute(
        "INSERT INTO sources(slug, title, author, kind, url, domain, topics, license, "
        "trust_reason, status, approved_by, approved_at) VALUES ('old-one', 't', 'a', 'review', "
        "'https://example.org', 'example.org', '[\"training\"]', 'cc_by', 'r', 'approved', "
        "'Sydney', '2026-10-19T00:00:00.000Z')"
    )
    c.commit()
    db.migrate(c)
    [s] = sources.list_sources(c)
    assert (s.slug, s.status, s.approved_by) == ("old-one", "approved", "Sydney")
    sources.add_source(c, sources.parse_source(_raw(kind="race_organizer", topics=["course"])))


def test_link_only_sources_never_feed_domains(conn: sqlite3.Connection) -> None:
    s = sources.parse_source(_raw(fetchable=False))
    assert s.fetchable is False and s.to_dict()["fetchable"] is False
    assert sources.parse_source(_raw(slug="other-one")).fetchable is True
    sources.add_many(
        conn,
        [
            s,
            sources.parse_source(
                _raw(slug="doi-one", url="https://doi.org/10.1/x", fetchable=False)
            ),
        ],
    )
    sources.approve(conn, ["example-position-stand", "doi-one"], by="Sydney")
    assert sources.approved_domains(conn) == []
    [listed, _] = sources.list_sources(conn)
    assert listed.fetchable is False
    # A fetchable approved source on the same domain opens it.
    sources.add_source(conn, sources.parse_source(_raw(slug="open-one")))
    sources.approve(conn, ["open-one"], by="Sydney")
    assert sources.approved_domains(conn) == ["example.org"]


def test_fetchable_must_be_bool() -> None:
    with pytest.raises(SourceError, match="fetchable"):
        sources.parse_source(_raw(fetchable="no"))


def test_doi_links_are_always_link_only() -> None:
    s = sources.parse_source(_raw(url="https://doi.org/10.1249/x", fetchable=True))
    assert s.fetchable is False


def test_cli_link_only_flag(tmp_db: Path) -> None:
    from typer.testing import CliRunner

    from mara_coach.cli import app

    args = [
        "source",
        "add",
        "--slug",
        "some-race-plans",
        "--title",
        "Plans",
        "--author",
        "Race",
        "--kind",
        "race_organizer",
        "--topics",
        "training",
        "--license",
        "copyrighted",
        "--trust-reason",
        "Official race site.",
        "--url",
        "https://race.example.com/plans",
        "--link-only",
    ]
    r = CliRunner().invoke(app, args)
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["source"]["fetchable"] is False


def test_female_evidence_defaults_unknown_and_validates() -> None:
    s = sources.parse_source(_raw())
    assert (s.female_evidence, s.population_note) == ("unknown", "")
    t = sources.parse_source(
        _raw(female_evidence="female_specific", population_note="Trained women only.")
    )
    assert t.to_dict()["female_evidence"] == "female_specific"
    assert t.women_tested is True and s.women_tested is False
    with pytest.raises(SourceError, match="female_evidence"):
        sources.parse_source(_raw(female_evidence="lots"))
    with pytest.raises(SourceError, match="population_note"):
        sources.parse_source(_raw(population_note="x" * 301))


def test_list_filters_women_tested(conn: sqlite3.Connection) -> None:
    sources.add_many(
        conn,
        [
            sources.parse_source(_raw(female_evidence="mostly_male")),
            sources.parse_source(_raw(slug="women-one", female_evidence="female_specific")),
            sources.parse_source(_raw(slug="split-one", female_evidence="sex_reported")),
        ],
    )
    got = [s.slug for s in sources.list_sources(conn, women_tested=True)]
    assert got == ["split-one", "women-one"]


def test_update_refreshes_metadata_but_keeps_approval(conn: sqlite3.Connection) -> None:
    sources.add_source(conn, sources.parse_source(_raw()))
    sources.approve(conn, ["example-position-stand"], by="Sydney")
    fresh = sources.parse_source(
        _raw(female_evidence="sex_reported", population_note="Reports results by sex.")
    )
    assert sources.add_many(conn, [fresh]) == {"added": [], "skipped": ["example-position-stand"]}
    result = sources.add_many(conn, [fresh], update=True)
    assert result == {"added": [], "updated": ["example-position-stand"], "skipped": []}
    [s] = sources.list_sources(conn)
    assert (s.female_evidence, s.status, s.approved_by) == ("sex_reported", "approved", "Sydney")


def test_update_skips_identical_rows(conn: sqlite3.Connection) -> None:
    src = sources.parse_source(_raw())
    sources.add_source(conn, src)
    assert sources.add_many(conn, [src], update=True) == {
        "added": [],
        "updated": [],
        "skipped": ["example-position-stand"],
    }


def test_shipped_seed_marks_research_population() -> None:
    research = [
        s for s in sources.load_seed() if s.kind in {"position_stand", "review", "research"}
    ]
    assert research and all(s.female_evidence != "not_applicable" for s in research)
    assert sum(s.women_tested for s in research) >= 8
