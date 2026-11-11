# Changelog

All notable changes to this project. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [SemVer](https://semver.org/) (before 1.0: minor = features, patch = fixes).

## [Unreleased]

### Changed
- Slack: Sunday is one conversation. The 6pm check-in syncs the week's COROS runs first (the 5pm
  reminder and the "push in Claude Code" nudge are gone). It asks how the week felt, and for the
  long-run debrief if Saturday's was missed; her reply through Monday logs that debrief (yes),
  then always gets a proposed week to say yes to.

### Added
- Slack: "sync my runs" pulls this week's COROS runs into the database in plain code (no AI, no
  API cost), asks before importing possible duplicates, and replies with the week's total. Also
  `coach coros login` (one-time sign-in, saved in the macOS keychain) and `coach coros sync`.
- Coach: writes the coming week after the Sunday check-in, with exact lifts, through a new
  `propose_week` tool. Guardrails check it, and the Slack bot saves it only on a yes; the previous
  plan stays archived.
- Knowledge: training notes now cover Venice Run Club's 2027 Level I and II LA Marathon plans.
- Knowledge: new `coaching-practice` section with evidence labels (Supported, Coaching experience),
  and 17 new sources covering coaching, injury, stretching, foam rolling and shoe rotation.
- Coach: adapts the current week any day. For illness or a warning sign it can shorten or rest
  sessions, never add. Saved weeks post a plain-text week card to copy into notes.

### Changed
- `coach eval live` runs as a fictional runner in a throwaway database, so eval answers no longer
  mix in the real runner's profile and plan. Eval costs are in its report, not `coach usage`.

### Fixed
- Slack: answers arrive as new messages, so your phone notifies you (they used to edit the
  "On it..." message, and Slack doesn't notify on edits).
- Slack: a reply cut off by a bot restart is finished when the bot starts again (it used to
  stay stuck on "On it..."). The conversation and a waiting "Save it?" question survive
  restarts too, so a yes after a restart still saves.
- Slack: no 10am long-run check-in once that day's run is in the fuel log (it asked again
  after an early debrief).
- Coach: no longer says Strava data lives in Claude Code; offers COROS, FIT, GPX or Apple Health.
- Coach: says a reworked week hasn't passed validation, and names low energy availability
  (RED-S) for bone stress injuries, low bone density and missed periods.
- Coach: `show_plan` gives each session its date and weekday from code, and the accepted plan
  always reads as accepted. The coach had shifted a run by a day and called the plan a draft.
- Slack bot: exit after 5 minutes of repeating socket errors so launchd restarts it, instead of
  looping on broken sessions while it looks healthy.

## [0.2.0] - 2026-10-26

Evals, cost tracking, COROS sync, and a Slack coach that keeps working while the laptop sleeps.

### Added
- `coach eval live`: run the eval set against the live coach, save the report to
  `data/eval-runs/`, and show regressions and fixes vs the previous run; eval routine in
  CONTRIBUTING.md (#19).
- Usage and cost logging: one `api_usage` row per coach answer (channel, model, tokens, cost; no
  text), `coach usage` with a 30-day projection, `coach eval live --model` to test a cheaper
  tier; decision record in `docs/model-tiers.md` (#20).
- `coros-sync` skill: pull the current training week's (Monday-Sunday) COROS runs as FIT files and
  import them, idempotently; Slack nudges at 5pm Sunday when planned runs are missing (#21).
- `deploy/macos/install.sh`: run the Slack bot as a LaunchAgent with auto-restart and keep-awake
  (hosting Option 0) (#25).
- Slack works while the laptop sleeps: fixed-time messages are queued on Slack's servers a week
  ahead (live versions replace them when awake), missed DMs are answered on wake, never twice;
  debrief context comes from the database, so it survives restarts (#26).
- Decision docs: `docs/multi-user.md` (single-user assumptions) and `docs/hosting.md` (options,
  costs, decision due 2026-11-15) (#22, #23).

### Changed
- All 14 knowledge sections approved, so the coach can use every topic (#24).
- The repo lives in `~/code/mara-trainer`: macOS privacy protection blocks background jobs from
  reading `~/Desktop` (#25).

### Fixed
- `coros-sync` downloads through `downloadActivityFitFiles`; URL + curl is blocked by permission
  settings and the date-range download errors on COROS's side. Found in the first live sync (#25).

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

[Unreleased]: https://github.com/sydthomas/mara-trainer/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/sydthomas/mara-trainer/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/sydthomas/mara-trainer/releases/tag/v0.1.0
