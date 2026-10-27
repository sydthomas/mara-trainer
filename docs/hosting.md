# Hosting: options and decision

Status 2026-10-26: decision doc only. Nothing is set up; hosting needs Sydney's go-ahead
(CLAUDE.md). **Decision date: 2026-11-15** (two weeks into the plan, with real data on missed
check-ins and `coach usage` cost).

## The problem
The Slack bot (`coach slack run`) only runs while the laptop is awake. When it sleeps, the 10am
debrief, the 5pm sync nudge and the 6pm weekly check-in don't send, and DMs go unanswered.

## What the bot needs
- **No public URL.** Socket Mode is an outbound WebSocket, so there's no TLS, domain or inbound
  port to manage. Any always-on machine works.
- **Small:** one Python process, tens of MB of RAM, a SQLite file.
- **Secrets:** Slack tokens and `ANTHROPIC_API_KEY` on the host.
- **The real constraint: one database.** COROS sync, the weekly push and plan changes run in
  Claude Code on the laptop (COROS OAuth lives there) and write `data/coach.db`. A hosted bot
  needs that same database. Options: the server owns the DB and the laptop runs `coach` there
  over ssh (copy FIT files up, import remotely), or the DB stays on a machine at home.

## Options (prices checked 2026-10-26; confirm on the vendor page before buying)

| Option | Monthly | Data lives | Fits because | Watch out |
|---|---|---|---|---|
| **0. Laptop, made reliable** | $0 | laptop | launchd keeps the bot running and restarts it; `pmset` wakes the Mac before the Sunday 5pm/6pm and Saturday 10am windows | lid closed or travel still stops it |
| **1. Always-on device at home** (old Mac mini, small Linux box) | power only, one-time hardware | home | same DB for bot and Claude Code over the local network | home internet and power; you maintain it |
| **2a. Fly.io** shared-cpu-1x 256 MB + 1 GB volume | about $2.19 + $0.15/GB | Fly (US region) | cheapest always-on; volume for SQLite | prices rose 1 Oct 2026; volume billed even when stopped |
| **2b. DigitalOcean** Basic Droplet 512 MB | $4 (+20% weekly backups) | DO | plain Linux box, simple | 512 MB is enough for this bot, not for more |
| **2c. Hetzner** CX23 | about €4.29-4.49 + VAT | Hetzner (US or EU) | most RAM for the money | prices rose in 2026; CX22 is being retired |
| **3. PaaS** (Railway Hobby / Render worker) | $5 floor / $7 | vendor | git-push deploys | Render workers have no free tier; usage above Railway's $5 is billed extra |

Coach API cost is the same everywhere (see `docs/model-tiers.md`).

## If hosted (options 2-3), also needed
- **Backups:** the SQLite file holds health data. Nightly `sqlite3 .backup` to off-site storage
  (or a streaming replica), and a tested restore.
- **Privacy:** her runs, fuel log and profile move to a third-party server. Encrypt at rest,
  restrict ssh, keep secrets out of the image.
- **Deploys:** CI already gates `main`; deploy only tagged releases.
- **Monitoring:** a heartbeat (the bot DMs nothing for 24 h around a check-in → alert).

## Option 0 setup (installed 2026-10-26)
- `deploy/macos/install.sh` writes a LaunchAgent (`com.maratrainer.slack`): starts at login,
  restarts within 30 s if the bot exits, runs it under `caffeinate -is` so the Mac stays awake on
  power. `deploy/macos/uninstall.sh` removes it. Logs: `~/Library/Logs/mara-trainer/slack.log`.
- **The repo must not live in `~/Desktop`, `~/Documents` or `~/Downloads`.** macOS privacy
  protection (TCC) blocks background jobs from reading those folders; the bot hangs with
  "Operation not permitted". The repo lives in `~/code/mara-trainer`.
- Limits: lid closed without an external display still sleeps; so does battery-only idle once
  the bot isn't running.

## Asleep-proof Slack (2026-10-26)
Scheduling moved to the platform that's always on. The bot queues fixed-time messages on Slack's
servers (`chat.scheduleMessage`) a week ahead, so the 10am debrief ping, the 5pm sync reminder
and a 6pm note with next week's runs arrive even when the laptop sleeps. When it's awake, live
versions replace the 5pm and 6pm fallbacks (real counts, the coach's recap). DMs sent while it
slept are answered when it wakes. Only answers and the recap still need the laptop awake.

## Recommendation
1. **Now:** Option 0. Free, and it covers the scheduled windows this week.
2. **On 2026-11-15:** if check-ins still get missed, or before any second user, move to **2a or
   2b**, with the server owning the DB and the laptop running `coach` over ssh. Option 1 instead
   if the data should stay at home.
