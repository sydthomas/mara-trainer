"""Source registry: the vetted references the coach may cite.

Rows hold links, metadata and our own short notes, never copied text. A source starts as
`proposed`; only a named person can approve it, and only approved sources with a URL feed the
agent's allowed web domains. Link-only sources (`fetchable: false`) and books are cited by
reference and never fetched.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_SEED = Path(__file__).resolve().parents[2] / "sources" / "seed.json"

TOPICS = frozenset(
    {
        "training",
        "fueling",
        "daily_nutrition",
        "race_week",
        "hydration",
        "gear",
        "recovery",
        "cross_training",
        "injury",
        "health",
        "course",
    }
)
KINDS = frozenset(
    {
        "position_stand",
        "review",
        "research",
        "guideline",
        "organization",
        "coach",
        "book",
        "race_organizer",
    }
)
LICENSES = frozenset({"open_access", "cc_by", "public_domain", "copyrighted", "unknown"})
SUMMARIZABLE = frozenset({"open_access", "cc_by", "public_domain"})
STATUSES = frozenset({"proposed", "approved", "retired"})
# URL resolvers redirect anywhere, so allowing them would open every publisher.
LINK_ONLY_DOMAINS = frozenset({"doi.org", "dx.doi.org"})
# How well the evidence covers women. Most sport science has studied mostly men, so the coach
# prefers women-tested sources and says so when only male data exists.
FEMALE_EVIDENCE = frozenset(
    {
        "female_specific",
        "sex_reported",
        "mixed_unreported",
        "mostly_male",
        "not_applicable",
        "unknown",
    }
)
WOMEN_TESTED = frozenset({"female_specific", "sex_reported"})

_SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_REQUIRED = ("slug", "title", "author", "kind", "url", "topics", "license", "trust_reason")
_OPTIONAL = ("year", "notes", "fetchable", "female_evidence", "population_note")
_COLUMNS = (
    "slug, title, author, kind, url, domain, topics, license, year, trust_reason, notes, "
    "fetchable, female_evidence, population_note, status, approved_by, approved_at"
)
# Metadata columns written on insert and refreshed by `add_many(update=True)`; never status.
_META = (
    "slug, title, author, kind, url, domain, topics, license, year, trust_reason, notes, "
    "fetchable, female_evidence, population_note"
)


class SourceError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Source:
    slug: str
    title: str
    author: str
    kind: str
    url: str | None
    topics: tuple[str, ...]
    license: str
    trust_reason: str
    year: int | None = None
    notes: str = ""
    fetchable: bool = True
    female_evidence: str = "unknown"
    population_note: str = ""
    status: str = "proposed"
    approved_by: str | None = None
    approved_at: str | None = None

    @property
    def domain(self) -> str | None:
        return domain_of(self.url) if self.url else None

    @property
    def women_tested(self) -> bool:
        return self.female_evidence in WOMEN_TESTED

    @property
    def may_summarize(self) -> bool:
        """Open-access and public-domain material may be summarized; the rest is cite-only."""
        return self.license in SUMMARIZABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "title": self.title,
            "author": self.author,
            "kind": self.kind,
            "url": self.url,
            "domain": self.domain,
            "topics": list(self.topics),
            "license": self.license,
            "may_summarize": self.may_summarize,
            "year": self.year,
            "trust_reason": self.trust_reason,
            "notes": self.notes,
            "fetchable": self.fetchable,
            "female_evidence": self.female_evidence,
            "population_note": self.population_note,
            "status": self.status,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
        }


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def parse_source(raw: Any) -> Source:
    if not isinstance(raw, dict):
        raise SourceError(["source must be an object"])
    problems: list[str] = []
    missing = [k for k in _REQUIRED if k not in raw]
    if missing:
        problems.append(f"missing {', '.join(missing)}")
    unknown = sorted(set(raw) - set(_REQUIRED) - set(_OPTIONAL))
    if unknown:
        problems.append(f"unknown keys {', '.join(unknown)}")
    if problems:
        raise SourceError(problems)

    slug, kind, lic, url = raw["slug"], raw["kind"], raw["license"], raw["url"]
    if not isinstance(slug, str) or not _SLUG.match(slug) or not 2 <= len(slug) <= 80:
        problems.append("slug must be lowercase-kebab-case, 2-80 chars")
    for key, limit in (("title", 300), ("author", 300), ("trust_reason", 500)):
        if not _non_empty_str(raw[key]) or len(raw[key]) > limit:
            problems.append(f"{key} must be a non-empty string of at most {limit} chars")
    if kind not in KINDS:
        problems.append(f"unknown kind {kind!r}")
    if lic not in LICENSES:
        problems.append(f"unknown license {lic!r}")
    if url is not None:
        if not isinstance(url, str) or not url.startswith("https://"):
            problems.append("url must start with https:// (or be null)")
        elif not domain_of(url):
            problems.append(f"url has no host: {url!r}")
    topics = raw["topics"]
    if not isinstance(topics, list) or not topics:
        problems.append("topics must be a non-empty list")
    else:
        bad = [t for t in topics if t not in TOPICS]
        if bad:
            problems.append(f"unknown topics {bad}")
    year = raw.get("year")
    if year is not None and (not isinstance(year, int) or not 1900 <= year <= 2100):
        problems.append("year must be an integer 1900-2100 (or null)")
    fetchable = raw.get("fetchable", True)
    if not isinstance(fetchable, bool):
        problems.append("fetchable must be true or false")
    female = raw.get("female_evidence", "unknown")
    if female not in FEMALE_EVIDENCE:
        problems.append(f"unknown female_evidence {female!r}")
    pop_note = raw.get("population_note", "")
    if not isinstance(pop_note, str) or len(pop_note) > 300:
        problems.append("population_note must be a string of at most 300 chars")
    notes = raw.get("notes", "")
    if not isinstance(notes, str) or len(notes) > 1000:
        problems.append("notes must be a string of at most 1000 chars")
    if problems:
        raise SourceError(problems)
    return Source(
        slug=slug,
        title=raw["title"].strip(),
        author=raw["author"].strip(),
        kind=kind,
        url=url,
        topics=tuple(dict.fromkeys(topics)),
        license=lic,
        trust_reason=raw["trust_reason"].strip(),
        year=year,
        notes=notes.strip(),
        fetchable=fetchable and (url is None or domain_of(url) not in LINK_ONLY_DOMAINS),
        female_evidence=female,
        population_note=pop_note.strip(),
    )


def load_seed(path: Path = DEFAULT_SEED) -> list[Source]:
    """Parse a seed file `{"version": 1, "sources": [...]}`. Every entry loads as `proposed`."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceError([f"cannot read {path}: {exc}"]) from exc
    if (
        not isinstance(raw, dict)
        or raw.get("version") != 1
        or not isinstance(raw.get("sources"), list)
    ):
        raise SourceError(["seed must be an object with version 1 and a 'sources' list"])
    problems: list[str] = []
    parsed: list[Source] = []
    for i, item in enumerate(raw["sources"]):
        label = item.get("slug", i) if isinstance(item, dict) else i
        try:
            parsed.append(parse_source(item))
        except SourceError as exc:
            problems.extend(f"source {label!r}: {p}" for p in exc.problems)
    if problems:
        raise SourceError(problems)
    return parsed


