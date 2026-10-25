"""Coach tool layer: plain typed functions over the deterministic core, plus a registry with a
name, description and JSON input schema for each, so a Claude API tool runner (later) or the
ask-coach skill (now, via `coach tools call`) can use them directly.

Rules:
- No mileage or pace math here. Every number comes from `mara_trainer` (or the DB) unchanged.
- Read-only. `validate_plan` checks a draft but never accepts it; Sydney accepts via the CLI.
  `propose_fuel_entry` checks a debrief entry but never saves it; the Slack channel saves it in
  code after she replies yes.
- No Strava data. The DB has no Strava source, and no tool may fetch or receive it.
- Sources: only `approved` rows are visible to the coach.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mara_trainer import db, fueling, reports

from . import knowledge, profile, sources

# The only folders `validate_plan` may read from (repo root = three levels above this file).
REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN_DIRS: tuple[Path, ...] = (REPO_ROOT / "plans", REPO_ROOT / "examples")
KNOWLEDGE_DIR = knowledge.KNOWLEDGE_DIR


class ToolError(ValueError):
    """A tool call the coach should see as an error result (bad input or missing setup)."""


# ---------------------------------------------------------------- tools


def plan_brief(conn: sqlite3.Connection, today: str | None = None) -> dict[str, Any]:
    try:
        return reports.plan_brief(conn, today)
    except reports.ReportError as exc:
        raise ToolError(str(exc)) from exc


def summary(conn: sqlite3.Connection, week: str | None = None, weeks: int = 0) -> dict[str, Any]:
    try:
        return reports.summary(conn, week, weeks)
    except reports.ReportError as exc:
        raise ToolError(str(exc)) from exc


def validate_plan(conn: sqlite3.Connection, path: str, today: str | None = None) -> dict[str, Any]:
    plan_file = Path(path)
    # Check the resolved target too, so a .json symlink can't point the tool at another file.
    if plan_file.suffix.lower() != ".json" or plan_file.resolve().suffix.lower() != ".json":
        raise ToolError("path must point to a .json plan file")
    target = plan_file.resolve()
    if not any(target.is_relative_to(d.resolve()) for d in PLAN_DIRS):
        raise ToolError("plan file must be inside plans/ or examples/")
    if not plan_file.is_file():
        raise ToolError(f"plan file not found: {path}")
    try:
        raw = reports.read_plan_file(plan_file)
        check = reports.check_plan(raw, db.load_race_setup(conn), today)
    except reports.ReportError as exc:
        raise ToolError(str(exc)) from exc
    return {
        **check.result.to_dict(),
        "brief_date": check.brief_date,
        "weeks": len(check.brief.weeks),
        "training_level": check.brief.training_level,
    }


def show_plan(conn: sqlite3.Connection) -> dict[str, Any]:
    return {"plan": db.load_accepted_plan(conn)}


def plan_week(conn: sqlite3.Connection, date: str) -> dict[str, Any]:
    week = reports.plan_week(db.load_accepted_plan(conn), date)
    if week is None:
        return {"date": date, "week": None, "message": "That week isn't in the accepted plan."}
    return week


def get_profile(conn: sqlite3.Connection) -> dict[str, Any]:
    return {"profile": profile.get_profile(conn)}


def fuel_log(conn: sqlite3.Connection, last: int = 10, since: str | None = None) -> dict[str, Any]:
    try:
        return fueling.list_entries(conn, last, since)
    except fueling.FuelError as exc:
        raise ToolError(str(exc)) from exc


def search_sources(
    conn: sqlite3.Connection, topic: str | None = None, women_tested: bool = False
) -> dict[str, Any]:
    try:
        rows = sources.list_sources(conn, status="approved", topic=topic, women_tested=women_tested)
    except sources.SourceError as exc:
        raise ToolError(str(exc)) from exc
    data: dict[str, Any] = {
        "topic": topic,
        "women_tested_only": women_tested,
        "count": len(rows),
        "sources": [s.to_dict() for s in rows],
    }
    if not rows:
        data["message"] = (
            "No approved source covers this. Say so; don't answer from memory."
            if not women_tested
            else "No approved women-tested source covers this. Retry without women_tested and "
            "say the evidence is not women-specific."
        )
    return data


def allowed_domains(conn: sqlite3.Connection) -> dict[str, Any]:
    return {"allowed_domains": sources.approved_domains(conn)}


MI_METERS = 1609.344
_HMS = re.compile(r"^(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)$")


def _seconds(text: str) -> int:
    m = _HMS.match(text.strip())
    if not m:
        raise ToolError(f"duration must be H:MM:SS or MM:SS, got {text!r}")
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def build_fuel_entry(args: dict[str, Any]) -> fueling.FuelEntry:
    """Debrief fields (miles or km, H:MM:SS) -> a FuelEntry in SI units. Raises ToolError."""
    values = {k: v for k, v in args.items() if k not in ("distance", "unit", "duration")}
    if "distance" in args:
        per_unit = MI_METERS if args.get("unit", "mi") == "mi" else 1000.0
        values["distance_meters"] = round(float(args["distance"]) * per_unit)
    if "duration" in args:
        values["duration_seconds"] = _seconds(str(args["duration"]))
    return fueling.FuelEntry(**values)


def propose_fuel_entry(conn: sqlite3.Connection, **args: Any) -> dict[str, Any]:
    """Validate a debrief's fueling entry and return a read-back. Never saves: the channel saves
    it in code only after the runner says yes."""
    try:
        entry = build_fuel_entry(args)
    except ToolError as exc:
        return {"ok": False, "saved": False, "problems": [str(exc)]}
    problems = entry.problems()
    if problems:
        return {"ok": False, "saved": False, "problems": problems}
    cph = fueling.carbs_per_hour(entry.carbs_grams, entry.duration_seconds)
    return {
        "ok": True,
        "saved": False,
        "entry": {k: v for k, v in entry.__dict__.items() if v is not None},
        "distance_miles": round(entry.distance_meters / MI_METERS, 2)
        if entry.distance_meters
        else None,
        "carbs_per_hour": cph,
        "message": "Not saved yet. Read this back in one or two lines and ask 'Save it?'. "
        "The app saves it when she says yes.",
    }


def approved_slugs(conn: sqlite3.Connection) -> set[str]:
    return {s.slug for s in sources.list_sources(conn, status="approved")}


def read_knowledge(conn: sqlite3.Connection, section: str | None = None) -> dict[str, Any]:
    """Approved knowledge-base sections only. Drafts, edited-after-approval sections and sections
    citing a source that is no longer approved come back without a body."""
    try:
        if section is None:
            return {
                "sections": [s.summary() for s in knowledge.load_all(KNOWLEDGE_DIR)],
                "not_written": [
                    n
                    for n in knowledge.SECTIONS
                    if not knowledge.path_for(n, KNOWLEDGE_DIR).is_file()
                ],
            }
        found = knowledge.load(section, KNOWLEDGE_DIR)
    except knowledge.KnowledgeError as exc:
        raise ToolError(str(exc)) from exc
    data = found.summary()
    if found.state != "approved":
        data["message"] = (
            "Not approved yet. Don't use it; answer from search_sources notes or say no "
            "approved source covers this."
        )
        return data
    if knowledge.check(found, approved_slugs(conn)):
        data["state"] = "needs_review"
        data["message"] = "Cites a source that is no longer approved. Don't use it."
        return data
    data["content"] = found.body
    return data


# ---------------------------------------------------------------- registry


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[..., dict[str, Any]]

    def spec(self) -> dict[str, Any]:
        """The API tool definition: name, description, input_schema."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def _schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> Any:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


