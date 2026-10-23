"""FIT/GPX/Apple Health decoding + duplicate fingerprinting (ported from fit.ts / imports.ts)."""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mara_trainer import imports
from mara_trainer.imports import ImportError_, commit, decode_apple_health, decode_fit, decode_gpx

from .fit_builder import build_fit

NOW = datetime(2026, 4, 1, tzinfo=UTC)
START = datetime(2026, 3, 1, 15, 0, tzinfo=UTC)


def run(
    start: datetime = START,
    meters: float = 10000.0,
    seconds: float = 3600.0,
    sport: str = "running",
) -> dict[str, object]:
    return {"start": start, "meters": meters, "seconds": seconds, "sport": sport}


# ------------------------------------------------------------------ fingerprint


def test_fingerprint_matches_legacy_json_stringify() -> None:
    # sha256('["2026-03-01T15:00:00.000Z",10000,3600]') — same bytes as JS JSON.stringify
    import hashlib

    expected = hashlib.sha256(b'["2026-03-01T15:00:00.000Z",10000,3600]').hexdigest()
    assert imports.fingerprint("2026-03-01T15:00:00.000Z", 10000, 3600) == expected


# ------------------------------------------------------------------ FIT


def test_decode_fit_running_session() -> None:
    out = decode_fit(build_fit([run(meters=10012.4, seconds=3605.5)]), NOW)
    assert out.source == "fit" and len(out.file_hash) == 64
    [d] = out.drafts
    assert d.started_at == "2026-03-01T15:00:00.000Z"
    assert d.distance_meters == 10012
    assert d.duration_seconds == 3606  # JS Math.round(3605.5)
    assert d.rpe is None
    assert d.title == "Imported run · 2026-03-01"


def test_decode_fit_skips_non_running_with_warning() -> None:
    out = decode_fit(build_fit([run(), run(sport="cycling")]), NOW)
    assert len(out.drafts) == 1
    assert "not a running session" in out.warnings[0]


def test_decode_fit_skips_identical_repeat_in_same_file() -> None:
    out = decode_fit(build_fit([run(), run()]), NOW)
    assert len(out.drafts) == 1
    assert "repeated an identical" in out.warnings[0]


def test_decode_fit_only_cycling_fails() -> None:
    with pytest.raises(ImportError_) as exc:
        decode_fit(build_fit([run(sport="cycling")]), NOW)
    assert exc.value.code == "NO_SUPPORTED_RUNNING_SESSIONS"


def test_decode_fit_rejects_corrupt_crc() -> None:
    data = bytearray(build_fit([run()]))
    data[20] ^= 0xFF
    with pytest.raises(ImportError_) as exc:
        decode_fit(bytes(data), NOW)
    assert exc.value.code == "INVALID_FIT_FILE"


def test_decode_fit_rejects_garbage_and_oversize() -> None:
    with pytest.raises(ImportError_):
        decode_fit(b"not a fit file at all", NOW)
    with pytest.raises(ImportError_) as exc:
        decode_fit(b"\0" * (imports.MAX_FIT_BYTES + 1), NOW)
    assert exc.value.code == "FIT_TOO_LARGE"


def test_decode_fit_rejects_future_run() -> None:
    with pytest.raises(ImportError_) as exc:
        decode_fit(build_fit([run(start=datetime(2026, 5, 1, tzinfo=UTC))]), NOW)
    assert exc.value.code == "RUNNING_SESSION_INVALID_METRICS"


def test_decode_fit_too_many_sessions() -> None:
    sessions = [run(start=START.replace(hour=0, minute=i)) for i in range(21)]
    with pytest.raises(ImportError_) as exc:
        decode_fit(build_fit(sessions), NOW)
    assert exc.value.code == "TOO_MANY_FIT_SESSIONS"


# ------------------------------------------------------------------ GPX

