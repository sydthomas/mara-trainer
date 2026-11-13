"""Weather in Slack: the forecast block in the live Sunday check-in and the "weather" phrases.
No network: the bot's forecast is a fake wrapped around `weather.forecast` with a fake fetch.
Plan, coordinates and values are synthetic."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from mara_coach import slack_bot, slack_schedule, weather
from mara_trainer import db

from .test_slack_bot import (
    FakeCoach,
    SharedDB,
    SlackFake,
    _debrief_bot,
    _dm,
    _inflight,
    _leftover,
    _sun,
)
from .test_slack_schedule import ScheduleFake
from .test_weather import CONTACT, FakeFetch, FakeNws, grid_body, payload

TZ = "America/Los_Angeles"
ENV = {"COACH_LAT": "12.34567", "COACH_LON": "-98.76543", "COACH_PLACES": "coast:21.987,-101.234"}
FIRST = datetime(2026, 11, 8, 0, 0, tzinfo=ZoneInfo(TZ))  # the Sunday of the check-in
# Week 1 ends Sun 2026-11-08; week 2 has runs Tue 11-10, Thu 11-12 and Sat 11-14.
PLAN = {
    "weeks": [
        {
            "week": 1,
            "start_date": "2026-11-02",
            "sessions": [
                {"day": 0, "kind": "rest", "distance_meters": 0},
                {"day": 6, "kind": "long", "distance_meters": 16093},
            ],
        },
        {
            "week": 2,
            "start_date": "2026-11-09",
            "sessions": [
                {"day": 0, "kind": "rest", "distance_meters": 0},
                {"day": 1, "kind": "strength", "distance_meters": 0},
                {"day": 2, "kind": "easy", "distance_meters": 8047},
                {"day": 4, "kind": "workout", "distance_meters": 9656},
                {"day": 6, "kind": "long", "distance_meters": 19312},
            ],
        },
    ]
}
LINE = "63°F (feels -40°F), dew point 32°F, wind 22 mph, rain chance 20%"  # 7am in `payload`
OPEN_METEO = "<https://open-meteo.com/|Weather data by Open-Meteo.com>"
WEATHER_WORDS = ("°F", "dew point", "rain chance", "WBGT", "Open-Meteo", "Forecast", "wind")


class FakeForecast:
    """Stands in for the bot's forecast: the real `weather.forecast` over a fake fetch."""

    def __init__(self, days: int = 8, fetch: Any = None, nws: FakeNws | None = None) -> None:
        self.fetch = fetch or FakeFetch(json.dumps(payload(days=days, first=FIRST)).encode())
        self.nws = nws
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *, nws: bool = True, **window: Any) -> dict[str, Any]:
        self.calls.append({"nws": nws, **window})
        env = {**ENV, "COACH_NWS_CONTACT": CONTACT} if self.nws and nws else ENV
        return weather.forecast(env=env, fetch=self.fetch, nws_fetch=self.nws, **window)


def _weekly(forecast: Any, coach: FakeCoach | None = None) -> tuple[Any, SlackFake]:
    shared, client = SharedDB(), SlackFake()
    db.save_plan(shared.conn, PLAN)
    bot = _debrief_bot(coach or FakeCoach(["Recap and preview."]), shared, _sun(2, 9))  # Sun 6pm
    bot.forecast = forecast
    assert bot.maybe_weekly(client)
    return bot, client


def test_live_check_in_ends_with_a_line_per_run_and_the_attribution() -> None:
    forecast = FakeForecast()
    _, client = _weekly(forecast)
    assert client.posted[-1]["text"] == "\n".join(
        [
            "Recap and preview.",
            "",
            slack_bot.WEATHER_HEADER,
            f"• Tue 11-10 morning (7am): {LINE}",
            f"• Thu 11-12 morning (7am): {LINE}",
            f"• Sat 11-14 morning (7am): {LINE}",
            f"_{OPEN_METEO}_",
        ]
    )
    # one lookup per run day (not the strength or rest day), default location, default hour
    assert [c["date"] for c in forecast.calls] == ["2026-11-10", "2026-11-12", "2026-11-14"]
    assert all(c["hour"] == slack_bot.DEFAULT_MORNING_HOUR == 7 for c in forecast.calls)
    assert all("place" not in c and c["hours"] == 1 for c in forecast.calls)
    # ... but one request for the whole week: the response already holds every day
    assert len(forecast.fetch.calls) == 1
    assert all(c["reuse"] is forecast.calls[0]["reuse"] for c in forecast.calls)


