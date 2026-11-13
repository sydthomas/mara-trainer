# Multi-user assumptions

Status 2026-10-26: built for one runner on one laptop, on purpose. This note lists every place
that assumes one user and what changes for user #2. Nothing here is built yet. Multi-user auth,
Postgres and hosting are post-MVP and need Sydney's go-ahead (CLAUDE.md).

## What assumes one user

| Assumption | Where | For user #2 |
|---|---|---|
| One athlete row | `athlete`, `race_setup`, `athlete_profile`: `id INTEGER PRIMARY KEY CHECK (id = 1)` | Drop the check; `athlete.id` becomes the tenant key |
| Per-athlete rows with no owner column | `activities`, `daily_check_ins`, `import_batches`, `training_plans`, `fuel_log`, `niggle_log`, `notifications`, `api_usage` | Add `athlete_id` (FK, NOT NULL, indexed) to each; every query filters by it |
| Single-file SQLite | `data/coach.db` (`COACH_DB_PATH`) | Postgres once hosted: concurrent writers, backups, row-level security. Migrations use SQLite SQL (`strftime`, `json_valid`, `GLOB`) and need a port |
| Name and story in the prompt | `agent.py` system prompt: "You are Sydney's marathon coach... her first marathon" | Build from the athlete's profile (name, experience, pronouns) per request |
| One Slack user | `SLACK_ALLOWED_USER_ID`; one bot process | A `slack_user_id -> athlete_id` table; unknown users get an invite flow, not the coach |
| One DM channel and message cursor | `slack_state` (`dm_channel`, `last_handled_ts`, `inflight_reply`, `dm_turns`), `scheduled_messages` | Key both by athlete (and Slack user) |
| Schedule constants | `slack_bot.py`: 10am debrief, 6pm weekly (syncs COROS first) ("Sydney's choice") | Per-athlete settings in the DB |
| COROS through Claude Code | `.mcp.json`, one OAuth login on her laptop; `coros-sync` and `coros-run-planner` are skills | Per-user OAuth with tokens stored server-side; sync and push become server jobs |
| One COROS sign-in for Slack sync | `coros.py`: one keychain service (`mara-trainer-coros`) on the laptop, fixed loopback port; `exports/coros/` shared | Tokens per athlete in a secrets store; downloads keyed by athlete |
| One COROS sign-in for Slack sync | `coros.py`: one keychain service (`mara-trainer-coros`) on the laptop, fixed loopback port; `exports/coros/` shared | Tokens per athlete in a secrets store; downloads keyed by athlete |
| One dashboard file | `dashboard.py`: reads the single athlete row (timezone, units), writes `data/dashboard.html` | Build per athlete behind sign-in; never a shared file |
| One folder of FIT files, one timezone | `hr_pace.py` / `coach hr-pace`: reads every `.fit` in `exports/coros/` as one runner's, weeks from the single `athlete` row's timezone | Pass the athlete's own files and timezone (the function already takes both as arguments) |
| One FIT folder for the Slack line and the dashboard | `slack_bot.HR_PACE_DIR` (= `coros.EXPORTS`) for the Sunday HR-at-pace line; `coach dashboard --fit-dir` defaults to `exports/coros` | Per-athlete file storage; pass that athlete's folder |
| One home location | `COACH_LAT` / `COACH_LON` in `.env` (`weather.py`) | Per-athlete location in the DB (or per request), still rounded before it leaves the server |
| One NWS contact | `COACH_NWS_CONTACT` in `.env` (`weather.py`): the runner's own contact in the User-Agent to `api.weather.gov` | The service's own operator contact from server config; never a runner's |
| Local files | `exports/`, `plans/`, `data/eval-runs/` | Object storage keyed by athlete, or rows in the DB |
| Approvals by name | `sources.approved_by`, knowledge approvals, "only Sydney approves" | A coach/admin role. Sources and knowledge stay shared; runners never approve them |
| Cost by channel only | `api_usage` | Add `athlete_id` for cost per user and per-user limits |
| One API key | `ANTHROPIC_API_KEY` in `.env` | A secrets manager; never per-user keys in the DB |

## Shared vs per-athlete
- **Shared** (one copy for everyone): `sources`, `knowledge/`, eval set, plan rules, watch-target
  mapping. Changes go through the coach/admin review.
- **Per-athlete** (every row owned): everything in the second row of the table above, plus the
  profile, race setup and plan.

## Before user #2 (not code)
- Consent and deletion: runners can export and delete their data. Cycle, HRV and sleep data are
  health data; get advice on the privacy laws that apply (some US states regulate consumer health
  data) before collecting them from anyone but Sydney.
- Onboarding: `coach race set` + `profile set` as a guided first conversation.

## Rules for new code now (cheap, keeps the move mechanical)
1. Don't add a new single-user assumption without adding a row above in the same PR.
2. New per-athlete tables: plain `INTEGER PRIMARY KEY`, no `CHECK (id = 1)`.
3. Per-athlete settings go in data, not constants.
4. No athlete name or personal detail in new prompts or code; read it from the profile.
