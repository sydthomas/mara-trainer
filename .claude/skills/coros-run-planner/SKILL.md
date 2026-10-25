---
name: coros-run-planner
description: Put Sydney's coming week of runs on her COROS watch. Use when she says "push my week", "put this week on my watch/COROS", "schedule my workouts", or after the Sunday weekly check-in. Builds the week's COROS workouts from the accepted plan with `coach coros week`, reads them back, and only after one yes creates them on her COROS calendar with the official COROS MCP.
allowed-tools: Bash(uv run coach coros week *) Bash(uv run coach coros week) Bash(uv run coach plan show) Bash(uv run coach race show)
---

# COROS run planner

Sunday evening, after the weekly check-in previews the coming week, this puts that week's runs on
her COROS calendar so they sync to the watch. Code builds every workout; you read it back, get one
yes, and send it. You never write distances, paces or sections yourself.

## Rules
- Writes change her COROS account. Send nothing without her yes for this week, every week.
- Only sessions from the accepted plan (it passed `validate-plan`). If she wants a change, that
  goes through the plan-builder skill first, then run this again.
- Send each course exactly as `coach coros week` printed it. Don't add a pace or heart-rate target
  to easy and long runs: Sydney asked for those to run by effort, with no alert (the sources
  define easy by effort). Don't edit numbers, names or descriptions.
- The MCP can't move or delete scheduled workouts, so never create a workout twice. Changes to
  something already there go through `queryScheduledWorkoutDetails` then `updateScheduledWorkout`,
  and only for workouts this skill created.
- Never show workout IDs or idInPlan to her.

## 1. Build the week
- `uv run coach coros week` (default: the week containing tomorrow, so on Sunday it's the coming
  Monday-Sunday). Use `--date YYYY-MM-DD` for another week.
- Exit 1 means no race set or the week isn't in the accepted plan: tell her and stop.
- COROS MCP tools are `mcp__coros__*` (or `mcp__claude_ai_COROS__*`, the same server). If neither
  is connected, tell her to run `/mcp` and stop.

## 2. Check her COROS calendar
- `queryTrainingSchedule` with `startDate`/`endDate` = the week's first and last dates (yyyyMMdd).
- For each workout to send, look at what's already on that date:
  - Nothing there: send it.
  - A workout from an earlier run of this skill with the same name: skip it (already sent). If its
    distance or targets differ from what code built now, offer to update it in place.
  - Anything else (a Runna or COROS training-plan session, a workout she made): don't send
    over it blindly. List it and ask whether to add ours alongside or skip that day.

## 3. Read it back (one message)
One compact table: day, date, workout name, miles, watch target ("by feel" or the pace range per
mile from the description). Under it: skipped sessions with their reason (strength days: "add in
the COROS app"), and anything already on her calendar from step 2. End with one question:
"Put these N runs on your COROS calendar?"

## 4. Send after yes
- For each workout: `createScheduledWorkout` with `date` = `coros_date` and `course` = the printed
  `course` object, unchanged. One call per workout.
- If a call fails, say which day and why, and continue with the rest. Don't retry the same call in
  a loop.
- Report back: which runs are on her calendar, which were skipped, and that they'll sync to the
  watch next time it connects to the COROS app. Tell her about any other workouts the results list
  on those days.

A "no" or a change request: send nothing. Changes to the plan go through plan-builder.
