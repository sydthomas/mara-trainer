"""Weather: Open-Meteo forecast for a run window, plus the optional NWS WBGT lookup. Never hits
the outside network (fake fetch, or a server on this machine for the real NWS transport);
coordinates, contact and values are synthetic."""

from __future__ import annotations

import http.server
import json
import threading
import urllib.error
from collections.abc import Iterator
from datetime import datetime
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
FIRST = datetime(2026, 3, 1, 0, 0, tzinfo=ZoneInfo(TZ))


def payload(days: int = 3, first: datetime = FIRST, **overrides: Any) -> dict[str, Any]:
    """A response shaped like Open-Meteo's with `timeformat=unixtime`: parallel hourly arrays,
    one unix timestamp per real hour from local midnight on the first day."""
    n = days * 24
    times = [int(first.timestamp()) + 3600 * i for i in range(n)]
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
        "time": "unixtime",
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
    assert out["attribution"] == [
        {"text": "Weather data by Open-Meteo.com", "url": "https://open-meteo.com/"}
    ]
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
    assert query.pop("timeformat") == ["unixtime"]
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


def test_hours_are_local_after_daylight_saving_starts() -> None:
    # The range starts in PST (2026-03-01); clocks go forward on 2026-03-08. 07:00 PDT that day
    # is 06:00 PST, the 6th hour of the API's day, not the 7th.
    out = run(ok_fetch(days=16), date="2026-03-08", hour=7, hours=2)
    assert [h["time"] for h in out["hours"]] == ["2026-03-08T07:00", "2026-03-08T08:00"]
    assert [h["temperature_c"] for h in out["hours"]] == [16.0, 17.0]


def test_window_across_the_spring_change_is_real_hours() -> None:
    out = run(ok_fetch(days=16), date="2026-03-08", hour=1, hours=3)
    assert [h["time"] for h in out["hours"]] == [
        "2026-03-08T01:00",
        "2026-03-08T03:00",
        "2026-03-08T04:00",
    ]
    assert [h["temperature_c"] for h in out["hours"]] == [11.0, 12.0, 13.0]


def test_hours_are_local_after_daylight_saving_ends() -> None:
    # The range starts in PDT (2026-10-31); clocks go back on 2026-11-01. 07:00 PST that day
    # is 32 real hours after the first midnight.
    first = datetime(2026, 10, 31, tzinfo=ZoneInfo(TZ))
    now = datetime(2026, 10, 31, 5, 30, tzinfo=ZoneInfo(TZ))
    out = run(ok_fetch(first=first), date="2026-11-01", hour=7, hours=1, now=now)
    assert [h["time"] for h in out["hours"]] == ["2026-11-01T07:00"]
    assert out["hours"][0]["temperature_c"] == 18.0
    assert out["forecast_through"] == "2026-11-02T22:00"


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


def test_missing_location_reason_stands_alone() -> None:
    reason = run(ok_fetch(), env={})["reason"]
    assert "COACH_LAT" in reason and "COACH_LON" in reason and ".env.example" not in reason


def test_tiny_negative_coordinate_is_sent_as_zero() -> None:
    fetch = ok_fetch()
    run(fetch, env={"COACH_LAT": "-0.001", "COACH_LON": "0.001"})
    query = parse_qs(urlsplit(fetch.calls[0][0]).query)
    assert query["latitude"] == ["0.00"] and query["longitude"] == ["0.00"]


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


@pytest.mark.parametrize(
    "error",
    [TimeoutError("timed out"), urllib.error.URLError(TimeoutError("timed out"))],
)
def test_read_and_connect_timeouts_say_timed_out(error: Exception) -> None:
    assert "timed out" in run(FakeFetch(error))["reason"]


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
        json.dumps(payload(uv_index=[10**400] * 72)).encode(),  # too large for a float
        json.dumps(payload(time=["2026-03-02T07:00"] * 72)).encode(),  # not unix time
        json.dumps(payload(time=[10**30] * 72)).encode(),
        json.dumps(payload(**{k: [None] * 72 for k in weather.VARIABLES})).encode(),  # no values
        b"[" * 100_000 + b"]" * 100_000,  # nesting deeper than the JSON parser allows
    ],
)
def test_malformed_responses_return_a_reason(body: bytes) -> None:
    out = run(FakeFetch(body))
    assert out["ok"] is False
    assert out["reason"]


def test_iso_times_are_refused_because_they_ignore_daylight_saving() -> None:
    data = payload()
    data["hourly_units"]["time"] = "iso8601"
    assert run(FakeFetch(json.dumps(data).encode()))["ok"] is False


