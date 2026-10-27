# Model tiers and cost

Status 2026-10-26: every coach call runs on `claude-opus-5-5` at effort `medium` with prompt
caching on. No tier change until there are two weeks of `coach usage` data and an eval run that
shows the change holds quality.

## Measure first
- `uv run coach usage [--days 30]`: answers, cost by channel (`cli`, `slack`, `eval`) and model,
  cost per answer, 30-day projection. Rows hold token counts and cost only, no text.
- Check `tokens.cache_read_input_tokens`: if it stays near zero, caching is broken and that is
  the cheapest fix there is.

## Lever order (cheapest risk first)
1. **Caching** (on). Free once the prompt prefix is stable.
2. **Lower effort** on the same model. Often holds quality on simple routes; one cache namespace.
3. **Smaller model** for a route. Biggest saving, biggest quality risk; caches are per model.

## Routes and current call

| Route | Volume | Stakes | Call |
|---|---|---|---|
| Coaching Q&A (`coach ask`, Slack chat) | most calls | safety, citations, referrals | Stay on Opus 5.5 `medium` |
| Long-run debrief (gather + `propose_fuel_entry`) | 1 a week | low: code validates, she confirms | First candidate: try `low` effort, then Sonnet 5.5 |
| Sunday weekly check-in | 1 a week | medium | Not worth tuning at this volume |
| Evals | on demand | must match production | Always the production model and effort |

## How to change a tier
1. Baseline: `coach eval live --category <route's category>` on the current model.
2. Candidate: same command with `--model claude-sonnet-5-5` (or `--effort low`).
3. Switch only if `changes.regressions` is empty and the referral and citation cases still pass.
   Record the decision and numbers here and in CHANGELOG.

Prices live in `src/mara_coach/usage.py` (`PRICES`, `PRICES_AS_OF`); update them when Anthropic's
list prices change. Per million tokens on 2026-10-06: Opus 5.5 $4 in / $20 out, Sonnet 5.5
$2 / $10, Haiku 5.5 $0.10 / $0.50.
