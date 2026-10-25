---
name: import-workouts
description: Import Sydney's workout files into the local database. Use when she provides or mentions FIT files (Garmin/watch exports), GPX files, or an Apple Health export (export.zip / export.xml). Validates, flags possible duplicates for her confirmation, then imports.
allowed-tools: Bash(uv run coach import *) Bash(uv run coach summary *) Bash(uv run coach summary)
---

# Import workouts

Supported files: `.fit` (the default path for Garmin and Runna-on-watch runs), `.gpx`, and Apple
Health `export.zip` or `export.xml` (running workouts plus daily HRV and resting HR; sleep isn't
imported yet). COROS runs come in as FIT files: if the COROS MCP is connected, download them with
`downloadActivityFitFiles` into `exports/`, then import as below. Strava data is never imported
(see CLAUDE.md).

Personal files belong in `exports/` (gitignored). Never copy them into `tests/` or any committed path.

## Steps (one file at a time)
1. **Validate and preview:** `uv run coach import <file> --dry-run`
   - Exit 1 means the file is invalid or unsupported. Report the error code (for example
     `INVALID_FIT_FILE` or `NO_SUPPORTED_RUNNING_SESSIONS`) and stop.
   - Show each session: local date and time in her timezone, distance in miles, duration, and any
     warnings (skipped non-running sessions, repeated sessions).
   - `exact_duplicate: true` means it's already imported and will be skipped. That's fine.
   - A non-empty `possible_duplicates` means a near match from any source (for example, the same run
     from the watch and from Apple Health). Show both side by side.
2. **Confirm duplicates with Sydney.** For each possible duplicate, ask whether it's the same run
   (skip it) or a different run (import it). Never decide for her, and never delete existing rows.
3. **Import:** `uv run coach import <file>` plus `--allow-duplicate <key>` for each session she
   confirmed is a different run.
   - Exit 2 means unconfirmed near-duplicates remain and nothing was written. Go back to step 2.
   - If she wants to skip a run that's a near-duplicate, don't import that file. (The import is
     all-or-nothing per file.) Tell her the existing record is kept.
4. **Report:** the counts imported, already present, and check-in days, then
   `uv run coach summary --week current`.

For a folder of files, loop over them the same way, batching the duplicate questions into one message.