GPX = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="synthetic-test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>Synthetic run</name><type>running</type><trkseg>
    <trkpt lat="0.0000" lon="0.0000"><time>2026-03-02T15:00:00Z</time></trkpt>
    <trkpt lat="0.0000" lon="0.0090"><time>2026-03-02T15:05:00Z</time></trkpt>
    <trkpt lat="0.0000" lon="0.0180"><time>2026-03-02T15:10:00Z</time></trkpt>
  </trkseg></trk>
</gpx>"""


def test_decode_gpx() -> None:
    out = decode_gpx(GPX.encode(), NOW)
    [d] = out.drafts
    assert d.source == "gpx"
    assert d.started_at == "2026-03-02T15:00:00.000Z"
    assert 1950 <= d.distance_meters <= 2050  # ~0.018 deg longitude at the equator ≈ 2 km
    assert d.duration_seconds == 600


def test_decode_gpx_skips_cycling() -> None:
    with pytest.raises(ImportError_):
        decode_gpx(GPX.replace("running", "cycling").encode(), NOW)


# ------------------------------------------------------------------ Apple Health

HK = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Record type="HKQuantityTypeIdentifierRestingHeartRate" unit="count/min" startDate="2026-03-03 06:00:00 -0800" endDate="2026-03-03 06:00:00 -0800" value="50"/>
 <Record type="HKQuantityTypeIdentifierHeartRateVariabilitySDNN" unit="ms" startDate="2026-03-03 23:30:00 -0800" endDate="2026-03-03 23:30:00 -0800" value="40"/>
 <Record type="HKQuantityTypeIdentifierHeartRateVariabilitySDNN" unit="ms" startDate="2026-03-03 07:00:00 -0800" endDate="2026-03-03 07:00:00 -0800" value="60"/>
 <Record type="HKQuantityTypeIdentifierStepCount" unit="count" startDate="2026-03-03 07:00:00 -0800" endDate="2026-03-03 07:10:00 -0800" value="900"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="50" durationUnit="min" totalDistance="8" totalDistanceUnit="km" startDate="2026-03-03 07:00:00 -0800" endDate="2026-03-03 07:50:00 -0800"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min" startDate="2026-03-04 07:00:00 -0800" endDate="2026-03-04 07:30:00 -0800">
  <WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning" sum="3.1" unit="mi"/>
 </Workout>
 <Workout workoutActivityType="HKWorkoutActivityTypeCycling" duration="60" durationUnit="min" totalDistance="20" totalDistanceUnit="km" startDate="2026-03-05 07:00:00 -0800" endDate="2026-03-05 08:00:00 -0800"/>
</HealthData>"""


def test_decode_apple_health_workouts_and_daily_metrics() -> None:
    out = decode_apple_health(HK.encode(), "America/Los_Angeles", NOW)
    assert [(d.started_at, d.distance_meters, d.duration_seconds) for d in out.drafts] == [
        ("2026-03-03T15:00:00.000Z", 8000, 3000),
        ("2026-03-04T15:00:00.000Z", 4989, 1800),
    ]
    assert out.check_ins == {"2026-03-03": {"resting_hr": 50.0, "hrv_ms": 50.0}}


def test_decode_apple_health_zip() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("apple_health_export/export.xml", HK)
    out = decode_apple_health(buf.getvalue(), "America/Los_Angeles", NOW)
    assert len(out.drafts) == 2


def test_decode_file_dispatch_and_unsupported(tmp_path: Path) -> None:
    p = tmp_path / "x.tcx"
    p.write_text("<x/>")
    with pytest.raises(ImportError_) as exc:
        imports.decode_file(p)
    assert exc.value.code == "UNSUPPORTED_FILE_TYPE"
    f = tmp_path / "run.fit"
    f.write_bytes(build_fit([run()]))
    assert imports.decode_file(f, now=NOW).source == "fit"


# ------------------------------------------------------------------ dedup + commit


def count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT count(*) FROM activities").fetchone()[0])


