"""COROS run sync in plain code: the official COROS MCP server, no LLM, no unofficial APIs.

- Same flow as the `coros-sync` skill: this training week's runs (Monday through today, athlete
  timezone) from `querySportRecords`, one `downloadActivityFitFiles` call per run that isn't in
  `exports/coros/<labelId>.fit` yet (COROS limits downloads per day), then `imports.commit`, so
  dedup applies and the source stays `fit`. Safe to repeat.
- Near-duplicates are never imported here: they come back in `SyncReport.pending` for the runner
  to confirm (`import_confirmed`). Nothing is auto-merged or deleted.
- Sign-in: `coach coros login` runs the OAuth flow once in the browser (dynamic client
  registration + PKCE, handled by the `mcp` SDK). The client registration and tokens live in the
  macOS keychain (`KEYCHAIN_SERVICE`), never in a file, log or message. The bot refreshes tokens
  on its own; when it can't, it says to sign in again (`SignInNeeded`) instead of opening a
  browser.
- Only the run list and FIT files are read. Health data tools (sleep, HRV, cycles) aren't called.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sqlite3
import subprocess
import webbrowser
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from mara_trainer import db, imports, reports

from .tools import MI_METERS

SERVER_URL = "https://mcpus.coros.com/mcp"  # US region; see CLAUDE.md
RUN_SPORT_TYPES = (100, 101, 102, 103)  # outdoor, indoor, trail, track
EXPORTS = Path(__file__).resolve().parents[2] / "exports" / "coros"
KEYCHAIN_SERVICE = "mara-trainer-coros"
REDIRECT_PORT = 33418  # loopback port for the one-time sign-in
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
LOGIN_TIMEOUT = 5 * 60  # seconds to finish signing in
SIGN_IN_AGAIN = "COROS needs you to sign in again: run `uv run coach coros login` on the laptop."


class CorosError(Exception):
    """A COROS failure with a message that's safe to show the runner."""


class SignInNeeded(CorosError):
    def __init__(self) -> None:
        super().__init__(SIGN_IN_AGAIN)


@dataclass(frozen=True)
class CorosRun:
    label_id: str
    sport_type: int
    day: str  # YYYY-MM-DD as COROS lists it


class CorosApi(Protocol):
    async def runs(self, start: date, end: date) -> list[CorosRun]: ...
    async def fit(self, run: CorosRun) -> bytes: ...


# ------------------------------------------------------------------ parsing MCP results

_RECORD = re.compile(r"^\s*\d+\.\s.*?(\d{4}-\d{2}-\d{2})\s*$", re.M)
_IDS = re.compile(r"LabelId:\s*(\d+)\s*\|\s*SportType:\s*(\d+)")
_NONE = re.compile(r"\(0 records\)|no (sport )?records", re.I)


def parse_runs(text: str) -> list[CorosRun]:
    """Runs from `querySportRecords` text: each record is a numbered line ending in its date,
    followed later by a `LabelId: N | SportType: N` line."""
    runs: list[CorosRun] = []
    for i, record in enumerate(heads := list(_RECORD.finditer(text))):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        ids = _IDS.search(text, record.end(), end)
        if ids and int(ids.group(2)) in RUN_SPORT_TYPES:
            runs.append(CorosRun(ids.group(1), int(ids.group(2)), record.group(1)))
    if not runs and (heads or _IDS.search(text)) and not _NONE.search(text):
        raise CorosError("COROS changed its run list format; sync in Claude Code for now.")
    return runs


def fit_bytes(content: Sequence[Any]) -> bytes:
    """The FIT file in a `downloadActivityFitFiles` result: the first base64 blob resource."""
    for block in content:
        blob = getattr(getattr(block, "resource", None), "blob", None)
        if isinstance(blob, str):
            return base64.b64decode(blob)
    raise CorosError("COROS sent no FIT file (the daily download limit may be used up).")


def _text(content: Sequence[Any]) -> str:
    """Text blocks joined. COROS sends each as a JSON-encoded string, so decode those."""
    parts = []
    for block in content:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        if text.startswith('"'):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                decoded = None
            text = decoded if isinstance(decoded, str) else text
        parts.append(text)
    return "\n".join(parts)


# ------------------------------------------------------------------ sync


@dataclass(frozen=True)
class Pending:
    """A file whose runs look like ones already imported; imported only after a yes."""

    path: Path
    keys: tuple[str, ...]
    runs: tuple[str, ...]  # display lines for the question


@dataclass
class SyncReport:
    start: str
    end: str
    imported: list[str] = field(default_factory=list)  # display lines, oldest first
    already_present: int = 0
    pending: list[Pending] = field(default_factory=list)
    not_downloaded: int = 0  # left for later (download limit or an error)
    error: str | None = None


