"""Weather: a place looked up by name (Open-Meteo geocoding). No network (fake fetch); place
names, coordinates and values are synthetic."""

from __future__ import annotations

import json
import urllib.error
from typing import Any
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
    payload,
    run,
    runner,
)

PLACES_ENV = {**ENV, "COACH_PLACES": "coast:21.98765,-101.23456;riverton:33.111,-99.555"}


def place(name: str = "Riverton", region: str | None = "Lake State", **fields: Any) -> dict:
    """One result shaped like the geocoding API's (checked against the docs and a live response
    on 2026-11-12): empty fields are left out, extra ones (id, postcodes, ...) come along."""
    out: dict[str, Any] = {
        "id": 1,
        "name": name,
        "latitude": 44.44444,
        "longitude": -55.55555,
        "elevation": 12.0,
        "feature_code": "PPL",
        "country_code": "TL",
        "timezone": "Etc/UTC",
        "postcodes": ["00001"],
        "country": "Testland",
    }
    if region:
        out["admin1"] = region
    out.update(fields)
    return out


def geo(*results: Any) -> bytes:
    body: dict[str, Any] = {"generationtime_ms": 0.2}
    if results:  # no match: the key is left out
        body["results"] = list(results)
    return json.dumps(body).encode()


class Routed:
    """Answers the geocoding URL with `geo` and the forecast URL with a normal payload."""

    def __init__(self, geo_body: bytes | Exception) -> None:
        self.geo = FakeFetch(geo_body)
        self.weather = FakeFetch(json.dumps(payload()).encode())
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout: float) -> bytes:
        self.calls.append((url, timeout))
        return (self.geo if url.startswith(weather.GEOCODE_URL) else self.weather)(url, timeout)


def lookup(name: str, body: bytes | Exception) -> tuple[dict[str, Any], FakeFetch]:
    fetch = FakeFetch(body)
    return weather.geocode(name, fetch=fetch), fetch


def query(fetch: Any, call: int = 0) -> dict[str, list[str]]:
    return parse_qs(urlsplit(fetch.calls[call][0]).query)


# --- geocode -----------------------------------------------------------------------------------


def test_top_match_with_rounded_coordinates_and_a_label() -> None:
    out, fetch = lookup("  riverton ", geo(place(), place(region="East Shire")))
    assert out == {
        "ok": True,
        "label": "Riverton, Lake State, Testland",
        "latitude": "44.44",
        "longitude": "-55.56",
    }
    assert len(fetch.calls) == 1 and 0 < fetch.calls[0][1] <= 5


def test_request_carries_only_the_name_and_three_fixed_parameters() -> None:
    _, fetch = lookup("Port  San\tRiverton", geo(place()))  # whitespace collapsed
    parts = urlsplit(fetch.calls[0][0])
    assert (parts.scheme, parts.netloc, parts.path) == (
        "https",
        "geocoding-api.open-meteo.com",
        "/v1/search",
    )
    assert query(fetch) == {
        "name": ["Port San Riverton"],
        "count": ["1"],
        "language": ["en"],
        "format": ["json"],
    }


def test_non_ascii_names_are_percent_encoded_utf8() -> None:
    out, fetch = lookup("Åre Ñu", geo(place(name="Åre Ñu", region="Jämtland")))
    assert out["label"] == "Åre Ñu, Jämtland, Testland"
    assert "name=%C3%85re+%C3%91u&" in fetch.calls[0][0]
    assert fetch.calls[0][0].isascii()


