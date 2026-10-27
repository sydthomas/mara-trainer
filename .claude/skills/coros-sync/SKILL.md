---
name: coros-sync
description: Pull Sydney's recent runs from COROS into the local database. Use when she says "sync my runs", "import from COROS", "get my runs in", before the Sunday check-in, or when a review or recap is missing runs. Finds this training week's runs (Monday through today) with the official COROS MCP, downloads each FIT once into exports/coros/, and imports them with the import-workouts flow (dedup and duplicate confirmation).
allowed-tools: Bash(uv run coach import *) Bash(uv run coach summary *) Bash(uv run coach summary) Bash(ls exports/coros*) Bash(mkdir -p exports/coros)
---

# COROS sync

Gets her watch runs into `coach.db` so summaries, the Sunday recap and the plan comparison have
real data. Every step is safe to repeat: re-running the sync downloads nothing new and imports
nothing twice.

## Rules
- Runs reach the database only as FIT files through `coach import` (CLAUDE.md). Never write MCP
  results (records, laps, health data) to a file or the DB yourself.
- COROS limits FIT downloads per day: download only files that aren't already in `exports/coros/`.
- `exports/` is gitignored. Never move these files anywhere committed.
- COROS tools are `mcp__coros__*` (or `mcp__claude_ai_COROS__*`, the same server). If neither is
  connected, tell her to run `/mcp` and stop.

## 1. Find runs
`querySportRecords` with `sportTypeCodes: [100, 101, 102, 103]` (outdoor, indoor, trail, track),
the current training week: `startDate` = this week's Monday, `endDate` = today (yyyyMMdd, her
timezone). Training weeks run Monday to Sunday, the same weeks as the plan, so on Sunday this is
the whole week. If she asks for last week (for example, syncing on a Monday), use last Monday
through last Sunday instead. A fixed window means no state to keep; dedup handles any overlap. Note each run's `labelId` and `sportType`.

## 2. Download only what's new
- `mkdir -p exports/coros` and `ls exports/coros/` once.
- For each run without `exports/coros/<labelId>.fit`: `queryActivityFitFileDownloadUrls` with its
  `labelId` and `sportType`, then download that URL to `exports/coros/<labelId>.fit` with curl
  (`curl -sSfL -o exports/coros/<labelId>.fit "<url>"`). The URL is short-lived and signed: don't
  print or save it anywhere else.
- If the daily limit is hit, import what you have and say how many runs are left for tomorrow.

## 3. Import
Follow the import-workouts skill for each new file: `--dry-run` first, batch any
`possible_duplicates` (often an Apple Health copy of the same run) into one question for her,
then import. Exact duplicates are skipped automatically.

## 4. Report
One line per run (date, miles, duration), counts imported / already present / waiting on her
answer, then `uv run coach summary --week current`. If she's about to do the weekly check-in,
say the recap now has her runs.
