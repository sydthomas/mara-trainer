"""Weather: Open-Meteo forecast for a run window. Never hits the network (fake fetch only);
coordinates and values are synthetic."""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from mara_coach import weather
from mara_coach.cli import app

TZ = "America/Los_Angeles"
ENV = {"COACH_LAT": "12.34567", "COACH_LON": "-98.76543"}
NOW = datetime(2026, 3, 1, 5, 30, tzinfo=ZoneInfo(TZ))
FIRST = datetime(2026, 3, 1, 0, 0)


def payload(days: int = 3, **overrides: Any) -> dict[str, Any]:
    """A response shaped like Open-Meteo's: parallel hourly arrays, local times, no offset."""
    n = days * 24
    times = [(FIRST + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
    hourly: dict[str, list[Any]] = {
        "time": times,
        "temperature_2m": [10.0 + (i % 24) for i in range(n)],
        "relative_humidity_2m": [50] * n,
        "dew_point_2m": [0.0] * n,
        "apparent_temperature": [-40.0] * n,
        "precipitation_probability": [20] * n,
        "precipitation": [0.5] * n,
        "wind_speed_10m": [10.0] * n,
        "wind_gusts_10m": [4.4704] * n,
        "uv_index": [3.5] * n,
    }
    hourly.update(overrides)
    units = {
        "time": "iso8601",
        "temperature_2m": "°C",
        "relative_humidity_2m": "%",
        "dew_point_2m": "°C",
        "apparent_temperature": "°C",
        "precipitation_probability": "%",
        "precipitation": "mm",
        "wind_speed_10m": "m/s",
        "wind_gusts_10m": "m/s",
        "uv_index": "",
    }
    return {"latitude": 12.3, "longitude": -98.8, "hourly_units": units, "hourly": hourly}


class FakeFetch:
    def __init__(self, body: bytes | Exception) -> None:
        self.body = body
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout: float) -> bytes:
        self.calls.append((url, timeout))
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def ok_fetch(**kw: Any) -> FakeFetch:
    return FakeFetch(json.dumps(payload(**kw)).encode())


def run(fetch: FakeFetch, **kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"timezone": TZ, "date": "2026-03-02", "hour": 7, "env": ENV, "now": NOW}
    args.update(kw)
    return weather.forecast(fetch=fetch, **args)


def test_success_returns_run_window_with_si_and_display_values() -> None:
    fetch = ok_fetch()
    out = run(fetch)
    assert out["ok"] is True
    assert out["date"] == "2026-03-02"
    assert out["timezone"] == TZ
    assert [h["time"] for h in out["hours"]] == [
        "2026-03-02T07:00",
        "2026-03-02T08:00",
        "2026-03-02T09:00",
    ]
    first = out["hours"][0]
    assert first["temperature_c"] == 17.0
    assert first["temperature_f"] == 62.6
    assert first["dew_point_c"] == 0.0 and first["dew_point_f"] == 32.0
    assert first["apparent_temperature_c"] == -40.0 and first["apparent_temperature_f"] == -40.0
    assert first["relative_humidity_pct"] == 50
    assert first["precipitation_probability_pct"] == 20
    assert first["precipitation_mm"] == 0.5
    assert first["wind_speed_m_s"] == 10.0 and first["wind_speed_mph"] == 22.4
    assert first["wind_gusts_m_s"] == 4.47 and first["wind_gusts_mph"] == 10.0
    assert first["uv_index"] == 3.5
    assert out["forecast_through"] == "2026-03-03T23:00"
    assert out["attribution"] == {
        "text": "Weather data by Open-Meteo.com",
        "url": "https://open-meteo.com/",
    }
    assert len(fetch.calls) == 1 and 0 < fetch.calls[0][1] <= 15


def test_request_carries_only_rounded_coordinates_timezone_and_variables() -> None:
    fetch = ok_fetch()
    run(fetch)
    parts = urlsplit(fetch.calls[0][0])
    assert (parts.scheme, parts.netloc, parts.path) == (
        "https",
        "api.open-meteo.com",
        "/v1/forecast",
    )
    query = parse_qs(parts.query)
    assert query.pop("latitude") == ["12.35"]
    assert query.pop("longitude") == ["-98.77"]
    assert query.pop("timezone") == [TZ]
    assert query.pop("hourly") == [
        "temperature_2m,relative_humidity_2m,dew_point_2m,apparent_temperature,"
        "precipitation_probability,precipitation,wind_speed_10m,wind_gusts_10m,uv_index"
    ]
    assert query.pop("wind_speed_unit") == ["ms"]
    assert query.pop("forecast_days") == ["16"]
    assert query == {}
    assert "12.34567" not in fetch.calls[0][0] and "98.76543" not in fetch.calls[0][0]


def test_coordinates_are_not_in_the_output() -> None:
    text = json.dumps(run(ok_fetch()))
    for secret in ("12.3", "98.7", "98.8"):
        assert secret not in text


def test_defaults_to_today_and_the_current_local_hour() -> None:
    out = run(ok_fetch(), date=None, hour=None, hours=1)
    assert [h["time"] for h in out["hours"]] == ["2026-03-01T05:00"]


def test_window_crosses_midnight() -> None:
    out = run(ok_fetch(), date="2026-03-01", hour=23, hours=2)
    assert [h["time"] for h in out["hours"]] == ["2026-03-01T23:00", "2026-03-02T00:00"]


def test_missing_values_stay_null() -> None:
    n = 72
    out = run(ok_fetch(uv_index=[None] * n, wind_gusts_10m=[None] * n, temperature_2m=[None] * n))
    first = out["hours"][0]
    assert out["ok"] is True
    assert first["uv_index"] is None
    assert first["wind_gusts_m_s"] is None and first["wind_gusts_mph"] is None
    assert first["temperature_c"] is None and first["temperature_f"] is None


@pytest.mark.parametrize(
    ("env", "names"),
    [
        ({}, ["COACH_LAT", "COACH_LON"]),
        ({"COACH_LAT": "12.3"}, ["COACH_LON"]),
        ({"COACH_LAT": " ", "COACH_LON": "-98.7"}, ["COACH_LAT"]),
    ],
)
def test_missing_env_vars_fail_without_a_request(env: dict[str, str], names: list[str]) -> None:
    fetch = ok_fetch()
    out = run(fetch, env=env)
    assert out["ok"] is False
    assert out["missing"] == names
    assert fetch.calls == []


@pytest.mark.parametrize(
    "env",
    [
        {"COACH_LAT": "north", "COACH_LON": "-98.7"},
        {"COACH_LAT": "91", "COACH_LON": "-98.7"},
        {"COACH_LAT": "12.3", "COACH_LON": "181"},
        {"COACH_LAT": "nan", "COACH_LON": "-98.7"},
    ],
)
def test_bad_coordinates_fail_without_echoing_them(env: dict[str, str]) -> None:
    fetch = ok_fetch()
    out = run(fetch, env=env)
    assert out["ok"] is False and fetch.calls == []
    assert env["COACH_LAT"] not in out["reason"] and env["COACH_LON"] not in out["reason"]


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("timed out"),
        urllib.error.URLError("no route to host"),
        urllib.error.HTTPError(
            "https://api.open-meteo.com/v1/forecast?latitude=12.35",
            500,
            "Server Error",
            None,  # type: ignore[arg-type]
            None,
        ),
        RuntimeError("anything else"),
    ],
)
def test_network_errors_return_a_reason_and_never_raise(error: Exception) -> None:
    out = run(FakeFetch(error))
    assert out["ok"] is False
    assert out["reason"]
    assert "12.35" not in out["reason"]
    assert "hours" not in out


