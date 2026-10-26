---
name: weekly-review
description: Weekly training review for Sydney. Use when she asks how her week went, wants a check-in or weekly review, or asks whether to adjust the plan. Compares `coach summary` (local data) and, optionally, the official COROS MCP against the accepted plan, then proposes changes for her approval.
allowed-tools: Bash(uv run coach summary *) Bash(uv run coach summary) Bash(uv run coach plan show) Bash(uv run coach plan brief *) Bash(uv run coach validate-plan *) Bash(uv run coach fuel show *) Bash(uv run coach fuel show)
---

# Weekly review

## 1. Gather (read-only)
- `uv run coach summary --week current --weeks 4` for local data (FIT/GPX/Apple Health). Report null
  values as "no data", never as zero. Mention the coverage counts (for example, "sleep logged 3/7 days").
- `uv run coach plan show` for the accepted plan. Find this week by `start_date`.
- **COROS (optional):** if the COROS MCP is connected (`/mcp`), fetch this week's activities to
  find runs that never got a FIT import. Offer to import them: download the FIT files into
  `exports/` and use the import-workouts skill. COROS rules:
  - Don't add COROS numbers to local totals until they're imported. Until then, list them
    separately as "from COROS (not imported)".
  - Sleep, HRV and recovery from COROS are fine as context in this conversation, but don't write
    them to any file.
  - If the MCP isn't connected, continue with local data and tell her she can authorize it with `/mcp`.
- Ask Sydney briefly how she feels (energy, soreness, sleep, any pain) if she hasn't said.

- `uv run coach fuel show --since <Monday>` for this week's fueling entries (long runs mostly):
  note stomach/energy ratings and carbs per hour. No calorie or weight talk.

## 2. Compare to the plan
For each planned session, show planned against actual: done, partial, missed, or extra. Then show
weekly distance against the target, the long run against its target, and whether the quality
session happened. Use miles. Keep it to one compact table plus 3-5 bullet points.

## 3. Propose (don't apply)
- Suggest concrete changes for the coming 1-2 weeks only, such as moving the long run, swapping the
  workout for easy running, or a cutback. Base them on what happened and how she feels.
- Be conservative when signs stack up: missed sessions plus fatigue or poor sleep mean hold or
  reduce, never "make up" missed miles.
- Any change to the plan must go through the plan-builder rules. Edit a copy in `plans/`, run
  `uv run coach validate-plan`, and only after Sydney approves, run it again with `--accept`.
- Pain that persists, gets worse, or changes her stride means stop and see a professional. Don't diagnose.

## 4. Output
Give a short summary, the comparison table, the proposed changes, and one question asking her to approve.
Store nothing unless she approves a validated plan change. Once next week is settled, offer to put
it on her watch with the coros-run-planner skill.