def test_runs_beyond_the_forecast_get_no_line_and_no_more_requests() -> None:
    forecast = FakeForecast(days=4)  # covers Sun 11-08 through Wed 11-11
    _, client = _weekly(forecast)
    text = client.posted[-1]["text"]
    assert "• Tue 11-10 morning (7am)" in text and text.endswith(f"_{OPEN_METEO}_")
    assert "Thu" not in text and "Sat" not in text
    assert len(forecast.calls) == 2  # Thursday was past the end, so Saturday wasn't asked


def test_wbgt_and_its_attribution_appear_only_when_nws_returned_it() -> None:
    tuesday_7am = [{"validTime": "2026-11-10T15:00:00+00:00/PT1H", "value": 10.0}]
    forecast = FakeForecast(nws=FakeNws(grid=grid_body(values=tuesday_7am)))
    _, client = _weekly(forecast)
    lines = client.posted[-1]["text"].splitlines()
    assert lines[3] == f"• Tue 11-10 morning (7am): {LINE}, WBGT 50°F"
    assert lines[4] == f"• Thu 11-12 morning (7am): {LINE}"
    assert lines[-1].startswith(f"_{OPEN_METEO} · <https://www.weather.gov/|Wet-bulb globe")
    # NWS had nothing for Thursday, so Saturday didn't wait on it again
    assert [c["nws"] for c in forecast.calls] == [True, True, False]
    # two NWS requests for the week (points, then its grid), not two per run
    assert forecast.nws is not None and len(forecast.nws.calls) == 2
    assert len(forecast.fetch.calls) == 1
    assert CONTACT not in client.posted[-1]["text"]


class Raises:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **window: Any) -> dict[str, Any]:
        self.calls += 1
        raise RuntimeError("https://example.invalid/?latitude=12.35")


class NoLocation:
    """The real `weather.forecast` with no location set: `ok: False` with `missing`."""

    def __init__(self) -> None:
        self.fetch = FakeFetch(b"")
        self.results: list[dict[str, Any]] = []

    def __call__(self, *, nws: bool = True, **window: Any) -> dict[str, Any]:
        self.results.append(weather.forecast(env={}, fetch=self.fetch, **window))
        return self.results[-1]


@pytest.mark.parametrize(
    "forecast",
    [
        FakeForecast(fetch=FakeFetch(TimeoutError("timed out"))),
        FakeForecast(fetch=FakeFetch(b"not json")),
        NoLocation(),
        lambda **window: {"ok": False},
        Raises(),
    ],
)
def test_a_weather_failure_never_blocks_the_check_in(forecast: Any) -> None:
    _, client = _weekly(forecast)
    assert client.posted == [{"channel": "D1", "text": "Recap and preview."}]
    if isinstance(forecast, FakeForecast):
        assert len(forecast.fetch.calls) == 1  # one timeout at most, not one per run
    if isinstance(forecast, NoLocation):  # the missing-location result itself, and no request
        assert len(forecast.results) == 1 and forecast.fetch.calls == []
        assert forecast.results[0]["ok"] is False
        assert forecast.results[0]["missing"] == ["COACH_LAT", "COACH_LON"]
    if isinstance(forecast, Raises):
        assert forecast.calls == 1


def test_no_forecast_when_the_coach_failed_or_no_week_is_coming() -> None:
    forecast = FakeForecast()
    _, client = _weekly(forecast, FakeCoach([RuntimeError("API down")]))
    assert client.posted[-1]["text"] == slack_bot.WEEKLY_FAILED
    assert forecast.calls == []
    shared, client = SharedDB(), SlackFake()
    db.save_plan(shared.conn, {"weeks": PLAN["weeks"][:1]})  # week 1 is the last plan week
    bot = _debrief_bot(FakeCoach(["Recap."]), shared, _sun(2, 9))
    bot.forecast = forecast
    assert bot.maybe_weekly(client)
    assert client.posted[-1]["text"] == "Recap." and forecast.calls == []


