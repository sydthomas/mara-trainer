# Contributing

How work moves from idea to `main`. CLAUDE.md has the project rules; this file is the process.

## Setup
`uv sync && uv run pre-commit install`. Hooks run ruff and hygiene checks before each commit; CI
(`.github/workflows/ci.yml`) runs ruff, mypy and pytest on every PR. `main` is protected: the
`check` job must pass before anything merges, admins included.

## Branches
`type/short-topic`, lowercase, hyphens: `feature/coros-run-planner`, `fix/slack-timezone`.

| Type | For |
|---|---|
| `feature/` | new behavior |
| `fix/` | a bug |
| `docs/` | docs only |
| `chore/` | tooling, CI, dependencies, process |

One topic per branch, small enough to review in one sitting. Branch from `main`; avoid stacking
PRs on unmerged branches unless they truly depend on each other.

## Pull requests
The template asks the same questions every time: what and why, risk, test plan, done. Merge with
a merge commit (keeps each PR's commits visible).

## Who merges
Gate on risk, not on who wrote it.

**An agent may merge its own PR** when all of these hold:
- CI is green.
- No guarded path is touched (list below).
- No new dependency.
- Sydney asked for the work in this session.

**Guarded paths: Sydney reviews and merges.**

| Path | Why |
|---|---|
| `src/mara_trainer/plan_rules.py`, `docs/plan-rules-sources.md` | Coaching rules must match approved sources |
| `src/mara_trainer/coros_workouts.py` | Watch targets on her real account |
| `knowledge/`, `sources/` | She approves every source and section |
| `src/mara_coach/agent.py` (system prompt), `src/mara_coach/tools.py` | Coach behavior and safety |
| `src/mara_trainer/migrations/` | Forward-only schema; mistakes are permanent |
| `.github/`, `.pre-commit-config.yaml`, `.claude/settings.json` | The checks and permissions themselves |
| `CLAUDE.md` rules sections (data sources, privacy, domain conventions) | The rules agents follow |
| `pyproject.toml` dependencies, `uv.lock` | License and supply-chain review |

## Review rhythm
Batch: at the end of a work session, Sydney reviews and merges everything green in one pass,
instead of one PR at a time.

## Done means
Docs move with the code in the same PR: CLAUDE.md commands and layout, a roadmap entry in
`docs/public-mvp-goal.md` for milestones, and a new eval case whenever the coach got something
wrong.