@pytest.mark.parametrize(
    ("typed", "label"),
    [
        ("Riverton, East Shire", "Riverton, East Shire, Testland"),
        ("riverton,  EAST SHIRE", "Riverton, East Shire, Testland"),  # case-insensitive
        ("Riverton, Otherland", "Riverton, North Shire, Otherland"),  # a country
        ("Riverton, ol", "Riverton, North Shire, Otherland"),  # a country code
        ("Riverton, North Shire, Otherland", "Riverton, North Shire, Otherland"),
        ("Riverton,", "Riverton, Lake State, Testland"),  # an empty qualifier is none
    ],
)
def test_a_qualifier_picks_the_result_whose_region_or_country_matches(
    typed: str, label: str
) -> None:
    results = [
        place(),
        place(region="East Shire", latitude=11.11111, longitude=22.22222),
        place(region="North Shire", country="Otherland", country_code="OL"),
    ]
    out, fetch = lookup(typed, geo(*results))
    assert out["ok"] is True and out["label"] == label
    assert query(fetch)["name"] == ["Riverton" if typed[0] == "R" else "riverton"]
    assert len(fetch.calls) == 1
    if typed != "Riverton,":
        assert query(fetch)["count"] == ["100"]  # enough results to find the qualifier in


def test_a_us_state_abbreviation_matches_the_full_region_name() -> None:
    results = [
        place(region="California", country="United States", country_code="US"),
        place(region="Oregon", country="United States", country_code="US"),
    ]
    assert lookup("Riverton, OR", geo(*results))[0]["label"] == "Riverton, Oregon, United States"
    assert lookup("Riverton, usa", geo(*results))[0]["label"].endswith("California, United States")
    # ... only for US results: "CA" elsewhere is not California
    out, _ = lookup("Riverton, OR", geo(place(region="Oregon")))
    assert out["ok"] is False


def test_a_qualifier_that_matches_nothing_fails_and_names_what_was_found() -> None:
    out, _ = lookup("Riverton, West Shire", geo(place(), place(region="East Shire")))
    assert out == {
        "ok": False,
        "not_found": True,
        "reason": (
            'no "Riverton" found in "West Shire" (found: Riverton, Lake State, Testland; '
            "Riverton, East Shire, Testland)"
        ),
    }


@pytest.mark.parametrize("body", [geo(), json.dumps({"results": []}).encode()])
def test_not_found(body: bytes) -> None:
    out, _ = lookup("Nowhereville", body)
    assert out == {"ok": False, "not_found": True, "reason": 'no place found for "Nowhereville"'}


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (TimeoutError("timed out"), "place lookup timed out after 5 s"),
        (urllib.error.URLError(TimeoutError("timed out")), "place lookup timed out after 5 s"),
        (urllib.error.URLError("refused"), "couldn't reach Open-Meteo (URLError)"),
        (
            urllib.error.HTTPError("https://x.invalid/?name=secret", 503, "no", None, None),  # type: ignore[arg-type]
            "place lookup failed: HTTP 503",
        ),
        (RuntimeError("https://x.invalid/?name=secret"), "place lookup failed (RuntimeError)"),
    ],
)
def test_a_failed_request_is_a_reason_and_never_raises(error: Exception, reason: str) -> None:
    out, _ = lookup("Riverton", error)
    assert out == {"ok": False, "not_found": False, "reason": reason}


@pytest.mark.parametrize(
    "body",
    [
        b"<html>busy</html>",
        b"\xff\xfe",
        b"[]",
        b'{"results": {"name": "Riverton"}}',
        b'{"error": true, "reason": "Parameter count must be between 1 and 100."}',
        geo("Riverton"),
        geo(place(latitude=None)),
        geo(place(latitude="44.4")),
        geo(place(latitude=True)),
        geo(place(longitude=181.5)),
        geo(place(name=7)),
        geo(place(name="\x00")),
        b"[" * 100_000,
    ],
)
def test_malformed_json_or_results_fail_cleanly(body: bytes) -> None:
    out, _ = lookup("Riverton", body)
    assert out["ok"] is False and out["not_found"] is False
    assert "place lookup" in out["reason"]


def test_an_unusable_result_is_skipped_for_the_next_usable_one() -> None:
    out, _ = lookup("Riverton", geo(place(latitude=None), place(region="East Shire")))
    assert out["ok"] is True and out["label"] == "Riverton, East Shire, Testland"


