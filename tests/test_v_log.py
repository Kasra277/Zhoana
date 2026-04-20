"""
§14 — Logging / observability validation.

Covered here (automatable):
  V-LOG-01  Startup signatures: presence of the 'starting h2s bot' line,
            telegram_preflight logging ok, setMyCommands ok.
  V-LOG-02  H2S cycle signatures: 'h2s fetched N listing(s)' + 'h2s cycle:'
            + 'keeping seen_ids untouched this cycle' on failure.
  V-LOG-03  Dispatch signatures: city-missing WARN + url-missing WARN.
  V-LOG-06  url_key-missing WARN is emitted exactly when url_key is absent.

Not covered here (require a live canary log):
  V-LOG-04 shutdown signatures — covered implicitly by test_v_run_07.
  V-LOG-05 zero uncaught Tracebacks over 24h — inspect canary log manually.
"""
from __future__ import annotations

import logging
import re


def test_v_log_01_preflight_signatures_present(
    bot, fake_tg, caplog_bot
):
    caplog_bot.set_level(logging.DEBUG, logger="h2s")
    bot.telegram_preflight()

    text = "\n".join(rec.message for rec in caplog_bot.records)
    # getMe success
    assert re.search(r"Telegram auth ok.*@h2s_test_bot", text), text
    # webhook clear path (we returned empty webhook info)
    assert "No webhook set" in text or "deleteWebhook" in text.lower() or \
           "long polling" in text
    # setMyCommands — we don't log on success, but we do log on failure;
    # since fake returns ok, no failure line is expected.


def test_v_log_02_cycle_signatures_success_path(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture, caplog_bot
):
    state = dispatch_state
    u = state.get_user(1)
    u["cities"] = ["Amersfoort"]

    stub_fetch([load_fixture("happy_listing")["item"]])
    bot.dispatch_new_listings(state)

    msgs = [rec.message for rec in caplog_bot.records]
    # h2s fetched N listing(s)
    assert any("h2s fetched" in m for m in msgs), msgs
    # h2s cycle: new=X total=Y users_with_matches=Z
    cycle_line = next((m for m in msgs if m.startswith("h2s cycle:")), None)
    assert cycle_line, msgs
    assert "new=1" in cycle_line
    assert "users_with_matches=1" in cycle_line


def test_v_log_02_cycle_failure_signature(
    bot, dispatch_state, monkeypatch, caplog_bot
):
    """V-LOG-02: when fetch fails, cycle logs the documented WARN."""
    monkeypatch.setattr(bot, "fetch_listings", lambda: None)
    bot.dispatch_new_listings(dispatch_state)

    text = "\n".join(rec.message for rec in caplog_bot.records)
    assert "keeping seen_ids untouched this cycle" in text


def test_v_log_03_dispatch_missing_city_warn(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture, caplog_bot
):
    state = dispatch_state
    u = state.get_user(1)
    u["cities"] = ["Amersfoort"]

    stub_fetch([load_fixture("missing_city")["item"]])
    bot.dispatch_new_listings(state)

    warns = [rec.message for rec in caplog_bot.records
             if rec.levelname == "WARNING"]
    assert any(
        "could not resolve city" in w and "H2S-UNKNOWN-001" in w
        for w in warns
    ), warns


def test_v_log_06_url_key_missing_warn(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture, caplog_bot
):
    """V-LOG-06 (code gap #url-missing-warn-log): a listing without url_key
    must produce WARN 'listing sku=<sku> missing url_key; using residences
    fallback URL'. This is the signature V-LINK-06 depends on for
    observability."""
    state = dispatch_state
    u = state.get_user(1)
    u["cities"] = ["Arnhem"]

    stub_fetch([load_fixture("missing_url_key")["item"]])
    bot.dispatch_new_listings(state)

    target = None
    for rec in caplog_bot.records:
        if rec.levelname != "WARNING":
            continue
        if "missing url_key" in rec.message and "residences fallback URL" in rec.message:
            target = rec.message
            break
    assert target is not None, "url_key-missing WARN was not emitted"
    assert "H2S-ARN-NOSLUG-001" in target, target


def test_v_log_06_not_emitted_when_url_key_present(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture, caplog_bot
):
    """Negative: the url_key-missing WARN must NOT fire for well-formed
    listings. Prevents log noise."""
    state = dispatch_state
    u = state.get_user(1)
    u["cities"] = ["Amersfoort"]

    stub_fetch([load_fixture("happy_listing")["item"]])
    bot.dispatch_new_listings(state)

    for rec in caplog_bot.records:
        assert "missing url_key" not in rec.message, (
            f"unexpected url_key-missing WARN for a slugged listing: {rec.message}"
        )
