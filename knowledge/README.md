# Coaching knowledge base

The coach's reference book: what the approved sources say, organized by topic, written in our
own words, with every claim cited. The coach reads a section with the `read_knowledge` tool
instead of fetching papers at answer time. Sydney approves each section before the coach can
see it (`coach knowledge approve SECTION --by NAME`); editing an approved section's body sends it
back to draft until it is approved again.

Rules (checked by `coach knowledge check` and the tests):
- **Our own words only.** Facts, numbers and findings, never copied passages. No quote longer
  than a short phrase. Open-access sources (`may_summarize: true`) may be summarized in full;
  copyrighted ones get short facts from their abstract or public page; books are cited by
  reference only, never summarized from memory.
- **Every claim cites.** Each bullet in the claim sections ends with one or more tags like
  `[@jeukendrup-carbohydrate-during-exercise-2014]` (slugs from the source registry). Only
  approved sources may be cited. Every cited slug appears in Works cited, and every Works cited
  entry is cited somewhere.
- **Only what was read.** A claim must come from text actually read during the audit (full text
  or abstract), not from memory. If a source couldn't be read, it goes under Gaps, not in claims.
- **Women first.** Say who was studied when it matters ("mostly men", "trained women, n=19").
  Prefer women-tested findings and say plainly when guidance rests mostly on studies of men.
- **Safety.** No calorie numbers, no body-weight or weight-loss targets. Ranges given per kg of
  body mass are kept for reference but flagged: the coach never asks for or computes with body
  weight. Pain, illness and medical topics point to a qualified professional.

## Sections

| file | covers |
|---|---|
| `training.md` | running: weekly structure, easy vs hard, long run, workouts, load and progression, published plans |
| `strength.md` | strength training for runners |
| `cross-training.md` | non-running training (bike, swim, etc.) and concurrent training |
| `fueling-before.md` | eating before runs and before the race, carb loading |
| `fueling-during.md` | carbs, gels, gut training, caffeine during runs and the race |
| `fueling-after.md` | recovery eating: carbs, protein, timing |
| `daily-nutrition.md` | everyday eating for training, protein, micronutrients, supplements |
| `hydration.md` | fluids and sodium before, during, after; heat; hyponatremia |
| `recovery.md` | sleep, rest days, recovery practices |
| `race-week.md` | taper, race-week routine, race-day pacing and logistics |
| `gear.md` | shoes (including carbon plates), other gear |
| `women.md` | menstrual cycle, hormonal contraception, low energy availability / RED-S, iron, heat, postpartum |
| `health-referrals.md` | warning signs and when to send the runner to a professional (bone stress, heat illness, hyponatremia, etc.) |
| `course.md` | course-specific preparation (LA Marathon and general course notes) |

## Section format

```markdown
---
section: fueling-during
title: Fueling during runs
status: draft
approved_by:
approved_on:
content_sha256:
---
# Fueling during runs

## Bottom line for the coach
- 3 to 8 bullets the coach can act on, each cited. [@slug]

## What the sources agree on
- Claim, with numbers and who was studied. [@slug-a] [@slug-b]

## Where they differ
- What differs and why (population, date, method). [@slug-a] [@slug-b]

## Women-specific notes
- ... [@slug]

## Gaps
- What no approved source covers, and sources that couldn't be read (no tag needed).

## Works cited
- [@slug] Author(s). "Title." *Container*, vol. X, no. Y, Year, pp. N-M. https://doi.org/...
```

The front matter `status`, `approved_by`, `approved_on` and `content_sha256` are written by
`coach knowledge approve`; never edit them by hand. Works cited uses MLA 9 style.
