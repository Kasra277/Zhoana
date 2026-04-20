# Railway Persistence Runbook — P-PERSIST-07..14

This document is the operator's checklist for the persistence tests that
**cannot** be proven with pytest — they require a real Railway staging
service, a staging bot token, and an observable Telegram client.

Source plan: `.cursor/plans/railway_persistence_coverage_fb36b62a.plan.md`

Automated tests for §6.1 (P-PERSIST-01..06) live in
[tests/test_p_persist.py](../tests/test_p_persist.py) and run on every CI
invocation of the validation suite. The tests below (§6.2 and §6.3) must
be executed by a human and their artifacts archived under
`validation/evidence/persistence/<P-ID>/`.

---

## Prerequisites

- A dedicated **staging Railway service** deployed from the same `bot.py`
  that is under test. Do not reuse the production service.
- A **staging BotFather token** bound to that service via
  `TELEGRAM_BOT_TOKEN`. Never run these tests with the production token.
- A Telegram chat that has `/start`'d the staging bot and is the sole
  subscriber (so state diffs are unambiguous).
- Railway CLI (`railway`) installed and logged in, OR dashboard access
  with permission to trigger restarts and redeploys.
- Local access to the staging container's `state.json` via the Railway
  CLI file browser or a temporary shell (`railway run` / `railway shell`).
- Evidence folder skeleton pre-created:
  `validation/evidence/persistence/<P-ID>/` for each test.

Before each test, capture the current state as `state_before.json` by
downloading `/app/state.json` from the container (or `/data/state.json`
if a volume is attached).

---

## Shared evidence checklist (per test)

Every scenario requires at minimum:

1. `state_before.json` — snapshot immediately before the action.
2. `state_after.json`  — snapshot immediately after the action.
3. `state.diff`        — `diff --unified state_before.json state_after.json`
                         OR a note "identical" when survival is expected.
4. `railway_event.png` — screenshot of the Railway deploy/restart event.
5. `railway.log`       — at least 50 lines of Railway log output bracketing
                         the event. Must include `Loaded state: users=X`.
6. `telegram_chat.png` — screenshot of the staging Telegram chat showing
                         user-visible behavior (dashboard edited vs new,
                         alerts arriving or not, `/status` responses).
7. `notes.md`          — pass/fail call, timestamps, and any anomaly.

Optional per test:

- `fixture.json` — the exact h2s payload injected (for P-PERSIST-13).
- `timing.csv`   — wall-clock timestamps for multi-step scenarios
                   (P-PERSIST-11, P-PERSIST-14).

---

## P-PERSIST-07 — Railway in-place restart

**Claim under test:** every persisted field survives a `railway redeploy`
action that restarts the SAME container instance.

**Setup:**

- Confirm `state_before.json` contains a configured user with
  `cities=[Amersfoort]`, `min_price=800`, `max_price=1500`, `paused=True`,
  and a non-zero `update_offset`.

**Action:**

1. Trigger `Restart` from the Railway dashboard (not "Redeploy").
2. Wait for the service to come back (Health check passes).

**Expected:**

- State file is byte-identical before and after.
- Railway log shows `Loaded state: users=1 seen=N offset=M first_run=False`
  with the same `N`, `M` as before the restart.
- Dashboard, when reopened via `/start`, edits in place (no new message).

**Pass criterion:** `state.diff` reports "identical".
**Severity:** B (release blocker for the "survives restart" claim).

---

## P-PERSIST-08 — Railway redeploy from same image

**Claim under test:** a redeploy replaces the container and wipes the
writable layer. Every persisted field is lost. This is a documented
limitation, not a defect.

**Setup:** same as P-PERSIST-07.

**Action:**

1. Trigger `Redeploy` (or `railway redeploy` CLI) without pushing code.

**Expected:**

- `state_after.json` shows `users={}`, `update_offset=0`, `seen_ids=[]`.
- Railway log shows `No state file; starting fresh`.
- The staging Telegram user:
  - sees their dashboard **edit attempt fail silently** (stale
    `dashboard_message_id`). The next `/start` creates a NEW dashboard.
  - `/status` shows default filters (`No cities set · price gate off`).
- If a 3-listing fixture is injected post-redeploy, the user receives
  them regardless of the prior `paused=True` setting.

**Pass criterion:** every persisted field is missing (documented loss).
**Severity:** B (release blocker — operator must accept this behavior).

---

## P-PERSIST-09 — Redeploy from new image (git push)

**Claim under test:** identical to P-PERSIST-08; proves that any git-push
auto-deploy has the same persistence effect.

**Action:**

1. Push a trivial commit to the branch Railway deploys from (e.g. a
   single whitespace change in `README.md`).

**Expected:** same as P-PERSIST-08.
**Pass criterion:** state_after matches fresh-boot defaults.
**Severity:** B.

---

## P-PERSIST-10 — Environment variable change triggers rebuild

**Claim under test:** editing an env var on Railway triggers a redeploy
and therefore loses state. Operators must know this before they tweak
`LOG_LEVEL` in production.

**Action:**

1. In the Railway dashboard, change `LOG_LEVEL` from `INFO` to `DEBUG`.
2. Save. Railway will trigger an automatic redeploy.

**Expected:** same loss as P-PERSIST-08.
**Pass criterion:** state reset. Archive a `railway_event.png` showing
the "variable changed → redeploy" chain.
**Severity:** H.

---

## P-PERSIST-11 — Simulated OOM

**Claim under test:** an out-of-memory kill restarts the container but —
on today's Railway behavior — usually reuses the same filesystem. This
is best-effort; record whichever outcome is observed.