def add_source(conn: sqlite3.Connection, source: Source) -> None:
    result = add_many(conn, [source])
    if result["skipped"]:
        raise SourceError([f"source {source.slug!r} already exists"])


def add_many(
    conn: sqlite3.Connection, batch: list[Source], update: bool = False
) -> dict[str, list[str]]:
    """Insert new sources as `proposed`. Existing slugs are skipped, or with `update=True` get
    their metadata refreshed. Status and approval are never changed here."""
    slugs = [s.slug for s in batch]
    dupes = sorted({s for s in slugs if slugs.count(s) > 1})
    if dupes:
        raise SourceError([f"duplicate slugs in batch: {', '.join(dupes)}"])
    existing = {s.slug: s for s in _load_slugs(conn, slugs)}
    added: list[str] = []
    updated: list[str] = []
    marks = ", ".join("?" * len(_META.split(", ")))
    with conn:
        for s in batch:
            old = existing.get(s.slug)
            if old is None:
                conn.execute(f"INSERT INTO sources({_META}) VALUES ({marks})", _meta_values(s))
                added.append(s.slug)
            elif update and _meta_values(old) != _meta_values(s):
                sets = ", ".join(f"{c} = ?" for c in _META.split(", ")[1:])
                conn.execute(
                    f"UPDATE sources SET {sets} WHERE slug = ?", (*_meta_values(s)[1:], s.slug)
                )
                updated.append(s.slug)
    skipped = [s for s in slugs if s in existing and s not in updated]
    if update:
        return {"added": added, "updated": updated, "skipped": skipped}
    return {"added": added, "skipped": skipped}