def test_any_other_failure_still_returns_a_reason() -> None:
    out = run(FakeFetch(12345))  # type: ignore[arg-type]  # a fetch that returns the wrong type
    assert out["ok"] is False and out["reason"]


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


def tail_null_fetch() -> FakeFetch:
    """Like the live API near the end of its range: the last 4 hours of every variable are null
    and rain probability stops a day earlier."""
    series = {k: [1.0] * 68 + [None] * 4 for k in weather.VARIABLES}
    series["precipitation_probability"] = [1.0] * 48 + [None] * 24
    return ok_fetch(**series)


def test_forecast_ends_at_the_last_hour_with_data() -> None:
    out = run(tail_null_fetch(), date="2026-03-03", hour=21, hours=3)
    assert out["ok"] is False
    assert out["forecast_through"] == "2026-03-03T19:00"
    assert "hours" not in out


def test_window_running_into_the_null_tail_fails() -> None:
    out = run(tail_null_fetch(), date="2026-03-03", hour=18, hours=3)
    assert out["ok"] is False and "2026-03-03T19:00" in out["reason"]


def test_window_before_the_null_tail_keeps_single_missing_values_null() -> None:
    out = run(tail_null_fetch(), date="2026-03-03", hour=18, hours=2)
    assert out["ok"] is True and out["forecast_through"] == "2026-03-03T19:00"
    assert out["hours"][1]["temperature_c"] == 1.0
    assert out["hours"][1]["precipitation_probability_pct"] is None


def test_date_in_the_past_fails() -> None:
    assert run(ok_fetch(), date="2026-02-20")["ok"] is False


