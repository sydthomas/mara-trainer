"""Deterministic race-time predictions from one recent race result.

Method: Riegel's endurance formula, T2 = T1 * (D2 / D1) ** 1.06 (Riegel PS, "Athletic Records
and Human Endurance", American Scientist 69(3):285-290, 1981). Nothing else is computed here.

Training-pace zones (easy, marathon, threshold, interval) are deliberately NOT produced. The
published zone systems (for example Daniels' VDOT tables) live in copyrighted books whose exact
percentages we can't check against the text here, and CLAUDE.md forbids invented paces. The
report says so in `training_zones_note`.

Inputs within 0.5% of 5K, 10K, half or marathon snap to the exact distance (`entered_meters`
in the report keeps what was typed).

Units: SI in, SI out (meters, seconds, seconds per meter / km / mile). `*_per_mile` and the
formatted strings exist for display only (1 mi = 1609.344 m).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .plan_rules import MARATHON_METERS

MI = 1609.344
KM = 1000.0
HALF_MARATHON_METERS = 21097.5
RIEGEL_EXPONENT = 1.06
RIEGEL_SOURCE_SLUG = "riegel-athletic-records-human-endurance-1981"
METHOD_ID = "riegel_1981"

# Same input bounds as `RaceSetup.recent_race` in plan_rules.
MIN_INPUT_METERS = 800
MAX_INPUT_METERS = MARATHON_METERS
MIN_INPUT_SECONDS = 60
MAX_INPUT_SECONDS = 86400

STANDARD_DISTANCES: tuple[tuple[str, float], ...] = (
    ("5k", 5000.0),
    ("10k", 10000.0),
    ("half_marathon", HALF_MARATHON_METERS),
    ("marathon", float(MARATHON_METERS)),
)

# Entries within this fraction of a standard distance (13.1 mi, 26.2 mi, 3.1 mi...) are treated as
# that exact distance, so a 13.1 mi entry predicts from a true half.
SNAP_TOLERANCE = 0.005

METHOD: dict[str, Any] = {
    "id": METHOD_ID,
    "name": "Riegel endurance formula",
    "formula": "T2 = T1 * (D2 / D1) ** 1.06",
    "exponent": RIEGEL_EXPONENT,
    "citation": (
        "Riegel PS. Athletic Records and Human Endurance. American Scientist 69(3):285-290, 1981."
    ),
    "url": "https://www.jstor.org/stable/27850427",
    "source_slug": RIEGEL_SOURCE_SLUG,
}

TRAINING_ZONES_NOTE = (
    "Training-pace zones (easy, marathon, threshold, interval) are not included. The published "
    "zone systems sit in copyrighted coaching books whose exact numbers we have not checked, and "
    "this tool does not invent paces. Only race-time predictions from the Riegel formula are given."
)

CAVEATS: tuple[str, ...] = (
    "Riegel's 1.06 exponent was fit to world-record performances by elite athletes, not "
    "recreational runners. We have not confirmed from the full paper how much women's records "
    "shaped it, and the 1.06 value is applied to women without a separate check, so treat these "
    "numbers as rough estimates, not targets.",
    "Predicting a marathon from a shorter race tends to be optimistic, most of all for a first "
    "marathon (for example going from a half). Many runners finish slower than the prediction; "
    "marathon pace depends on long-run volume and fueling as much as on short-race speed.",
    "Predictions assume a similar course, weather and effort to the input race, and current "
    "fitness. They are not a guarantee of any finish time.",
)


def riegel_seconds(d1_meters: float, t1_seconds: float, d2_meters: float) -> float:
    """Predicted time (s) for `d2_meters` from a result of `t1_seconds` over `d1_meters`."""
    if d1_meters <= 0 or t1_seconds <= 0 or d2_meters <= 0:
        raise ValueError("distances and time must be positive")
    return float(t1_seconds * (d2_meters / d1_meters) ** RIEGEL_EXPONENT)


def snap_distance(meters: float) -> float:
    """The standard distance `meters` is within SNAP_TOLERANCE of, else `meters` unchanged."""
    for _, d in STANDARD_DISTANCES:
        if abs(meters - d) <= d * SNAP_TOLERANCE:
            return d
    return meters


def seconds_per_mile(seconds_per_meter: float) -> float:
    return seconds_per_meter * MI


def format_hms(seconds: float) -> str:
    """Whole-second H:MM:SS (rounded half up)."""
    total = int(seconds + 0.5)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def format_pace(seconds: float) -> str:
    """Whole-second M:SS for a pace (e.g. seconds per mile)."""
    total = int(seconds + 0.5)
    m, s = divmod(total, 60)
    return f"{m}:{s:02d}"


@dataclass(frozen=True)
class Prediction:
    label: str
    distance_meters: float
    seconds: float

    @property
    def sec_per_meter(self) -> float:
        return self.seconds / self.distance_meters

    def to_dict(self) -> dict[str, Any]:
        spm = self.sec_per_meter
        return {
            "label": self.label,
            "method": METHOD_ID,
            "distance_meters": self.distance_meters,
            "distance_miles": round(self.distance_meters / MI, 2),
            "seconds": round(self.seconds),
            "time": format_hms(self.seconds),
            "sec_per_meter": round(spm, 5),
            "sec_per_km": round(spm * KM, 1),
            "sec_per_mile": round(seconds_per_mile(spm), 1),
            "pace_per_mile": format_pace(seconds_per_mile(spm)),
        }


@dataclass(frozen=True)
class PaceReport:
    distance_meters: float
    duration_seconds: float
    predictions: list[Prediction]
    entered_meters: float | None = None

    def to_dict(self) -> dict[str, Any]:
        spm = self.duration_seconds / self.distance_meters
        return {
            "input": {
                "distance_meters": self.distance_meters,
                "entered_meters": self.entered_meters,
                "distance_miles": round(self.distance_meters / MI, 2),
                "duration_seconds": self.duration_seconds,
                "time": format_hms(self.duration_seconds),
                "pace_per_mile": format_pace(seconds_per_mile(spm)),
            },
            "method": dict(METHOD),
            "display_unit": "mi",
            "predictions": [p.to_dict() for p in self.predictions],
            "training_zones": None,
            "training_zones_note": TRAINING_ZONES_NOTE,
            "caveats": list(CAVEATS),
        }


def predict(distance_meters: float, duration_seconds: float) -> PaceReport:
    """Riegel predictions for 5K, 10K, half and marathon from one race result."""
    if not MIN_INPUT_METERS <= distance_meters <= MAX_INPUT_METERS:
        raise ValueError(f"race distance must be {MIN_INPUT_METERS}-{MAX_INPUT_METERS} meters")
    if not MIN_INPUT_SECONDS <= duration_seconds <= MAX_INPUT_SECONDS:
        raise ValueError(f"race time must be {MIN_INPUT_SECONDS}-{MAX_INPUT_SECONDS} seconds")
    snapped = snap_distance(distance_meters)
    preds = [
        Prediction(label, d, riegel_seconds(snapped, duration_seconds, d))
        for label, d in STANDARD_DISTANCES
    ]
    entered = distance_meters if snapped != distance_meters else None
    return PaceReport(snapped, duration_seconds, preds, entered)