_DATE = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}

TOOLS: tuple[Tool, ...] = (
    Tool(
        "plan_brief",
        "Plan guardrails for the runner's saved race: per-week phase, target distance and "
        "long-run bounds (meters), runs per week, and feasibility_problems. Use before drafting "
        "or discussing the plan. Errors if no race is set.",
        _schema({"today": {**_DATE, "description": "Brief start date; default athlete today"}}),
        plan_brief,
    ),
    Tool(
        "summary",
        "Training summary from imported runs and check-ins (meters, seconds). Default: trailing "
        "7 days. Null means no data, never zero; coverage counts say how many days had data.",
        _schema(
            {
                "week": {
                    "type": "string",
                    "description": "'current', 'last' or a YYYY-MM-DD in the Monday-Sunday week",
                },
                "weeks": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 52,
                    "description": "Also return N weekly rollups, oldest first",
                },
            }
        ),
        summary,
    ),
    Tool(
        "validate_plan",
        "Check a draft plan JSON file against the deterministic guardrails. Returns ok and a "
        "list of violations by week. Never accepts the plan; the runner accepts it herself.",
        _schema(
            {
                "path": {
                    "type": "string",
                    "description": "Path to the draft plan .json, inside plans/ or examples/",
                },
                "today": {**_DATE, "description": "Override the plan's brief_date"},
            },
            required=["path"],
        ),
        validate_plan,
    ),
    Tool(
        "show_plan",
        "The runner's accepted training plan (weeks of sessions, meters), or null if none.",
        _schema(),
        show_plan,
    ),
    Tool(
        "plan_week",
        "One Monday-Sunday week of the accepted plan: week number, phase, weekly target, and each "
        "session with its date, weekday, kind, miles and notes. Use it instead of show_plan for "
        "a single week.",
        _schema({"date": {**_DATE, "description": "Any date in the week"}}, required=["date"]),
        plan_week,
    ),
    Tool(
        "get_profile",
        "The runner's own profile: diet, allergies/foods avoided, shoe likes and dislikes, "
        "current shoes, injury history, notes. Null = not given; [] = runner said none.",
        _schema(),
        get_profile,
    ),
    Tool(
        "fuel_log",
        "The runner's fueling log, newest first: what she ate before runs (and how long "
        "before), what she took during, carbs per hour, stomach and energy (1-5). No calories "
        "or weight by design. Use it to spot what works; fueling targets come from sources.",
        _schema(
            {
                "last": {"type": "integer", "minimum": 1, "maximum": 200},
                "since": {**_DATE, "description": "Only entries on/after this date"},
            }
        ),
        fuel_log,
    ),
    Tool(
        "search_sources",
        "Approved sources the coach may cite, optionally by topic. Each has url, license, "
        "may_summarize, fetchable and female_evidence. Prefer women_tested for female runners. "
        "An empty result means no approved source covers it: say so.",
        _schema(
            {
                "topic": {"type": "string", "enum": sorted(sources.TOPICS)},
                "women_tested": {
                    "type": "boolean",
                    "description": "Only female_specific or sex_reported evidence",
                },
            }
        ),
        search_sources,
    ),
    Tool(
        "read_knowledge",
        "The coaching knowledge base: what the approved sources say on a topic, in our own "
        "words, with agreements, disagreements, women-specific notes, gaps and citations. Read "
        "the relevant section before fetching any source. No section: list sections and states.",
        _schema({"section": {"type": "string", "enum": list(knowledge.SECTIONS)}}),
        read_knowledge,
    ),
    Tool(
        "propose_fuel_entry",
        "Debriefs only: check a long-run fueling entry and get a read-back with carbs per hour. "
        "It does NOT save; the app saves it in code after the runner replies yes. Call it once "
        "you have her answers (call again with corrections). No calorie or weight fields.",
        _schema(
            {
                "date": {**_DATE, "description": "Run date (athlete-local)"},
                "distance": {"type": "number", "minimum": 0.1, "maximum": 100},
                "unit": {"type": "string", "enum": ["mi", "km"]},
                "duration": {"type": "string", "description": "H:MM:SS or MM:SS"},
                "pre_run": {"type": "string", "description": "What she ate before"},
                "pre_run_minutes": {"type": "integer", "minimum": 0, "maximum": 720},
                "during": {"type": "string", "description": "What she took during"},
                "carbs_grams": {"type": "number", "minimum": 0, "maximum": 1000},
                "fluid_ml": {"type": "number", "minimum": 0, "maximum": 10000},
                "stomach": {"type": "integer", "minimum": 1, "maximum": 5},
                "energy": {"type": "integer", "minimum": 1, "maximum": 5},
                "effort": {"type": "integer", "minimum": 1, "maximum": 10},
                "notes": {"type": "string"},
            },
            required=["date"],
        ),
        propose_fuel_entry,
    ),
    Tool(
        "allowed_domains",
        "Web domains the coach may fetch from at answer time (approved, fetchable sources only).",
        _schema(),
        allowed_domains,
    ),
)
_BY_NAME = {t.name: t for t in TOOLS}


