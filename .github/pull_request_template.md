## What and why
<!-- One or two sentences: what changes and the problem it solves. -->

## Risk
- [ ] Touches a **guarded path** (see CONTRIBUTING.md): needs Sydney's review, no agent merge
- [ ] Adds a dependency (license checked: MIT/BSD/Apache only)
- [ ] Changes coach behavior (prompt, tools, knowledge): `coach eval live` summary pasted below (totals, regressions, usage; no answers)

## Test plan
- [ ] `uv run pytest`, ruff, mypy pass locally (CI re-checks)
- [ ] Rules/dedup/units change: tests written first

## Done means
- [ ] CLAUDE.md commands/layout updated if they changed
- [ ] `CHANGELOG.md` line under Unreleased if behavior changed
- [ ] `docs/public-mvp-goal.md` progress entry if it's a milestone
- [ ] No personal data in code, tests, docs or this description (synthetic fixtures only)