async def sync_runs(
    conn: sqlite3.Connection, api: CorosApi, start: date, end: date, exports_dir: Path = EXPORTS
) -> SyncReport:
    report = SyncReport(start.isoformat(), end.isoformat())
    tz = db.athlete_timezone(conn)
    runs = await api.runs(start, end)
    exports_dir.mkdir(parents=True, exist_ok=True)
    for i, run in enumerate(runs):
        path = exports_dir / f"{run.label_id}.fit"
        if not path.exists():
            try:
                data = await api.fit(run)
            except CorosError as exc:
                report.error, report.not_downloaded = str(exc), len(runs) - i
                break
            path.write_bytes(data)
        try:
            decoded = imports.decode_file(path, tz)
        except imports.ImportError_ as exc:
            report.error = f"Couldn't read the COROS file for {run.day}: {exc}"
            continue
        candidates = imports.preview(conn, decoded)
        result = imports.commit(conn, decoded, path.name)
        if result.needs_confirmation:
            report.pending.append(
                Pending(
                    path,
                    tuple(result.needs_confirmation),
                    tuple(_line(c.draft, tz) for c in candidates if c.possible_duplicates),
                )
            )
            continue
        report.already_present += result.already_present
        report.imported += [_line(c.draft, tz) for c in candidates if not c.exact_duplicate]
    return report


def import_confirmed(conn: sqlite3.Connection, pending: Sequence[Pending]) -> int:
    """Import files the runner confirmed are different runs. Returns runs imported."""
    tz, count = db.athlete_timezone(conn), 0
    for item in pending:
        decoded = imports.decode_file(item.path, tz)
        count += imports.commit(conn, decoded, item.path.name, frozenset(item.keys)).imported
    return count


def week_window(conn: sqlite3.Connection, now: datetime) -> tuple[date, date]:
    """This training week so far: Monday through today, athlete-local."""
    today = now.astimezone(ZoneInfo(db.athlete_timezone(conn))).date()
    return today - timedelta(days=today.weekday()), today


def _line(draft: imports.Draft, tz: str) -> str:
    started = datetime.fromisoformat(draft.started_at.replace("Z", "+00:00"))
    local = started.astimezone(ZoneInfo(tz))
    miles = draft.distance_meters / MI_METERS
    return f"{local:%a %-m/%-d}: {miles:.2f} mi, {_clock(draft.duration_seconds)}"


def _clock(seconds: int) -> str:
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def report_text(conn: sqlite3.Connection, report: SyncReport) -> str:
    """The sync result for Slack: new runs, what's left, and the week so far from the DB."""
    lines = [f"Synced from COROS ({report.start} to {report.end}):"]
    lines += [f"• {line}" for line in report.imported]
    if not report.imported:
        done = " (all already imported)." if report.already_present else "."
        lines.append("• No new runs" + done)
    if report.not_downloaded:
        lines.append(f"{report.not_downloaded} run(s) not downloaded yet; try again later.")
    if report.error:
        lines.append(report.error)
    week = reports.summary(conn, week=report.end)["summary"]
    lines.append(
        f"*This week: {week['run_count']} runs, {week['distance_meters'] / MI_METERS:.2f} mi, "
        f"{_clock(week['duration_seconds'])}.*"
    )
    return "\n".join(lines)


def duplicate_question(pending: Sequence[Pending]) -> str:
    runs = "\n".join(f"• {line}" for item in pending for line in item.runs)
    return (
        f"These look like runs already imported (often an Apple Health copy):\n{runs}\n"
        "Reply *yes* to import them as separate runs, or *no* to skip them."
    )


# ------------------------------------------------------------------ MCP client + keychain


class KeychainStorage:
    """`mcp` TokenStorage in the macOS login keychain via `/usr/bin/security`. Values go over
    stdin (`security -i`), base64-encoded, so they never appear in a process list."""

    def __init__(self, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run):
        self._run = run

    def _get(self, account: str) -> str | None:
        found = self._run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
            capture_output=True,
            text=True,
        )
        if found.returncode != 0 or not found.stdout.strip():
            return None
        return base64.b64decode(found.stdout.strip()).decode()

    def _set(self, account: str, value: str) -> None:
        encoded = base64.b64encode(value.encode()).decode()
        command = f"add-generic-password -U -s {KEYCHAIN_SERVICE} -a {account} -w {encoded}\n"
        saved = self._run(["security", "-i"], input=command, capture_output=True, text=True)
        if saved.returncode != 0:
            raise CorosError("Couldn't save the COROS sign-in to the keychain.")

    async def get_tokens(self) -> Any:
        from mcp.shared.auth import OAuthToken

        raw = self._get("tokens")
        return None if raw is None else OAuthToken.model_validate_json(raw)

    async def set_tokens(self, tokens: Any) -> None:
        self._set("tokens", tokens.model_dump_json())

    async def get_client_info(self) -> Any:
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._get("client")
        return None if raw is None else OAuthClientInformationFull.model_validate_json(raw)

    async def set_client_info(self, client_info: Any) -> None:
        self._set("client", client_info.model_dump_json())