**Setup:** before the action, note the Railway plan's memory limit.

**Action:**

1. Attach a temporary shell and run a memory balloon:
   `python -c "a=[0]*(10**9)"` (adjust until Railway kills the container).
2. Record whether Railway's restart counter increments without creating
   a new deploy event.

**Expected (one of):**

- State survives (writable layer reused). Pass with note "OOM survived
  on Railway as of YYYY-MM-DD".
- State lost (writable layer replaced). Pass with note "OOM equivalent
  to redeploy as of YYYY-MM-DD; R1+R2+R4 would manifest in production".

**Pass criterion:** behavior is recorded and dated; the plan's §2 claim
is either confirmed ("best-effort, typically safe") or amended.
**Severity:** H.

---

## P-PERSIST-12 — Volume attached, redeploy

**Claim under test:** attaching a Railway Volume at `/data` and setting
`STATE_FILE=/data/state.json` makes every persisted field survive a
redeploy. This is the upgrade-path proof.

**This test is deferred until a volume is adopted.**

**Setup:**

1. Create a Railway Volume, mount at `/data`.
2. Set env `STATE_FILE=/data/state.json`, redeploy once so the new path
   takes effect. After this, re-seed user settings via Telegram.
3. Capture `state_before.json` (from `/data/state.json`).

**Action:**

1. Trigger `Redeploy` as in P-PERSIST-08.

**Expected:**

- State file byte-identical before and after.
- Dashboard edits in place; all filters intact; no alert replay.

**Pass criterion:** deep-equal state across redeploy.
**Severity:** B (for the volume-adoption release).

---

## P-PERSIST-13 — Field-loss impact matrix (Telegram-observable)

**Claim under test:** the predicted user-visible impact of R1+R2+R3 in
the plan is accurate. Exercised against a staging bot, post-redeploy.

**Fixture:**
[validation/fixtures/persistence/p_persist_13_field_loss.json](fixtures/persistence/p_persist_13_field_loss.json)

**Setup:**

1. Seed a staging user with:
   - `cities = ["Amersfoort"]`
   - `min_price = 800`
   - `max_price = 1500`
   - `paused = True`
2. Capture `state_before.json`.
3. Trigger P-PERSIST-08 (redeploy from same image).
4. Capture `state_after.json`. Confirm user row is gone.

**Action:** inject each fixture item into the staging h2s feed in
sequence (one per cycle), either by running a local h2s mock proxy or by
directly calling `dispatch_new_listings` via a one-shot Railway shell.

**Expected, per item:**

| Item | City | Price | Expected | Proves |
|------|------|-------|----------|--------|
| `p13_amf_1200_in_range` | Amersfoort | 1200 | **Alert delivered** | Paused=True lost (R2) |
| `p13_amf_3000_out_of_range` | Amersfoort | 3000 | **Alert delivered** | max_price=1500 lost (R1) |
| `p13_deventer_1200` | Deventer | 1200 | **No alert** | cities=[] default is failsafe (R3) |

**Pass criterion:** the chat exhibits exactly the behavior above.
Any deviation (e.g. Deventer alert arrives) is a CRITICAL defect.

**Severity:** B.

---

## P-PERSIST-14 — Telegram update replay on offset loss

**Claim under test:** losing `update_offset` causes Telegram to replay
pending updates from the last ~24h. The bot handles them without
crashing; duplicates are bounded to one drain.

**Setup:**

1. Ensure the staging user has a recent non-zero `update_offset`.
2. Send 3 commands (e.g. `/status`, `/status`, `/status`) and observe
   the replies. Capture timestamps in `timing.csv` (column: `sent_at`).

**Action:**

1. Immediately redeploy (P-PERSIST-08).
2. When the service is back, do NOT send new commands. Watch the chat.

**Expected:**

- Within one long-poll cycle (~25-30 s), the 3 `/status` commands re-fire
  and produce 3 new replies.
- After the drain, no further replies arrive.
- `update_offset` in `state_after.json` is now advanced past those
  updates.

**Pass criterion:** exactly 3 replay replies, not more, not fewer.
Replay completes within one drain cycle.
**Severity:** H.

---

## Rollup matrix

| ID  | Scenario | Severity | Evidence folder |
|-----|----------|----------|------------------|
| P-PERSIST-07 | In-place restart | B | `P-PERSIST-07/` |
| P-PERSIST-08 | Redeploy same image | B | `P-PERSIST-08/` |
| P-PERSIST-09 | Redeploy on git push | B | `P-PERSIST-09/` |
| P-PERSIST-10 | Env var change reboot | H | `P-PERSIST-10/` |
| P-PERSIST-11 | Simulated OOM | H | `P-PERSIST-11/` |
| P-PERSIST-12 | Volume attached redeploy | B (when adopted) | `P-PERSIST-12/` |
| P-PERSIST-13 | Field-loss impact | B | `P-PERSIST-13/` |
| P-PERSIST-14 | Offset replay | H | `P-PERSIST-14/` |

A release may ship with P-PERSIST-07, P-PERSIST-08 (documented loss
accepted), P-PERSIST-09, P-PERSIST-10 (documented loss accepted),
P-PERSIST-13, and P-PERSIST-14 complete. P-PERSIST-11 and P-PERSIST-12
are recorded as deferred with dates.

---

## Sign-off

After running each test, update
[validation/RELEASE_GATE.md](RELEASE_GATE.md) with the pass/fail state
and a relative link to the evidence folder.
