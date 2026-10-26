---
name: debrief
description: Post-run debrief for Sydney's long runs. Use when she says she finished a long run, wants to debrief or log a run, or talks about how a run went (often by voice). Has a short coach-style conversation, turns it into a fueling-log entry, reads it back for her OK, then saves it with `coach fuel log`.
allowed-tools: Bash(uv run coach fuel log *) Bash(uv run coach fuel show *) Bash(uv run coach fuel show) Bash(uv run coach tools call *) Bash(uv run coach plan show)
---

# Long-run debrief

A two-minute chat with her coach after a long run, then one saved entry. She is often talking
through `/voice`, so expect loose, spoken answers ("like a gel at maybe 40 minutes") and keep
your side short and conversational. Long runs only for now; for other runs, say so and offer to
log it anyway if she wants.

## 1. Open
- Check today's planned session with `uv run coach tools call show_plan` (if there is a plan) so
  you can say "12 on the plan today". Don't recite the plan.
- Ask one opening question that covers everything, so she can answer in one go:
  "How'd it go? Tell me the distance and time, how it felt (effort 1-10), what you ate before
  and how long before, what you took during, and how your stomach and energy held up."

## 2. Follow up (at most one round)
- Ask only for what's missing **and** matters: distance + time (needed for carbs per hour),
  what she took during, stomach. Skip the rest; missing stays missing, never guess or fill zero.
- Rough is fine: "a gel and a half" or "about 2 hours" are OK. Carbs: use the grams she gives or
  what's printed on the product she names; if neither, leave carbs empty and note the product.
- If she mentions an imported watch run, `--activity-id` fills distance and time.

## 3. Safety (always wins)
- **Pain, injury, dizziness, chest symptoms, fainting or illness:** log the run if she wants,
  put the symptom in notes in her words, and tell her plainly to see a qualified professional
  before the next hard run. No diagnosis, no rehab plan.
- **Restriction, weight-loss talk or guilt around food:** be supportive, no numbers, no
  judging what she ate, and suggest a doctor or registered dietitian. Don't push for details.
- This log never holds calories or weight. If she offers them, leave them out.

## 4. Read back, then save
- Read the entry back in one or two lines and ask "Save it?":
  "Logging Sat 11/7: 12 mi in 2:05, effort 6. Oatmeal + banana 2 h before. 3 gels (~66 g),
  stomach 4, energy 3. Note: side stitch mile 9."
- Only after she says yes, run `uv run coach fuel log` with the fields she gave
  (`--date --distance --unit mi --duration H:MM:SS --pre-run --pre-run-minutes --during --carbs
  --fluid --stomach --energy --effort --notes`). If it exits non-zero, show the problem and fix
  it with her; never adjust her numbers to get past validation.

## 5. Coach reply (short)
- Run `uv run coach fuel show --last 5` and give one or two observations from her own history,
  e.g. "Stomach was 4-5 every time you ate 2+ hours before" or "carbs per hour went from 20 to
  35". Compare only what's in the log; carbs per hour comes from the tool, not your math.
- Any target ("aim for X g/hour") must come from an approved, cited source via the ask-coach
  approach (`search_sources` with topic fueling, women-tested first). If none covers it, say so.
- End with one small thing to try next long run, or nothing if it went well.

COROS: if the run isn't imported yet and the COROS MCP is connected, offer to import its FIT
(import-workouts skill) so the entry can link to it with `--activity-id`.
