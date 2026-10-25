# Plan rules and their sources

The plan guardrails (`src/mara_trainer/plan_rules.py`) follow the approved sources. Each coaching
rule cites the knowledge-base section and sources behind it. Where sources disagree, the rule
allows the range they cover. Rules with no approved source yet are listed at the bottom: they
stay in place as conservative defaults only until a source is approved, then get fixed to match.

Audited 2026-10-24 against `knowledge/training.md`, `race-week.md` and `strength.md`.

## Coaching rules (source-backed)

| Rule | Value | Sources (slug) | Notes |
|---|---|---|---|
| Runs per week, first marathon | 4 (capped by the runner's available days) | hal-higdon-marathon-training, chicago-marathon-training-plan, la-marathon-larr-training-plans, boston-marathon-baa-training-plans | Novice plans use 4 or 5 runs a week. |
| One long run a week | exactly 1 | same four plans | Every plan has one long run a week. |
| Peak long run, first marathon | up to 20 mi | hal-higdon-marathon-training, chicago-marathon-training-plan, la-marathon-larr-training-plans; boston-marathon-baa-training-plans (16-18 mi) | Plans peak at 16 to 20 mi. |
| Peak long run timing | the week before the taper (4 weeks out) | chicago-marathon-training-plan, la-marathon-larr-training-plans; Higdon peaks 3 weeks out | Within the 3-4 week range. |
| Peak week, first marathon | up to 40 mi | hal-higdon-marathon-training, boston-marathon-baa-training-plans | Both novice plans peak at about 40 mi. |
| Long run at most half the peak week | 50% | hal-higdon-marathon-training (20 mi in a 40 mi week) | |
| Long-run growth | about 1 mi a week | hal-higdon-marathon-training (6 to 20 mi over 15 weeks with step-backs) | |
| Step-back week | every 4th week | hal-higdon-marathon-training (every 3rd), la-marathon-larr-training-plans (every 3 to 5) | Within the published range. Depth (82%) has no source; see below. |
| Taper length | 3 weeks including race week | wang-tapering-endurance-meta-analysis-2023 (21 days or less helps), hal-higdon-marathon-training, la-marathon-larr-training-plans (3 weeks), boston-marathon-baa-training-plans (2 weeks) | |
| Taper volume | 75%, then 55% of peak | wang-tapering-endurance-meta-analysis-2023 (cut 41-60%), hal-higdon-marathon-training (40 to 29 to 21 mi) | |
| Quality workouts | at most 1 a week, any phase | hal-higdon-marathon-training (none), chicago-marathon-training-plan (1 a week from week 1), la-marathon-larr-training-plans (5-20% of mileage), wang-tapering-endurance-meta-analysis-2023 (keep intensity in the taper) | Was "none in base or taper": removed, sources keep speed early and in the taper. |
| Workout spaced from the long run | at least 1 easy or rest day between | chicago-marathon-training-plan (keep speed and long runs apart) | |
| Strength sessions | up to 2 a week; 1 in taper and race week | blagrove-strength-training-distance-runners-2018, acsm-resistance-training-2026 (2 a week to build; 1 a week maintains in the racing phase) | Was "at most 1": raised to match the sources. |
| Race week | race on race day, plus up to (runs per week - 1) short runs on training days, no long run, at most 1 small quality session, pre-race running at most 25% of the peak week | hal-higdon-marathon-training (3, 4, 2 mi vs a 40 mi peak = 22.5%), la-marathon-larr-training-plans, chicago-marathon-training-plan, wang-tapering-endurance-meta-analysis-2023 (keep session frequency) | Was "rest every other day": changed, every plan keeps short runs. |
| Plan length warning | note if under 12 weeks | chicago-marathon-training-plan | A note, not a block. |
| Paces only from a recent race | no invented paces | chicago-marathon-training-plan, la-marathon-larr-training-plans | Enforced in `coach pace` and the coach prompt. |

## Structural rules (not coaching claims)

- 7 sessions a week, each weekday once; runs only on the runner's chosen days; the long run on her
  chosen day; the race on race day at 42195 m. These carry out the runner's own choices.
- Weekly running total within 100 m of the brief's target; long run within the brief's
  [target, target + 1 mi]. These keep the drafted plan matched to the guardrail targets above.
- Runs of at least 1 km; rest and strength days at 0 m. Data sanity.

## No approved source yet (kept as conservative defaults)

| Rule | Value | Needed |
|---|---|---|
| Weekly mileage growth | at most 6% a week compounded, and at most max(1 mi, 8% of baseline) a week | A study of safe weekly progression. Candidate: Nielsen et al. 2014, JOSPT (not yet in the registry). The IOC load consensus only says to avoid sudden jumps (abstract read). |
| Step-back depth | 82% of the trend | Plans step back but the depth wasn't captured in the audit. |
| Training levels | base-building under 10 mi a week; intermediate under 40 mi; advanced at 40+ mi with 5+ days | Thresholds came from the legacy app. Only the first-marathon (novice) path is source-checked. |
| Caps for non-novice levels | peak weeks 20 / 55 / 60 mi; long runs 10 / 20 / 22 mi; runs per week 3 / 5 / 6 | *Advanced Marathoning* and *Daniels' Running Formula* couldn't be read in the audit. |
| Starting long run | larger of the longest recent run and 30% of baseline | Mechanical starting point. |
| Phase split | base first 25% (min 3 weeks), build to 65%, then marathon-specific | Phases are labels only now (no rule depends on them except taper and race week). Boston uses 3 / 6 / 9 / 2 weeks. |
| Plan length cap | 24 weeks | Plans run 18 to 25 weeks. |