@pytest.mark.parametrize(
    "kw",
    [
        {"date": "03/02/2026"},
        {"date": "2026-02-30"},
        {"date": "2026-W10-1"},  # an ISO week date, 10 characters
        {"date": "9999-12-31", "hour": 23},  # the window runs past the last date
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
    monkeypatch.delenv("COACH_NWS_CONTACT", raising=False)  # a developer's shell may set it
    monkeypatch.delenv("COACH_PLACES", raising=False)  # same: a real .env must not leak in
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


# --- NWS wet-bulb globe temperature ------------------------------------------------------------

CONTACT = "test@example.invalid"
NWS_ENV = {**ENV, "COACH_NWS_CONTACT": CONTACT}
POINTS_URL = "https://api.weather.gov/points/12.35,-98.77"
GRID_URL = "https://api.weather.gov/gridpoints/XYZ/10,20"
# The default window (2026-03-02 07:00-09:00 PST) is 15:00, 16:00 and 17:00 UTC.
WBGT_VALUES: list[dict[str, Any]] = [
    {"validTime": "2026-03-02T14:00:00+00:00/PT1H", "value": 9.0},
    {"validTime": "2026-03-02T15:00:00+00:00/PT1H", "value": 10.555555555555555},
    {"validTime": "2026-03-02T16:00:00+00:00/PT2H", "value": 12},
]


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x.invalid", code, "error", None, None)  # type: ignore[arg-type]


def grid_body(values: Any = None, **layer: Any) -> dict[str, Any]:
    """A gridpoint response shaped like the live one: `properties` holds one object per layer."""
    wbgt = {"uom": "wmoUnit:degC", "values": WBGT_VALUES if values is None else values, **layer}
    return {"properties": {"temperature": {}, "wetBulbGlobeTemperature": wbgt}}


class FakeNws:
    """Answers the two NWS URLs; an Exception is raised instead of returned."""

    def __init__(self, points: Any = None, grid: Any = None) -> None:
        self.points = {"properties": {"forecastGridData": GRID_URL}} if points is None else points
        self.grid = grid_body() if grid is None else grid
        self.calls: list[tuple[str, float, dict[str, str]]] = []

    def __call__(self, url: str, timeout: float, headers: Any) -> bytes:
        self.calls.append((url, timeout, dict(headers)))
        answer = self.points if "/points/" in url else self.grid
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, bytes) else json.dumps(answer).encode()


def run_nws(nws: FakeNws, days: int = 3, **kw: Any) -> dict[str, Any]:
    return run(ok_fetch(days=days), nws_fetch=nws, **{"env": NWS_ENV, **kw})


def wbgt_c(out: dict[str, Any]) -> list[Any]:
    return [h["wbgt_c"] for h in out["hours"]]


def assert_nws_absent(out: dict[str, Any]) -> None:
    """The NWS part is gone with a reason, and the Open-Meteo result is whole."""
    assert out["ok"] is True
    assert out["nws"]["ok"] is False and out["nws"]["reason"]
    assert [h["temperature_c"] for h in out["hours"]] == [17.0, 18.0, 19.0]
    assert all(h["wbgt_c"] is None and h["wbgt_f"] is None for h in out["hours"])
    assert out["attribution"] == [weather.ATTRIBUTION]
    text = json.dumps(out)
    assert CONTACT not in text and "12.35" not in text and "api.weather.gov" not in text


@pytest.mark.parametrize("contact", [None, "", "  "])
def test_without_a_contact_no_nws_request_is_made(contact: str | None) -> None:
    nws = FakeNws()
    env = ENV if contact is None else {**ENV, "COACH_NWS_CONTACT": contact}
    out = run_nws(nws, env=env)
    assert out["nws"] == {"ok": False, "reason": "no contact set"}
    assert nws.calls == []
    assert_nws_absent(out)


def test_wbgt_is_added_per_hour_with_fahrenheit_and_a_second_attribution() -> None:
    out = run_nws(FakeNws())
    assert out["ok"] is True
    assert out["nws"] == {"ok": True, "hours_with_wbgt": 3}
    assert wbgt_c(out) == [10.6, 12.0, 12.0]  # PT2H covers 16:00 and 17:00 UTC
    assert [h["wbgt_f"] for h in out["hours"]] == [51.0, 53.6, 53.6]
    assert out["hours"][0]["temperature_c"] == 17.0
    assert out["attribution"] == [weather.ATTRIBUTION, weather.NWS_ATTRIBUTION]
    assert "wbgt_c" in out["units"] and "wbgt_f" in out["units"]


def test_nws_gets_two_requests_with_the_contact_and_rounded_coordinates() -> None:
    nws = FakeNws()
    run_nws(nws)
    assert [c[0] for c in nws.calls] == [POINTS_URL, GRID_URL]
    for _, timeout, headers in nws.calls:
        assert 0 < timeout <= 10
        assert headers["User-Agent"] == "(mara-trainer, test@example.invalid)"
    assert "12.34567" not in nws.calls[0][0] and "98.76543" not in nws.calls[0][0]


def test_contact_is_never_in_the_output() -> None:
    assert CONTACT not in json.dumps(run_nws(FakeNws()))
    assert "example.invalid" not in json.dumps(run_nws(FakeNws(points=http_error(500))))


def test_trailing_zeros_are_trimmed_so_nws_does_not_redirect() -> None:
    nws = FakeNws()
    run_nws(nws, env={**NWS_ENV, "COACH_LAT": "40.0", "COACH_LON": "-100.5"})
    assert nws.calls[0][0] == "https://api.weather.gov/points/40,-100.5"


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([{"validTime": "2026-03-02T00:00:00+00:00/P1D", "value": 5}], [5.0, 5.0, 5.0]),
        ([{"validTime": "2026-03-01T16:30:00+00:00/P1DT30M", "value": 5}], [5.0, 5.0, None]),
        ([{"validTime": "2026-02-23T16:00:00+00:00/P1W", "value": 5}], [5.0, None, None]),
        ([{"validTime": "2026-03-02T16:00:00Z/PT3600S", "value": 5}], [None, 5.0, None]),
        # an offset other than UTC: 08:00-08:00 is 16:00 UTC
        ([{"validTime": "2026-03-02T08:00:00-08:00/PT1H", "value": 5}], [None, 5.0, None]),
        # the end is exclusive: 15:00/PT1H does not cover 16:00
        ([{"validTime": "2026-03-02T15:00:00+00:00/PT1H", "value": 5}], [5.0, None, None]),
        # a null value stays null; a gap stays null
        (
            [
                {"validTime": "2026-03-02T15:00:00+00:00/PT1H", "value": None},
                {"validTime": "2026-03-02T17:00:00+00:00/PT6H", "value": -1.3},
            ],
            [None, None, -1.3],
        ),
    ],
)
def test_intervals_are_expanded_and_matched_as_instants(
    values: list[dict[str, Any]], expected: list[Any]
) -> None:
    out = run_nws(FakeNws(grid=grid_body(values)))
    assert out["nws"] == {"ok": True, "hours_with_wbgt": sum(v is not None for v in expected)}
    assert wbgt_c(out) == expected


