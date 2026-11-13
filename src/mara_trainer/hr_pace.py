"""Heart rate at a fixed easy pace, as a trend. Computed on demand from FIT files; nothing stored.

For each running session the per-second `record` messages (timestamp, heart rate, speed) are read
and reduced to "steady stretches" at one reference pace band. The output is a description of what
the watch recorded: seconds in band, mean heart rate and mean pace per run, per training week
(Monday-Sunday, athlete-local dates like the plan) and as a 4-week rolling mean.

**The numbers below are descriptive defaults, not coaching rules.** They are not taken from the
source registry and do not belong in `docs/plan-rules-sources.md`: they only decide which seconds
of a run are compared with each other. There is no fitness score, no decoupling threshold and no
advice here, and a caller should not read one into the trend.

- `WARMUP_SECONDS`: the first 10 minutes of each run are skipped.
- `MIN_STRETCH_SECONDS`: a stretch counts when it holds 5 minutes inside the band.
- `MAX_EXCURSION_SECONDS`: pace may leave the band for up to 30 s in a row (one smoothing
  window) without ending the stretch; those seconds are not counted in any mean.
- `SMOOTHING_SECONDS`: pace is a trailing 30-second mean of recorded speed, because raw
  per-second GPS speed leaves a 30 s/mile band every few seconds even on an even run.
- `MAX_GAP_SECONDS` / `MIN_SPEED`: a pause (no record for more than 10 s) or standing still ends
  a stretch.
- `BAND_SECONDS_PER_MILE` / `PACE_STEP_SECONDS_PER_MILE`: the default band is 30 s/mile wide and
  centred (to the nearest 5 s/mile) on the runner's own most-used pace after the warm-ups, so no
  pace is invented and the usual pace does not sit on a band edge.
- `ROLLING_WEEKS`: the rolling value is the mean of the weekly means that exist in 4 weeks.

Missing is not zero: a run with no heart-rate data or no time in band is counted in `coverage`
and left out of every mean; a week with no usable run has null means.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .dates import date_in_timezone, iso_utc, parse_date
from .imports import MAX_FIT_BYTES
from .paces import KM, MI, format_pace
from .summaries import week_bounds

WARMUP_SECONDS = 600
MIN_STRETCH_SECONDS = 300
SMOOTHING_SECONDS = 30
MAX_GAP_SECONDS = 10
MIN_SPEED = 0.5  # m/s; slower than this is standing or walking to a stop
MAX_EXCURSION_SECONDS = 30
BAND_SECONDS_PER_MILE = 30
PACE_STEP_SECONDS_PER_MILE = 5
ROLLING_WEEKS = 4
MAX_WEEKS = 104
CAVEATS = [
    "wrist_hr_noise",
    "conditions_vary",
    "terrain_not_adjusted",
    "descriptive_defaults_not_coaching_rules",
    "trend_not_fitness_score",
]

# One sample = the interval ending at a record: (seconds, smoothed speed m/s, heart rate or None,
# starts a new segment). Tuples keep a few thousand records per run cheap.
Sample = tuple[float, float, int | None, bool]


@dataclass(frozen=True)
class RunTrace:
    started_at: datetime  # aware, UTC
    has_hr: bool  # any record in the session carried a heart rate
    samples: list[Sample]  # after the warm-up only


@dataclass(frozen=True)
class Band:
    """Half-open pace band in seconds per meter: lo <= pace < hi (lo is the faster edge)."""

    lo: float
    hi: float
    chosen_by: str  # "most_used_pace" | "arguments"


@dataclass(frozen=True)
class Totals:
    seconds: float = 0.0
    meters: float = 0.0
    hr_weighted: float = 0.0  # sum of heart rate x seconds
    hr_seconds: float = 0.0

    def plus(self, other: Totals) -> Totals:
        return Totals(
            self.seconds + other.seconds,
            self.meters + other.meters,
            self.hr_weighted + other.hr_weighted,
            self.hr_seconds + other.hr_seconds,
        )

    @property
    def mean_hr(self) -> float | None:
        return self.hr_weighted / self.hr_seconds if self.hr_seconds > 0 else None

    @property
    def seconds_per_meter(self) -> float | None:
        return self.seconds / self.meters if self.meters > 0 else None


# ------------------------------------------------------------------ FIT records


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def read_runs(data: bytes) -> list[RunTrace]:
    """Decode one FIT file into a trace per running session. Raises ValueError if unreadable."""
    from garmin_fit_sdk import Decoder, Stream

    if len(data) > MAX_FIT_BYTES:
        raise ValueError("FIT_TOO_LARGE")
    try:
        checker = Decoder(Stream.from_byte_array(bytearray(data)))
        if not checker.is_fit() or not checker.check_integrity():
            raise ValueError("INVALID_FIT_FILE")
        decoder = Decoder(Stream.from_byte_array(bytearray(data)))
        messages, errors = decoder.read(merge_heart_rates=False, expand_sub_fields=False)
    except ValueError:
        raise
    except Exception as exc:  # the SDK raises assorted errors on garbage input
        raise ValueError("INVALID_FIT_FILE") from exc
    if errors:
        raise ValueError("FIT_DECODE_FAILED")
    records = []
    for r in messages.get("record_mesgs", []):
        when = _aware(r.get("timestamp"))
        if when is not None:
            records.append((when, r))
    records.sort(key=lambda pair: pair[0])
    traces = []
    for s in messages.get("session_mesgs", []):
        start = _aware(s.get("start_time"))
        if s.get("sport") != "running" or start is None:
            continue
        elapsed = _number(s.get("total_elapsed_time", s.get("total_timer_time")))
        end = start + timedelta(seconds=elapsed + 1) if elapsed is not None else None
        mine = [(w, r) for w, r in records if w >= start and (end is None or w <= end)]
        traces.append(_trace(start, mine))
    return traces


def _trace(start: datetime, records: Sequence[tuple[datetime, Mapping[str, Any]]]) -> RunTrace:
    samples: list[Sample] = []
    window: deque[tuple[float, float]] = deque()  # (seconds, speed) of the last ~30 s
    window_seconds = window_meters = 0.0
    prev_t: float | None = None
    prev_dist: float | None = None
    has_hr = False
    new_segment = True
    for when, r in records:
        t = (when - start).total_seconds()
        hr_value = _number(r.get("heart_rate"))
        hr = int(hr_value) if hr_value is not None and hr_value > 0 else None
        has_hr = has_hr or hr is not None
        dist = _number(r.get("distance"))
        if prev_t is None:
            prev_t, prev_dist = t, dist
            continue
        dt = t - prev_t
        if dt <= 0:
            continue
        speed = _number(r.get("enhanced_speed", r.get("speed")))
        if speed is None and dist is not None and prev_dist is not None and dist >= prev_dist:
            speed = (dist - prev_dist) / dt  # measured distance, not a guess
        interval_start, prev_t, prev_dist = prev_t, t, dist
        if dt > MAX_GAP_SECONDS or speed is None or speed < MIN_SPEED:
            window.clear()
            window_seconds = window_meters = 0.0
            new_segment = True
            continue
        window.append((dt, speed))
        window_seconds += dt
        window_meters += dt * speed
        while window_seconds - window[0][0] >= SMOOTHING_SECONDS:
            old_dt, old_speed = window.popleft()
            window_seconds -= old_dt
            window_meters -= old_dt * old_speed
        if interval_start >= WARMUP_SECONDS:
            samples.append((dt, window_meters / window_seconds, hr, new_segment))
            new_segment = False
    return RunTrace(start, has_hr, samples)


# ------------------------------------------------------------------ stretches and band


def most_used_band(runs: list[RunTrace]) -> Band | None:
    """A 30 s/mile band centred on the pace the runner used most after the warm-ups.

    Running time is counted per 5 s/mile of smoothed pace; the 30 s/mile window holding the most
    time wins (ties go to the faster one) and the band is then centred on the mean pace inside
    it, rounded to 5 s/mile. Centring keeps the usual pace away from the band's edges, which a
    fixed grid (10:00, 10:30 ...) cannot do for a runner who sits on one of its lines.
    """
    seconds_at: dict[int, float] = defaultdict(float)
    pace_seconds_at: dict[int, float] = defaultdict(float)  # sum of pace x seconds
    for run in runs:
        for dt, speed, _, _ in run.samples:
            pace = MI / speed
            step = int(pace // PACE_STEP_SECONDS_PER_MILE)
            seconds_at[step] += dt
            pace_seconds_at[step] += dt * pace
    if not seconds_at:
        return None
    width = BAND_SECONDS_PER_MILE // PACE_STEP_SECONDS_PER_MILE

    def seconds_in_window(first: int) -> float:
        return sum(seconds_at.get(step, 0.0) for step in range(first, first + width))

    firsts = {step - i for step in seconds_at for i in range(width)}
    best = min(firsts, key=lambda first: (-seconds_in_window(first), first))
    steps = [step for step in range(best, best + width) if step in seconds_at]
    mean_pace = sum(pace_seconds_at[step] for step in steps) / seconds_in_window(best)
    centre = round(mean_pace / PACE_STEP_SECONDS_PER_MILE) * PACE_STEP_SECONDS_PER_MILE
    half = BAND_SECONDS_PER_MILE / 2
    return Band((centre - half) / MI, (centre + half) / MI, "most_used_pace")


def time_in_band(run: RunTrace, band: Band) -> Totals:
    """Totals over the run's steady stretches: at least 5 minutes inside the band, where a brief
    excursion (up to `MAX_EXCURSION_SECONDS` in a row) does not end the stretch. Seconds outside
    the band are never counted; a pause or a stop always ends the stretch."""
    total = Totals()
    seconds = meters = hr_weighted = hr_seconds = outside = 0.0
    for dt, speed, hr, new_segment in run.samples:
        inside = band.lo <= 1 / speed < band.hi
        if not inside and not new_segment:
            outside += dt
        if new_segment or outside > MAX_EXCURSION_SECONDS:
            if seconds >= MIN_STRETCH_SECONDS:
                total = total.plus(Totals(seconds, meters, hr_weighted, hr_seconds))
            seconds = meters = hr_weighted = hr_seconds = outside = 0.0
        if not inside:
            continue
        outside = 0.0
        seconds += dt
        meters += dt * speed
        if hr is not None:
            hr_weighted += dt * hr
            hr_seconds += dt
    if seconds >= MIN_STRETCH_SECONDS:
        total = total.plus(Totals(seconds, meters, hr_weighted, hr_seconds))
    return total


# ------------------------------------------------------------------ report


def parse_pace(value: str) -> int:
    """M:SS -> seconds."""
    minutes, sep, seconds = value.strip().partition(":")
    if not sep or not minutes.isdigit() or not (len(seconds) == 2 and seconds.isdigit()):
        raise ValueError(f"pace must be M:SS, got {value!r}")
    if int(seconds) >= 60:
        raise ValueError(f"pace must be M:SS, got {value!r}")
    return int(minutes) * 60 + int(seconds)


def _pace_fields(seconds_per_meter: float | None, unit: str, name: str) -> dict[str, Any]:
    if seconds_per_meter is None:
        return {f"{name}_seconds_per_km": None, name: None}
    per_unit = seconds_per_meter * (MI if unit == "mi" else KM)
    return {
        f"{name}_seconds_per_km": round(seconds_per_meter * KM, 1),
        name: f"{format_pace(per_unit)}/{unit}",
    }


def _round_hr(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def report(
    directory: Path,
    timezone: str,
    today: str,
    weeks: int = 12,
    pace_min_seconds: int | None = None,
    pace_max_seconds: int | None = None,
    unit: str = "mi",
) -> dict[str, Any]:
    """Heart rate at one pace band for the last `weeks` Monday-Sunday weeks ending with `today`'s
    week. `pace_min_seconds`/`pace_max_seconds` are per `unit` (min = the faster edge)."""
    if unit not in ("mi", "km"):
        raise ValueError("--unit must be mi or km")
    if not 1 <= weeks <= MAX_WEEKS:
        raise ValueError(f"--weeks must be between 1 and {MAX_WEEKS}")
    if (pace_min_seconds is None) != (pace_max_seconds is None):
        raise ValueError("give both --pace-min and --pace-max, or neither")
    this_monday = parse_date(week_bounds(today)[0])
    week_starts = [
        (this_monday - timedelta(days=7 * i)).isoformat() for i in range(weeks - 1, -1, -1)
    ]
    window_start, window_end = week_starts[0], (this_monday + timedelta(days=6)).isoformat()

    coverage = {
        "files_read": 0,
        "files_unreadable": 0,
        "files_without_running": 0,
        "runs_outside_window": 0,
        "duplicate_runs_skipped": 0,
        "runs_in_window": 0,
        "runs_used": 0,
        "runs_without_hr": 0,
        "runs_without_time_in_band": 0,
    }
    runs: list[tuple[str, str, RunTrace]] = []  # (file name, local date, trace)
    seen: set[datetime] = set()
    files = sorted(directory.iterdir()) if directory.is_dir() else []
    for path in files:
        if not path.is_file() or path.suffix.lower() != ".fit":
            continue
        try:
            traces = read_runs(path.read_bytes())
        except (OSError, ValueError):
            coverage["files_unreadable"] += 1
            continue
        coverage["files_read"] += 1
        if not traces:
            coverage["files_without_running"] += 1
        for trace in traces:
            local_date = date_in_timezone(trace.started_at, timezone)
            if not window_start <= local_date <= min(window_end, today):
                coverage["runs_outside_window"] += 1
            elif trace.started_at in seen:
                coverage["duplicate_runs_skipped"] += 1
            else:
                seen.add(trace.started_at)
                runs.append((path.name, local_date, trace))
    runs.sort(key=lambda item: item[2].started_at)
    coverage["runs_in_window"] = len(runs)

    band: Band | None
    if pace_min_seconds is not None and pace_max_seconds is not None:
        if not 0 < pace_min_seconds < pace_max_seconds:
            raise ValueError("--pace-min must be faster (smaller) than --pace-max")
        per = MI if unit == "mi" else KM
        band = Band(pace_min_seconds / per, pace_max_seconds / per, "arguments")
    else:
        band = most_used_band([trace for _, _, trace in runs])

    run_rows: list[dict[str, Any]] = []
    by_week: dict[str, Totals] = {}
    used_by_week: dict[str, int] = defaultdict(int)
    all_totals: list[Totals] = []  # every run's stretch time, with or without heart rate
    for name, local_date, trace in runs:
        totals = time_in_band(trace, band) if band is not None else Totals()
        all_totals.append(totals)
        if not trace.has_hr:
            status = "no_hr"
        elif totals.seconds <= 0:
            status = "no_time_in_band"
        elif totals.hr_seconds <= 0:
            status = "no_hr"  # heart rate elsewhere in the run, none inside the band
        else:
            status = "used"
        week_start = week_bounds(local_date)[0]
        used = status == "used"
        if used:
            coverage["runs_used"] += 1
            by_week[week_start] = by_week.get(week_start, Totals()).plus(totals)
            used_by_week[week_start] += 1
        else:
            key = "runs_without_hr" if status == "no_hr" else "runs_without_time_in_band"
            coverage[key] += 1
        run_rows.append(
            {
                "file": name,
                "date": local_date,
                "week_start": week_start,
                "started_at": iso_utc(trace.started_at),
                "status": status,
                "seconds_in_band": round(totals.seconds),
                "mean_hr": _round_hr(totals.mean_hr) if used else None,
                **_pace_fields(
                    totals.seconds_per_meter if totals.seconds > 0 else None, unit, "mean_pace"
                ),
            }
        )

    week_rows: list[dict[str, Any]] = []
    rolling: list[dict[str, Any]] = []
    for i, week_start in enumerate(week_starts):
        totals = by_week.get(week_start, Totals())
        week_rows.append(
            {
                "week_start": week_start,
                "week_end": (parse_date(week_start) + timedelta(days=6)).isoformat(),
                "runs_used": used_by_week.get(week_start, 0),
                "seconds_in_band": round(totals.seconds),
                "mean_hr": _round_hr(totals.mean_hr),
                **_pace_fields(totals.seconds_per_meter, unit, "mean_pace"),
            }
        )
        span = week_starts[max(0, i - ROLLING_WEEKS + 1) : i + 1]
        recent = [by_week[w] for w in span if w in by_week]
        hrs = [t.mean_hr for t in recent if t.mean_hr is not None]
        paces = [t.seconds_per_meter for t in recent if t.seconds_per_meter is not None]
        rolling.append(
            {
                "week_start": week_start,
                "weeks_with_data": len(hrs),
                "mean_hr": _round_hr(sum(hrs) / len(hrs)) if hrs else None,
                **_pace_fields(sum(paces) / len(paces) if paces else None, unit, "mean_pace"),
            }
        )

    return {
        "directory": str(directory),
        "window": {
            "today": today,
            "weeks": weeks,
            "start": window_start,
            "end": window_end,
            "timezone": timezone,
        },
        "definition": {
            "kind": "descriptive_defaults_not_coaching_rules",
            "warmup_skipped_seconds": WARMUP_SECONDS,
            "min_stretch_seconds": MIN_STRETCH_SECONDS,
            "speed_smoothing_seconds": SMOOTHING_SECONDS,
            "max_record_gap_seconds": MAX_GAP_SECONDS,
            "max_excursion_seconds": MAX_EXCURSION_SECONDS,
            "band_width_seconds_per_mile": BAND_SECONDS_PER_MILE,
            "band_centre_step_seconds_per_mile": PACE_STEP_SECONDS_PER_MILE,
            "rolling_weeks": ROLLING_WEEKS,
            "rolling_method": "mean_of_weekly_means_with_data",
        },
        "band": {
            "chosen_by": band.chosen_by if band else "none",
            "unit": unit,
            **_pace_fields(band.lo if band else None, unit, "pace_min"),
            **_pace_fields(band.hi if band else None, unit, "pace_max"),
            "steady_seconds_in_band": round(sum(t.seconds for t in all_totals)) if band else None,
        },
        "runs": run_rows,
        "weeks": week_rows,
        "rolling_4_week": rolling,
        "coverage": coverage,
        "caveats": list(CAVEATS),
    }