def test_messages_queued_ahead_on_slack_never_contain_weather() -> None:
    """The fallbacks are written up to a week early: a forecast in them would be stale."""
    shared, client = SharedDB(), ScheduleFake()
    db.save_plan(shared.conn, PLAN)
    forecast = FakeForecast()
    bot = _debrief_bot(FakeCoach([]), shared, datetime(2026, 11, 6, 20, 0, tzinfo=UTC))  # Fri noon
    bot.forecast = forecast
    assert bot.maybe_schedule(client) > 0
    texts = [q["text"] for q in client.queued.values()]
    assert any(t.startswith("Sunday check-in.") and "Next week (week 2)" in t for t in texts)
    assert any(t.startswith("Long run day") for t in texts)
    for text in texts:
        assert not any(word in text for word in WEATHER_WORDS), text
    assert forecast.calls == []
    assert not hasattr(slack_schedule, "weather")  # the queue module can't even reach it
    week = {"week": 2, "sessions": [{"kind": "long", "weekday": "Saturday", "date": "2026-11-14"}]}
    week["sessions"][0]["miles"] = 12.0
    assert not any(w in slack_schedule.weekly_fallback_text(week) for w in WEATHER_WORDS)


def _add_run(shared: SharedDB, started_at: str) -> None:
    with shared.conn:
        shared.conn.execute(
            "INSERT INTO activities(source, source_id, fingerprint, title, started_at, "
            "distance_meters, duration_seconds) VALUES ('manual', ?, ?, 'Run', ?, 8047, 2700)",
            (started_at, "0" * 64, started_at),
        )


def _answer(text: str) -> list[dict[str, Any]]:
    """What a code-answered weather DM posts: the placeholder, then the answer."""
    return [{"channel": "D1", "text": slack_bot.CHECKING_WEATHER}, {"channel": "D1", "text": text}]


def _ask(
    text: str, when: datetime, forecast: Any, plan: Any = PLAN, ran: str | None = None
) -> tuple[SlackFake, FakeCoach]:
    shared, client, coach = SharedDB(), SlackFake(), FakeCoach(["from the coach"])
    if plan:
        db.save_plan(shared.conn, plan)
    if ran:
        _add_run(shared, ran)
    bot = _debrief_bot(coach, shared, when)
    bot.forecast = forecast
    bot.handle(_dm(text), client)
    return client, coach


MONDAY_NOON = datetime(2026, 11, 9, 20, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "text",
    ["weather", "Weather?", "weather for my run", "What's the weather for my next run?"],
)
def test_weather_phrases_are_answered_in_code_for_the_next_plan_run(text: str) -> None:
    forecast = FakeForecast()
    client, coach = _ask(text, MONDAY_NOON, forecast)
    assert coach.calls == []  # no LLM
    assert client.posted == _answer(
        f"*Tue 11-10: 5 mi easy, morning (7am)*\n{LINE}\n_{OPEN_METEO}_"
    )
    assert client.deleted == [{"channel": "D1", "ts": "ts1"}]  # the placeholder
    assert len(forecast.calls) == 1 and forecast.calls[0]["near"] is None
    assert len(forecast.fetch.calls) == 1  # the forecast only: nothing is looked up
    assert forecast.calls[0]["date"] == "2026-11-10" and forecast.calls[0]["hour"] == 7


@pytest.mark.parametrize("text", ["weather at Coast", "weather for my run at coast."])
def test_weather_at_a_named_place(text: str) -> None:
    forecast = FakeForecast()
    client, coach = _ask(text, MONDAY_NOON, forecast)
    assert coach.calls == []
    assert client.posted[-1]["text"].startswith("*Tue 11-10: 5 mi easy, morning (7am) at coast*\n")
    assert "latitude=21.99&longitude=-101.23" in forecast.fetch.calls[0][0]
    for secret in ("21.9", "101.2", "12.3", "98.7"):
        assert secret not in client.posted[-1]["text"]


GEONAMES = "<https://www.geonames.org/|Location data based on GeoNames>"


def _geo(*results: dict[str, Any]) -> bytes:
    return json.dumps({"results": list(results)} if results else {}).encode()


RIVERTON = {
    "name": "Riverton",
    "latitude": 44.44444,
    "longitude": -55.55555,
    "admin1": "Lake <State> & Co",
    "country": "Testland",
    "country_code": "TL",
}
RIVERTON_EAST = {**RIVERTON, "latitude": 11.11111, "longitude": -22.22222, "admin1": "East Shire"}


class Routed:
    """A fetch that answers the geocoding URL with `geo` and anything else with the forecast."""

    def __init__(self, geo: bytes | Exception) -> None:
        self.geo = FakeFetch(geo)
        self.weather = FakeFetch(json.dumps(payload(days=8, first=FIRST)).encode())
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout: float) -> bytes:
        self.calls.append((url, timeout))
        return (self.geo if url.startswith(weather.GEOCODE_URL) else self.weather)(url, timeout)


