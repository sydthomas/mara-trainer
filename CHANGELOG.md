# Changelog

All notable changes to this project. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [SemVer](https://semver.org/) (before 1.0: minor = features, patch = fixes).

## [Unreleased]

### Added
- `coach eval live`: run the eval set against the live coach, save the report to
  `data/eval-runs/`, and show regressions and fixes vs the previous run. Eval routine in
  CONTRIBUTING.md.
- Usage and cost logging: one `api_usage` row per coach answer (channel, model, tokens, cost; no
  text), `coach usage` report with a 30-day projection, `coach eval live --model` to test a
  cheaper tier. Decision record in `docs/model-tiers.md`.

## [0.1.0] - 2026-10-26

First tagged version: a working single-athlete coach.

### Added
- Deterministic core (`mara_trainer`): SQLite storage, FIT/GPX/Apple Health import with
  fingerprint dedup, timezone-aware summaries, plan guardrails and `coach validate-plan`.
- `coach pace`: Riegel race-time predictions from a recent race (#4).
- Runner profile, read-only coach tool layer, `ask-coach` skill (#5).
- Offline eval runner with rubric checks (#6).
- Fueling log (`coach fuel`), debrief skill, effort (RPE) per run (#9).
- `coach ask`: the coach on the Claude API, web fetch limited to approved source domains (#10).
- Knowledge base: 14 cited topic sections with checker and approval (#11).
- Slack bot: DM chat (#13), long-run debrief check-in (#14), Sunday weekly check-in (#15).
- COROS official MCP as the watch data source; `coros-run-planner` skill and `coach coros week`
  to put the coming plan week on the COROS calendar (#16).
- CI on every PR, pre-commit hooks, protected `main` (#16); PR template and CONTRIBUTING with a
  risk-based merge policy (#17).

### Changed
- Plan rules audited against the approved sources, with each rule mapped in
  `docs/plan-rules-sources.md` (#12).
- 13.1 mi entries snap to an exact half marathon; `validate_plan` limited to `plans/` and
  `examples/` (#8).

### Removed
- Strava integration: its 2026-06-01 API policy bars storing data or feeding it to AI apps (#16).

[Unreleased]: https://github.com/sydthomas/mara-trainer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sydthomas/mara-trainer/releases/tag/v0.1.0
