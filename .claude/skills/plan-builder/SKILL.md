---
name: plan-builder
description: Build or revise Sydney's marathon training plan. Use when she asks for a training plan, wants to set or change her goal race, available days, or baseline, or wants to rework upcoming weeks. Drafts week-by-week sessions and only accepts a plan that passes `coach validate-plan`.
allowed-tools: Bash(uv run coach race show) Bash(uv run coach plan brief *) Bash(uv run coach plan brief) Bash(uv run coach validate-plan *) Bash(uv run coach summary *) Bash(uv run coach summary)
---

# Plan builder — Claude proposes, code validates

Follow these steps in order. Never accept a plan that fails validation, and never fudge numbers to
pass. Fix the plan, or explain which guardrail can't be met.

## 1. Intake (ask only for what's missing)
Run `uv run coach race show`. If there's no race set, or Sydney wants changes, collect:
- Race name and date (spring marathon), and goal: finish, or a goal time (H:MM:SS)
- Days she can run, and her long-run day
- Baseline: recent weekly distance range, longest recent run, first marathon or returning
- A recent race result (distance + time) if she has one. Without it, don't prescribe paces.
- Constraints such as injuries, travel, or Runna sessions she wants kept (notes)

Optional context: `uv run coach summary --weeks 6` (imported FIT/GPX/Health data). With her OK,
you can also check the Strava MCP in-session for recent volume. Use it only to sanity-check the
baseline she states, and never write Strava data to any file.

Save the inputs with `uv run coach race set ...` (see CLAUDE.md for flags). Confirm the values with her first.

## 2. Get guardrails
`uv run coach plan brief` returns `brief_date`, `runs_per_week`, `training_level`, and per week
`phase`, `target_distance_meters`, `long_run_target_meters`, and `long_run_maximum_meters`.
If `feasibility_problems` isn't empty, stop. Explain the conflict, for example that her longest run
is too long for her weekly volume, and ask how she wants to adjust her inputs.

## 3. Draft (claude-running-coach pattern)
Draft week by week through phases: base, build, marathon-specific, a 3-week taper, then race week.
Read `docs/legacy-notes.md` § "Plan-drafting reference" for the full principles. In short:
- Each week has exactly 7 sessions (`day` 0=Sun…6=Sat, each day once). `kind` is one of easy,
  workout, long, race, strength, or rest. `distance_meters` is an integer. `details` is 1-500
  characters: purpose and effort, under 300 characters.
- Schedule exactly `runs_per_week` runs on her available days, and one long run on her long-run day
  within [target, max]. Run distances must add up to the weekly target (±100 m), and every run must
  be at least 1000 m.
- Allow at most 1 quality workout a week, with at least one easy or rest day between it and the
  long run. For a first marathon keep it small or skip it early on (sources differ: Higdon has
  none, Chicago has one every week). Up to 2 strength days (0 m), 1 in taper and race week.
- Race week: the race on race day plus up to `runs_per_week - 1` short easy runs (optionally
  strides or a short marathon-pace piece), no long run, at most a quarter of the peak week.
- Why each rule exists: `docs/plan-rules-sources.md`; the evidence: `knowledge/training.md`,
  `race-week.md`, `strength.md`.
- Mostly easy, conversational running. Pick plausible, unequal supporting runs. The long run is
  usually 25-40% of the week.

Write the draft to `plans/draft-YYYY-MM-DD.json` (gitignored) in this shape:
`{"brief_date": "<from brief>", "weeks": [{"week": 1, "sessions": [...]}, ...]}`.
`examples/sample-plan.json` shows the exact format.

## 4. Validate (mandatory)
Run `uv run coach validate-plan plans/draft-YYYY-MM-DD.json`. Exit 3 prints a JSON list of
violations, each with a week number. Fix every violation and re-run until it exits 0. Don't
show Sydney a plan as final before it passes.

## 5. Review and accept
Show Sydney a compact weekly table in miles: week, phase, total, long run, and key session. Point
out the cutback weeks and the taper. Only after she explicitly approves, run
`uv run coach validate-plan plans/<file>.json --accept` to store it as the accepted plan.

Health: no diagnoses. For pain that persists or gets worse, or any health concern, tell her to see a professional.
