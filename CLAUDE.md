# CLAUDE.md — mara-trainer (AI marathon coach)

## What this is
An AI marathon coach, built first for one athlete (Sydney, spring marathon) and designed so it can
later open to other runners. It covers training plans fitted to the runner, gear suggestions,
fueling (runs, daily eating, race week), recovery and cross-training, and a chat coach (CLI, then
Slack, later SMS). Goal and roadmap: `docs/public-mvp-goal.md`.

Two packages:
- `src/mara_trainer/` — the deterministic core: SQLite storage, imports, summaries, plan guardrails.
  No LLM calls here, ever.
- `src/mara_coach/` — the coach: Claude API agent, source registry, profile, channels, eval set.
  Calls into `mara_trainer` for every number.

Claude Code is still the dev interface and Sydney's personal interface (skills + Strava MCP). The
repo is also a public portfolio piece: original code only (no employer code, no AGPL/GPL code),
no personal data, MIT licensed.

**Core rule: Claude proposes, code validates.** No plan is accepted until `coach validate-plan`
passes. Never hand-edit numbers to sneak past a guardrail; change the plan or tell the runner why
the guardrails can't be met (`coach plan brief` reports `feasibility_problems`). The coach agent
gets mileage, targets and summaries from `mara_trainer` tools, never from its own arithmetic.

**Sources rule: curate and cite, don't scrape.** Coaching knowledge comes from a vetted source
registry (credentialed coaches, position statements, peer-reviewed or open-access research,
published books cited by reference). Don't write scrapers for blogs, YouTube, podcasts, X or any
site whose terms forbid it, and don't store copyrighted text. Store links, metadata and short
notes; fetch at answer time only from approved domains, with citations. Open-access material may
be summarized. If no approved source covers a question, the coach says so.

## Stack
- Python 3.12, managed with `uv`. Package `src/mara_trainer/`, tests in `tests/` (pytest).
- CLI: `coach` (typer), entry point `mara_coach.cli:app`, which mounts the core app from
  `mara_trainer.cli` plus coach commands. The core never imports `mara_coach`. Every command prints JSON.
- SQLite at `data/coach.db` (gitignored; override with `COACH_DB_PATH`). Plain SQL migrations in
  `src/mara_trainer/migrations/NNN_*.sql`, forward-only, applied automatically.
- FIT: `garmin-fit-sdk` (official Garmin SDK). GPX: `gpxpy`. Apple Health: stream-parsed `export.xml`.
- Lint/format/types: `ruff check`, `ruff format`, `mypy --strict src/`.
- LLM: Claude API via the official `anthropic` SDK, only inside `src/mara_coach/`. Default model
  `claude-opus-5-5`, adaptive thinking, explicit effort, server-side refusal fallback on.
  `ANTHROPIC_API_KEY` lives in `.env` only. Tests never hit the real API (use a fake client).
- Channels: CLI (`coach ask`) first, Slack via `slack-bolt` Socket Mode next. SMS, a web app,
  multi-user auth, Postgres and hosting are post-MVP; ask Sydney before starting any of them.

## Commands
- `uv sync` — install deps
- `uv run pytest` — tests (must pass before every commit)
- `uv run ruff check . && uv run ruff format --check . && uv run mypy src/`
- `uv run coach init-db [--timezone America/Los_Angeles]` — create/upgrade the DB
- `uv run coach race set --name ... --date YYYY-MM-DD --days sun,tue,thu,sat --long-run-day sun --weekly-min 25 --weekly-max 30 --longest 10 [--unit mi] [--goal-time 3:59:00] [--experience returning|first_marathon] [--recent-race-distance 13.1 --recent-race-time 1:55:00]`
- `uv run coach race show`
- `uv run coach plan brief [--today YYYY-MM-DD]` — weekly targets, long-run bounds, phases, runs/week
- `uv run coach validate-plan plans/<file>.json [--accept]` — exit 0 pass, 3 violations (JSON list)
- `uv run coach plan show` — accepted plan
- `uv run coach import <file> [--dry-run] [--allow-duplicate KEY ...]` — .fit / .gpx / export.xml / export.zip; exit 2 = near-duplicates need confirmation, nothing written
- `uv run coach summary [--week current|last|YYYY-MM-DD] [--weeks N]`
- `uv run coach source import [sources/seed.json] [--update]` — add seed sources as `proposed`; `--update` refreshes metadata of existing ones (approval kept)
- `uv run coach source add --slug ... --title ... --author ... --kind ... --topics a,b --license ... --trust-reason ... [--url https://...]`
- `uv run coach source list [--status proposed|approved|retired] [--topic fueling] [--women-tested]`
- `uv run coach source approve SLUG ... --by NAME` / `coach source retire SLUG ...` — only Sydney approves
- `uv run coach source domains` — allowed web domains for the agent (approved sources only)
- Eval set: `evals/coach-questions.json`, loaded and checked by `mara_coach.evals` (tests enforce
  its shape). Add a case whenever the coach gets something wrong.