def test_label_leaves_out_missing_and_repeated_parts_and_control_characters() -> None:
    assert lookup("Riverton", geo(place(region=None)))[0]["label"] == "Riverton, Testland"
    assert lookup("Testland", geo(place(name="Testland", region="Testland")))[0]["label"] == (
        "Testland"
    )
    out, _ = lookup("Riverton", geo(place(region="Lake\nState\x07", country="T" * 200)))
    assert out["label"] == "Riverton, Lake State, " + "T" * 60


@pytest.mark.parametrize(
    "name",
    ["", "   ", ",", " , East Shire", "x" * 81, "River\x00ton", "River​ton", "a\x1bb"],
)
def test_empty_over_long_or_control_character_input_sends_nothing(name: str) -> None:
    out, fetch = lookup(name, geo(place()))
    assert out["ok"] is False and out["not_found"] is False and fetch.calls == []
    assert name.strip() == "" or name not in out["reason"]


def test_eighty_characters_is_allowed() -> None:
    out, fetch = lookup("x" * 80, geo(place()))
    assert out["ok"] is True and len(fetch.calls) == 1


@pytest.mark.parametrize("name", ["12.34,56.78", "12.34, -56.78", "-12 56", "12.34;56.78"])
def test_coordinates_are_never_sent_as_a_name(name: str) -> None:
    out, fetch = lookup(name, geo(place()))
    assert out["ok"] is False and fetch.calls == []
    assert "coordinates" in out["reason"] and "12" not in out["reason"]


def test_a_postal_code_is_a_name_not_coordinates() -> None:
    out, fetch = lookup("00001", geo(place()))
    assert out["ok"] is True and query(fetch)["name"] == ["00001"]


# --- forecast(near=...) ------------------------------------------------------------------------


def test_forecast_near_looks_up_once_rounds_and_labels_the_place() -> None:
    fetch = Routed(geo(place()))
    out = run(fetch, near="Riverton")
    assert out["ok"] is True
    assert (out["place"], out["place_source"]) == ("Riverton, Lake State, Testland", "lookup")
    assert out["attribution"] == [weather.ATTRIBUTION, weather.GEOCODE_ATTRIBUTION]
    assert [url.startswith(weather.GEOCODE_URL) for url, _ in fetch.calls] == [True, False]
    sent = query(fetch, 1)
    assert (sent["latitude"], sent["longitude"]) == (["44.44"], ["-55.56"])
    # neither the unrounded match nor the default location is in the requests or the output
    rest = json.dumps({k: v for k, v in out.items() if k != "hours"}) + fetch.calls[1][0]
    for secret in ("44.444", "55.555", "12.3", "98.7"):
        assert secret not in rest
    assert "latitude" not in out and "longitude" not in out


def test_nws_gets_the_same_rounded_looked_up_coordinates() -> None:
    nws = FakeNws()
    out = run(Routed(geo(place())), near="Riverton", env=NWS_ENV, nws_fetch=nws)
    assert out["nws"]["ok"] is True
    assert nws.calls[0][0] == "https://api.weather.gov/points/44.44,-55.56"
    assert out["attribution"] == [
        weather.ATTRIBUTION,
        weather.GEOCODE_ATTRIBUTION,
        weather.NWS_ATTRIBUTION,
    ]


@pytest.mark.parametrize("near", ["riverton", " Riverton ", "COAST"])
def test_an_exact_named_place_beats_the_lookup(near: str) -> None:
    fetch = Routed(geo(place()))
    out = run(fetch, env=PLACES_ENV, near=near)
    assert out["ok"] is True and out["place"] == near.strip().lower()
    assert out["place_source"] == "named" and out["attribution"] == [weather.ATTRIBUTION]
    assert [url.startswith(weather.GEOCODE_URL) for url, _ in fetch.calls] == [False]
    assert query(fetch)["latitude"] == (["21.99"] if out["place"] == "coast" else ["33.11"])


def test_a_named_place_with_a_qualifier_is_not_the_named_place() -> None:
    fetch = Routed(geo(place(region="East Shire")))
    out = run(fetch, env=PLACES_ENV, near="Riverton, East Shire")
    assert out["place"] == "Riverton, East Shire, Testland" and out["place_source"] == "lookup"


