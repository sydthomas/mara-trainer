"""Deterministic plan guardrails: "Claude proposes, code validates".

Ported from the legacy TypeScript `buildScheduleBrief` (packages/domain) and `validateAiSchedule`
(apps/api/src/schedule-ai.ts), including JavaScript rounding, then audited against the knowledge
base (`knowledge/training.md`, `race-week.md`, `strength.md`). Every coaching rule comes from an
approved source; `docs/plan-rules-sources.md` maps each rule to its sources and lists the ones
still waiting for a source. The validator reports *every* violation so Claude can fix a draft in
one pass.

Weekdays: 0=Sunday ... 6=Saturday. Distances: integer meters.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from .dates import days_before, js_round, parse_date, weekday_sun0

MARATHON_METERS = 42195
# Race week: pre-race running capped at a quarter of the peak week. Higdon's Novice 1 race week is
# 9 mi of easy running against a 40 mi peak (22.5%); LA Road Runners and Chicago also keep short
# runs. The taper meta-analysis (Wang 2023) found keeping session frequency helped.
RACE_WEEK_RUNNING_SHARE = 0.25
# Strength: about 2 sessions a week build strength; 1 a week maintains it in the racing phase
# (Blagrove 2018; ACSM 2026).
MAX_STRENGTH_PER_WEEK = 2
MAX_STRENGTH_PER_WEEK_TAPER = 1
# Chicago Marathon advises against plans shorter than 12 weeks.
MIN_RECOMMENDED_WEEKS = 12
RUN_KINDS = ("easy", "workout", "long")
SESSION_KINDS = ("easy", "workout", "long", "race", "strength", "rest")
Phase = Literal["base", "build", "marathon_specific", "taper", "race"]
TrainingLevel = Literal["base_building", "novice", "intermediate", "advanced"]

INVALID = "INVALID_AI_SCHEDULE"
OUT_OF_BOUNDS = "AI_SCHEDULE_OUT_OF_BOUNDS"


# ---------------------------------------------------------------- race setup


@dataclass(frozen=True)
class RecentRace:
    distance_meters: int
    duration_seconds: int


@dataclass(frozen=True)
class RaceSetup:
    race_name: str
    race_date: str  # YYYY-MM-DD, athlete-local
    goal: Literal["finish", "time"]
    target_seconds: int | None
    training_days: tuple[int, ...]
    long_run_day: int
    weekly_distance_min_meters: int
    weekly_distance_max_meters: int
    longest_run_meters: int
    experience: Literal["first_marathon", "returning"]
    recent_race: RecentRace | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["training_days"] = list(self.training_days)
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RaceSetup:
        setup = cls(
            race_name=str(raw["race_name"]),
            race_date=str(raw["race_date"]),
            goal=raw["goal"],
            target_seconds=raw.get("target_seconds"),
            training_days=tuple(int(d) for d in raw["training_days"]),
            long_run_day=int(raw["long_run_day"]),
            weekly_distance_min_meters=int(raw["weekly_distance_min_meters"]),
            weekly_distance_max_meters=int(raw["weekly_distance_max_meters"]),
            longest_run_meters=int(raw["longest_run_meters"]),
            experience=raw["experience"],
            recent_race=RecentRace(**raw["recent_race"]) if raw.get("recent_race") else None,
            notes=str(raw.get("notes", "")),
        )
        problems = setup.problems()
        if problems:
            raise ValueError("invalid race setup: " + "; ".join(problems))
        return setup

    def problems(self) -> list[str]:
        """Same bounds as the legacy zod `raceSetupInput` schema."""
        p: list[str] = []
        if not 1 <= len(self.race_name.strip()) <= 160:
            p.append("race_name must be 1-160 characters")
        try:
            parse_date(self.race_date)
        except ValueError:
            p.append("race_date must be YYYY-MM-DD")
        if self.goal not in ("finish", "time"):
            p.append("goal must be 'finish' or 'time'")
        if self.target_seconds is not None and not 1 <= self.target_seconds <= 86400:
            p.append("target_seconds must be 1-86400 or null")
        if not 1 <= len(self.training_days) <= 7:
            p.append("training_days needs 1-7 days")
        if len(set(self.training_days)) != len(self.training_days):
            p.append("training_days must be unique")
        if any(not 0 <= d <= 6 for d in self.training_days):
            p.append("training_days must be 0-6 (0=Sunday)")
        if not 0 <= self.long_run_day <= 6:
            p.append("long_run_day must be 0-6 (0=Sunday)")
        elif self.long_run_day not in self.training_days:
            p.append("long_run_day must be one of training_days")
        for name in ("weekly_distance_min_meters", "weekly_distance_max_meters"):
            if not 0 <= getattr(self, name) <= 500000:
                p.append(f"{name} must be 0-500000")
        if self.weekly_distance_min_meters > self.weekly_distance_max_meters:
            p.append("weekly_distance_min_meters must be <= weekly_distance_max_meters")
        if not 0 <= self.longest_run_meters <= 100000:
            p.append("longest_run_meters must be 0-100000")
        if self.experience not in ("first_marathon", "returning"):
            p.append("experience must be 'first_marathon' or 'returning'")
        if self.recent_race is not None:
            if not 800 <= self.recent_race.distance_meters <= MARATHON_METERS:
                p.append("recent_race.distance_meters must be 800-42195")
            if not 60 <= self.recent_race.duration_seconds <= 86400:
                p.append("recent_race.duration_seconds must be 60-86400")
        if len(self.notes) > 2000:
            p.append("notes must be <= 2000 characters")
        return p


# ---------------------------------------------------------------- brief


@dataclass(frozen=True)
class WeekTarget:
    week: int
    start_date: str
    phase: Phase
    target_distance_meters: int
    long_run_target_meters: int
    long_run_maximum_meters: int


@dataclass(frozen=True)
class ScheduleBrief:
    race_date: str
    training_level: TrainingLevel
    runs_per_week: int
    weeks: list[WeekTarget]
    notes: list[str]
    race_week_running_max_meters: int = 0
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    status: str = "draft"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_schedule_brief(
    setup: RaceSetup, today: str, generated_at: str | None = None
) -> ScheduleBrief:
    """Conservative weekly mileage/long-run guardrails for a Claude-authored plan."""
    first_monday = days_before(today, (weekday_sun0(parse_date(today)) + 6) % 7)
    race = parse_date(setup.race_date)
    weeks_until_race = (race - parse_date(first_monday)).days // 7 + 1
    baseline = max(
        0, js_round((setup.weekly_distance_min_meters + setup.weekly_distance_max_meters) / 2)
    )
    level: TrainingLevel
    if baseline < 16093:
        level = "base_building"
    elif setup.experience == "first_marathon":
        level = "novice"
    elif baseline < 64374:
        level = "intermediate"
    elif len(setup.training_days) >= 5:
        level = "advanced"
    else:
        level = "intermediate"

    count = min(24, max(0, weeks_until_race))
    ends_at_race = weeks_until_race <= 24 or level != "base_building"
    if weeks_until_race > 24 and level != "base_building":
        schedule_start = days_before(setup.race_date, ((weekday_sun0(race) + 6) % 7) + 7 * 23)
    else:
        schedule_start = first_monday
    weeks_to_build = max(1, count - 3)
    experience_cap = {
        "base_building": 32187,
        "novice": 64374,
        "intermediate": 88514,
        "advanced": 96561,
    }[level]
    preferred_runs = {"base_building": 3, "novice": 4, "intermediate": 5, "advanced": 6}[level]
    weekly_peak = max(
        baseline,
        min(
            experience_cap,
            js_round(baseline * 1.06**weeks_to_build),
            baseline + weeks_to_build * max(1600, js_round(baseline * 0.08)),
        ),
    )
    starting_long = max(setup.longest_run_meters, js_round(baseline * 0.3))
    long_cap = 16093 if level == "base_building" else 35406 if level == "advanced" else 32187
    long_run_peak = max(
        starting_long,
        min(
            long_cap,
            js_round(weekly_peak * 0.5),
            starting_long + max(0, weeks_to_build - 3) * 1600,
        ),
    )

    notes = [
        "Claude-drafted plan based on your race, availability, experience and recent training. "
        "Review it before accepting.",
        "Mileage targets build from your stated current baseline and include easier recovery "
        "weeks and a taper. Adjust for recovery and life.",
        "Easy effort should feel conversational. No pace prescriptions without a recent race "
        "result.",
        "General training information, not medical care. Stop and seek qualified advice for "
        "pain that worsens or persists, or for health concerns.",
    ]
    if level == "base_building":
        notes.append(
            "Current volume points to a base-building level. This schedule grows your foundation "
            "but may not be enough marathon-specific preparation for the selected race; consider "
            "a later race or a qualified coach's advice."
            if ends_at_race
            else "Current volume points to a base-building level. This 24-week block builds your "
            "foundation; create a race-specific plan afterward."
        )
    if ends_at_race and count < MIN_RECOMMENDED_WEEKS:
        notes.append(
            f"Only {count} weeks until race week. The Chicago Marathon advises against plans "
            "shorter than 12 weeks; consider a later race or keep this one as a finish goal."
        )
    if weeks_until_race > 24:
        notes.append(
            "The race is more than 24 weeks away. This draft starts 24 weeks before race week."
            if ends_at_race
            else "The race is more than 24 weeks away. This base-building block starts now; "
            "return later for a race-specific schedule."
        )

    weeks: list[WeekTarget] = []
    taper_start = max(0, count - 3) if ends_at_race else count
    for index in range(count):
        start_date = days_before(schedule_start, -7 * index)
        is_race_week = ends_at_race and index == count - 1
        phase: Phase
        if is_race_week:
            phase = "race"
        elif ends_at_race and index >= taper_start:
            phase = "taper"
        elif index < max(3, int(count * 0.25)):
            phase = "base"
        elif index < int(count * 0.65):
            phase = "build"
        else:
            phase = "marathon_specific"
        recovery_week = index > 0 and (index + 1) % 4 == 0
        progress = min(1.0, index / max(1, taper_start - 1))
        target = js_round(baseline + (weekly_peak - baseline) * progress)
        long_target = js_round(starting_long + (long_run_peak - starting_long) * progress)
        if recovery_week:
            target = js_round(target * 0.82)
            long_target = js_round(long_target * 0.82)
        if phase == "taper":
            weeks_from_race = count - 1 - index
            target = js_round(weekly_peak * (0.75 if weeks_from_race == 2 else 0.55))
            long_target = js_round(long_run_peak * (0.75 if weeks_from_race == 2 else 0.58))
        weeks.append(
            WeekTarget(
                week=index + 1,
                start_date=start_date,
                phase=phase,
                target_distance_meters=MARATHON_METERS if is_race_week else target,
                long_run_target_meters=MARATHON_METERS if is_race_week else long_target,
                long_run_maximum_meters=MARATHON_METERS
                if is_race_week
                else max(
                    long_target,
                    min(long_target + 1600, long_target if phase == "taper" else long_run_peak),
                ),
            )
        )
    return ScheduleBrief(
        race_date=setup.race_date,
        training_level=level,
        runs_per_week=min(len(setup.training_days), preferred_runs),
        weeks=weeks,
        notes=notes,
        race_week_running_max_meters=js_round(weekly_peak * RACE_WEEK_RUNNING_SHARE)
        if ends_at_race
        else 0,
        **({"generated_at": generated_at} if generated_at else {}),
    )


def brief_feasibility_problems(brief: ScheduleBrief) -> list[str]:
    """Weeks whose guardrails cannot all be met at once (not checked by the legacy code).

    Example: a low weekly baseline plus a long recent long run gives a long-run minimum larger than
    the whole weekly target. Surface these to the athlete instead of letting Claude loop forever.
    """
    out = []
    for w in brief.weeks:
        if w.phase == "race":
            continue
        min_total = w.long_run_target_meters + 1000 * max(0, brief.runs_per_week - 1)
        if min_total > w.target_distance_meters + 100:
            out.append(
                f"week {w.week}: long run >= {w.long_run_target_meters} m plus "
                f"{brief.runs_per_week - 1} runs of >= 1000 m exceeds the weekly target "
                f"{w.target_distance_meters} m (+100 m tolerance)"
            )
    return out


# ---------------------------------------------------------------- validation


@dataclass
class Violation:
    code: str  # INVALID_AI_SCHEDULE (shape) or AI_SCHEDULE_OUT_OF_BOUNDS (rules)
    week: int | None
    message: str


@dataclass
class ValidationResult:
    violations: list[Violation]

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "violations": [asdict(v) for v in self.violations]}


class ScheduleValidationError(ValueError):
    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        first = result.violations[0]
        self.code = first.code
        super().__init__(f"{first.code}: {first.message}")


def _shape_problems(week: Any, wi: int) -> list[str]:
    if not isinstance(week, dict):
        return [f"weeks[{wi}] must be an object"]
    p: list[str] = []
    if not isinstance(week.get("week"), int) or isinstance(week.get("week"), bool):
        p.append(f"weeks[{wi}].week must be an integer")
    sessions = week.get("sessions")
    if not isinstance(sessions, list) or len(sessions) != 7:
        return p + [f"weeks[{wi}].sessions must be a list of exactly 7 sessions"]
    for si, s in enumerate(sessions):
        where = f"weeks[{wi}].sessions[{si}]"
        if not isinstance(s, dict):
            p.append(f"{where} must be an object")
            continue
        day, kind, dist, details = (s.get(k) for k in ("day", "kind", "distance_meters", "details"))
        if not isinstance(day, int) or isinstance(day, bool) or not 0 <= day <= 6:
            p.append(f"{where}.day must be an integer 0-6 (0=Sunday)")
        if kind not in SESSION_KINDS:
            p.append(f"{where}.kind must be one of {', '.join(SESSION_KINDS)}")
        if not isinstance(dist, int) or isinstance(dist, bool) or not 0 <= dist <= 500000:
            p.append(f"{where}.distance_meters must be an integer 0-500000")
        if not isinstance(details, str) or not 1 <= len(details) <= 500:
            p.append(f"{where}.details must be a 1-500 character string")
    return p


def _circular_gap(a: int, b: int) -> int:
    return min(abs(a - b), 7 - abs(a - b))


def check_schedule(value: Any, setup: RaceSetup, brief: ScheduleBrief) -> ValidationResult:
    """Return every rule violation for a proposed plan (`{"weeks": [...]}`)."""
    out: list[Violation] = []
    weeks = value.get("weeks") if isinstance(value, dict) else None
    if not isinstance(weeks, list):
        return ValidationResult([Violation(INVALID, None, "plan must have a 'weeks' list")])
    shape = [msg for wi, w in enumerate(weeks) for msg in _shape_problems(w, wi)]
    if shape:
        return ValidationResult([Violation(INVALID, None, m) for m in shape])
    if len(weeks) != len(brief.weeks):
        return ValidationResult(
            [
                Violation(
                    INVALID, None, f"plan has {len(weeks)} weeks; brief requires {len(brief.weeks)}"
                )
            ]
        )

    allowed = set(setup.training_days)
    race_day = weekday_sun0(parse_date(setup.race_date))
    for expected, proposed in zip(brief.weeks, weeks, strict=True):
        wk = expected.week

        def bad(msg: str, code: str = OUT_OF_BOUNDS, _wk: int = wk) -> None:
            out.append(Violation(code, _wk, msg))

        if proposed["week"] != wk:
            bad(f"week number {proposed['week']} should be {wk}", INVALID)
            continue
        sessions: list[dict[str, Any]] = proposed["sessions"]
        if len({s["day"] for s in sessions}) != len(sessions):
            bad("each weekday must appear exactly once")
        if expected.phase == "race":
            races = [s for s in sessions if s["kind"] == "race"]
            if len(races) != 1:
                bad("race week needs exactly one race session")
            for s in sessions:
                if s["kind"] == "race":
                    if s["day"] != race_day:
                        bad(f"race must be on day {race_day} (race date weekday)")
                    if s["distance_meters"] != MARATHON_METERS:
                        bad(f"race distance must be {MARATHON_METERS} m")
            # Short easy runs (and at most one small quality session) are allowed before the
            # race; no long run. Sources: Higdon, LA Road Runners, Chicago, Wang 2023.
            runs = [s for s in sessions if s["kind"] in RUN_KINDS]
            if any(s["kind"] == "long" for s in runs):
                bad("no long run in race week")
            off_days = sorted(s["day"] for s in runs if s["day"] not in allowed)
            if off_days:
                bad(f"runs scheduled on non-training days {off_days}")
            if len(runs) > brief.runs_per_week - 1:
                bad(
                    f"{len(runs)} race-week runs; at most {brief.runs_per_week - 1} before the race"
                )
            if sum(1 for s in runs if s["kind"] == "workout") > 1:
                bad("at most one quality workout per week")
            if any(s["distance_meters"] < 1000 for s in runs):
                bad("every run must be at least 1000 m")
            pre_race = sum(s["distance_meters"] for s in runs)
            if pre_race > brief.race_week_running_max_meters:
                bad(
                    f"race-week running {pre_race} m exceeds "
                    f"{brief.race_week_running_max_meters} m (a quarter of the peak week)"
                )
            if sum(1 for s in sessions if s["kind"] == "strength") > MAX_STRENGTH_PER_WEEK_TAPER:
                bad(f"at most {MAX_STRENGTH_PER_WEEK_TAPER} strength session in race week")
            if any(
                s["kind"] in ("strength", "rest") and s["distance_meters"] != 0 for s in sessions
            ):
                bad("rest and strength sessions must have 0 m")
        else:
            runs = [s for s in sessions if s["kind"] in RUN_KINDS]
            longs = [s for s in runs if s["kind"] == "long"]
            workouts = [s for s in runs if s["kind"] == "workout"]
            strengths = [s for s in sessions if s["kind"] == "strength"]
            total = sum(s["distance_meters"] for s in runs)
            if any(s["kind"] == "race" for s in sessions):
                bad("race sessions are only allowed in race week")
            off_days = sorted(s["day"] for s in runs if s["day"] not in allowed)
            if off_days:
                bad(f"runs scheduled on non-training days {off_days}")
            if len(runs) != brief.runs_per_week:
                bad(f"{len(runs)} runs scheduled; brief requires {brief.runs_per_week}")
            if len(longs) != 1:
                bad(f"exactly one long run required (found {len(longs)})")
            if longs:
                long = longs[0]
                if long["day"] != setup.long_run_day:
                    bad(f"long run must be on day {setup.long_run_day}")
                if not (
                    expected.long_run_target_meters
                    <= long["distance_meters"]
                    <= expected.long_run_maximum_meters
                ):
                    bad(
                        f"long run {long['distance_meters']} m outside "
                        f"[{expected.long_run_target_meters}, "
                        f"{expected.long_run_maximum_meters}] m"
                    )
                if len(workouts) == 1 and _circular_gap(workouts[0]["day"], long["day"]) < 2:
                    bad("need at least one easy/rest day between the long run and the workout")
            if len(workouts) > 1:
                bad("at most one quality workout per week")
            strength_cap = (
                MAX_STRENGTH_PER_WEEK_TAPER if expected.phase == "taper" else MAX_STRENGTH_PER_WEEK
            )
            if len(strengths) > strength_cap:
                bad(
                    f"at most {strength_cap} strength session"
                    f"{'s' if strength_cap > 1 else ''} per week"
                    f"{' in taper weeks' if expected.phase == 'taper' else ''}"
                )
            if any(s["distance_meters"] < 1000 for s in runs):
                bad("every run must be at least 1000 m")
            if any(
                s["kind"] in ("strength", "rest") and s["distance_meters"] != 0 for s in sessions
            ):
                bad("rest and strength sessions must have 0 m")
            if abs(total - expected.target_distance_meters) > 100:
                bad(
                    f"weekly running total {total} m must be within 100 m of target "
                    f"{expected.target_distance_meters} m"
                )
    return ValidationResult(out)


def validate_schedule(
    value: Any, setup: RaceSetup, brief: ScheduleBrief, generated_at: str | None = None
) -> dict[str, Any]:
    """Validate and return the accepted draft schedule; raise ScheduleValidationError otherwise."""
    result = check_schedule(value, setup, brief)
    if not result.ok:
        raise ScheduleValidationError(result)
    return {
        "status": "draft",
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "race_date": setup.race_date,
        "training_level": brief.training_level,
        "weeks": [
            {
                "week": exp.week,
                "start_date": exp.start_date,
                "phase": exp.phase,
                "target_distance_meters": exp.target_distance_meters,
                "sessions": proposed["sessions"],
            }
            for exp, proposed in zip(brief.weeks, value["weeks"], strict=True)
        ],
        "notes": brief.notes,
    }