## Data sources and rules (read before touching integrations)
- **Strava — official MCP only, and never in `mara_coach`.** The coach agent and any public
  feature must not receive Strava data; other runners bring FIT/GPX/Apple Health files. Since the Strava API Policy of 2026-06-01, Strava data must not be
  fed into AI apps except through Strava's official MCP, must not be stored permanently (a transient
  cache of at most 7 days is the ceiling), and third-party MCP servers may not expose it.
  Use only `strava-mcp` (`https://mcp.strava.com/mcp`, registered in `.mcp.json`), which requires a
  paid Strava subscription on a personal account (the Strava + Runna bundle qualifies).
  - Use Strava MCP results **in-session only**, for Sydney's own analysis. Do NOT write them to
    `coach.db`, `data/`, `plans/`, docs, commits, PRs, issues, or any file.
  - Do NOT add stravalib, Strava OAuth, webhooks, or sync code. Do NOT install third-party Strava MCPs.
  - The DB schema has no `strava` source on purpose. Don't add one.
- **Runna has no public API.** Never scrape Runna or call private endpoints. Runna runs reach Strava
  (see via Strava MCP) and the watch (import the FIT). Runna plans: Sydney enters them by hand.
- **Garmin:** FIT files exported from the watch / Garmin Connect are the default path.
  `garmin_mcp` / `python-garminconnect` use unofficial endpoints: optional, personal use only,
  OFF by default (`GARMIN_MCP_ENABLED=false`), not in `.mcp.json`. Never commit tokens.
- **Apple Health:** a local export is fine (`coach import export.zip`).
- Every activity keeps `source` + `source_id` + `fingerprint`. Dedup (ported from the legacy design):
  exact fingerprint (UTC start, meters, seconds) = replay, skipped; near match (±60 s, ±max(100 m, 2%),
  ±max(60 s, 2%), any source) = candidate that Sydney must confirm. Never auto-delete or auto-merge.

## Domain conventions
- SI units in storage: meters, seconds, UTC ISO instants (`2026-03-01T15:00:00.000Z`).
  Athlete timezone is IANA (in the `athlete` table); daily/weekly rollups use athlete-local dates.
  Display miles to Sydney unless she asks otherwise (1 mi = 1609.344 m).
- Weekdays are integers **0=Sunday … 6=Saturday** everywhere (plans, race setup).
- Missing ≠ zero: null RPE/sleep/HRV/resting HR stays null, reported as coverage counts, never imputed.
- Plan guardrails (`src/mara_trainer/plan_rules.py`, ported from legacy `buildScheduleBrief` /
  `validateAiSchedule`): exact weekly running total (±100 m), exactly `runs_per_week` runs on
  available days, one long run on the chosen day within [target, max], ≤1 quality workout per week
  and none in base/taper, ≥1 easy/rest day between long run and workout, ≤1 strength day, runs ≥1 km,
  3-week taper, race week = race on race day (42195 m) and rest otherwise.
- No medical diagnoses, no invented paces without a recent race result. For pain, illness or health
  concerns, tell the runner to see a qualified professional.
- Nutrition: general, cited guidance only (fueling ranges come from sources, not memory). Medical
  diets, allergies beyond avoidance, pregnancy, or any sign of disordered eating (restriction,
  weight-loss pressure, guilt around food) means: be supportive, skip numbers, refer to a doctor
  or registered dietitian.
- Women-tested evidence: every source records `female_evidence` (female_specific, sex_reported,
  mixed_unreported, mostly_male, not_applicable, unknown). For female runners, prefer women-tested
  sources and say plainly when guidance rests mostly on studies of men. Don't rate a source higher
  than what was actually checked; use `unknown` when unsure.
- Gear: recommend from the runner's stated preferences plus cited reviews. Never invent reviews;
  disclose any affiliate link.

## Secrets and privacy
- Secrets only in `.env` (gitignored) or the OS keychain. Never read, print, log, or commit tokens,
  passwords or API keys. `.env.example` holds names only.
- `data/`, `exports/`, `plans/`, `*.fit`, `*.gpx`, `*.db`, `export.zip` are gitignored. Test fixtures
  are synthetic only (`tests/fit_builder.py` generates FIT bytes in memory).
- Never paste real activity/health data into commits, PR text, issues or docs.

## Working agreements for Claude
- Small, reviewable increments. Write or update tests first for any rules/dedup/units change.
- Run `uv run pytest` after every code change; keep `ruff` and `mypy --strict` clean.
- Before adding a dependency, check its license (MIT/BSD/Apache OK; AGPL/GPL not OK) and say why.
  Note: `garmin-fit-sdk` is under Garmin's FIT Protocol License (used as a dependency, not vendored).
- `yihong0618/running_page` (MIT) is fine as a reference; credit adapted code in `NOTICE.md`.
- Ask before anything that needs network auth or real credentials. Don't `git commit` or `git push`
  unless Sydney asks; she commits under her own identity.

## Layout
- `src/mara_trainer/` — `cli.py`, `db.py`, `dates.py`, `plan_rules.py`, `summaries.py`, `imports.py`, `migrations/`
- `src/mara_coach/` — `cli.py` (entry point), `evals.py` (eval-set loader), `sources.py` (source
  registry); agent, profile and Slack land here
- `sources/seed.json` — proposed seed sources (links + metadata + our own one-line notes, no copied text)
- `evals/coach-questions.json` — synthetic eval questions + rubrics (no real athlete data)
- `tests/` — pytest; `fit_builder.py` (synthetic FIT), `plan_factory.py` (valid-plan generator)
- `examples/sample-plan.json` — synthetic plan that passes `validate-plan`
- `.claude/skills/{plan-builder,weekly-review,import-workouts}/SKILL.md`
- `docs/public-mvp-goal.md` (goal + roadmap), `docs/strava-requirements.md`,
  `docs/legacy-notes.md` (what was ported; plan-drafting reference)
- `plans/` — Sydney's personal plan JSON (gitignored)