def test_near_default_is_the_default_location_and_a_broken_named_place_is_not_guessed() -> None:
    fetch = Routed(geo(place()))
    out = run(fetch, near="default")
    assert (out["place"], out["place_source"]) == ("default", "default")
    assert len(fetch.calls) == 1
    fetch = Routed(geo(place()))
    out = run(fetch, env={**ENV, "COACH_PLACES": "riverton:north,west"}, near="Riverton")
    assert out["ok"] is False and "COACH_PLACES" in out["reason"] and fetch.calls == []


@pytest.mark.parametrize(
    ("body", "not_found"),
    [
        (geo(), True),
        (geo(place(region="East Shire")), True),  # asked for West Shire below
        (TimeoutError("timed out"), False),
        (b"not json", False),
    ],
)
def test_a_failed_lookup_requests_no_forecast_and_lists_named_places(
    body: bytes | Exception, not_found: bool
) -> None:
    fetch = Routed(body)
    out = run(fetch, env=PLACES_ENV, near="Riverton, West Shire")
    assert out["ok"] is False and out["not_found"] is not_found
    assert out["place_source"] == "lookup" and out["known_places"] == ["coast", "riverton"]
    assert len(fetch.calls) == 1 and fetch.weather.calls == []


def test_bad_text_requests_nothing() -> None:
    fetch = Routed(geo(place()))
    out = run(fetch, near="x" * 81)
    assert out["ok"] is False and "80" in out["reason"] and fetch.calls == []


def test_a_bad_window_is_refused_before_anything_is_looked_up() -> None:
    fetch = Routed(geo(place()))
    assert run(fetch, near="Riverton", hours=99)["ok"] is False
    assert fetch.calls == []


def test_place_and_near_together_are_refused() -> None:
    fetch = Routed(geo(place()))
    out = run(fetch, env=PLACES_ENV, place="coast", near="Riverton")
    assert out["ok"] is False and "not both" in out["reason"] and fetch.calls == []


def test_a_shared_reuse_dict_means_one_lookup_for_several_windows() -> None:
    fetch, reuse = Routed(geo(place())), {}
    for hour in (7, 8):
        assert run(fetch, near="Riverton", hour=hour, reuse=reuse)["ok"] is True
    assert len(fetch.calls) == 2  # one lookup, one forecast


def test_named_and_default_results_say_their_source() -> None:
    fetch = Routed(geo(place()))
    assert run(fetch)["place_source"] == "default"
    assert run(fetch, env=PLACES_ENV, place="coast")["place_source"] == "named"


# --- CLI ---------------------------------------------------------------------------------------


def test_cli_near(cli_env: None, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    fetch = Routed(geo(place(), place(region="East Shire")))
    monkeypatch.setattr(weather, "http_get", fetch)
    monkeypatch.setattr(weather, "_now", lambda tz: NOW)
    args = ["weather", "--date", "2026-03-02", "--hour", "7", "--near", "Riverton, East Shire"]
    r = runner.invoke(app, args)
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["place"] == "Riverton, East Shire, Testland" and data["place_source"] == "lookup"
    assert weather.GEOCODE_ATTRIBUTION in data["attribution"]
    assert "44.4" not in r.output and "55.5" not in r.output


def test_cli_near_not_found_exits_1(cli_env: None, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(weather, "http_get", Routed(geo()))
    r = runner.invoke(app, ["weather", "--near", "Nowhereville"])
    assert r.exit_code == 1
    data = json.loads(r.output)
    assert data["ok"] is False and data["not_found"] is True and data["known_places"] == []


def test_cli_place_with_near_is_a_json_error(
    cli_env: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch = Routed(geo(place()))
    monkeypatch.setattr(weather, "http_get", fetch)
    r = runner.invoke(app, ["weather", "--place", "coast", "--near", "Riverton"])
    assert r.exit_code == 1 and fetch.calls == []
    assert json.loads(r.output) == {"ok": False, "reason": "use --place or --near, not both"}
