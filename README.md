# mara-trainer

An AI marathon coach: training plans fitted to the runner, gear suggestions, fueling and race-week
food, recovery and cross-training, and a coach you can chat with. Built first for one runner and
designed to open up later (roadmap: `docs/public-mvp-goal.md`).

A small local Python core stores workouts in SQLite, computes timezone-aware summaries, and enforces
training-plan guardrails. Claude drafts plans and answers; **code validates every plan** before it's
accepted, and advice is grounded in a vetted, cited source list rather than scraped content.

- Data in: watch **FIT** files (default), GPX, Apple Health export, and the **official COROS MCP**
  in Claude Code (runs are imported as FIT files).
- Data stays local in `data/coach.db` (gitignored). API keys live only in `.env`.

## Quick start (macOS)

```bash
brew install uv              # Python toolchain manager (fetches Python 3.12 automatically)
cd ~/Desktop/mara-trainer
uv sync                      # create .venv and install dependencies
uv run pytest                # all tests should pass
uv run coach init-db         # creates data/coach.db (timezone defaults to America/Los_Angeles)
claude                       # start Claude Code in this folder
```

The first time you run `claude` here, accept the workspace-trust prompt and approve the project
MCP server `coros` from `.mcp.json`.

### Authorize the COROS MCP

1. In Claude Code, run `/mcp`, select **coros**, and choose **Authenticate**. Finish the COROS
   login in your browser.
2. Run `/mcp` again to check it shows as connected.

Only COROS's official MCP is used (no unofficial COROS APIs). Runs reach the database as FIT
files through `coach import`. Strava isn't used: its API policy (effective 2026-06-01) bars storing
its data or feeding it to AI apps. Runna has no public API; Runna runs reach your COROS watch.

## Good first prompts

1. "Set up my goal race: <race name> on <date>. I can run Sun, Tue, Thu and Sat, long runs on Sunday,
   I've been doing 25-30 miles a week, longest run 10 miles, goal time 3:59."
2. "Build me a training plan for my spring marathon." (uses the **plan-builder** skill)
3. "Import the FIT files in ~/Desktop/mara-trainer/exports/ and flag anything that looks like a duplicate."
4. "Import my Apple Health export at exports/export.zip."
5. "Do my weekly review: how did this week go against the plan? Check COROS for anything I didn't import."
6. "I'm traveling next Thursday through Sunday. Rework next week so it still passes validation."
7. "Show my last 6 weeks of mileage and long runs in miles."
8. "Explain the guardrails in `coach plan brief` for my race in plain English."

## CLI

```bash
uv run coach --help
uv run coach race set --name "Sample Marathon" --date 2027-04-25 --days sun,tue,thu,sat \
  --long-run-day sun --weekly-min 25 --weekly-max 30 --longest 10 --unit mi
uv run coach plan brief                       # weekly guardrails for the saved race
uv run coach validate-plan examples/sample-plan.json
uv run coach import exports/run.fit --dry-run # preview + duplicate check
uv run coach import exports/run.fit
uv run coach summary --week current --weeks 4
```

All commands print JSON. Weekdays are `0=Sunday … 6=Saturday`; distances are stored in meters.

## Project layout

```
src/mara_trainer/   deterministic core: cli.py, db.py, plan_rules.py, summaries.py, imports.py, dates.py
src/mara_coach/     the coach: eval-set loader now; Claude agent, sources, profile, Slack next
evals/              coach-questions.json (synthetic questions + grading rubrics)
tests/              pytest suite (synthetic FIT generated in memory; no real data)
examples/           sample-plan.json (synthetic, passes validation)
.claude/skills/     plan-builder, weekly-review, import-workouts
.mcp.json           official COROS MCP (http, https://mcpus.coros.com/mcp)
docs/               public-mvp-goal.md, strava-requirements.md, legacy-notes.md
```

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy src/
```

See `CLAUDE.md` for the data-source rules and working agreements.

## License

MIT © 2026 Sydney Thomas. The `garmin-fit-sdk` dependency is distributed by Garmin under its own
FIT Protocol License. It's installed as a dependency, not vendored.
