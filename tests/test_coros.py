from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest
from typer.testing import CliRunner

from mara_coach import coros
from mara_coach.cli import app
from mara_coach.coros import CorosError, CorosRun
from mara_trainer import imports
from tests.fit_builder import build_fit

RECORDS = """Sport Records — 2026-11-02 to 2026-11-08 (3 records)
========================

1. Outdoor Run — 2026-11-07
   Location: Long run 10 mi
   Duration: 1:30:00 | Distance: 16.09 km
   LabelId: 111 | SportType: 100

2. Walk — 2026-11-05
   LabelId: 999 | SportType: 900

3. Indoor Run — 2026-11-04
   Location: Easy 3 mi
   Duration: 30:00 | Distance: 4.83 km
   LabelId: 222 | SportType: 101"""

TUE = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)  # 7am LA
SAT = datetime(2026, 11, 7, 16, 0, tzinfo=UTC)


def test_parse_runs_keeps_run_types_with_ids_and_dates() -> None:
    assert coros.parse_runs(RECORDS) == [
        CorosRun("111", 100, "2026-11-07"),
        CorosRun("222", 101, "2026-11-04"),
    ]


def test_text_decodes_json_encoded_strings_like_coros_sends() -> None:
    content = [NS(type="text", text=json.dumps(RECORDS))]
    assert coros.parse_runs(coros._text(content))[0] == CorosRun("111", 100, "2026-11-07")
    assert coros._text([NS(type="text", text='"not json')]) == '"not json'


def test_parse_runs_empty_week_and_unknown_format() -> None:
    assert coros.parse_runs("Sport Records — 2026-11-02 to 2026-11-08 (0 records)") == []
    with pytest.raises(CorosError, match="format"):
        coros.parse_runs("1. Outdoor Run — 2026-11-07\n   Id=111 type=100")
    with pytest.raises(CorosError, match="format"):  # one undecoded line: ids but no records
        coros.parse_runs(json.dumps(RECORDS))


def test_fit_bytes_reads_the_blob_resource() -> None:
    blob = base64.b64encode(b"FIT!").decode()
    content = [NS(type="text", text="Returned raw FIT"), NS(resource=NS(blob=blob))]
    assert coros.fit_bytes(content) == b"FIT!"
    with pytest.raises(CorosError, match="no FIT file"):
        coros.fit_bytes([NS(type="text", text="limit reached")])


class FakeApi:
    def __init__(self, files: dict[str, bytes | Exception], runs: list[CorosRun]) -> None:
        self.files, self._runs = files, runs
        self.downloads: list[str] = []
        self.windows: list[tuple[date, date]] = []

    async def runs(self, start: date, end: date) -> list[CorosRun]:
        self.windows.append((start, end))
        return self._runs

    async def fit(self, run: CorosRun) -> bytes:
        self.downloads.append(run.label_id)
        data = self.files[run.label_id]
        if isinstance(data, Exception):
            raise data
        return data


def _fit(start: datetime, meters: float = 16093, seconds: float = 5400) -> bytes:
    return build_fit([{"start": start, "meters": meters, "seconds": seconds, "sport": "running"}])


def _sync(conn: sqlite3.Connection, api: FakeApi, exports: Path) -> coros.SyncReport:
    return asyncio.run(coros.sync_runs(conn, api, date(2026, 11, 2), date(2026, 11, 8), exports))


RUNS = [CorosRun("1", 100, "2026-11-03"), CorosRun("2", 100, "2026-11-07")]