@pytest.mark.parametrize(
    ("text", "searched"),
    [
        ("weather in Riverton", "Riverton"),
        ("Weather near  riverton?", "riverton"),
        ("what's the weather for my run at Riverton.", "Riverton"),
    ],
)
def test_weather_at_an_unlisted_place_is_looked_up_and_shows_the_match(
    text: str, searched: str
) -> None:
    forecast = FakeForecast(fetch=Routed(_geo(RIVERTON)))
    client, coach = _ask(text, MONDAY_NOON, forecast)
    assert coach.calls == []  # no LLM
    assert client.posted == _answer(
        "*Tue 11-10: 5 mi easy, morning (7am) at Riverton, Lake &lt;State&gt; &amp; Co, "
        f"Testland*\n{LINE}\n_{OPEN_METEO} · {GEONAMES}_"
    )
    geo, forecast_call = forecast.fetch.calls  # one lookup, one forecast
    assert f"name={searched}&" in geo[0] and geo[1] <= 5
    assert "latitude=44.44&longitude=-55.56" in forecast_call[0]
    for secret in ("44.4", "55.5"):
        assert secret not in client.posted[-1]["text"]


def test_multi_word_place_with_a_region_picks_the_matching_result() -> None:
    forecast = FakeForecast(fetch=Routed(_geo(RIVERTON, RIVERTON_EAST)))
    client, _ = _ask("weather in Riverton, east shire", MONDAY_NOON, forecast)
    assert " at Riverton, East Shire, Testland*" in client.posted[-1]["text"]
    assert "name=Riverton&" in forecast.fetch.calls[0][0]
    assert "latitude=11.11&longitude=-22.22" in forecast.fetch.calls[1][0]
    forecast = FakeForecast(fetch=Routed(_geo({**RIVERTON, "name": "Port San Riverton"})))
    client, _ = _ask("weather near Port San Riverton", MONDAY_NOON, forecast)
    assert " at Port San Riverton, " in client.posted[-1]["text"]
    assert "name=Port+San+Riverton&" in forecast.fetch.calls[0][0]


def test_a_named_place_is_never_looked_up() -> None:
    forecast = FakeForecast(fetch=Routed(_geo(RIVERTON)))
    client, _ = _ask("weather in coast", MONDAY_NOON, forecast)
    assert " at coast*" in client.posted[-1]["text"] and "GeoNames" not in client.posted[-1]["text"]
    assert [url.startswith(weather.GEOCODE_URL) for url, _ in forecast.fetch.calls] == [False]


@pytest.mark.parametrize(
    ("geo", "reason"),
    [
        (_geo(), 'no place found for "desert"'),
        (TimeoutError("timed out"), "place lookup timed out after 5 s"),
        (b"<html>", "unexpected place lookup response: not JSON"),
    ],
)
def test_place_not_found_or_lookup_failed_says_so_and_lists_named_places(
    geo: bytes | Exception, reason: str
) -> None:
    forecast = FakeForecast(fetch=Routed(geo))
    client, coach = _ask("weather at desert", MONDAY_NOON, forecast)
    assert coach.calls == [] and len(forecast.fetch.calls) == 1  # no forecast request
    assert client.posted == _answer(f"Couldn't look up that place: {reason}. Named places: coast.")


def test_a_region_that_matches_nothing_is_not_silently_replaced() -> None:
    forecast = FakeForecast(fetch=Routed(_geo(RIVERTON_EAST)))
    client, _ = _ask("weather in Riverton, West Shire", MONDAY_NOON, forecast)
    assert client.posted[-1]["text"] == (
        'Couldn\'t look up that place: no "Riverton" found in "West Shire" '
        "(found: Riverton, East Shire, Testland). Named places: coast."
    )
    assert len(forecast.fetch.calls) == 1


def test_resume_reruns_a_cut_off_lookup() -> None:
    shared, client, coach = SharedDB(), SlackFake(), FakeCoach([])
    db.save_plan(shared.conn, PLAN)
    _leftover(shared, kind="weather", question="Riverton, East Shire", at=MONDAY_NOON.timestamp())
    bot = _debrief_bot(coach, shared, MONDAY_NOON)
    bot.forecast = FakeForecast(fetch=Routed(_geo(RIVERTON_EAST)))
    assert bot.resume(client)
    assert coach.calls == [] and _inflight(shared) is None
    assert " at Riverton, East Shire, Testland*" in client.posted[-1]["text"]


