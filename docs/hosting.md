# Hosting: decision and runbook

## Decision (2026-11-24)
The Slack bot moves to a **DigitalOcean Droplet** (Basic, 1 GB, Ubuntu 24.04 LTS, SFO). Sydney
approved it after the laptop trial (Option 0, below), which stays as the fallback.

Why:
- The bot only works while the laptop is awake. Queued Slack messages cover the fixed-time
  pings, but answers, the live Sunday check-in and the COROS sync need a running process.
- A plain Linux box is the simplest always-on host: Socket Mode needs no public URL, so the
  server takes no inbound traffic at all.
- 1 GB instead of 512 MB leaves room for `uv sync`, restic and a second runner later.

Shape:
- **Data and secrets on an attached DO Volume** (encrypted at rest by DO) mounted at
  `/srv/mara`. The boot disk holds the OS and nothing personal. `bootstrap.sh` refuses to run
  if `/srv/mara` is not a separate mount.
- **No inbound traffic except Tailscale.** ufw denies incoming and allows only `tailscale0`;
  SSH works only over Tailscale.
- **Nightly encrypted backups** with restic to a private Backblaze B2 bucket (03:30 Pacific):
  a consistent DB snapshot (`coach backup`) plus the FIT files in `exports/`. Restic encrypts
  on the server before upload. Keeps 7 daily, 8 weekly, 12 monthly; `restic check` on Sundays.
- **The server owns the database.** Dev stays on the laptop; `coach` commands that touch her
  data run on the server over SSH (`deploy/linux/coach.sh`). The laptop copy of
  `data/coach.db` is stale after the move.
- Secrets: one value per file in `/srv/mara/secrets/env/`, passed to the bot by systemd
  (`LoadCredential`, read from `$CREDENTIALS_DIRECTORY`); COROS tokens in
  `/srv/mara/secrets` (`COACH_SECRET_BACKEND=file`); restic settings in
  `/srv/mara/secrets/restic/`. No `.env` on the server.

Cost, about **$8/month**: Droplet $6 + weekly DO backups $1.20 (20%; they cover the boot
disk only, not the Volume) + 10 GiB Volume $1 + B2 free under 10 GB + Tailscale free
(personal plan). Coach API cost is unchanged (`docs/model-tiers.md`). Confirm prices on the
vendor pages before buying.

## Files (`deploy/linux/`)
| File | Runs | Does |
|---|---|---|
| `bootstrap.sh` | once, as root, on the Droplet | volume check, packages (git, restic, sqlite3, uv, Tailscale), `mara` user, `/srv/mara/{app,data,exports,secrets,backup,cache}`, clone, `app/data` and `app/exports` symlinks, units, ufw (only once Tailscale is up). Re-runnable |
| `install-units.sh` | as root (bootstrap runs it) | installs the three units, writes the optional-credentials drop-in, the deploy sudoers rule |
| `mara-slack.service` | systemd | the bot, as `mara`, hardened, restarts after 10 s |
| `mara-backup.service` / `.timer` | systemd, 03:30 Pacific, `Persistent=true` | runs `backup.sh` |
| `backup.sh` | as mara | snapshot, restic backup, forget/prune, Sunday check; `init` and `restic ARGS` helpers |
| `deploy.sh [REF]` | as mara | fetch + reset to `origin/main` or a tag, `uv sync --frozen`, pre-deploy snapshot, `init-db`, restart, waits 60 s for "Slack bot running" |
| `coach.sh ARGS` | as mara | any `coach` command with the bot's settings |
| `move-data.sh HOST` | on the laptop | stops the LaunchAgent, snapshots, copies DB + `exports/` to the server |

Paths: the app reads FIT files from `<repo>/exports/coros` (found from the package location)
and writes `data/` relative to the working directory, so bootstrap makes
`/srv/mara/app/data -> /srv/mara/data` and `/srv/mara/app/exports -> /srv/mara/exports`
(listed in `.git/info/exclude`). The DB path is set explicitly (`COACH_DB_PATH`).

## Runbook: first setup
Every step that needs an account or a credential is Sydney's.

1. **Accounts (Sydney).** DigitalOcean (2FA on), Tailscale, Backblaze B2. In B2: a **private**
   bucket (for example `mara-coach-backup`) and an application key limited to that bucket.
   Save the key ID and key in a password manager. Generate a restic password and save it
   there too: without it the backups cannot be read.
2. **Droplet and Volume (Sydney).** Create a 10 GiB Volume in SFO3 ("Manually Format & Mount").
   Create the Droplet: Basic, 1 GB, Ubuntu 24.04 LTS, SFO3, SSH key login, weekly backups on.
   Attach the Volume.
