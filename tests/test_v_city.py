"""
§4 — City selection validation.

Automatable scenarios:
  V-CITY-01..06  — callback-router level (single, multi, remove, cancel,
                   select-all/clear, duplicate-tap idempotence).
  V-CITY-07      — empty selection mutes (matches_user + dispatch proof).
  V-CITY-08      — monitoring proof: only selected city produces an alert,
                   non-matching listings still enter seen_ids.

Manual-only portions (see validation/README.md §3):
  Screenshot evidence of toggle checkmarks, toast text, dashboard re-render.
"""
from __future__ import annotations

import pytest


# ─────────────────────── V-CITY callback tests ────────────────────
def _make_cb(data: str, chat_id: int, msg_id: int, cb_id: str = "cb") -> dict:
    return {
        "update_id": 1,
        "callback_query": {
            "id": cb_id,
            "data": data,
            "message": {"chat": {"id": chat_id}, "message_id": msg_id},
        },
    }


def _get_user(state, bot, chat_id: int, dashboard_id: int) -> dict:
    u = state.get_user(chat_id)
    u["dashboard_message_id"] = dashboard_id
    u["screen"] = bot.SCR_CITIES
    u["pending"]["cities"] = list(u["cities"])
    return u


def test_v_city_01_single_city_selection(bot, fresh_state, fake_tg):
    """V-CITY-01: tap one city → selected → Save → users[chat].cities = [city]."""
    chat_id, dash_id = 111, 500
    state = fresh_state
    user = _get_user(state, bot, chat_id, dash_id)

    bot.handle_callback(_make_cb("city:Amersfoort", chat_id, dash_id), state)
    assert user["pending"]["cities"] == ["Amersfoort"]

    bot.handle_callback(_make_cb("cities:save", chat_id, dash_id), state)
    assert user["cities"] == ["Amersfoort"]
    assert user["pending"]["cities"] is None


def test_v_city_02_multi_city_selection(bot, fresh_state, fake_tg):
    """V-CITY-02: three toggles in a row, each recorded."""
    chat_id, dash_id = 111, 500
    state = fresh_state
    user = _get_user(state, bot, chat_id, dash_id)

    for city in ("Amersfoort", "Arnhem", "Deventer"):
        bot.handle_callback(_make_cb(f"city:{city}", chat_id, dash_id), state)

    bot.handle_callback(_make_cb("cities:save", chat_id, dash_id), state)
    # cities list preserves ALLOWED_CITIES ordering
    assert user["cities"] == ["Amersfoort", "Arnhem", "Deventer"]


def test_v_city_03_removing_a_city(bot, fresh_state, fake_tg):
    """V-CITY-03: toggling a selected city removes it."""
    chat_id, dash_id = 111, 500
    state = fresh_state
    user = state.get_user(chat_id)
    user["cities"] = ["Amersfoort", "Arnhem"]
    user["dashboard_message_id"] = dash_id
    user["screen"] = bot.SCR_CITIES
    user["pending"]["cities"] = list(user["cities"])

    bot.handle_callback(_make_cb("city:Arnhem", chat_id, dash_id), state)
    bot.handle_callback(_make_cb("cities:save", chat_id, dash_id), state)
    assert user["cities"] == ["Amersfoort"]


def test_v_city_04_cancel_discards_pending(bot, fresh_state, fake_tg):
    """V-CITY-04: Cancel must leave the committed cities unchanged."""
    chat_id, dash_id = 111, 500
    state = fresh_state
    user = state.get_user(chat_id)
    user["cities"] = ["Zwolle"]
    user["dashboard_message_id"] = dash_id
    user["screen"] = bot.SCR_CITIES
    user["pending"]["cities"] = list(user["cities"])

    bot.handle_callback(_make_cb("city:Amersfoort", chat_id, dash_id), state)
    bot.handle_callback(_make_cb("city:Arnhem", chat_id, dash_id), state)
    bot.handle_callback(_make_cb("cities:cancel", chat_id, dash_id), state)

    assert user["cities"] == ["Zwolle"]
    assert user["pending"]["cities"] is None


def test_v_city_05_select_all_and_clear(bot, fresh_state, fake_tg):
    """V-CITY-05: Select all → every allowed city; Clear → empty."""
    chat_id, dash_id = 111, 500
    state = fresh_state
    user = _get_user(state, bot, chat_id, dash_id)

    bot.handle_callback(_make_cb("cities:all", chat_id, dash_id), state)
    assert sorted(user["pending"]["cities"]) == sorted(bot.ALLOWED_CITIES)

    bot.handle_callback(_make_cb("cities:save", chat_id, dash_id), state)
    assert sorted(user["cities"]) == sorted(bot.ALLOWED_CITIES)

    # Round-trip: clear and save.
    user["pending"]["cities"] = list(user["cities"])
    bot.handle_callback(_make_cb("cities:clear", chat_id, dash_id), state)
    bot.handle_callback(_make_cb("cities:save", chat_id, dash_id), state)
    assert user["cities"] == []


def test_v_city_06_duplicate_tap_is_idempotent_toggle(bot, fresh_state, fake_tg):
    """V-CITY-06: two rapid taps on the same city end in the opposite
    of the starting state — i.e. the toggle is clean and never crashes."""
    chat_id, dash_id = 111, 500
    state = fresh_state
    user = _get_user(state, bot, chat_id, dash_id)

    bot.handle_callback(_make_cb("city:Nijmegen", chat_id, dash_id), state)
    bot.handle_callback(_make_cb("city:Nijmegen", chat_id, dash_id), state)
    assert user["pending"]["cities"] == []


# ─────────────────────── V-CITY-07 / 08 filtering ────────────────
def test_v_city_07_empty_selection_mutes_alerts(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture, caplog_bot
):
    """V-CITY-07: user saved empty cities → matches_user returns False →
    no send calls, even though a listing would have matched otherwise."""
    state = dispatch_state
    u = state.get_user(123)
    u["cities"] = []  # explicitly muted
    u["dashboard_message_id"] = 900

    listing = load_fixture("happy_listing")["item"]
    stub_fetch([listing])

    bot.dispatch_new_listings(state)

    # No sends at all.
    assert fake_tg.calls_of("sendPhoto") == []
    assert fake_tg.calls_of("sendMessage") == []
    # Listing is in seen_ids so the next cycle won't re-process it.
    assert listing["sku"] in state.seen_ids


def test_v_city_08_monitoring_proof_only_selected_city_alerted(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture, caplog_bot
):
    """V-CITY-08: chat subscribed to {Amersfoort}. Three listings injected
    across three cities. Exactly one alert → Amersfoort listing; the other
    two entered seen_ids without producing a send call."""
    state = dispatch_state
    u = state.get_user(777)
    u["cities"] = ["Amersfoort"]
    u["min_price"] = 0
    u["max_price"] = 0

    cycle = load_fixture("multi_city_cycle")["items"]
    stub_fetch(cycle)

    bot.dispatch_new_listings(state)

    photos = fake_tg.calls_of("sendPhoto")
    messages = fake_tg.calls_of("sendMessage")
    assert len(photos) == 1, f"expected exactly one photo alert, got {len(photos)}"
    assert messages == [], f"no message alerts expected, got {messages}"

    # The alert must be for the Amersfoort listing (captions show the city).
    assert "Amersfoort" in photos[0]["caption"]
    assert photos[0]["chat_id"] == 777

    # All three listings seen.
    for sku in ("MC-AMF-001", "MC-DEV-001", "MC-NIJ-001"):
        assert sku in state.seen_ids
