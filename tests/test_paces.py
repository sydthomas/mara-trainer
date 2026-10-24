from __future__ import annotations

import math

import pytest

from mara_trainer import paces
from mara_trainer.paces import (
    HALF_MARATHON_METERS,
    MARATHON_METERS,
    MI,
    format_hms,
    format_pace,
    predict,
    riegel_seconds,
    seconds_per_mile,
)

# Synthetic inputs only.


def test_riegel_formula_matches_hand_calculation() -> None:
    # 10K in 50:00 -> half: 3000 * (21097.5 / 10000) ** 1.06
    expected = 3000 * (21097.5 / 10000) ** 1.06
    assert riegel_seconds(10000, 3000, 21097.5) == pytest.approx(expected)
    assert riegel_seconds(10000, 3000, 21097.5) == pytest.approx(6619.2, abs=0.5)


def test_same_distance_returns_same_time() -> None:
    assert riegel_seconds(5000, 1500, 5000) == pytest.approx(1500)


def test_longer_distance_slows_pace() -> None:
    t5 = riegel_seconds(10000, 3000, 5000)
    t42 = riegel_seconds(10000, 3000, MARATHON_METERS)
    assert t5 / 5000 < 3000 / 10000 < t42 / MARATHON_METERS


@pytest.mark.parametrize(
    ("d1", "t1", "d2"),
    [(0, 3000, 5000), (10000, 0, 5000), (10000, 3000, 0), (-1, 3000, 5000)],
)
def test_riegel_rejects_non_positive(d1: float, t1: float, d2: float) -> None:
    with pytest.raises(ValueError):
        riegel_seconds(d1, t1, d2)


def test_unit_helpers() -> None:
    assert MI == 1609.344
    assert seconds_per_mile(0.5) == pytest.approx(804.672)
    assert format_hms(3725.4) == "1:02:05"
    assert format_hms(59.6) == "0:01:00"
    assert format_pace(479.6) == "8:00"
    assert format_pace(600) == "10:00"


def test_predict_report_shape_and_values() -> None:
    report = predict(HALF_MARATHON_METERS, 6900)  # synthetic 1:55:00 half
    data = report.to_dict()
    labels = [p["label"] for p in data["predictions"]]
    assert labels == ["5k", "10k", "half_marathon", "marathon"]
    marathon = data["predictions"][-1]
    expected = 6900 * (MARATHON_METERS / HALF_MARATHON_METERS) ** 1.06
    assert marathon["seconds"] == round(expected)
    assert marathon["time"] == format_hms(expected)
    assert marathon["sec_per_meter"] == pytest.approx(expected / MARATHON_METERS, abs=1e-4)
    assert marathon["sec_per_km"] == pytest.approx(expected / MARATHON_METERS * 1000, abs=0.05)
    assert marathon["sec_per_mile"] == pytest.approx(expected / MARATHON_METERS * MI, abs=0.05)
    assert marathon["distance_miles"] == pytest.approx(26.22, abs=0.01)
    assert data["display_unit"] == "mi"
    # Half from a half is the input itself.
    assert data["predictions"][2]["seconds"] == 6900


def test_every_output_names_its_method() -> None:
    data = predict(5000, 1500).to_dict()
    assert data["method"]["id"] == "riegel_1981"
    assert "Riegel" in data["method"]["citation"]
    assert data["method"]["exponent"] == 1.06
    assert data["method"]["source_slug"] == paces.RIEGEL_SOURCE_SLUG
    assert all(p["method"] == "riegel_1981" for p in data["predictions"])


def test_no_training_zones_and_says_why() -> None:
    data = predict(5000, 1500).to_dict()
    assert data["training_zones"] is None
    assert "not included" in data["training_zones_note"]


def test_caveats_flag_evidence_and_marathon_optimism() -> None:
    caveats = " ".join(predict(HALF_MARATHON_METERS, 6900).to_dict()["caveats"]).lower()
    assert "world-record" in caveats
    assert "women" in caveats
    assert "first marathon" in caveats
    assert "optimistic" in caveats


@pytest.mark.parametrize(("d", "t"), [(799, 300), (42196, 20000), (5000, 59), (5000, 86401)])
def test_predict_rejects_out_of_range_input(d: float, t: float) -> None:
    with pytest.raises(ValueError):
        predict(d, t)


def test_predictions_are_finite_and_increasing() -> None:
    preds = predict(10000, 3300).predictions
    secs = [p.seconds for p in preds]
    assert all(math.isfinite(s) for s in secs)
    assert secs == sorted(secs)