3. **Mount the Volume** (DO web console or SSH as root, while the public IP still works):
   ```sh
   ls /dev/disk/by-id/                        # scsi-0DO_Volume_<name>
   mkfs.ext4 /dev/disk/by-id/scsi-0DO_Volume_<name>   # new, empty volume only
   mkdir -p /srv/mara
   echo '/dev/disk/by-id/scsi-0DO_Volume_<name> /srv/mara ext4 defaults,nofail,discard,noatime 0 2' >> /etc/fstab
   mount -a && findmnt /srv/mara
   ```
4. **Bootstrap:**
   ```sh
   curl -fsSLO https://raw.githubusercontent.com/sydthomas/mara-trainer/main/deploy/linux/bootstrap.sh
   less bootstrap.sh && bash bootstrap.sh
   tailscale up --ssh            # sign in with the printed link (Sydney)
   ```
   In the Tailscale admin console, **disable key expiry** for this machine (otherwise SSH stops
   working after 180 days; the bot keeps running). From the laptop, check
   `ssh root@<tailscale-name>` works, then run `bash /srv/mara/app/deploy/linux/bootstrap.sh`
   again: it enables ufw. Check: `ufw status verbose` shows deny incoming, allow on tailscale0;
   SSH to the public IP now times out.
5. **Secrets (Sydney, over Tailscale SSH).** One value per file, typed or pasted at a hidden
   prompt (nothing in shell history, no trailing newline):
   ```sh
   put() { sudo -u mara bash -c 'umask 077; read -rsp "$1: " v; echo; printf %s "$v" > "$2"' _ "$1" "$2"; }
   for n in ANTHROPIC_API_KEY SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_ALLOWED_USER_ID; do
     put "$n" "/srv/mara/secrets/env/$n"; done
   # optional: COACH_LAT COACH_LON COACH_PLACES COACH_NWS_CONTACT, same way, then:
   bash /srv/mara/app/deploy/linux/install-units.sh
   put repository /srv/mara/secrets/restic/repository        # b2:mara-coach-backup:mara
   put password /srv/mara/secrets/restic/password
   put b2-account-id /srv/mara/secrets/restic/b2-account-id
   put b2-account-key /srv/mara/secrets/restic/b2-account-key
   sudo -u mara /srv/mara/app/deploy/linux/backup.sh init
   ```
   Never `cat` these files. Same Slack app and tokens as the laptop.
6. **Move the data** (laptop): `deploy/linux/move-data.sh root@<tailscale-name>`. It stops the
   LaunchAgent (two bots would both answer every DM), takes a snapshot, copies it and
   `exports/` to `/srv/mara/backup/incoming/`, and prints the server commands that put them in
   place. Nothing local is deleted.
7. **Start:** `sudo -u mara /srv/mara/app/deploy/linux/deploy.sh` (exit 0 = "Slack bot running"
   seen in the log).
8. **COROS sign-in** (Sydney). From the laptop, with the callback port forwarded:
   ```sh
   ssh -L 33418:127.0.0.1:33418 root@<tailscale-name>
   sudo -u mara /srv/mara/app/deploy/linux/coach.sh coros login --no-browser
   ```
   Open the printed link in the laptop browser; the redirect to `127.0.0.1:33418` reaches the
   server through the tunnel. Tokens land in `/srv/mara/secrets`.
9. **Verify by outcome:**
   - `systemctl status mara-slack` active; `journalctl -u mara-slack -n 50` shows the start line.
   - DM the bot "green" and a question; both answered. "sync my runs" imports this week's runs.
   - `sudo -u mara /srv/mara/app/deploy/linux/coach.sh summary` matches the laptop's last summary.
   - `systemctl start mara-backup` then `journalctl -u mara-backup` ends with "backup done";
     `sudo -u mara /srv/mara/app/deploy/linux/backup.sh restic snapshots` lists it.
     `systemctl list-timers mara-backup.timer` shows the next run.
10. **Restore drill** (below) once, before relying on it.

## Day to day
- **Deploy** after merges that change the bot: `ssh root@<tailscale-name> sudo -u mara
  /srv/mara/app/deploy/linux/deploy.sh [vX.Y.Z]`. It snapshots the DB to
  `/srv/mara/backup/pre-deploy.db` before migrations and keeps the stored timezone.
- **Run coach commands:** `ssh root@<tailscale-name> sudo -u mara
  /srv/mara/app/deploy/linux/coach.sh summary` (any command). To import a FIT file, `scp` it
  into `/srv/mara/exports/` first, then `coach.sh import /srv/mara/exports/<file>`.
- **Logs:** `journalctl -u mara-slack -f`.
- The deploy sudoers rule (`/etc/sudoers.d/mara-deploy`) allows exactly
  `mara ALL=(root) NOPASSWD: /usr/bin/systemctl restart mara-slack.service`.

## Re-auth
- **COROS** (sync or push says the sign-in was rejected): repeat runbook step 8.
- **Slack or Anthropic key rotated:** `put NAME /srv/mara/secrets/env/NAME` (step 5), then
  `systemctl restart mara-slack`.
