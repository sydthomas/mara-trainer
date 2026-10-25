# Goal: mara-trainer public MVP backend

Status: approved 2026-10-19, option A chosen. Day 1 done except the API key (see Progress).

## Goal statement

Build the backend for an AI marathon coach that gives each runner what a good human coach would:
a training plan fitted to their body, history and goal; gear suggestions based on their likes;
fueling for runs, daily eating and race week; recovery and cross-training advice; and a coach
they can text with. Every recommendation is grounded in vetted, cited sources, and every plan
passes the deterministic guardrails in `plan_rules.py` before a runner sees it.

MVP done = Sydney can talk to the coach in Slack, ask about her plan, fueling, gear or recovery,
and get answers that use her own data and cite trusted sources. Built so it can later open to
other runners.

## Ground rules (these change how the "scraping" part works)

1. **Curate and cite, don't scrape.** Most coach blogs, YouTube videos, podcasts and textbooks
   are copyrighted, and YouTube/X/most sites forbid scraping in their terms. A public product that
   copies their content invites takedowns. Instead:
   - Keep a **source registry**: who the source is, why they're trusted (credential, publication,
     peer review), URL, license, date reviewed, and who approved it.
   - At answer time, use Claude's `web_search` / `web_fetch` tools restricted to the registry's
     domains (`allowed_domains`), with citations turned on. The app stores links and short notes,
     not copies of other people's work.
   - Open-access material can be summarized and stored: consensus and position statements
     (ACSM, IOC, ISSN, etc.), open-access papers in PubMed Central.
   - Textbooks: cite them, don't ingest them, unless you have a license.
   - Podcasts: public RSS gives titles and show notes; use transcripts only when the show
     publishes them or gives permission.
   - Coaches and creators: ask them. Partnerships are a feature for a public launch.
   - X posts: inspiration and discovery only. Reading X programmatically needs the paid X API
     and its terms; don't treat posts as authoritative sources.
2. **Claude proposes, code validates.** Keep this. Plans go through `validate-plan`; fueling
   numbers come from cited ranges, not invented ones.
3. **Health and nutrition.** General, source-backed guidance only. No diagnoses. Pain, illness,
   injury, disordered-eating signals, medical diets, pregnancy: refer to a qualified
   professional (doctor, physio, registered dietitian). Build this into the system prompt and the
   eval set.
4. **Strava stays in-session via the official MCP** (see `docs/strava-requirements.md`). A public
   product cannot store Strava data or feed it to the AI outside Strava's MCP; FIT/GPX/Apple
   Health uploads remain the data path for other users.
5. **Gear.** Recommend from the runner's stated preferences plus cited reviews. If affiliate links
   ever appear, disclose them. Never invent reviews.

## What has to change in this repo first (needs Sydney's call)

`CLAUDE.md` currently says: personal tool for one athlete, no web app, no auth, no direct LLM API
calls ("ask first"). The MVP needs at least direct Claude API calls and a Slack bot. Options:

- **A (recommended):** keep `mara_trainer` as the deterministic core library and add a sibling
  package `src/mara_coach/` (agent + channels) in this repo, with CLAUDE.md updated to allow
  Claude API calls and Slack. Multi-user, auth and hosting come later as their own step.
- **B:** new repo for the public product that depends on `mara_trainer` as a library.

## Architecture (MVP)

```
Slack (Socket Mode)  ─┐
coach ask (CLI)      ─┼─> agent (Claude API, tool runner)
                      │      tools: plan_brief, summary, validate_plan, show_plan,
                      │             get_profile, search_sources, web_search (allowed_domains)
                      │
                      └─> mara_trainer core: SQLite, plan_rules, summaries, imports
```

- Model: `claude-opus-5-5`, adaptive thinking, effort set explicitly; server-side refusal
  fallback enabled. Prompt caching on the system prompt and tool list.