def test_commit_then_replay_is_already_present(conn: sqlite3.Connection) -> None:
    decoded = decode_fit(build_fit([run()]), NOW)
    r1 = commit(conn, decoded, "a.fit")
    assert (r1.imported, r1.already_present, r1.replayed_file) == (1, 0, False)
    r2 = commit(conn, decode_fit(build_fit([run()]), NOW), "a.fit")
    assert (r2.imported, r2.already_present, r2.replayed_file) == (0, 1, True)
    assert count(conn) == 1


def test_near_duplicate_needs_confirmation_and_imports_nothing(conn: sqlite3.Connection) -> None:
    commit(conn, decode_fit(build_fit([run()]), NOW), "watch.fit")
    # Same run recorded by the phone: 30 s later, 1.5% longer, 20 s longer -> candidate
    near = decode_fit(
        build_fit(
            [
                run(start=START.replace(second=30), meters=10150, seconds=3620),
                run(start=START.replace(day=2)),
            ]
        ),
        NOW,
    )
    preview = imports.preview(conn, near)
    assert preview[0].possible_duplicates and not preview[0].exact_duplicate
    assert preview[1].possible_duplicates == []
    r = commit(conn, near, "phone.fit")
    assert r.needs_confirmation == [near.drafts[0].key]
    assert r.imported == 0 and count(conn) == 1  # all-or-nothing


def test_confirmed_near_duplicate_is_imported(conn: sqlite3.Connection) -> None:
    commit(conn, decode_fit(build_fit([run()]), NOW), "watch.fit")
    near = decode_fit(build_fit([run(start=START.replace(second=30), meters=10150)]), NOW)
    r = commit(conn, near, "phone.fit", frozenset({near.drafts[0].key}))
    assert r.imported == 1 and count(conn) == 2


@pytest.mark.parametrize(
    ("start_s", "meters", "seconds", "is_candidate"),
    [
        (60, 10000, 3600, True),  # exactly 60 s apart
        (61, 10000, 3600, False),  # 61 s apart
        (0, 10200, 3600, True),  # within 2% (relative to the incoming run, as in legacy)
        (0, 10204, 3600, True),  # 204 <= 2% of the incoming 10204
        (0, 10205, 3600, False),
        (0, 10000, 3672, True),  # 2% duration (72 s > 60 s floor)
        (0, 10000, 3675, False),  # 75 > 2% of the incoming 3675
    ],
)
def test_near_match_window(
    conn: sqlite3.Connection, start_s: int, meters: int, seconds: int, is_candidate: bool
) -> None:
    commit(conn, decode_fit(build_fit([run()]), NOW), "base.fit")
    start = START.replace(minute=start_s // 60, second=start_s % 60)
    other = decode_fit(build_fit([run(start=start, meters=meters, seconds=seconds)]), NOW)
    assert bool(imports.preview(conn, other)[0].possible_duplicates) is is_candidate


def test_cross_source_duplicate_detected(conn: sqlite3.Connection) -> None:
    commit(conn, decode_apple_health(HK.encode(), "America/Los_Angeles", NOW), "export.xml")
    fit = decode_fit(
        build_fit(
            [run(start=datetime(2026, 3, 3, 15, 0, 20, tzinfo=UTC), meters=8050, seconds=3010)]
        ),
        NOW,
    )
    [c] = imports.preview(conn, fit)
    assert c.possible_duplicates[0]["source"] == "apple_health"


def test_apple_health_checkins_stored(conn: sqlite3.Connection) -> None:
    r = commit(conn, decode_apple_health(HK.encode(), "America/Los_Angeles", NOW), "export.xml")
    assert r.check_in_days == 1
    row = conn.execute("SELECT * FROM daily_check_ins").fetchone()
    assert row["hrv_ms"] == 50.0 and row["resting_hr"] == 50.0 and row["sleep_hours"] is None


def test_import_batch_recorded(conn: sqlite3.Connection) -> None:
    decoded = decode_fit(build_fit([run()]), NOW)
    commit(conn, decoded, "a.fit")
    row = conn.execute("SELECT * FROM import_batches").fetchone()
    assert row["file_hash"] == decoded.file_hash
    assert json.loads(row["result"])["imported"] == 1