@pytest.mark.parametrize(
    ("text", "place"),
    [
        ("weather", ""),
        ("weather for my run", ""),
        ("weather in Riverton", "Riverton"),
        ("weather at  Port San   Riverton!", "Port San Riverton"),
        ("Weather near Riverton, East Shire?", "Riverton, East Shire"),
        ("weather in St. Riverton-on-Sea.", "St. Riverton-on-Sea"),
        ("weather in Åre", "Åre"),
        ("weather in Riverton &amp; Hove", "Riverton & Hove"),
        ("how does weather in summer affect pace?", None),
        ("is the weather in Riverton ok for a long run", None),
        ("weather in Riverton? should I move my long run", None),
        ("weather in Riverton\nand what should I eat", None),
        ("weather in the place where I ran my last half marathon", None),  # too many words
        ("weather at 12.34,56.78", None),
        ("weather in " + "x" * 81, None),
        ("weather in &lt;https://example.invalid&gt;", None),
        # "me" / "here" mean the default location: no lookup
        ("weather near me", ""),
        ("Weather near me?", ""),
        ("weather here", None),  # not the phrase (no at/in/near)
        ("weather in here", ""),
        ("weather for my run near my location", ""),
        # a time or a generic spot is not a place name: the coach gets it, nothing is looked up
        ("weather in the morning", None),
        ("weather for my run in the afternoon", None),
        ("weather at 6am tomorrow", None),
        ("weather at 6", None),
        ("weather at 6:30 pm", None),
        ("weather at noon", None),
        ("weather at sunrise", None),
        ("weather in an hour", None),
        ("weather near my office", None),
        ("weather in Riverton tomorrow", None),
        ("weather in Riverton on Saturday", None),
        ("weather at the track", None),
        ("weather at the Track.", None),
        # ... but names that only look like one still are
        ("weather at The Strand", "The Strand"),
        ("weather in Sunrise, East Shire", "Sunrise, East Shire"),
        ("weather near Sun Valley", "Sun Valley"),
        ("weather in Morningside Park", "Morningside Park"),
        ("weather in 90210", "90210"),
        ("weather at home", "home"),  # can be a named place
    ],
)
def test_only_a_whole_message_weather_phrase_is_a_place_request(
    text: str, place: str | None
) -> None:
    assert slack_bot.weather_place(text) == place


def test_todays_run_after_the_morning_hour_is_forecast_from_now() -> None:
    forecast = FakeForecast()
    tuesday_2pm = datetime(2026, 11, 10, 22, 0, tzinfo=UTC)
    client, _ = _ask("weather", tuesday_2pm, forecast)
    assert forecast.calls[0]["date"] == "2026-11-10" and forecast.calls[0]["hour"] == 14
    assert client.posted[-1]["text"].startswith("*Tue 11-10: 5 mi easy, 2pm*\n")
    forecast = FakeForecast()
    tuesday_6am = datetime(2026, 11, 10, 14, 0, tzinfo=UTC)
    client, _ = _ask("weather", tuesday_6am, forecast)
    assert forecast.calls[0]["hour"] == 7 and "morning (7am)" in client.posted[-1]["text"]


def test_todays_run_already_in_the_database_means_the_next_run() -> None:
    forecast = FakeForecast()
    tuesday_8pm = datetime(2026, 11, 11, 4, 0, tzinfo=UTC)
    # ran Tuesday 7am local (15:00 UTC); a Monday-evening run (local) doesn't count as Tuesday's
    client, _ = _ask(
        "weather for my next run", tuesday_8pm, forecast, ran="2026-11-10T15:00:00.000Z"
    )
    assert forecast.calls[0]["date"] == "2026-11-12" and forecast.calls[0]["hour"] == 7
    assert client.posted[-1]["text"].startswith("*Thu 11-12: 6 mi workout, morning (7am)*\n")
    forecast = FakeForecast()
    client, _ = _ask("weather", tuesday_8pm, forecast, ran="2026-11-10T03:00:00.000Z")
    assert forecast.calls[0]["date"] == "2026-11-10" and forecast.calls[0]["hour"] == 20
    assert client.posted[-1]["text"].startswith("*Tue 11-10: 5 mi easy, 8pm*\n")


def test_a_weather_reply_is_recorded_while_it_is_fetched_then_cleared() -> None:
    shared, client, seen = SharedDB(), SlackFake(), []
    db.save_plan(shared.conn, PLAN)
    forecast = FakeForecast()

    def peek(**window: Any) -> dict[str, Any]:
        seen.append(_inflight(shared))
        return forecast(**window)

    bot = _debrief_bot(FakeCoach([]), shared, MONDAY_NOON)
    bot.forecast = peek
    bot.handle(_dm("weather at coast"), client)
    assert seen[0] is not None
    assert (seen[0]["kind"], seen[0]["question"], seen[0]["placeholder"], seen[0]["tries"]) == (
        "weather",
        "coast",
        "ts1",
        1,
    )
    assert _inflight(shared) is None


