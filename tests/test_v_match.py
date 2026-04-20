"""
§6 — Matching logic validation.

matches_user() is the single choke point for whether a listing alerts a
user. These tests exercise every branch:
  V-MATCH-01  paused user rejected
  V-MATCH-02  empty cities rejected
  V-MATCH-03  case-insensitive city match
  V-MATCH-04  missing city handling + WARN log signature
  V-MATCH-05  inferred city from url_key
  V-MATCH-06  missing price skips the price gate and caption says 'price on site'
  V-MATCH-07  zero min/max disables the gate
  V-MATCH-08  combined city × price exhaustive grid
"""
from __future__ import annotations

import pytest


def test_v_match_01_paused_user_rejected(bot, make_user):
    u = make_user(cities=["Amersfoort"], min_price=0, max_price=0, paused=True)
    assert bot.matches_user("Amersfoort", 1000.0, u) is False


def test_v_match_02_empty_cities_rejected(bot, make_user):
    u = make_user(cities=[])
    assert bot.matches_user("Amersfoort", 1000.0, u) is False


def test_v_match_03_city_case_insensitive(bot, make_user, load_fixture):
    u = make_user(cities=["Amersfoort"])
    item = load_fixture("case_insensitive_city")["item"]
    city = bot.listing_city(item)
    assert city.lower() == "amersfoort"
    price = bot.listing_price(item)
    assert bot.matches_user(city, price, u) is True


def test_v_match_04_missing_city_warn_and_no_alert(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture, caplog_bot
):
    """V-MATCH-04: listing has no resolvable city → dispatch emits a WARN
    containing the sku, no send calls are made."""
    state = dispatch_state
    u = state.get_user(111)
    u["cities"] = ["Amersfoort"]

    item = load_fixture("missing_city")["item"]
    stub_fetch([item])
    bot.dispatch_new_listings(state)

    assert fake_tg.calls_of("sendPhoto") == []
    assert fake_tg.calls_of("sendMessage") == []
    assert any(
        "could not resolve city" in rec.message and item["sku"] in rec.message
        for rec in caplog_bot.records
    ), "expected WARN 'could not resolve city for listing sku=...'"


def test_v_match_05_inferred_city_from_url_key(bot, load_fixture, make_user):
    item = load_fixture("inferred_city_from_url_key")["item"]
    city = bot.listing_city(item)
    assert city == "Arnhem"
    u = make_user(cities=["Arnhem"])
    assert bot.matches_user(city, bot.listing_price(item), u) is True


def test_v_match_06_missing_price_skips_gate_and_caption(
    bot, load_fixture, make_user
):
    item = load_fixture("missing_price")["item"]
    price = bot.listing_price(item)
    assert price is None

    u = make_user(cities=["Deventer"], min_price=500, max_price=1500)
    assert bot.matches_user("Deventer", None, u) is True

    caption = bot.build_alert_caption(item, "Deventer", include_url=False)
    assert "price on site" in caption


def test_v_match_07_zero_min_max_disables_gate(bot, make_user):
    u = make_user(cities=["Amersfoort"], min_price=0, max_price=0)
    # Extreme values both pass
    assert bot.matches_user("Amersfoort", 1.0, u) is True
    assert bot.matches_user("Amersfoort", 99999.0, u) is True


@pytest.mark.parametrize(
    "city,price,expected",
    [
        # city matches × price in → True
        ("Amersfoort", 1000.0, True),
        # city matches × price below → False
        ("Amersfoort", 300.0, False),
        # city matches × price above → False
        ("Amersfoort", 1800.0, False),
        # city matches × price missing → True (gate skipped when price None)
        ("Amersfoort", None, True),
        # city no-match × price in → False
        ("Zwolle", 1000.0, False),
        # city no-match × price below → False
        ("Zwolle", 300.0, False),
        # city no-match × price above → False
        ("Zwolle", 1800.0, False),
        # city no-match × price missing → False
        ("Zwolle", None, False),
    ],
)
def test_v_match_08_exhaustive_city_price_grid(bot, make_user, city, price, expected):
    u = make_user(cities=["Amersfoort"], min_price=500, max_price=1500)
    assert bot.matches_user(city, price, u) is expected


def test_v_match_08_dispatch_grid_matches_matches_user(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture
):
    """dispatch_new_listings must send exactly the cells (city match, price
    in) and (city match, price missing) from the price_grid fixture."""
    state = dispatch_state
    u = state.get_user(101)
    u["cities"] = ["Amersfoort"]
    u["min_price"] = 500
    u["max_price"] = 1500

    cycle = load_fixture("price_grid_cycle")["items"]
    stub_fetch(cycle)
    bot.dispatch_new_listings(state)

    photos = fake_tg.calls_of("sendPhoto")
    # 4 inputs → 2 sends: Inside range, Price missing.
    assert len(photos) == 2
    captions_joined = "\n".join(p["caption"] for p in photos)
    assert "Inside range" in captions_joined
    assert "Price missing" in captions_joined
    assert "Below range" not in captions_joined
    assert "Above range" not in captions_joined