def _meta_values(s: Source) -> tuple[object, ...]:
    return (
        s.slug,
        s.title,
        s.author,
        s.kind,
        s.url,
        s.domain,
        json.dumps(list(s.topics)),
        s.license,
        s.year,
        s.trust_reason,
        s.notes,
        int(s.fetchable),
        s.female_evidence,
        s.population_note,
    )


def list_sources(
    conn: sqlite3.Connection,
    status: str | None = None,
    topic: str | None = None,
    women_tested: bool = False,
) -> list[Source]:
    if status is not None and status not in STATUSES:
        raise SourceError([f"unknown status {status!r}"])
    if topic is not None and topic not in TOPICS:
        raise SourceError([f"unknown topic {topic!r}"])
    sql = f"SELECT {_COLUMNS} FROM sources"
    where: list[str] = []
    params: list[str] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if topic:
        where.append("EXISTS (SELECT 1 FROM json_each(sources.topics) WHERE value = ?)")
        params.append(topic)
    if women_tested:
        where.append(f"female_evidence IN ({', '.join(repr(v) for v in sorted(WOMEN_TESTED))})")
    if where:
        sql += " WHERE " + " AND ".join(where)
    return [_row_to_source(r) for r in conn.execute(sql + " ORDER BY slug", params)]


def approve(conn: sqlite3.Connection, slugs: list[str], by: str) -> dict[str, list[str]]:
    """Mark sources approved by a named person. All-or-nothing; already-approved rows keep
    their original approver."""
    if not by.strip():
        raise SourceError(["approver name is required"])
    statuses = _statuses(conn, slugs)
    approved = [s for s in slugs if statuses[s] != "approved"]
    with conn:
        conn.executemany(
            "UPDATE sources SET status='approved', approved_by=?, "
            "approved_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE slug=?",
            [(by.strip(), s) for s in approved],
        )
    return {"approved": approved, "unchanged": [s for s in slugs if s not in approved]}


def retire(conn: sqlite3.Connection, slugs: list[str]) -> dict[str, list[str]]:
    """Stop citing these sources. Rows are kept so past citations stay explainable."""
    _statuses(conn, slugs)
    with conn:
        conn.executemany(
            "UPDATE sources SET status='retired', approved_by=NULL, approved_at=NULL WHERE slug=?",
            [(s,) for s in slugs],
        )
    return {"retired": list(slugs)}


def approved_domains(conn: sqlite3.Connection) -> list[str]:
    """Domains the agent may fetch from at answer time: approved, fetchable sources only."""
    rows = conn.execute(
        "SELECT DISTINCT domain FROM sources "
        "WHERE status='approved' AND fetchable = 1 AND domain IS NOT NULL ORDER BY domain"
    )
    return [str(r[0]) for r in rows]


def _load_slugs(conn: sqlite3.Connection, slugs: list[str]) -> list[Source]:
    if not slugs:
        return []
    marks = ",".join("?" * len(slugs))
    rows = conn.execute(f"SELECT {_COLUMNS} FROM sources WHERE slug IN ({marks})", slugs)
    return [_row_to_source(r) for r in rows]


def _statuses(conn: sqlite3.Connection, slugs: list[str]) -> dict[str, str]:
    if not slugs:
        raise SourceError(["give at least one slug"])
    marks = ",".join("?" * len(slugs))
    found = {
        r[0]: r[1]
        for r in conn.execute(f"SELECT slug, status FROM sources WHERE slug IN ({marks})", slugs)
    }
    unknown = [s for s in slugs if s not in found]
    if unknown:
        raise SourceError([f"unknown slugs: {', '.join(unknown)}"])
    return found


def _row_to_source(r: sqlite3.Row) -> Source:
    return Source(
        slug=r["slug"],
        title=r["title"],
        author=r["author"],
        kind=r["kind"],
        url=r["url"],
        topics=tuple(json.loads(r["topics"])),
        license=r["license"],
        trust_reason=r["trust_reason"],
        year=r["year"],
        notes=r["notes"],
        fetchable=bool(r["fetchable"]),
        female_evidence=r["female_evidence"],
        population_note=r["population_note"],
        status=r["status"],
        approved_by=r["approved_by"],
        approved_at=r["approved_at"],
    )


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
