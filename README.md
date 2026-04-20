# 🏠 Holland2Stay Telegram Alerts

Notifies you on Telegram whenever a new apartment becomes **available to book**
on [holland2stay.com](https://www.holland2stay.com/residences) in cities you
care about, within your price range.

Each user chooses their own cities and price range by talking to the bot —
the bot handles multiple subscribers.

Supported cities out-of-the-box:
**Deventer · Nijmegen · Amersfoort · Enschede · Zwolle · Zutphen · Arnhem**

Runs for free on GitHub Actions, polling every 5 minutes.

---

## One-time setup (≈ 10 minutes)

### 1. Create a GitHub account (if you don't have one)

Go to <https://github.com> and sign up. It's free.

### 2. Create a new repository

- Click the **+** in the top-right → **New repository**
- Name it anything, e.g. `h2s-bot`
- Set it to **Private** (recommended — your state file is visible to anyone who can see the repo)
- Tick **Add a README file** — then click **Create repository**

### 3. Upload these files

In your new repo:

- Click **Add file → Upload files**
- Drag **all** files from the downloaded bundle into the browser:
  - `bot.py`
  - `requirements.txt`
  - `state.json`
  - `.gitignore`
  - `README.md`
  - the `.github/workflows/monitor.yml` file (keep the folder structure!)
- Click **Commit changes**

> 💡 The easiest way: drag the **entire folder** into GitHub's upload page — it preserves the folder structure.

### 4. Add your bot token as a secret

- In your repo → **Settings** (top bar) → **Secrets and variables → Actions** (left sidebar)
- Click **New repository secret**
- Name: `TELEGRAM_BOT_TOKEN`
- Value: paste the **new** token you got from @BotFather after revoking
- Click **Add secret**

### 5. Enable Actions

- Click the **Actions** tab in your repo
- If prompted, click **I understand my workflows, go ahead and enable them**
- You should see **Holland2Stay Monitor** listed — click it, then **Run workflow** (on the right) to trigger the first run immediately.

### 6. Talk to your bot

- On your phone, open Telegram and go to `t.me/Zhoana2stay_bot`
- Send `/start`
- Send `/cities` → tap the cities you want, tap **✓ Done**
- Send `/price` → reply with your range, e.g. `800-1500`
- Send `/status` to confirm

You're done. Every 5 minutes GitHub will poll Holland2Stay and ping you when something matching shows up. 🎉

---

## What the bot does on each run

1. Fetches any new Telegram messages you sent (commands) and updates your filters.
2. Queries Holland2Stay's GraphQL API for listings currently available to book.
3. Compares against `state.json` (already-seen listings). First run seeds the list silently so you don't get spammed with 50 alerts for existing listings.
4. For each genuinely new listing, sends a message to every subscribed user whose filters match.
5. Commits the updated `state.json` back to the repo.

## Commands the bot understands

| Command   | What it does                                    |
|-----------|-------------------------------------------------|
| `/start`  | Welcome + instructions                          |
| `/cities` | Pick which cities to watch (toggleable buttons) |
| `/price`  | Set your price range, e.g. `800-1500` or `1500` |
| `/status` | Show your current filters                       |
| `/pause`  | Stop receiving alerts                           |
| `/resume` | Resume alerts                                   |
| `/help`   | Show the command list                           |

## Troubleshooting

**Nothing happens after I send `/start`.**
GitHub Actions runs every 5 minutes, so there's up to a 5-minute delay before the bot "hears" you. Give it a moment. You can speed up the first run by going to Actions → Holland2Stay Monitor → Run workflow.

**I want alerts faster than 5 minutes.**
GitHub Actions' cron minimum is 5 minutes. For faster polling (60s) you need to move to a cheap VPS (Hetzner CAX11 ≈ €3.29/mo, or Oracle Cloud's always-free tier). Ask and I'll give you the systemd service file.

**I got lots of alerts at once the first time alerts started working.**
The first successful run seeds state silently — you should not get a flood. If you do, it means the previous run wrote empty state; just `/pause`, let one cycle pass, then `/resume`.

**A listing got me an alert but doesn't match my city.**
The bot falls back to matching city names in the listing title/URL if Holland2Stay's schema doesn't expose `city` as a queryable field. If false positives are common, report the listing name and I'll tighten the match.

**I want to share the bot with a friend.**
Just send them `t.me/Zhoana2stay_bot`. They `/start` it, set their own filters, and they get their own independent alerts. The bot already supports multiple users.

## Security notes

- Your bot token lives in GitHub Actions secrets — never in the code, never in logs.
- Anyone who can read your repo can see who's subscribed (their chat IDs, filters). Keep the repo **Private** unless you want that public.
- The bot only sends messages; it never reads your other Telegram chats.

---

## Running on Railway — persistence limitations (IMPORTANT)

The always-on build of this bot is designed for Railway (or an equivalent
always-on container platform). Before deploying, understand exactly what
`state.json` survives and what it does not. This bot stores every user's
cities, price range, paused/running state, Telegram update offset, and
dedup set in a single local JSON file. On Railway's default (ephemeral)
filesystem, that file behaves as follows:

**Survives:**

- Normal process restarts inside the same container instance.
- Typical crash / OOM recoveries (best-effort, not a contractual guarantee).

**Lost — resets to defaults on next boot:**

- Manual redeploys.
- Git-push auto-deploys (any new image build).
- Environment-variable changes (Railway redeploys when env changes).
- Region migrations or platform-initiated container replacements.

**When `state.json` is lost, each user's state returns to defaults. This has
real user-visible effects:**

- Cities list empties → user receives no alerts until they reopen the
  dashboard and re-select cities. (Failsafe: no spurious alerts.)
- Price range resets to `0 – 0` (gate disabled) → user may receive
  listings outside their previous range until they re-enter `/price`.
- Paused users become active → previously-muted users start receiving
  alerts again.
- Telegram buffered commands from the last ~24 h may be replayed once.
- Dedup list resets — protected from mass re-alert storms by the
  first-run seed guard; a small number of currently-visible listings
  may be re-marked without alerting.

**What to do about it:**

1. Recommended: attach a **Railway Volume** (e.g. mount path `/data`) and
   set the service env `STATE_FILE=/data/state.json`. This makes every
   field above survive redeploys and rebuilds. Cost is minimal.
2. Always run this service with a **single replica**. Multiple replicas
   double-poll Telegram (returns `409 Conflict`) and corrupt dedup.
3. Do not rely on `state.json` across redeploys for any user-facing
   guarantee. Communicate the above to users if the bot is public.

See [validation/PERSISTENCE_RUNBOOK.md](validation/PERSISTENCE_RUNBOOK.md)
for the full persistence coverage plan and evidence-driven validation
procedure used to verify these claims.