def test_hours_match_as_instants_across_a_daylight_saving_change() -> None:
    # 01:00 PST, 03:00 PDT and 04:00 PDT on 2026-03-08 are 09:00, 10:00 and 11:00 UTC.
    values = [
        {"validTime": "2026-03-08T09:00:00+00:00/PT1H", "value": 1},
        {"validTime": "2026-03-08T10:00:00+00:00/PT1H", "value": 2},
        {"validTime": "2026-03-08T11:00:00+00:00/PT1H", "value": 3},
    ]
    out = run_nws(FakeNws(grid=grid_body(values)), days=16, date="2026-03-08", hour=1, hours=3)
    assert [h["time"] for h in out["hours"]] == [
        "2026-03-08T01:00",
        "2026-03-08T03:00",
        "2026-03-08T04:00",
    ]
    assert wbgt_c(out) == [1.0, 2.0, 3.0]


def test_outside_the_us_is_absent_with_a_reason_after_one_request() -> None:
    nws = FakeNws(points=http_error(404))
    out = run_nws(nws)
    assert_nws_absent(out)
    assert "US" in out["nws"]["reason"]
    assert len(nws.calls) == 1


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("timed out"),
        urllib.error.URLError("no route"),
        http_error(500),
        http_error(301),
        RuntimeError(f"boom {CONTACT} {POINTS_URL}"),
    ],
)
@pytest.mark.parametrize("stage", ["points", "grid"])
def test_nws_request_failures_leave_open_meteo_intact(error: Exception, stage: str) -> None:
    nws = FakeNws(**{stage: error})
    assert_nws_absent(run_nws(nws))
    assert len(nws.calls) == (1 if stage == "points" else 2)  # no retries


def test_nws_timeout_and_status_are_named() -> None:
    assert "timed out" in run_nws(FakeNws(grid=TimeoutError()))["nws"]["reason"]
    assert "503" in run_nws(FakeNws(points=http_error(503)))["nws"]["reason"]


@pytest.mark.parametrize(
    "points",
    [
        b"<html>",
        [],
        {},
        {"properties": None},
        {"properties": {}},
        {"properties": {"forecastGridData": None}},
        {"properties": {"forecastGridData": "https://evil.invalid/gridpoints/XYZ/1,2"}},
        {"properties": {"forecastGridData": "http://api.weather.gov/gridpoints/XYZ/1,2"}},
    ],
)
def test_bad_points_response_stops_before_a_second_request(points: Any) -> None:
    nws = FakeNws(points=points)
    assert_nws_absent(run_nws(nws))
    assert len(nws.calls) == 1


@pytest.mark.parametrize(
    "grid",
    [
        b"not json",
        {},
        {"properties": {}},  # layer missing
        {"properties": {"wetBulbGlobeTemperature": None}},
        {"properties": {"wetBulbGlobeTemperature": []}},
        {"properties": {"wetBulbGlobeTemperature": {"uom": "wmoUnit:degC"}}},
        {"properties": {"wetBulbGlobeTemperature": {"uom": "wmoUnit:degC", "values": None}}},
        grid_body([]),
        grid_body(uom="wmoUnit:degF"),
        grid_body(uom=None),
        grid_body(["2026-03-02T15:00:00+00:00/PT1H"]),
        grid_body([{"value": 5}]),
        grid_body([{"validTime": "2026-03-02T15:00:00+00:00", "value": 5}]),
        grid_body([{"validTime": "2026-03-02T15:00:00/PT1H", "value": 5}]),  # no offset
        grid_body([{"validTime": "soon/PT1H", "value": 5}]),
        grid_body([{"validTime": "2026-03-02T15:00:00+00:00/P1M", "value": 5}]),
        grid_body([{"validTime": "2026-03-02T15:00:00+00:00/PT", "value": 5}]),
        grid_body([{"validTime": "2026-03-02T15:00:00+00:00/P1DT", "value": 5}]),
        grid_body([{"validTime": "2026-03-02T15:00:00+00:00/1H", "value": 5}]),
        grid_body([{"validTime": "2026-03-02T15:00:00+00:00/PT1H", "value": "warm"}]),
        grid_body([{"validTime": "2026-03-02T15:00:00+00:00/PT1H", "value": True}]),
        grid_body([{"validTime": "2026-03-02T15:00:00+00:00/PT1H", "value": None}]),
        grid_body([{"validTime": "2026-03-09T15:00:00+00:00/PT1H", "value": 5}]),  # another day
    ],
)
def test_missing_or_unexpected_wbgt_layer_is_absent_never_an_error(grid: Any) -> None:
    assert_nws_absent(run_nws(FakeNws(grid=grid)))


def test_wrong_unit_reason_says_so() -> None:
    assert "°C" in run_nws(FakeNws(grid=grid_body(uom="wmoUnit:degF")))["nws"]["reason"]