- **Optional setting added or removed:** write or delete its file, then
  `bash /srv/mara/app/deploy/linux/install-units.sh && systemctl restart mara-slack`.
- **B2 key rotated:** `put` the two `b2-account-*` files; the next backup uses them. The restic
  password does not change with the key.
- **Tailscale:** if key expiry was left on and SSH stops, re-authenticate from the DO web
  console with `tailscale up --ssh`.

## Restore
**Drill** (no change to live data):
```sh
sudo -u mara /srv/mara/app/deploy/linux/backup.sh restic snapshots
sudo -u mara /srv/mara/app/deploy/linux/backup.sh restic restore latest \
  --target /srv/mara/restore-test --include /srv/mara/backup/coach.db
sqlite3 -readonly /srv/mara/restore-test/srv/mara/backup/coach.db "PRAGMA integrity_check"
sudo -u mara env COACH_DB_PATH=/srv/mara/restore-test/srv/mara/backup/coach.db \
  /srv/mara/app/deploy/linux/coach.sh summary --week last
rm -rf /srv/mara/restore-test
```
Note the date and result here when done.

**Real restore** (DB lost or corrupt, server fine):
```sh
systemctl stop mara-slack
sudo -u mara /srv/mara/app/deploy/linux/backup.sh restic restore latest --target /srv/mara/restore
install -o mara -g mara -m 0600 /srv/mara/restore/srv/mara/backup/coach.db /srv/mara/data/coach.db
sudo -u mara cp -a /srv/mara/restore/srv/mara/exports/. /srv/mara/exports/
rm -rf /srv/mara/restore
sudo -u mara /srv/mara/app/deploy/linux/deploy.sh
```
Use a snapshot ID instead of `latest` for an older copy. Runs logged since that backup come
back with "sync my runs" (FIT files re-download); fuel and body entries since then are lost.

**Server or Volume lost:** new Droplet and Volume (runbook steps 2-5, the restic password from
the password manager), skip `backup.sh init` (the repository exists), do the real restore
above, then step 8 (COROS sign-in) and step 9.

## Fallback: Option 0, the laptop (installed 2026-10-26)
If the server is down for long, run the bot on the laptop again. **Only one bot at a time**:
stop `mara-slack` first (`systemctl stop mara-slack`), and copy the server DB down
(`coach.sh backup`, then `scp`) so the laptop doesn't answer from stale data.
- `deploy/macos/install.sh` writes a LaunchAgent (`com.maratrainer.slack`): starts at login,
  restarts within 30 s if the bot exits, runs it under `caffeinate -is` so the Mac stays awake on
  power. `deploy/macos/uninstall.sh` removes it. Logs: `~/Library/Logs/mara-trainer/slack.log`.
  After `move-data.sh` the plist is still there:
  `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.maratrainer.slack.plist`.
- **The repo must not live in `~/Desktop`, `~/Documents` or `~/Downloads`.** macOS privacy
  protection (TCC) blocks background jobs from reading those folders; the bot hangs with
  "Operation not permitted". The repo lives in `~/code/mara-trainer`.
- Limits: lid closed without an external display still sleeps; so does battery-only idle once
  the bot isn't running.

## Asleep-proof Slack (2026-10-26)
Scheduling moved to the platform that's always on. The bot queues fixed-time messages on Slack's
servers (`chat.scheduleMessage`) a week ahead, so the 10am debrief ping and a 6pm note with
next week's runs arrive even when the host is down. When it's up, the live check-in replaces
the 6pm fallback (runs synced from COROS, the coach's recap). DMs sent while it was down are
answered when it comes back.

## Options considered (prices checked 2026-10-26)
| Option | Monthly | Data lives | Fits because | Watch out |
|---|---|---|---|---|
| **0. Laptop, made reliable** (fallback) | $0 | laptop | launchd keeps the bot running | lid closed or travel stops it |
| **1. Always-on device at home** | power, one-time hardware | home | same DB over the local network | home internet and power; you maintain it |
| **2a. Fly.io** shared-cpu-1x 256 MB + 1 GB volume | about $2.19 + $0.15/GB | Fly | cheapest always-on | prices rose 1 Oct 2026 |
| **2b. DigitalOcean** Droplet (chosen, 1 GB) | $6 + backups + volume | DO | plain Linux box, simple | Droplet backups skip Volumes (restic covers them) |
| **2c. Hetzner** CX23 | about €4.29-4.49 + VAT | Hetzner | most RAM for the money | prices rose in 2026 |
| **3. PaaS** (Railway Hobby / Render worker) | $5 floor / $7 | vendor | git-push deploys | usage-based extras |

Not done yet: a heartbeat alert (the bot DMs nothing for 24 h around a check-in), and deploys
limited to tagged releases (supported by `deploy.sh vX.Y.Z`; the default is `origin/main`).
