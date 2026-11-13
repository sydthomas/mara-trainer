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
Gate on risk, not on who wrote it. Sydney agrees the plan; agents build, review, merge and deploy
it. Work outside an agreed plan waits for her.

**An agent may merge its own PR** when all of these hold:
- The work is part of a plan Sydney agreed to (in the session, or written down).
- CI is green.
- A second agent with fresh context reviewed the diff (given the code, not the author's
  conclusion) and its findings are fixed or answered. The PR says what it found.
- No new dependency.
- If a guarded path is touched, its extra check below is done and noted in the PR.

**Guarded paths: an extra check before an agent merges.**

| Path | Why | Extra check |
|---|---|---|
| `src/mara_trainer/plan_rules.py`, `docs/plan-rules-sources.md` | Coaching rules must match approved sources | Every changed rule cites an approved source in the doc |
| `src/mara_trainer/coros_workouts.py` | Watch targets on her real account | Tests only; nothing is sent to the watch without her yes |
| `knowledge/`, `sources/` | She approves every source and section | Agents merge proposals and drafts only; approving stays hers |
| `src/mara_coach/agent.py` (system prompt), `src/mara_coach/tools.py` | Coach behavior and safety | Targeted eval run (below), no regressions, summary in the PR |
| `src/mara_trainer/migrations/` | Forward-only schema; mistakes are permanent | Applied to a copy of the real DB first; DB backed up before deploy |

**Still Sydney's, plan or no plan:**
- Approving sources and knowledge sections (`coach source approve`, `coach knowledge approve`).
- Anything that writes to her COROS account.
- Secrets, new paid services, new dependencies (`pyproject.toml`, `uv.lock`).
- The checks and permissions themselves: `.github/`, `.pre-commit-config.yaml`,
  `.claude/settings.json`.
- `CLAUDE.md` rules sections (data sources, privacy, domain conventions).

**Stop and report instead of pushing on** when: CI is still red after two fix attempts, an eval
regresses on a safety case, a change needs something from the list above, or eval spend passes
the cap she set for the plan.

## Deploy
After merges that change the bot: back up `data/coach.db` to `data/backups/`, restart the
LaunchAgent (`launchctl kickstart -k gui/$(id -u)/com.maratrainer.slack`), and check the outcome
(process running, startup line in the log, scheduled messages queued), not just that it started.

## Evals
The eval set (`evals/coach-questions.json`) is the coach's test suite. Its shape is checked by
pytest on every PR; answers are checked by a live run, which costs money, so it stays out of CI.
- **When:** after any change to the system prompt, tools, knowledge sections or sources, and
  before a release. Ask Sydney before a full run. Inside an agreed plan, targeted runs
  (`--only`/`--category`, the cases the change can affect plus `safety_referral`) are fine up to
  the cap she set; batch changes into one run where possible.
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
- An agent tags and publishes only when Sydney said so for that release or that plan.

## Review rhythm
Agents merge plan work as it passes the gates above. Anything left open (off-plan work, a stop
condition) waits for Sydney's batch pass: `gh pr list` shows the queue; `gh pr checks N` shows CI.
Approvals (sources, knowledge sections) batch the same way: `coach knowledge list` shows drafts,
`coach knowledge check` must pass, then one `coach knowledge approve A B C --by Sydney`.

## Done means
Docs move with the code in the same PR: CLAUDE.md commands and layout, a roadmap entry in
`docs/public-mvp-goal.md` for milestones, a new eval case whenever the coach got something
wrong, and a row in `docs/multi-user.md` if the change assumes a single user.