class McpCorosApi:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def _call(self, name: str, args: dict[str, Any]) -> Any:
        result = await self._client.call_tool(name, args)
        if result.is_error:
            raise CorosError(f"COROS said: {_text(result.content)[:200] or 'error'}")
        return result

    async def runs(self, start: date, end: date) -> list[CorosRun]:
        args = {
            "sportTypeCodes": list(RUN_SPORT_TYPES),
            "startDate": f"{start:%Y%m%d}",
            "endDate": f"{end:%Y%m%d}",
            "limit": 50,
        }
        return parse_runs(_text((await self._call("querySportRecords", args)).content))

    async def fit(self, run: CorosRun) -> bytes:
        args = {"labelId": run.label_id, "sportType": run.sport_type}
        return fit_bytes((await self._call("downloadActivityFitFiles", args)).content)


async def _no_browser(url: str) -> None:
    raise SignInNeeded


async def _no_callback() -> Any:
    raise SignInNeeded


async def _open_browser(url: str) -> None:
    print(f"Opening COROS sign-in in your browser. If it doesn't open, visit:\n{url}")
    webbrowser.open(url)


async def _await_callback() -> Any:
    from mcp.client.auth import AuthorizationCodeResult

    params = await asyncio.to_thread(_serve_one_callback)
    if "code" not in params:
        raise CorosError("COROS sign-in was cancelled or failed.")
    return AuthorizationCodeResult(
        code=params["code"], state=params.get("state"), iss=params.get("iss")
    )


def _serve_one_callback() -> dict[str, str]:
    """Answer one request on the loopback redirect URI and return its query parameters."""
    got: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server name)
            query = parse_qs(urlparse(self.path).query)
            got.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"COROS is connected to mara-trainer. You can close this tab.")

        def log_message(self, *args: Any) -> None:  # keep the code out of the terminal
            pass

    server = HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
    server.timeout = LOGIN_TIMEOUT  # handle_request gives up after this
    try:
        server.handle_request()
    finally:
        server.server_close()
    return got


@asynccontextmanager
async def connect(interactive: bool = False, storage: Any = None) -> AsyncIterator[CorosApi]:
    """An authenticated COROS session. Only `interactive` (the login command) opens a browser."""
    import httpx2
    from mcp import Client
    from mcp.client.auth import OAuthClientProvider
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.auth import OAuthClientMetadata
    from pydantic import AnyUrl

    auth = OAuthClientProvider(
        server_url=SERVER_URL,
        client_metadata=OAuthClientMetadata(
            client_name="mara-trainer",
            redirect_uris=[AnyUrl(REDIRECT_URI)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=storage or KeychainStorage(),
        redirect_handler=_open_browser if interactive else _no_browser,
        callback_handler=_await_callback if interactive else _no_callback,
    )
    async with (
        httpx2.AsyncClient(auth=auth, timeout=httpx2.Timeout(30, read=300)) as http,
        Client(streamable_http_client(SERVER_URL, http_client=http)) as client,
    ):
        yield McpCorosApi(client)


def _cause(exc: BaseException) -> CorosError | None:
    """The CorosError inside an exception (anyio wraps errors in exception groups)."""
    if isinstance(exc, CorosError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            if found := _cause(inner):
                return found
    return _cause(exc.__cause__) if exc.__cause__ else None


def sync_week(conn: sqlite3.Connection, now: datetime | None = None) -> SyncReport:
    """Blocking entry point for the CLI and the Slack bot."""
    start, end = week_window(conn, now or datetime.now(UTC))

    async def go() -> SyncReport:
        async with connect() as api:
            return await sync_runs(conn, api, start, end)

    try:
        return asyncio.run(go())
    except Exception as exc:
        found = _cause(exc)
        if found is None:
            raise CorosError("Couldn't reach COROS just now. Try again in a few minutes.") from exc
        raise found from exc


def login() -> None:
    """One-time interactive sign-in; makes one harmless call so tokens get stored."""

    async def go() -> None:
        async with connect(interactive=True) as api:
            today = datetime.now(UTC).date()
            await api.runs(today, today)

    try:
        asyncio.run(go())
    except Exception as exc:
        found = _cause(exc)
        raise (found or CorosError("COROS sign-in failed.")) from exc
