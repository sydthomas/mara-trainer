"""Coaching knowledge base: what the approved sources say, by topic, in our own words.

One Markdown file per section in `knowledge/` (format in `knowledge/README.md`). Every claim
cites registry slugs as `[@slug]`. A section starts as `draft`; only a named person approves it,
and approval records a hash of the body, so any later edit sends it back to review. The coach's
`read_knowledge` tool serves approved sections only.

Checks (`check`): known section name, the six headings in order, a cite on every claim bullet,
cites and Works cited match, cites resolve to known (or approved) sources, no long quotes, no
calorie numbers.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge"

SECTIONS: dict[str, str] = {
    "training": "Running: weekly structure, easy vs hard, long run, workouts, load, plans",
    "strength": "Strength training for runners",
    "cross-training": "Non-running training and concurrent training",
    "fueling-before": "Eating before runs and the race, carb loading",
    "fueling-during": "Carbs, gels, gut training and caffeine during runs and the race",
    "fueling-after": "Recovery eating: carbs, protein, timing",
    "daily-nutrition": "Everyday eating for training, protein, micronutrients, supplements",
    "hydration": "Fluids and sodium, heat, hyponatremia prevention",
    "recovery": "Sleep, rest days, recovery practices",
    "race-week": "Taper, race-week routine, race-day pacing and logistics",
    "gear": "Shoes (including carbon plates) and other gear",
    "women": "Menstrual cycle, contraception, low energy availability / RED-S, iron, postpartum",
    "health-referrals": "Warning signs and when to send the runner to a professional",
    "course": "Course-specific preparation (LA Marathon first)",
}
HEADINGS = (
    "Bottom line for the coach",
    "What the sources agree on",
    "Where they differ",
    "Women-specific notes",
    "Gaps",
    "Works cited",
)
CLAIM_HEADINGS = HEADINGS[:4]
STATUSES = frozenset({"draft", "approved"})
_FRONT_KEYS = ("section", "title", "status", "approved_by", "approved_on", "content_sha256")

_CITE = re.compile(r"\[@([a-z0-9]+(?:-[a-z0-9]+)*)\]")
_QUOTE = re.compile(r"[\"“]([^\"“”]+)[\"”]")
_CALORIES = re.compile(r"\d[\d,.]*\s*(?:kcal|kilocalories|calories|cal)\b", re.IGNORECASE)
MAX_QUOTE_WORDS = 12


class KnowledgeError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def body_hash(body: str) -> str:
    return hashlib.sha256(body.replace("\r\n", "\n").strip().encode("utf-8")).hexdigest()


@dataclass
class Section:
    name: str
    title: str
    status: str
    approved_by: str | None
    approved_on: str | None
    content_sha256: str | None
    body: str

    @property
    def state(self) -> str:
        """approved, draft, or changed_since_approval (approved, then the body was edited)."""
        if self.status != "approved":
            return "draft"
        if self.content_sha256 != body_hash(self.body):
            return "changed_since_approval"
        return "approved"

    def parts(self) -> dict[str, str]:
        """Body text under each `## ` heading, in file order."""
        out: dict[str, str] = {}
        current: str | None = None
        for line in self.body.splitlines():
            if line.startswith("## "):
                current = line[3:].strip()
                out[current] = ""
            elif current is not None:
                out[current] += line + "\n"
        return out

    def cited(self) -> set[str]:
        """Slugs cited in the claim sections and Gaps (everything but Works cited)."""
        return {
            slug
            for heading, text in self.parts().items()
            if heading != "Works cited"
            for slug in _CITE.findall(text)
        }

    def works_cited(self) -> list[str]:
        """Slugs that open a Works cited entry, in order."""
        text = self.parts().get("Works cited", "")
        return [m.group(1) for b in _bullets(text) if (m := _CITE.match(b))]

    def summary(self) -> dict[str, Any]:
        return {
            "section": self.name,
            "title": self.title,
            "state": self.state,
            "approved_by": self.approved_by,
            "approved_on": self.approved_on,
        }

    def render(self) -> str:
        values = {
            "section": self.name,
            "title": self.title,
            "status": self.status,
            "approved_by": self.approved_by,
            "approved_on": self.approved_on,
            "content_sha256": self.content_sha256,
        }
        front = "\n".join(f"{k}: {values[k] or ''}".rstrip() for k in _FRONT_KEYS)
        return f"---\n{front}\n---\n{self.body.strip()}\n"


def _bullets(text: str) -> list[str]:
    """Top-level `- ` list items, with their wrapped and nested lines joined in."""
    items: list[str] = []
    for line in text.splitlines():
        if line.startswith("- "):
            items.append(line[2:])
        elif items and line.strip():
            items[-1] += " " + line.strip()
    return items


def parse(text: str) -> Section:
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        raise KnowledgeError(["missing front matter (file must start with ---)"])
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise KnowledgeError(["front matter is not closed with ---"]) from None
    front: dict[str, str | None] = {}
    problems: list[str] = []
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            problems.append(f"bad front matter line: {line!r}")
            continue
        front[key.strip()] = value.strip() or None
    problems += [f"unknown front matter key {k}" for k in sorted(set(front) - set(_FRONT_KEYS))]
    if not front.get("section"):
        problems.append("front matter needs section")
    if not front.get("title"):
        problems.append("front matter needs title")
    status = front.get("status") or "draft"
    if status not in STATUSES:
        problems.append(f"status must be one of {', '.join(sorted(STATUSES))}")
    if problems:
        raise KnowledgeError(problems)
    return Section(
        name=str(front["section"]),
        title=str(front["title"]),
        status=status,
        approved_by=front.get("approved_by"),
        approved_on=front.get("approved_on"),
        content_sha256=front.get("content_sha256"),
        body="\n".join(lines[end + 1 :]).strip() + "\n",
    )


def path_for(name: str, directory: Path | None = None) -> Path:
    if name not in SECTIONS:
        raise KnowledgeError([f"unknown section {name!r}; known: {', '.join(SECTIONS)}"])
    return (directory or KNOWLEDGE_DIR) / f"{name}.md"


def load(name: str, directory: Path | None = None) -> Section:
    path = path_for(name, directory)
    if not path.is_file():
        raise KnowledgeError([f"section {name!r} has not been written yet"])
    section = parse(path.read_text(encoding="utf-8"))
    if section.name != name:
        raise KnowledgeError([f"{path.name} says section {section.name!r}; expected {name!r}"])
    return section


def load_all(directory: Path | None = None) -> list[Section]:
    """Every written section, in SECTIONS order. Missing files are skipped."""
    return [load(n, directory) for n in SECTIONS if path_for(n, directory).is_file()]


def check(section: Section, known_slugs: set[str] | None = None) -> list[str]:
    """Problems with a section. `known_slugs`: the slugs it may cite (None skips that check)."""
    problems: list[str] = []
    if section.name not in SECTIONS:
        problems.append(f"unknown section {section.name!r}")
    parts = section.parts()
    if tuple(parts) != HEADINGS:
        problems.append("headings must be, in order: " + " / ".join(HEADINGS))
    for heading in CLAIM_HEADINGS:
        items = _bullets(parts.get(heading, ""))
        if not items:
            problems.append(f"{heading}: needs at least one bullet")
        for item in items:
            if not _CITE.search(item):
                problems.append(f"{heading}: uncited claim: {item[:80]!r}")
    listed = section.works_cited()
    for bullet in _bullets(parts.get("Works cited", "")):
        if not _CITE.match(bullet):
            problems.append(f"Works cited: entry must start with [@slug]: {bullet[:60]!r}")
    dupes = sorted({s for s in listed if listed.count(s) > 1})
    if dupes:
        problems.append(f"Works cited lists twice: {', '.join(dupes)}")
    cited = section.cited()
    for slug in sorted(cited - set(listed)):
        problems.append(f"cited but missing from Works cited: {slug}")
    for slug in sorted(set(listed) - cited):
        problems.append(f"in Works cited but never cited: {slug}")
    if known_slugs is not None:
        for slug in sorted((cited | set(listed)) - known_slugs):
            problems.append(f"cites a source that isn't approved: {slug}")
    for heading, text in parts.items():
        if heading == "Works cited":  # MLA puts titles in quotes
            continue
        for quote in _QUOTE.findall(text):
            if len(quote.split()) > MAX_QUOTE_WORDS:
                problems.append(f"{heading}: quote too long, paraphrase it: {quote[:60]!r}")
        if _CALORIES.search(text):
            problems.append(f"{heading}: calorie numbers aren't allowed")
    return problems


def approve(
    name: str,
    by: str,
    known_slugs: set[str],
    directory: Path | None = None,
    today: str | None = None,
) -> Section:
    """Approve a section after it passes `check` against the approved source slugs."""
    if not by.strip():
        raise KnowledgeError(["approval needs a person's name (--by)"])
    section = load(name, directory)
    problems = check(section, known_slugs)
    if problems:
        raise KnowledgeError(problems)
    section.status = "approved"
    section.approved_by = by.strip()
    section.approved_on = today or date.today().isoformat()
    section.content_sha256 = body_hash(section.body)
    path_for(name, directory).write_text(section.render(), encoding="utf-8")
    return section
