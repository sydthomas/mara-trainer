# Multi-user assumptions

Status 2026-10-26: built for one runner on one laptop, on purpose. This note lists every place
that assumes one user and what changes for user #2. Nothing here is built yet. Multi-user auth,
Postgres and hosting are post-MVP and need Sydney's go-ahead (CLAUDE.md).

## What assumes one user

| Assumption | Where | For user #2 |
|---|---|---|
| One athlete row | `athlete`, `race_setup`, `athlete_profile`: `id INTEGER PRIMARY KEY CHECK (id = 1)` | Drop the check; `athlete.id` becomes the tenant key |
| Per-athlete rows with no owner column | `activities`, `daily_check_ins`, `import_batches`, `training_plans`, `fuel_log`, `notifications`, `api_usage` | Add `athlete_id` (FK, NOT NULL, indexed) to each; every query filters by it |
| Single-file SQLite | `data/coach.db` (`COACH_DB_PATH`) | Postgres once hosted: concurrent writers, backups, row-level security. Migrations use SQLite SQL (`strftime`, `json_valid`, `GLOB`) and need a port |
| Name and story in the prompt | `agent.py` system prompt: "You are Sydney's marathon coach... her first marathon" | Build from the athlete's profile (name, experience, pronouns) per request |
| One Slack user | `SLACK_ALLOWED_USER_ID`; one bot process | A `slack_user_id -> athlete_id` table; unknown users get an invite flow, not the coach |
| Schedule constants | `slack_bot.py`: 10am debrief, 5pm sync nudge, 6pm weekly ("Sydney's choice") | Per-athlete settings in the DB |
| COROS through Claude Code | `.mcp.json`, one OAuth login on her laptop; `coros-sync` and `coros-run-planner` are skills | Per-user OAuth with tokens stored server-side; sync and push become server jobs |
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