def test_http_error_reason_names_the_status() -> None:
    err = urllib.error.HTTPError("https://x.invalid", 429, "Too Many", None, None)  # type: ignore[arg-type]
    assert "429" in run(FakeFetch(err))["reason"]


@pytest.mark.parametrize(
    "body",
    [
        b"<html>not json</html>",
        b"",
        b"[]",
        b"{}",
        json.dumps({"hourly": {"time": ["2026-03-02T07:00"]}}).encode(),
        json.dumps({"error": True, "reason": "bad request"}).encode(),
        json.dumps(payload(uv_index=[1.0])).encode(),  # array shorter than time
        json.dumps(payload(temperature_2m=["warm"] * 72)).encode(),
    ],
)
def test_malformed_responses_return_a_reason(body: bytes) -> None:
    out = run(FakeFetch(body))
    assert out["ok"] is False
    assert out["reason"]


def test_unexpected_units_are_refused_not_mislabelled() -> None:
    data = payload()
    data["hourly_units"]["wind_speed_10m"] = "km/h"
    out = run(FakeFetch(json.dumps(data).encode()))
    assert out["ok"] is False
    assert "unit" in out["reason"]


def test_date_beyond_the_forecast_horizon() -> None:
    out = run(ok_fetch(), date="2026-03-20")
    assert out["ok"] is False
    assert "2026-03-03T23:00" in out["reason"]
    assert out["forecast_through"] == "2026-03-03T23:00"
    assert "hours" not in out


