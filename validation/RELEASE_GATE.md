# h2s Bot — Release Gate

Copy this file to `validation/evidence/<release-tag>/RELEASE_GATE.md` for
every release candidate. Fill in each row with pass/fail + a path to
the evidence artifact.

---

Release tag: `________________`
Date: `________________`
Tester: `________________`
Canary window: `________________` (start → end, ≥ 24h required)

## 0. Code gap fixes applied before validation

- [ ] **url-missing-warn-log** — `dispatch_new_listings` emits
      `WARN listing sku=<sku> missing url_key; using residences fallback URL`
      when the listing is missing a slug. Required for V-LINK-06 / V-LOG-06.
      Status in this release: ☑ already merged (see commit adding
      `"missing url_key; using residences fallback URL"` to `bot.py`).
- [ ] **batch-dedup-fix** — `dispatch_new_listings` deduplicates SKUs
      within a single fetch batch (the `batch_seen` set), so a duplicate
      in the wire payload cannot produce two alerts. Required for
      V-NOTIFY-02. Status: ☑ already merged.

## 1. Automated suite — `pytest tests/`

Command executed: `pytest tests/ -v --tb=short`
Report file: `evidence/<tag>/reports/pytest_report.txt`
Result: ☐ all pass ☐ failures (list below)

| Area     | Test file              | Pass/fail | Notes |
| -------- | ---------------------- | --------- | ----- |
| Cities   | test_v_city.py         |           |       |
| Price    | test_v_price.py        |           |       |
| Match    | test_v_match.py        |           |       |
| Link     | test_v_link.py         |           |       |
| Image    | test_v_img.py          |           |       |
| Notify   | test_v_notify.py       |           |       |
| UX       | test_v_ux.py           |           |       |
| Run      | test_v_run.py          |           |       |
| State    | test_v_state.py        |           |       |
| Live     | test_v_live.py         |           |       |
| Log      | test_v_log.py          |           |       |
| Persist  | test_p_persist.py      |           |       |

## 2. Manual — live-Telegram sandbox

| ID           | Scenario                              | Evidence file              | Pass |
| ------------ | ------------------------------------- | -------------------------- | ---- |
| V-UX-01      | /start dashboard lifecycle            | evidence/<tag>/v-ux/01.png |      |
| V-UX-03      | Cities flow smooth                    | evidence/<tag>/v-ux/03.mp4 |      |
| V-UX-04      | Price flow smooth                     | evidence/<tag>/v-ux/04.mp4 |      |
| V-UX-09      | Deleted dashboard recovery            | evidence/<tag>/v-ux/09.png |      |
| V-UX-10      | Native-feel qualitative review        | evidence/<tag>/v-ux/10.md  |      |
| V-NOTIFY-07  | Button behavior under alerts          | evidence/<tag>/v-notify/07.mp4 |  |
| V-NOTIFY-08  | Readability review (10 real alerts)   | evidence/<tag>/v-notify/08.md  |  |
| V-IMG-07     | Photo matches listing (visual)        | evidence/<tag>/v-img/07.png    |  |

## 3. Manual — staging / canary

| ID         | Scenario                                  | Evidence                        | Pass |
| ---------- | ----------------------------------------- | ------------------------------- | ---- |
| V-RUN-01   | ≥ 24h uptime, one PID throughout          | evidence/<tag>/v-run/uptime.png |      |
| V-RUN-02   | Zero restart loop                          | evidence/<tag>/v-run/restarts.png |    |
| V-RUN-06   | 409 Conflict recovery                      | evidence/<tag>/v-run/409.log    |      |
| V-RUN-07   | SIGTERM graceful flush                     | evidence/<tag>/v-run/sigterm.txt |     |
| V-RUN-08   | Webhook preflight cleared old webhook      | evidence/<tag>/v-run/preflight.log |   |
| V-LIVE-01  | Visible running state for 30min           | evidence/<tag>/v-live/01.mp4    |      |
| V-LIVE-02  | Last check / Next check timestamps        | evidence/<tag>/v-live/02.png    |      |
| V-LIVE-03  | Zero new chat messages in 24h no-match    | evidence/<tag>/v-live/03.png    |      |
| V-LOG-05   | No uncaught exceptions in canary log      | evidence/<tag>/v-log/grep.txt   |      |
| V-STATE-02 | mid-write SIGKILL does not corrupt        | evidence/<tag>/v-state/02.txt   |      |
| V-STATE-03 | Corrupt quarantine seen in log           | evidence/<tag>/v-state/03.txt   |      |
| V-STATE-07 | Ephemeral disk caveat in operator runbook | evidence/<tag>/v-state/07.md    |      |
| V-STATE-08 | Offset durability at-most-once            | evidence/<tag>/v-state/08.log   |      |