def tool_specs() -> list[dict[str, Any]]:
    return [t.spec() for t in TOOLS]


def call_tool(conn: sqlite3.Connection, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate `args` against the tool's input schema, then run it."""
    tool = _BY_NAME.get(name)
    if tool is None:
        raise ToolError(f"unknown tool {name!r}; known: {', '.join(_BY_NAME)}")
    problems = check_args(tool.input_schema, args)
    if problems:
        raise ToolError(f"bad arguments for {name}: " + "; ".join(problems))
    return tool.run(conn, **args)


_TYPES: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


def check_args(schema: dict[str, Any], args: Any) -> list[str]:
    """The subset of JSON Schema the registry uses: object, required, no extra keys, and per
    property type, enum, minimum, maximum, pattern."""
    if not isinstance(args, dict):
        return ["arguments must be a JSON object"]
    props: dict[str, Any] = schema["properties"]
    problems = [f"missing {k}" for k in schema.get("required", []) if k not in args]
    problems += [f"unexpected argument {k}" for k in sorted(set(args) - set(props))]
    for key, value in args.items():
        rule = props.get(key)
        if rule is None:
            continue
        if not _TYPES[rule["type"]](value):
            problems.append(f"{key} must be a {rule['type']}")
            continue
        if "enum" in rule and value not in rule["enum"]:
            problems.append(f"{key} must be one of {', '.join(rule['enum'])}")
        if "minimum" in rule and value < rule["minimum"]:
            problems.append(f"{key} must be >= {rule['minimum']}")
        if "maximum" in rule and value > rule["maximum"]:
            problems.append(f"{key} must be <= {rule['maximum']}")
        if "pattern" in rule and not re.fullmatch(rule["pattern"], value):
            problems.append(f"{key} must be YYYY-MM-DD")
    return problems
