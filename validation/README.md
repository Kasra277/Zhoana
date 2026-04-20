# h2s Bot — Validation Runbook

Per-release operator guide for executing the Master Validation Plan.
See the plan document for scenario detail and pass/fail criteria.

---

## 0. Directory layout

```
validation/
  README.md              (this file)
  RELEASE_GATE.md        (sign-off template, one per release)
  fixtures/              (synthetic GraphQL payloads used by tests)
    happy_listing.json
    missing_url_key.json
    missing_price.json
    ...
  evidence/              (per-release artifact folders)
    <release-tag>/
      v-city/
        v-city-01.png
        v-city-01.state.before.json
        v-city-01.state.after.json
        ...
      v-price/
      v-link/
      ...
  reports/
    pytest_report_<release-tag>.txt
```

Each validation area has a folder under `evidence/<release-tag>/` with the
numbered test IDs as filename prefixes.

---

## 1. The four environments

| Env                    | How to run                                              | Purpose                                                            |
| ---------------------- | ------------------------------------------------------- | ------------------------------------------------------------------ |
| Local (unit/logic)     | `pytest tests/` from repo root                          | Blocker logic: matches_user, listing_url, captions, state, dispatch |
| Local (live-Telegram)  | `TELEGRAM_BOT_TOKEN=<sandbox> python bot.py`            | Real Telegram UX: buttons, toasts, photo delivery                  |
| Staging (Railway)      | Railway deploy on `staging` bot token                    | Always-on behavior, graceful shutdown, webhook preflight, logs     |
| Canary (>= 24h)        | Railway deploy on production bot token                   | No restart loops, no duplicate alerts, memory stability            |

Every environment uses an isolated `STATE_FILE` path. Never reuse the
production `state.json`.

---

## 2. Running the automated suite (local / CI)

Prerequisites:

- Python 3.11+
- `pip install -r requirements.txt -r requirements-dev.txt`

Commands (PowerShell):

```powershell
$env:TELEGRAM_BOT_TOKEN = "test-harness-token"
pytest tests/ -v --tb=short
```

Or with a formatted report file:

```powershell
$env:TELEGRAM_BOT_TOKEN = "test-harness-token"
pytest tests/ -v --tb=short | Tee-Object -FilePath "validation/reports/pytest_report_$(Get-Date -Format yyyyMMdd_HHmmss).txt"
```

Expected result: every test passes. Any failure is documented as a
defect in that release's gate. See [RELEASE_GATE.md](RELEASE_GATE.md).

### What the automated suite covers (blockers)

- V-CITY-07, V-CITY-08 (filter muting; per-user routing)
- V-PRICE-08, V-PRICE-09 (inside-range vs outside-range)
- V-MATCH-01 through V-MATCH-08 (every branch of matches_user)
- V-LINK-01 through V-LINK-06 (including the full-cycle no-silent-fallback scan)
- V-IMG-01 through V-IMG-08 (every step of the photo-first fallback chain)
- V-NOTIFY-01, V-NOTIFY-02, V-NOTIFY-03, V-NOTIFY-05, V-NOTIFY-06 (dedup,
  pacing, soft cap)
- V-STATE-01 through V-STATE-08 (atomic write, corrupt quarantine, bounded
  seen_ids, schema tolerance, at-most-once offset)
- V-LOG-02, V-LOG-03 (log-signature assertions executed against recorded
  handler output)
- V-LOG-06 (url_key-missing warning emitted on the full link-cycle fixture)

### What the automated suite does NOT cover (must be manual)

- All V-UX scenarios that require clicking a real inline button in Telegram.
- V-RUN-01, V-RUN-02, V-RUN-07, V-RUN-08 — require a live process, SIGTERM,
  and Railway deploy.
- V-NOTIFY-07 — Telegram-side button behavior.
- V-IMG-07 — visual match between photo and listing page.
- V-LIVE-01, V-LIVE-02, V-LIVE-03 — require 24h observation of a real chat.
- V-NOTIFY-08 — qualitative readability review.

These are executed by the human tester against the sandbox bot per the
scenario descriptions in the plan.

---

## 3. Manual runbook (live-Telegram sandbox)

1. Create a throwaway BotFather token. Keep a note in the evidence folder.
2. Deploy `bot.py` locally with:
   ```powershell
   $env:TELEGRAM_BOT_TOKEN = "<sandbox-token>"
   $env:STATE_FILE = "validation/sandbox_state.json"
   $env:LOG_LEVEL = "DEBUG"
   python bot.py 2>&1 | Tee-Object -FilePath "validation/sandbox.log"
   ```
3. Execute each manual scenario in order. For each, capture:
   - Screenshot in `evidence/<tag>/<area>/<test-id>.png`
   - Relevant log excerpt in `evidence/<tag>/<area>/<test-id>.log`
   - State snapshot in `evidence/<tag>/<area>/<test-id>.state.json`
4. On completion, update [RELEASE_GATE.md](RELEASE_GATE.md) with pass/fail
   and paths to evidence.

---

## 4. Canary runbook (Railway / always-on)

1. Deploy bot.py with real bot token.
2. Capture:
   - `evidence/<tag>/v-run/railway_uptime.png` after 24h.
   - `evidence/<tag>/v-run/railway_logs_24h.txt` full log dump.
   - `evidence/<tag>/v-run/state_before.json` at start of window.
   - `evidence/<tag>/v-run/state_after.json` at end of window.
3. Grep the log for `Traceback|Exception|ERROR` and attach grep output.
4. Grep for required signatures (see plan §14):
   - `starting h2s bot`
   - `telegram preflight: getMe ok`
   - `telegram preflight: webhook cleared`
   - `setMyCommands`
   - `h2s fetched`
   - `h2s cycle:`
   - `telegram loop started` / `telegram loop stopped`
5. Send `SIGTERM` via Railway restart; capture the graceful-shutdown log
   excerpt.

---

## 5. Defect handling

Any failing blocker = release is held. Open a defect in your tracker with:

- Test ID
- Evidence artifact paths
- Observed vs. expected
- Suspected component

Fix, re-run the automated suite, and re-execute only the impacted manual
tests. Re-stamp [RELEASE_GATE.md](RELEASE_GATE.md).
