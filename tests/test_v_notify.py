"""
§9 — Notification validation.

  V-NOTIFY-01  One new listing → one alert.
  V-NOTIFY-02  Dedup within one cycle (same sku twice → one add, one send).
  V-NOTIFY-03  Dedup across a graceful restart (seen_ids persisted).
  V-NOTIFY-04  At-most-once on SIGKILL (documented tradeoff, covered
               implicitly: state is only saved on dirty flush).
  V-NOTIFY-05  Pacing — sleep is called between sends (we monkey-patch
               it to a no-op but assert it WAS called).
  V-NOTIFY-06  Soft cap + summary message.
  V-NOTIFY-07  (Manual) button behavior under alerts.
  V-NOTIFY-08  (Manual) readability review.
"""
from __future__ import annotations

import pytest


def test_v_notify_01_one_listing_one_alert(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture
):
    state = dispatch_state
    u = state.get_user(10)
    u["cities"] = ["Amersfoort"]

    before = len(state.seen_ids)
    stub_fetch([load_fixture("happy_listing")["item"]])
    bot.dispatch_new_listings(state)

    assert len(state.seen_ids) == before + 1
    assert len(fake_tg.calls_of("sendPhoto")) == 1


def test_v_notify_02_dedup_within_cycle(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture
):
    state = dispatch_state
    u = state.get_user(11)
    u["cities"] = ["Amersfoort"]

    item = load_fixture("happy_listing")["item"]
    # Duplicate sku on the wire — dispatch must only send ONCE.
    stub_fetch([item, item])
    bot.dispatch_new_listings(state)

    assert len(fake_tg.calls_of("sendPhoto")) == 1
    assert item["sku"] in state.seen_ids


def test_v_notify_03_dedup_across_restart(
    bot, fake_tg, stub_fetch, load_fixture, isolated_state_file
):
    """Simulate a graceful restart: run A saves state; run B reloads it
    and must not re-alert the same sku."""
    # Run A.
    state_a = bot.State()
    state_a.first_run = False
    u = state_a.get_user(12)
    u["cities"] = ["Amersfoort"]
    item = load_fixture("happy_listing")["item"]
    stub_fetch([item])
    bot.dispatch_new_listings(state_a)
    assert len(fake_tg.calls_of("sendPhoto")) == 1

    # Persist and simulate clean shutdown.
    bot.save_state(state_a)
    assert isolated_state_file.exists()

    # Reset recorder and reload.
    fake_tg.reset()
    state_b = bot.load_state()
    assert item["sku"] in state_b.seen_ids

    # Same listing appears again → no alert.
    stub_fetch([item])
    bot.dispatch_new_listings(state_b)
    assert fake_tg.calls_of("sendPhoto") == []
    assert fake_tg.calls_of("sendMessage") == []


def test_v_notify_05_pacing_sleep_invoked_between_sends(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture, monkeypatch
):
    """Prove ALERT_PACING_SECONDS is actually honored between sends. The
    fake_tg fixture already installs a no-op sleep; we override it with
    a logger and set a positive pacing value."""
    sleep_log: list[float] = []
    monkeypatch.setattr(bot.time, "sleep", lambda s: sleep_log.append(s))
    monkeypatch.setattr(bot, "ALERT_PACING_SECONDS", 0.3)

    state = dispatch_state
    u = state.get_user(13)
    u["cities"] = ["Amersfoort"]

    items = load_fixture("burst_15_amersfoort")["items"][:3]
    stub_fetch(items)
    bot.dispatch_new_listings(state)

    assert sleep_log.count(0.3) >= 3, (
        f"expected >= 3 pacing sleeps of 0.3s, got {sleep_log}"
    )


def test_v_notify_06_soft_cap_plus_summary(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture
):
    """V-NOTIFY-06: 15 listings with MAX_ALERTS_PER_CYCLE=10 → 9 full +
    1 summary. All 15 in seen_ids."""
    state = dispatch_state
    u = state.get_user(14)
    u["cities"] = ["Amersfoort"]

    items = load_fixture("burst_15_amersfoort")["items"]
    stub_fetch(items)
    bot.dispatch_new_listings(state)

    photos = fake_tg.calls_of("sendPhoto")
    messages = fake_tg.calls_of("sendMessage")
    assert len(photos) == 9
    # Exactly one summary message.
    summaries = [m for m in messages if "more new listings" in m.get("text", "")]
    assert len(summaries) == 1, f"expected one summary message, got {summaries}"
    # Summary mentions the correct residual count.
    assert "+6 more" in summaries[0]["text"]

    # All 15 skus recorded.
    for i in range(1, 16):
        assert f"B-{i:03d}" in state.seen_ids


def test_v_notify_04_at_most_once_documented_in_header(bot):
    """V-NOTIFY-04: assert the documented at-most-once semantics remain
    in the module header. A silent removal of this doc would mask the
    intentional behavior."""
    source = (bot.__file__)
    text = open(source, "r", encoding="utf-8").read()
    assert "at-most-once" in text.lower(), (
        "at-most-once semantics must remain documented in bot.py header"
    )