def test_window_running_past_the_horizon_fails() -> None:
    out = run(ok_fetch(), date="2026-03-03", hour=22, hours=3)
    assert out["ok"] is False
    assert out["forecast_through"] == "2026-03-03T23:00"


def test_date_in_the_past_fails() -> None:
    assert run(ok_fetch(), date="2026-02-20")["ok"] is False


@pytest.mark.parametrize(
    "kw",
    [
        {"date": "03/02/2026"},
        {"date": "2026-02-30"},
        {"hour": 24},
        {"hour": -1},
        {"hours": 0},
        {"hours": 25},
        {"timezone": "Mars/Olympus"},
    ],
)
def test_bad_arguments_fail_without_a_request(kw: dict[str, Any]) -> None:
    fetch = ok_fetch()
    out = run(fetch, **kw)
    assert out["ok"] is False and out["reason"]
    assert fetch.calls == []


# --- CLI ---------------------------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mara_coach.cli.load_env", lambda: None)  # never read a real .env
    monkeypatch.setenv("COACH_LAT", ENV["COACH_LAT"])
    monkeypatch.setenv("COACH_LON", ENV["COACH_LON"])
    assert runner.invoke(app, ["init-db", "--timezone", TZ]).exit_code == 0


def test_cli_prints_json_and_exits_0(cli_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    fetch = ok_fetch()
    monkeypatch.setattr(weather, "http_get", fetch)
    monkeypatch.setattr(weather, "_now", lambda tz: NOW)
    r = runner.invoke(app, ["weather", "--date", "2026-03-02", "--hour", "7", "--hours", "2"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["ok"] is True and len(data["hours"]) == 2
    assert parse_qs(urlsplit(fetch.calls[0][0]).query)["timezone"] == [TZ]


def test_cli_exits_1_on_failure(cli_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(weather, "http_get", FakeFetch(TimeoutError("timed out")))
    r = runner.invoke(app, ["weather"])
    assert r.exit_code == 1
    assert json.loads(r.output)["ok"] is False


def test_cli_missing_location_exits_1(cli_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COACH_LAT")
    fetch = ok_fetch()
    monkeypatch.setattr(weather, "http_get", fetch)
    r = runner.invoke(app, ["weather"])
    assert r.exit_code == 1
    assert json.loads(r.output)["missing"] == ["COACH_LAT"]
    assert fetch.calls == []


def test_cli_without_a_database_exits_1(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mara_coach.cli.load_env", lambda: None)
    fetch = ok_fetch()
    monkeypatch.setattr(weather, "http_get", fetch)
    r = runner.invoke(app, ["weather"])
    assert r.exit_code == 1
    assert json.loads(r.output)["ok"] is False
    assert fetch.calls == []
