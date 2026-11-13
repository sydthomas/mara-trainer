"""Weather: named places (`COACH_PLACES`) and the one-line summary of an hour. No network
(fake fetch); coordinates and values are synthetic."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from mara_coach import weather
from mara_coach.cli import app

from .test_weather import (
    ENV,
    NOW,
    NWS_ENV,
    FakeFetch,
    FakeNws,
    cli_env,  # noqa: F401 (fixture)
    ok_fetch,
    run,
    run_nws,
    runner,
)

PLACES = "Coast:21.98765,-101.23456; inland : 33.111 , -99.555 ;"
PLACES_ENV = {**ENV, "COACH_PLACES": PLACES}


def sent(fetch: FakeFetch) -> tuple[str, str]:
    query = parse_qs(urlsplit(fetch.calls[0][0]).query)
    return query["latitude"][0], query["longitude"][0]


def test_named_place_is_rounded_and_the_only_location_sent() -> None:
    fetch = ok_fetch()
    out = run(fetch, env=PLACES_ENV, place="COAST")  # names are case-insensitive
    assert out["ok"] is True and out["place"] == "coast"
    assert len(fetch.calls) == 1 and sent(fetch) == ("21.99", "-101.23")
    rest_of_url = fetch.calls[0][0].replace("21.99", "").replace("101.23", "")
    rest_of_output = json.dumps({k: v for k, v in out.items() if k != "hours"})
    for secret in ("12.3", "98.7", "33.1", "99.5", "21.9", "101.2"):
        assert secret not in rest_of_url
        assert secret not in rest_of_output


def test_without_a_place_the_default_location_is_used() -> None:
    fetch = ok_fetch()
    out = run(fetch, env=PLACES_ENV)
    assert out["place"] == "default" and sent(fetch) == ("12.35", "-98.77")
    fetch = ok_fetch()
    assert run(fetch, env=PLACES_ENV, place="Default")["place"] == "default"
    assert sent(fetch) == ("12.35", "-98.77")


def test_a_named_place_does_not_need_the_default_location() -> None:
    fetch = ok_fetch()
    out = run(fetch, env={"COACH_PLACES": PLACES}, place="inland")
    assert out["ok"] is True and sent(fetch) == ("33.11", "-99.56")


def test_nws_gets_the_named_place_too() -> None:
    nws = FakeNws()
    out = run_nws(nws, env={**NWS_ENV, "COACH_PLACES": PLACES}, place="coast")
    assert out["nws"]["ok"] is True
    assert nws.calls[0][0] == "https://api.weather.gov/points/21.99,-101.23"


def test_unknown_place_lists_the_known_names_without_a_request() -> None:
    fetch = ok_fetch()
    out = run(fetch, env=PLACES_ENV, place="desert")
    assert out["ok"] is False and out["known_places"] == ["coast", "inland"]
    assert "desert" in out["reason"] and "coast, inland" in out["reason"]
    assert fetch.calls == []


def test_unknown_place_with_none_set_says_how_to_add_one() -> None:
    out = run(ok_fetch(), place="coast")
    assert out["ok"] is False and out["known_places"] == []
    assert "COACH_PLACES" in out["reason"]


@pytest.mark.parametrize("name", ["12.34,56.78", "a b", "x" * 50, ""])
def test_a_place_that_is_not_a_name_is_never_echoed(name: str) -> None:
    fetch = ok_fetch()
    out = run(fetch, env=PLACES_ENV, place=name)
    assert out["ok"] is False and fetch.calls == []
    assert out["known_places"] == ["coast", "inland"]
    assert name == "" or name not in out["reason"]


@pytest.mark.parametrize(
    "entry",
    [
        "bad:91.2345,10.5678",
        "bad:10.2345,181.5678",
        "bad:10.2345",
        "bad:10.2345,20.5678,30.9876",
        "bad:north,west",
        "bad:nan,10.5678",
        "bad:",
        "bad:10.2345;bad:20.5678",  # a ";" where the "," belongs: neither half is guessed at
    ],
)
def test_a_malformed_entry_only_breaks_its_own_name(entry: str) -> None:
    env = {**ENV, "COACH_PLACES": f"coast:21.98765,-101.23456;{entry}"}
    fetch = ok_fetch()
    out = run(fetch, env=env, place="bad")
    assert out["ok"] is False and fetch.calls == []
    assert '"bad"' in out["reason"] and "COACH_PLACES" in out["reason"]
    for secret in ("91.2", "2345", "5678", "9876", "north", "west"):
        assert secret not in json.dumps(out)
    assert out["known_places"] == ["coast"]
    assert run(ok_fetch(), env=env, place="coast")["ok"] is True
    assert run(ok_fetch(), env=env)["ok"] is True


def test_an_entry_without_a_usable_name_is_named_by_position() -> None:
    env = {**ENV, "COACH_PLACES": "coast:21.98765,-101.23456;44.4444,55.5555;my place:1.5,2.5"}
    usable, problems = weather.places(env)
    assert sorted(usable) == ["coast"]
    assert sorted(problems) == ["entry 2", "entry 3"]
    assert all("444" not in reason and "555" not in reason for reason in problems.values())
    assert all("my place" not in reason for reason in problems.values())
    assert run(ok_fetch(), env=env, place="coast")["ok"] is True


def test_duplicate_and_reserved_names_are_unusable() -> None:
    env = {"COACH_PLACES": "coast:1.5,2.5;COAST:3.5,4.5;default:5.5,6.5"}
    usable, problems = weather.places(env)
    assert usable == {} and sorted(problems) == ["coast", "default"]
    out = run(ok_fetch(), env=env, place="coast")
    assert out["ok"] is False and "twice" in out["reason"]


def test_no_places_set_is_empty_not_an_error() -> None:
    assert weather.places({}) == ({}, {})
    assert weather.places({"COACH_PLACES": "  ; "}) == ({}, {})


def test_cli_place(cli_env: None, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setenv("COACH_PLACES", PLACES)
    fetch = ok_fetch()
    monkeypatch.setattr(weather, "http_get", fetch)
    monkeypatch.setattr(weather, "_now", lambda tz: NOW)
    r = runner.invoke(app, ["weather", "--date", "2026-03-02", "--hour", "7", "--place", "Inland"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["place"] == "inland" and sent(fetch) == ("33.11", "-99.56")
    for secret in ("33.1", "99.5"):
        assert secret not in r.output
    r = runner.invoke(app, ["weather", "--place", "desert"])
    assert r.exit_code == 1
    assert json.loads(r.output)["known_places"] == ["coast", "inland"]


# --- one-line summary of an hour ---------------------------------------------------------------


def test_conditions_text_is_whole_fahrenheit_and_mph_values_only() -> None:
    hour = run(ok_fetch())["hours"][0]  # 17 °C, feels -40 °C, dew point 0 °C, wind 10 m/s
    assert weather.conditions_text(hour) == (
        "63°F (feels -40°F), dew point 32°F, wind 22 mph, rain chance 20%"
    )


def test_conditions_text_adds_wbgt_only_when_nws_returned_it() -> None:
    hour = run_nws(FakeNws())["hours"][0]
    assert weather.conditions_text(hour).endswith("rain chance 20%, WBGT 51°F")


def test_conditions_text_says_when_a_value_is_missing() -> None:
    n = 72
    hour = run(ok_fetch(dew_point_2m=[None] * n, precipitation_probability=[None] * n))["hours"][0]
    text = weather.conditions_text(hour)
    assert "dew point n/a" in text and "rain chance n/a" in text and "WBGT" not in text
