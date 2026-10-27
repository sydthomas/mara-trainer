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

## Evals
The eval set (`evals/coach-questions.json`) is the coach's test suite. Its shape is checked by
pytest on every PR; answers are checked by a live run, which costs money, so it stays out of CI.
- **When:** after any change to the system prompt, tools, knowledge sections or sources, and
  before a release. Ask Sydney before a full run; `--only`/`--category` keep it cheap.
- **Run:** `uv run coach eval live`. Read `changes.regressions` first: a case that passed before
  and fails now is the signal. Paste the summary (totals, regressions, usage) into the PR, never
  the answers.
- **Grow it:** every wrong or unsafe answer the coach gives (in Slack, `coach ask`, an eval) becomes
  a new case with `checks`, in the PR that fixes it. Re-run to show it passing.

## Changelog and releases
- Every PR that changes behavior adds a line under `## [Unreleased]` in `CHANGELOG.md` (Added /
  Changed / Fixed / Removed), with the PR number.
- Release when a set of changes is worth naming (for example, after a batch merge):
  1. Pick the version (SemVer; before 1.0, minor = features, patch = fixes).
  2. Move `Unreleased` items under `## [x.y.z] - YYYY-MM-DD`; bump `version` in `pyproject.toml`
     (then `uv lock`); update the compare links at the bottom.
  3. Merge that PR, then tag the merge commit and publish:
     `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z`
     `gh release create vX.Y.Z --title vX.Y.Z --notes "<that version's section>"`
- Tags are permanent: never move or delete a published tag; release a new patch instead.

## Review rhythm
Batch: at the end of a work session, Sydney reviews and merges everything green in one pass,
instead of one PR at a time. `gh pr list` shows the queue; `gh pr checks N` shows CI.
Approvals (sources, knowledge sections) batch the same way: `coach knowledge list` shows drafts,
`coach knowledge check` must pass, then one `coach knowledge approve A B C --by Sydney`.

## Done means
Docs move with the code in the same PR: CLAUDE.md commands and layout, a roadmap entry in
`docs/public-mvp-goal.md` for milestones, a new eval case whenever the coach got something
wrong, and a row in `docs/multi-user.md` if the change assumes a single user.