- Tools wrap existing functions in `plan_rules.py` / `summaries.py` so the model never does
  mileage math itself.
- New tables (forward-only migrations): `athlete_profile` (diet, allergies, shoe likes/dislikes,
  injuries history as free text the runner writes), `sources` (registry), `conversations`
  (Sydney's own chat history; no Strava content).
- Channels: Slack first (Bolt for Python, Socket Mode, so no public server needed). SMS later
  via Twilio; US carriers require A2P 10DLC registration, which can take a couple of weeks, so
  start it early if SMS matters.

New dependencies to vet: `anthropic` (MIT), `slack-bolt` (MIT). Both fine under the license rule.

## Next steps (next 5 days)

**Day 1: decide and set up**
- Pick option A or B; update `CLAUDE.md` scope accordingly.
- Get an Anthropic API key into `.env` (`ANTHROPIC_API_KEY`), name only in `.env.example`.
- Write the first 20 eval questions (plan, fueling, gear, recovery, cross-training, and 5
  "should refer to a professional" cases) with what a good answer must include.

**Day 2: source registry**
- Migration + `coach source add/list/approve`.
- Seed 20–30 sources by hand: position stands, open-access reviews, certified coaches' sites
  you trust. Record why each is trusted.

**Day 3: the agent**
- `coach ask "<question>"`: tool runner over the core functions + `web_search` limited to
  approved domains, citations on. Tests use a fake client; no real API calls in pytest.
- System prompt: coach persona, safety rules, "cite or say you don't know", miles for display.

**Day 4: profile and modules**
- `athlete_profile` migration + `coach profile set`.
- Fueling (training runs, daily eating, race week), gear, recovery/cross-training as focused
  prompt sections or skills that read the profile and current phase from `plan brief`.

**Day 5: Slack + evals**
- Slack app in Socket Mode, DM-only, single allowed user (Sydney).
- Run the eval set, fix the worst failures, write up results in this doc.

## Progress

- 2026-10-19 (Day 1): Option A chosen. Kept all existing code: it becomes the deterministic core.
  `CLAUDE.md` rewritten for the coach product (two packages, curate-and-cite rule, nutrition and
  gear rules, Claude API allowed in `mara_coach` only). Added `src/mara_coach/evals.py` and
  `evals/coach-questions.json`: 23 synthetic cases across 9 categories, 5 expecting a referral.
  Still to do: Sydney adds `ANTHROPIC_API_KEY` to `.env` and its name to `.env.example`.
- 2026-10-19 (Day 2): Source registry built. Migration `002_sources.sql`, `mara_coach/sources.py`,
  and `coach source add/import/list/approve/retire/domains`. The `coach` entry point moved to
  `mara_coach.cli:app`, which mounts the core CLI, so the core still never imports the coach.
  Sources start `proposed`; approval needs `--by NAME` and is enforced by a DB check. Only approved
  sources with a URL feed `allowed_domains`. `sources/seed.json` holds 27 proposed sources (10
  position stands, 7 open-access papers, 6 guideline bodies, 4 books cited by reference), each URL
  checked against a live page or bibliographic record. Still to do: Sydney reviews and approves.
  Caveat for Day 3: six paywalled stands use `doi.org` links. Approving them adds `doi.org`, which
  redirects to any publisher, to the allowed domains. (Resolved the same day, see next entry.)
- 2026-10-20 (Day 2, cont.): Plan-building sources added. Migration 003 adds the `race_organizer`
  kind and the `course` topic (hills, elevation, weather, start time) for race-specific training.
  Migration 004 adds `fetchable`: link-only sources are cited but their domain never enters
  `allowed_domains`. `doi.org` is always link-only. London Marathon Events (terms ban bots and AI
  use), Marathon Handbook (terms ban automated scripts) and the NYRR course page (sends automated
  visitors to a queue) are link-only. Seed is now 43 sources, adding 16 race plan, course and coach
  pages. Not included: Paris (site blocks fetches; plan is in-app only), the NYC, Berlin, Tokyo and
  Sydney plans (in-app or none), Runna and Strava plans (no API, or the Strava rules).
- 2026-10-20 (Day 2, cont.): Women-tested evidence. Migration 005 adds `female_evidence` and
  `population_note` to sources; `source list --women-tested` and `source import --update` added.
  The 43 existing sources were audited from their methods and participant tables: 4 mostly male,
  8 mixed without a sex breakdown, 3 that report by sex, 1 female-specific, 4 unknown. 14 sources on
  women were added (ISSN female athlete stand, protein, menstrual cycle, oral contraceptives, iron,
  bone stress, the 2025 Triad update, heat, injury by sex, postnatal return to running, ROAR), for
  57 in total. 3 eval cases added. Gap: no open-access study of strength training in women runners.
  Day 3 system prompt: prefer women-tested sources and flag mostly-male evidence.
- 2026-10-22: Pace calculator. `src/mara_trainer/paces.py` and `coach pace [--distance N --time
  H:MM:SS --unit mi|km]` predict 5K, 10K, half and marathon times from one race result with
  Riegel's formula (T2 = T1 * (D2/D1)^1.06, American Scientist 1981). It defaults to the saved
  recent race and errors if there is none. Output is SI plus per-mile display, names the method
  on every prediction, and carries caveats: the exponent was fit to elite world records, isn't
  checked separately for women, and runs optimistic for a first marathon from a half. No
  training-pace zones: the published systems (e.g. Daniels' VDOT) are in copyrighted books we
  haven't checked, so none are invented. Riegel added to the seed as a proposed, link-only
  source (58 total), `female_evidence: unknown` because the full text wasn't reviewed.
- 2026-10-22 (Days 3-4): Sydney's call: Day 3 became a Claude Code skill, `.claude/skills/ask-coach`,
  and the Claude API agent (`coach ask`, `anthropic` dependency) is deferred. The skill pulls her
  context through a new tool layer, `mara_coach/tools.py`: seven read-only tools (plan_brief,
  summary, validate_plan, show_plan, get_profile, search_sources, allowed_domains), each with a
  name, description and JSON input schema, exposed as `coach tools list` / `coach tools call`. The
  future API agent can pass the same registry to a tool runner. `search_sources` returns approved
  sources only, `validate_plan` never accepts, and no tool touches Strava data. Plan brief, summary
  and plan checks moved into `mara_trainer/reports.py` so the CLI and the tools share one code path.
  Day 4 profile done: migration 006 adds `athlete_profile` (diet, allergies, shoe likes/dislikes,
  current shoes, injury history, notes; all nullable, `[]` = "none"), with `coach profile set/show`.
  Still to do for Day 4: fueling, gear and recovery sections (in the skill for now).
- 2026-10-22: Offline eval runner. `mara_coach/evals_runner.py` scores answers from a pluggable
  answerer (any callable from case to text; `MappingAnswerer` doubles as the test fake), and
  `coach eval run --answers FILE.json` prints a JSON report: per-case pass, fail, unchecked or
  missing with reasons, totals by category, unknown answer ids. Exit 3 if any case fails. Cases
  gained an optional `checks` object (`require` regex groups, `forbid` regexes, `cite`), and
  referral cases are always checked for a referral. These are text heuristics that catch clear
  misses; the `must`/`must_not` lines still need a grader and are listed under `manual_review`.
  All 28 cases now carry checks, including 2 new ones (strength for women runners, lifting after
  low bone density). Strength gap partly closed with 3 proposed sources: Vikmoen 2016 (female-only
  RCT, PLOS ONE, CC BY), Huiberts 2024 (concurrent-training meta-analysis analysed by sex, CC BY)
  and Barnes 2013 (collegiate runners, results by sex; free repository copy, publisher copyright).
  Still missing: a trial in women marathon or recreational distance runners. Seed is now 60.
- 2026-10-23: Sydney approved Vikmoen 2016, Huiberts 2024 and Barnes 2013 (Riegel stays
  proposed). Pace inputs within 0.5% of a standard distance snap to it; the coach's
  `validate_plan` tool only reads `plans/` and `examples/`.
- 2026-10-23: Fueling log. Migration 007 adds `fuel_log`; `mara_trainer/fueling.py` and
  `coach fuel log|show|delete` record pre-run food and timing, what was taken during, carbs
  (grams), fluid, and stomach/energy 1-5, optionally linked to an imported activity. Carbs per
  hour is the only computed number. Sydney chose a fueling log over food tracking: no calorie,
  weight or daily-intake fields, by design (gut training for race day, and the disordered-eating
  rule). The coach reads it through the `fuel_log` tool; ask-coach and weekly-review use it.
  Effort (RPE 1-10) is logged too. New `.claude/skills/debrief`: a short post-long-run chat
  (Sydney talks via `/voice`) that reads the entry back and saves it after she says yes. Sydney's
  call: long runs only, stay in Claude Code for now. Later the same debrief moves to Slack with a
  scheduled ping after the planned long run (needs an API key), then maybe phone calls.
- 2026-10-24: Plan accepted (18 weeks from 2026-11-02, peak 40 mi with a 20 mi long run). Sydney
  approved the API agent, Slack, a COROS MCP and Twilio phone debriefs (free tunnel first;
  hosting later), building them together in that order. `coach ask` added: `mara_coach/agent.py`
  runs a manual tool loop on `claude-opus-5-5` (adaptive thinking, effort medium, server-side
  refusal fallback `"default"`, prompt caching) over the read-only tools, with server-side web
  fetch restricted to approved source domains. Tests use a scripted fake client. `anthropic`
  added (Apache-2.0). Strava stays in Claude Code sessions only; the coach gets runs from COROS.

## Later, before any public launch
Multi-user accounts and auth, hosted DB (Postgres), privacy policy and terms, data deletion,
health-data handling review, creator partnerships, web onboarding, SMS, rate limits and cost
caps per user.
- 2026-10-24 (cont.): Live test of `coach ask` worked (~10-15 cents a question), but PMC pages
  block web fetch, so the coach couldn't read open-access papers. Sydney's call: build a
  knowledge base instead of per-source notes. `knowledge/` holds one cited Markdown section per
  topic (training, strength, cross-training, fueling before/during/after, daily nutrition,
  hydration, recovery, race week, gear, women, health referrals, course): agreements,
  disagreements, women-specific notes, gaps, MLA works cited. Drafted by audit agents reading
  full open-access text through Europe PMC's API (abstracts only for copyrighted sources).
  `mara_coach/knowledge.py` checks the format and citations; `coach knowledge approve` (Sydney
  only) records a body hash, so edits send a section back to review. The coach reads approved
  sections through the `read_knowledge` tool before fetching anything. Upkeep:
  `.claude/skills/knowledge-audit`.
- 2026-10-25: Plan rules audited against the sources (PR #12, `docs/plan-rules-sources.md`);
  Sydney's plan updated (second strength day, race-week shakeouts, taper maintenance strength).
  Knowledge sections approved: fueling-during, training, women. Slack bot added:
  `coach slack run`, Socket Mode (no hosting), DMs from one allowed user, text-only history of the
  last 10 exchanges, placeholder then edited answer. `slack-bolt` added (MIT). Next: the Saturday
  long-run debrief ping in Slack, then COROS.
- 2026-10-25 (cont.): Slack long-run debrief. On plan long-run days the bot DMs a check-in at 10am
  athlete-local (Sydney's choice; late until 8pm if the bot was off; once a day via migration
  008 `notifications`). The coach gathers details and calls `propose_fuel_entry` (validates and
  reads back, never saves); the bot saves in code only on "yes", drops on "no".