@pytest.mark.parametrize(("place", "where"), [("", ""), ("coast", " at coast")])
def test_resume_reruns_a_cut_off_weather_reply(place: str, where: str) -> None:
    shared, client, coach = SharedDB(), SlackFake(), FakeCoach([])
    db.save_plan(shared.conn, PLAN)
    _leftover(shared, kind="weather", question=place, at=MONDAY_NOON.timestamp())
    bot = _debrief_bot(coach, shared, MONDAY_NOON)
    bot.forecast = FakeForecast()
    assert bot.resume(client)
    assert coach.calls == []
    assert client.posted == [  # no new placeholder
        {
            "channel": "D1",
            "text": f"*Tue 11-10: 5 mi easy, morning (7am){where}*\n{LINE}\n_{OPEN_METEO}_",
        }
    ]
    assert client.deleted == [{"channel": "D1", "ts": "ts9"}]
    assert _inflight(shared) is None


def test_no_run_inside_the_forecast_means_no_request() -> None:
    forecast = FakeForecast()
    client, coach = _ask("weather", MONDAY_NOON, forecast, plan=None)
    assert client.posted == _answer(slack_bot.NO_RUN_SOON)
    assert forecast.calls == [] and coach.calls == []


@pytest.mark.parametrize(
    ("forecast", "reply"),
    [
        (
            FakeForecast(fetch=FakeFetch(TimeoutError("timed out"))),
            "Couldn't get the forecast: weather request timed out after 10 s.",
        ),
        (Raises(), slack_bot.WEATHER_FAILED),
    ],
)
def test_a_failed_weather_reply_says_so_without_the_coach(forecast: Any, reply: str) -> None:
    client, coach = _ask("weather", MONDAY_NOON, forecast)
    assert client.posted == _answer(reply) and coach.calls == []


@pytest.mark.parametrize(
    "text",
    [
        "what's the weather like for marathon training?",
        "weather at 12.34,56.78",
        "should I run in this weather",
        "how do I fuel in hot weather",
        "how does weather in summer affect pace?",
        "weather in Riverton? should I move my long run",
    ],
)
def test_other_weather_questions_still_go_to_the_coach(text: str) -> None:
    forecast = FakeForecast()
    client, coach = _ask(text, MONDAY_NOON, forecast)
    assert len(coach.calls) == 1 and forecast.calls == []
    assert client.posted[-1]["text"] == "from the coach"


def test_the_live_forecast_gets_only_the_weather_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        return {"ok": False, "reason": "test"}

    monkeypatch.setattr(weather, "forecast", fake)
    for name, value in {**ENV, "COACH_NWS_CONTACT": CONTACT, "SLACK_BOT_TOKEN": "x"}.items():
        monkeypatch.setenv(name, value)
    slack_bot._live_forecast(timezone=TZ, hours=1)
    slack_bot._live_forecast(timezone=TZ, hours=1, nws=False)
    assert seen[0] == {"env": {**ENV, "COACH_NWS_CONTACT": CONTACT}, "timezone": TZ, "hours": 1}
    assert seen[1]["env"] == ENV


@pytest.mark.parametrize("text", ["weather near me", "weather in here"])
def test_weather_near_me_uses_the_default_location_without_a_lookup(text: str) -> None:
    forecast = FakeForecast()
    client, coach = _ask(text, MONDAY_NOON, forecast)
    assert coach.calls == []
    assert forecast.calls[0]["near"] is None
    assert len(forecast.fetch.calls) == 1  # the forecast only: no geocoding request
    assert "geocoding" not in forecast.fetch.calls[0][0]
    assert client.posted[-1]["text"].startswith("*Tue 11-10: 5 mi easy, morning (7am)*\n")


@pytest.mark.parametrize(
    "text", ["weather in the morning", "weather at 6am tomorrow", "weather at the track"]
)
def test_a_time_or_generic_spot_goes_to_the_coach_with_no_lookup(text: str) -> None:
    forecast = FakeForecast()
    _, coach = _ask(text, MONDAY_NOON, forecast)
    assert len(coach.calls) == 1
    assert forecast.calls == [] and forecast.fetch.calls == []