def test_sync_downloads_new_files_once_and_imports_once(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    api = FakeApi({"1": _fit(TUE, 5423, 1785), "2": _fit(SAT)}, RUNS)
    report = _sync(conn, api, tmp_path)
    assert api.downloads == ["1", "2"]
    assert report.imported == ["Tue 11/3: 3.37 mi, 29:45", "Sat 11/7: 10.00 mi, 1:30:00"]
    assert (tmp_path / "1.fit").exists() and (tmp_path / "2.fit").exists()
    again = _sync(conn, api, tmp_path)
    assert api.downloads == ["1", "2"]  # files on disk: nothing downloaded twice
    assert again.imported == [] and again.already_present == 2
    text = coros.report_text(conn, again)
    assert "No new runs (all already imported)" in text
    assert "This week: 2 runs, 13.37 mi, 1:59:45" in text


def test_near_duplicates_wait_for_confirmation(conn: sqlite3.Connection, tmp_path: Path) -> None:
    draft = imports.make_draft("apple_health", SAT, 16050, 5410, SAT)
    imports.commit(conn, imports.DecodedFile("apple_health", "h" * 64, [draft]), "export.zip")
    report = _sync(conn, FakeApi({"2": _fit(SAT)}, RUNS[1:]), tmp_path)
    assert report.imported == []
    assert [p.runs for p in report.pending] == [("Sat 11/7: 10.00 mi, 1:30:00",)]
    assert conn.execute("SELECT count(*) FROM activities").fetchone()[0] == 1
    assert "Reply *yes*" in coros.duplicate_question(report.pending)
    assert coros.import_confirmed(conn, report.pending) == 1
    assert conn.execute("SELECT count(*) FROM activities").fetchone()[0] == 2


def test_download_failure_stops_and_keeps_what_it_has(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    api = FakeApi({"1": _fit(TUE), "2": CorosError("limit")}, RUNS)
    report = _sync(conn, api, tmp_path)
    assert len(report.imported) == 1
    assert report.error == "limit" and report.not_downloaded == 1
    assert "1 run(s) not downloaded yet" in coros.report_text(conn, report)


def test_week_window_is_monday_through_today_local(conn: sqlite3.Connection) -> None:
    sunday_night_la = datetime(2026, 11, 9, 4, 0, tzinfo=UTC)  # Sun 11-08 8pm LA
    assert coros.week_window(conn, sunday_night_la) == (date(2026, 11, 2), date(2026, 11, 8))


def test_keychain_values_go_over_stdin_never_argv() -> None:
    store: dict[str, str] = {}
    calls: list[tuple[list[str], str | None]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs.get("input")))
        if args[1] == "-i":
            parts = kwargs["input"].split()
            store[parts[parts.index("-a") + 1]] = parts[parts.index("-w") + 1]
            return subprocess.CompletedProcess(args, 0, "", "")
        value = store.get(args[args.index("-a") + 1])
        return subprocess.CompletedProcess(args, 0 if value else 44, value or "", "")

    from mcp.shared.auth import OAuthToken

    keychain = coros.KeychainStorage(run)
    assert asyncio.run(keychain.get_tokens()) is None
    asyncio.run(keychain.set_tokens(OAuthToken(access_token="secret-a", refresh_token="r")))
    assert asyncio.run(keychain.get_tokens()).access_token == "secret-a"
    assert all("secret" not in " ".join(args) for args, _ in calls)
    assert all("secret" not in (stdin or "") for _, stdin in calls)  # base64 on stdin too


def test_errors_inside_exception_groups_are_found() -> None:
    wrapped = ExceptionGroup("tg", [ValueError("x"), ExceptionGroup("in", [coros.SignInNeeded()])])
    assert isinstance(coros._cause(wrapped), coros.SignInNeeded)
    assert coros._cause(RuntimeError("other")) is None


def test_cli_sync_exit_2_on_duplicates(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pending = coros.Pending(Path("x.fit"), ("k1",), ("Sat 11/7: 10.00 mi, 1:30:00",))
    report = coros.SyncReport("2026-11-02", "2026-11-08", pending=[pending])
    monkeypatch.setattr(coros, "sync_week", lambda conn: report)
    runner = CliRunner()
    assert runner.invoke(app, ["init-db"]).exit_code == 0
    result = runner.invoke(app, ["coros", "sync"])
    assert result.exit_code == 2 and '"keys": [\n        "k1"' in result.output


def test_cli_sync_reports_sign_in_needed(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(conn: sqlite3.Connection) -> coros.SyncReport:
        raise coros.SignInNeeded

    monkeypatch.setattr(coros, "sync_week", fail)
    runner = CliRunner()
    runner.invoke(app, ["init-db"])
    result = runner.invoke(app, ["coros", "sync"])
    assert result.exit_code == 1 and "coach coros login" in result.output


# ------------------------------------------------------------------ watch push

SCHEDULE = """Training Schedule
========================

2026-11-17
Easy 3 mi
Standalone workout (scheduled from the workout library)
idInPlan: 7
Distance: 4.83 km

2026-11-18
Intervals 5x1K
Plan workout
idInPlan: 3
Distance: 8.00 km"""


def _workout(day: str, weekday: str, name: str) -> dict[str, Any]:
    course = {"sportType": 1, "courseName": name, "courseDescription": "x", "sections": []}
    return {"date": day, "coros_date": day.replace("-", ""), "weekday": weekday, "course": course}


WEEK = {
    "week": 3,
    "workouts": [
        _workout("2026-11-16", "Monday", "Easy 2 mi"),
        _workout("2026-11-17", "Tuesday", "Easy 3 mi"),
        _workout("2026-11-18", "Wednesday", "Easy 4 mi"),
        _workout("2026-11-21", "Saturday", "Long run 12 mi"),
    ],
    "skipped": [
        {
            "date": "2026-11-19",
            "weekday": "Thursday",
            "kind": "strength",
            "reason": "Add it in the app.",
        }
    ],
}


class FakeWatch:
    def __init__(self, fail_on: str | None = None) -> None:
        self.created: list[tuple[str, str]] = []
        self.asked: list[tuple[str, str]] = []
        self.fail_on = fail_on

    async def schedule(self, start: str, end: str) -> dict[str, list[str]]:
        self.asked.append((start, end))
        return coros.parse_schedule(SCHEDULE)

    async def create(self, coros_date: str, course: dict[str, Any]) -> None:
        if coros_date == self.fail_on:
            raise CorosError("COROS said: bad date")
        self.created.append((coros_date, course["courseName"]))


def test_parse_schedule_names_by_date() -> None:
    assert coros.parse_schedule(SCHEDULE) == {
        "2026-11-17": ["Easy 3 mi"],
        "2026-11-18": ["Intervals 5x1K"],
    }
    assert coros.parse_schedule("Training Schedule\n\nNo workouts scheduled.") == {}


def test_push_never_doubles_a_day() -> None:
    """Empty days get ours; our own earlier push is left; any other workout blocks the day."""
    api = FakeWatch(fail_on="20261121")
    report = asyncio.run(coros.push_workouts(api, WEEK))
    assert api.asked == [("20261116", "20261121")]
    assert api.created == [("20261116", "Easy 2 mi")]
    assert report.sent == ["Mon 11/16: Easy 2 mi"]
    assert report.already == ["Tue 11/17: Easy 3 mi"]
    assert report.conflicts == ["Wed 11/18: Easy 4 mi (already there: Intervals 5x1K)"]
    assert report.failed == ["Sat 11/21: Long run 12 mi: COROS said: bad date"]
    assert report.not_sent == ["Thu 11/19: Add it in the app."]
    text = coros.push_text(report)
    assert "On your watch calendar (Week 3):" in text and "can't move them" in text


def test_push_with_no_runs_calls_nothing() -> None:
    api = FakeWatch()
    report = asyncio.run(coros.push_workouts(api, {**WEEK, "workouts": []}))
    assert api.asked == [] and api.created == [] and report.sent == []


def test_watch_week_needs_a_race_and_a_plan_week(conn: sqlite3.Connection) -> None:
    with pytest.raises(CorosError, match="No race"):
        coros.watch_week(conn, "2026-11-16", TUE)


AUTH_FAILED = (
    "Client authentication credentials missing, expired, or identity mismatched. Diagnostic "
    "advice: Verify COROS App login status, or confirm whether the current App account aligns "
    "with the MCP authorized account."
)


class AuthFailClient:
    """COROS answers a lapsed sign-in with a normal (not is_error) text result."""

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        return NS(is_error=False, content=[NS(text=json.dumps(AUTH_FAILED))])


def test_lapsed_sign_in_is_an_error_not_zero_runs() -> None:
    api = coros.McpCorosApi(AuthFailClient())
    with pytest.raises(coros.SignInNeeded):
        asyncio.run(api.runs(date(2026, 11, 16), date(2026, 11, 22)))
    with pytest.raises(coros.SignInNeeded):
        asyncio.run(api.schedule("20261116", "20261122"))


def test_login_ignores_the_saved_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        blob = base64.b64encode(b'{"access_token": "old", "token_type": "Bearer"}').decode()
        return subprocess.CompletedProcess(cmd, 0, stdout=blob)

    keychain = coros.KeychainStorage(run)
    assert asyncio.run(keychain.get_tokens()) is not None
    assert asyncio.run(coros.FreshSignIn(keychain).get_tokens()) is None

    used: list[Any] = []

    class Stop(Exception):
        pass

    def fake_connect(interactive: bool = False, storage: Any = None, browser: bool = True) -> Any:
        used.append((interactive, storage, browser))
        raise Stop

    monkeypatch.setattr(coros, "connect", fake_connect)
    monkeypatch.setattr(coros, "token_storage", lambda: keychain)
    with pytest.raises(CorosError):
        coros.login()
    assert used[0][0] is True and isinstance(used[0][1], coros.FreshSignIn)
    assert used[0][2] is True


# ------------------------------------------------------------------ file token storage (server)


def _secrets_dir(tmp_path: Path, mode: int = 0o700) -> Path:
    folder = tmp_path / "secrets"
    folder.mkdir()
    folder.chmod(mode)
    return folder


def test_file_storage_round_trips_tokens_and_client_with_0600_files(tmp_path: Path) -> None:
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
    from pydantic import AnyUrl

    folder = _secrets_dir(tmp_path)
    store = coros.FileTokenStorage(folder)
    assert asyncio.run(store.get_tokens()) is None
    assert asyncio.run(store.get_client_info()) is None
    asyncio.run(store.set_tokens(OAuthToken(access_token="secret-a", refresh_token="r")))
    client = OAuthClientInformationFull(client_id="c1", redirect_uris=[AnyUrl(coros.REDIRECT_URI)])
    asyncio.run(store.set_client_info(client))
    assert asyncio.run(store.get_tokens()).access_token == "secret-a"
    assert asyncio.run(store.get_client_info()).client_id == "c1"
    for name in ("coros-tokens.json", "coros-client.json"):
        assert (folder / name).stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in folder.iterdir()) == ["coros-client.json", "coros-tokens.json"]


def test_file_storage_overwrites_atomically(tmp_path: Path) -> None:
    from mcp.shared.auth import OAuthToken

    folder = _secrets_dir(tmp_path)
    store = coros.FileTokenStorage(folder)
    asyncio.run(store.set_tokens(OAuthToken(access_token="one")))
    asyncio.run(store.set_tokens(OAuthToken(access_token="two")))
    assert asyncio.run(store.get_tokens()).access_token == "two"
    assert [p.name for p in folder.iterdir()] == ["coros-tokens.json"]  # no tmp file left


@pytest.mark.parametrize("mode", [0o750, 0o705, 0o777])
def test_file_storage_refuses_a_loose_directory(tmp_path: Path, mode: int) -> None:
    from mcp.shared.auth import OAuthToken

    folder = _secrets_dir(tmp_path, mode)
    store = coros.FileTokenStorage(folder)
    with pytest.raises(CorosError, match="group or others"):
        asyncio.run(store.get_tokens())
    with pytest.raises(CorosError, match="group or others"):
        asyncio.run(store.set_tokens(OAuthToken(access_token="secret-a")))
    assert list(folder.iterdir()) == []


def test_file_storage_refuses_a_loose_file_without_showing_it(tmp_path: Path) -> None:
    folder = _secrets_dir(tmp_path)
    token = folder / "coros-tokens.json"
    token.write_text('{"access_token": "tok-value-a", "token_type": "Bearer"}')
    token.chmod(0o644)
    with pytest.raises(CorosError, match="group or others") as caught:
        asyncio.run(coros.FileTokenStorage(folder).get_tokens())
    assert "tok-value-a" not in str(caught.value)


def test_file_storage_needs_an_existing_directory(tmp_path: Path) -> None:
    with pytest.raises(CorosError, match="doesn't exist"):
        asyncio.run(coros.FileTokenStorage(tmp_path / "missing").get_tokens())


def test_fresh_sign_in_works_with_file_storage(tmp_path: Path) -> None:
    from mcp.shared.auth import OAuthToken

    store = coros.FileTokenStorage(_secrets_dir(tmp_path))
    asyncio.run(store.set_tokens(OAuthToken(access_token="old")))
    fresh = coros.FreshSignIn(store)
    assert asyncio.run(fresh.get_tokens()) is None
    asyncio.run(fresh.set_tokens(OAuthToken(access_token="new")))
    assert asyncio.run(store.get_tokens()).access_token == "new"


def test_token_storage_picks_the_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = _secrets_dir(tmp_path)
    monkeypatch.setenv("COACH_SECRET_BACKEND", "file")
    monkeypatch.setenv("COACH_SECRETS_DIR", str(folder))
    store = coros.token_storage()
    assert isinstance(store, coros.FileTokenStorage) and store.folder == folder
    monkeypatch.setenv("COACH_SECRET_BACKEND", "keychain")
    assert isinstance(coros.token_storage(), coros.KeychainStorage)
    monkeypatch.setenv("COACH_SECRET_BACKEND", "vault")
    with pytest.raises(CorosError, match="COACH_SECRET_BACKEND"):
        coros.token_storage()


def test_token_storage_default_follows_the_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coros.sys, "platform", "darwin")
    assert isinstance(coros.token_storage(), coros.KeychainStorage)
    monkeypatch.setattr(coros.sys, "platform", "linux")
    with pytest.raises(CorosError, match="COACH_SECRETS_DIR"):
        coros.token_storage()
    monkeypatch.setenv("COACH_SECRETS_DIR", str(_secrets_dir(tmp_path)))
    assert isinstance(coros.token_storage(), coros.FileTokenStorage)


def test_headless_login_prints_the_url_and_tunnel_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(coros.webbrowser, "open", opened.append)
    asyncio.run(coros._print_url("https://coros.example/authorize?x=1"))
    err = capsys.readouterr().err
    assert "https://coros.example/authorize?x=1" in err
    assert "ssh -L 33418:127.0.0.1:33418" in err
    assert opened == []


def test_connect_picks_the_redirect_handler() -> None:
    assert coros._redirect_handler(interactive=True, browser=True) is coros._open_browser
    assert coros._redirect_handler(interactive=True, browser=False) is coros._print_url
    assert coros._redirect_handler(interactive=False, browser=False) is coros._no_browser


def test_cli_login_no_browser_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(coros, "login", lambda browser=True: calls.append(browser))
    runner = CliRunner()
    assert runner.invoke(app, ["coros", "login", "--no-browser"]).exit_code == 0
    assert runner.invoke(app, ["coros", "login"]).exit_code == 0
    assert calls == [False, True]


def test_cli_login_and_sync_load_env_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """The storage backend can live in .env or a systemd credential, so login must see it."""
    monkeypatch.delenv("COACH_SECRET_BACKEND", raising=False)
    import mara_coach.cli as cli

    monkeypatch.setattr(cli, "load_env", lambda: monkeypatch.setenv("COACH_SECRET_BACKEND", "file"))
    seen: list[str | None] = []
    monkeypatch.setattr(
        coros, "login", lambda browser=True: seen.append(os.environ.get("COACH_SECRET_BACKEND"))
    )
    assert CliRunner().invoke(app, ["coros", "login"]).exit_code == 0
    assert seen == ["file"]


def test_damaged_token_file_names_the_file_not_the_token(tmp_path: Path) -> None:
    folder = tmp_path / "secrets"
    folder.mkdir(mode=0o700)
    bad = folder / "coros-tokens.json"
    bad.write_text('{"access_token": "SECRETVALUE12345')
    bad.chmod(0o600)
    with pytest.raises(CorosError) as caught:
        asyncio.run(coros.FileTokenStorage(folder).get_tokens())
    assert "damaged" in str(caught.value) and "SECRETVALUE" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