## 3b. Railway persistence coverage (P-PERSIST-*)

Source: `.cursor/plans/railway_persistence_coverage_fb36b62a.plan.md`.
Runbook: [PERSISTENCE_RUNBOOK.md](PERSISTENCE_RUNBOOK.md).

| ID            | Scenario                               | Severity | Evidence                                   | Pass |
| ------------- | -------------------------------------- | -------- | ------------------------------------------ | ---- |
| P-PERSIST-01  | Full field round-trip (pytest)         | H        | pytest_report.txt                          |      |
| P-PERSIST-02  | Mid-write crash, live file intact      | B        | pytest_report.txt                          |      |
| P-PERSIST-03  | Corrupt JSON quarantine + recovery     | B        | pytest_report.txt                          |      |
| P-PERSIST-04  | Type-drift tolerance (documented)      | M        | pytest_report.txt                          |      |
| P-PERSIST-05  | Bounded seen_ids FIFO across reload    | H        | pytest_report.txt                          |      |
| P-PERSIST-06  | Debounce-window loss (at-most-once)    | M        | pytest_report.txt                          |      |
| P-PERSIST-07  | Railway in-place restart               | B        | evidence/<tag>/persistence/P-PERSIST-07/   |      |
| P-PERSIST-08  | Railway redeploy loses state (accept)  | B        | evidence/<tag>/persistence/P-PERSIST-08/   |      |
| P-PERSIST-09  | Redeploy on git push                   | B        | evidence/<tag>/persistence/P-PERSIST-09/   |      |
| P-PERSIST-10  | Env-var change triggers redeploy       | H        | evidence/<tag>/persistence/P-PERSIST-10/   |      |
| P-PERSIST-11  | Simulated OOM (record outcome + date)  | H        | evidence/<tag>/persistence/P-PERSIST-11/   |      |
| P-PERSIST-12  | Volume attached survives redeploy      | B*       | evidence/<tag>/persistence/P-PERSIST-12/   |      |
| P-PERSIST-13  | Field-loss impact matrix (R1,R2,R3)    | B        | evidence/<tag>/persistence/P-PERSIST-13/   |      |
| P-PERSIST-14  | Telegram update replay on offset loss  | H        | evidence/<tag>/persistence/P-PERSIST-14/   |      |

*B when a volume is attached; defer with a dated note when not.

Persistence docs gate:

- [ ] `bot.py` module docstring contains the Railway persistence warning
      block ("Persistence on Railway (IMPORTANT — read before deploying)").
- [ ] `README.md` contains the "Running on Railway — persistence limitations"
      section.
- [ ] Operator acknowledged the field-loss impact from P-PERSIST-13 as
      acceptable for this release OR a Railway Volume is attached and
      P-PERSIST-12 is signed off.

## 4. Blocker decision

All severity-B (release-blocker) tests from §15 of the plan must pass:

- [ ] V-CITY-07, V-CITY-08
- [ ] V-PRICE-08, V-PRICE-09
- [ ] V-MATCH-04, V-MATCH-08
- [ ] V-LINK-01, V-LINK-03, V-LINK-04, V-LINK-06
- [ ] V-IMG-01, V-IMG-04, V-IMG-05, V-IMG-06, V-IMG-07 (manual)
- [ ] V-NOTIFY-01, V-NOTIFY-02, V-NOTIFY-03, V-NOTIFY-07 (manual)
- [ ] V-UX-01, V-UX-05, V-UX-08, V-UX-09
- [ ] V-RUN-01, V-RUN-02, V-RUN-04, V-RUN-05, V-RUN-06, V-RUN-07, V-RUN-08
- [ ] V-STATE-02, V-STATE-03, V-STATE-06, V-STATE-07, V-STATE-08
- [ ] V-LIVE-01, V-LIVE-02, V-LIVE-03
- [ ] V-LOG-01…V-LOG-06
- [ ] P-PERSIST-02, P-PERSIST-03, P-PERSIST-07, P-PERSIST-08,
      P-PERSIST-09, P-PERSIST-13 (P-PERSIST-12 instead of 08 if volume)

## 5. Sign-off

I attest that every item above is checked, every evidence artifact
exists and was inspected, and no uncaught exceptions appear in the
canary log.

Tester signature: `________________`
Approver signature: `________________`
Date: `________________`