@pytest.mark.parametrize("contact", ["a@example.invalid\r\nX-Evil: 1", "tést@example.invalid"])
def test_contact_that_cannot_be_a_header_is_not_sent_or_echoed(contact: str) -> None:
    nws = FakeNws()
    out = run_nws(nws, env={**ENV, "COACH_NWS_CONTACT": contact})
    assert out["ok"] is True and out["nws"]["ok"] is False
    assert nws.calls == []
    assert "example.invalid" not in json.dumps(out)


def test_nws_is_not_asked_when_open_meteo_fails() -> None:
    nws = FakeNws()
    out = run(FakeFetch(TimeoutError()), nws_fetch=nws, env=NWS_ENV)
    assert out["ok"] is False and "nws" not in out
    assert nws.calls == []


def test_nws_is_not_asked_for_bad_arguments() -> None:
    nws = FakeNws()
    assert run_nws(nws, hours=0)["ok"] is False
    assert nws.calls == []


def cli_weather(monkeypatch: pytest.MonkeyPatch, nws: FakeNws) -> Any:
    monkeypatch.setattr(weather, "http_get", ok_fetch())
    monkeypatch.setattr(weather, "nws_http_get", nws)
    monkeypatch.setattr(weather, "_now", lambda tz: NOW)
    return runner.invoke(app, ["weather", "--date", "2026-03-02", "--hour", "7"])


def test_cli_adds_wbgt_when_the_contact_is_set(
    cli_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    nws = FakeNws()
    monkeypatch.setenv("COACH_NWS_CONTACT", CONTACT)
    r = cli_weather(monkeypatch, nws)
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["nws"]["ok"] is True and data["hours"][0]["wbgt_c"] == 10.6
    assert len(data["attribution"]) == 2
    assert CONTACT not in r.output
    assert nws.calls[0][2]["User-Agent"] == f"(mara-trainer, {CONTACT})"


def test_cli_nws_failure_still_exits_0(cli_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COACH_NWS_CONTACT", CONTACT)
    r = cli_weather(monkeypatch, FakeNws(points=http_error(404)))
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["ok"] is True and data["nws"]["ok"] is False


def test_cli_without_a_contact_makes_no_nws_request(
    cli_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    nws = FakeNws()
    r = cli_weather(monkeypatch, nws)
    assert json.loads(r.output)["nws"] == {"ok": False, "reason": "no contact set"}
    assert nws.calls == []


# --- the real NWS transport, against a server on this machine (no outside network) ---

Seen = list[tuple[str, str | None]]


@pytest.fixture
def local_server() -> Iterator[tuple[str, Seen]]:
    """A loopback HTTP server: `/moved` answers 301 to `/target`, anything else 200.

    Yields its base URL and the (path, User-Agent) of every request it received.
    """
    seen: Seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append((self.path, self.headers.get("User-Agent")))
            if self.path == "/moved":
                self.send_response(301)
                self.send_header("Location", "/target")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_nws_http_get_sends_the_given_user_agent(local_server: tuple[str, Seen]) -> None:
    base, seen = local_server
    user_agent = f"(mara-trainer, {CONTACT})"
    body = weather.nws_http_get(f"{base}/points", 5, {"User-Agent": user_agent})
    assert body == b'{"ok": true}'
    assert seen == [("/points", user_agent)]


def test_nws_http_get_does_not_follow_redirects(local_server: tuple[str, Seen]) -> None:
    base, seen = local_server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        weather.nws_http_get(f"{base}/moved", 5, {"User-Agent": f"(mara-trainer, {CONTACT})"})
    assert excinfo.value.code == 301
    # The contact header went to the URL asked for and to no other.
    assert [path for path, _ in seen] == ["/moved"]


def test_reuse_asks_once_for_several_windows_at_one_location() -> None:
    fetch, nws, bodies = ok_fetch(), FakeNws(), {}
    first = run(fetch, nws_fetch=nws, env=NWS_ENV, reuse=bodies)
    second = run(fetch, nws_fetch=nws, env=NWS_ENV, hour=8, hours=1, reuse=bodies)
    assert first["ok"] and second["ok"]
    assert second["hours"][0] == first["hours"][1]
    assert len(fetch.calls) == 1 and len(nws.calls) == 2
    assert first == run(ok_fetch(), nws_fetch=FakeNws(), env=NWS_ENV)  # same result as without


def test_reuse_does_not_keep_failures_and_is_off_by_default() -> None:
    bodies: dict[str, bytes] = {}
    assert not run(FakeFetch(TimeoutError("timed out")), reuse=bodies)["ok"]
    assert bodies == {}
    fetch = ok_fetch()
    run(fetch)
    run(fetch)
    assert len(fetch.calls) == 2
