"""
§5 — Price range validation.

Automated scenarios:
  V-PRICE-01..02  — happy path (text input + quick-pick)
  V-PRICE-03     — No minimum / No limit disable the bound
  V-PRICE-04     — Invalid numeric input keeps wizard on same step
  V-PRICE-05     — Min > Max auto-swap
  V-PRICE-06     — Cancel mid-flow preserves prior values
  V-PRICE-07     — Back navigation returns to step 1
  V-PRICE-08     — Inside-range listings alert
  V-PRICE-09     — Outside-range listings silenced
"""
from __future__ import annotations


def _cb(data: str, chat_id: int, msg_id: int) -> dict:
    return {
        "update_id": 1,
        "callback_query": {
            "id": "cb",
            "data": data,
            "message": {"chat": {"id": chat_id}, "message_id": msg_id},
        },
    }


def _text_msg(chat_id: int, text: str) -> dict:
    return {"update_id": 1, "message": {
        "chat": {"id": chat_id},
        "message_id": 99,
        "text": text,
    }}


def _open_wizard(bot, state, chat_id: int, wizard_msg_id: int = 700) -> dict:
    u = state.get_user(chat_id)
    u["wizard_message_id"] = wizard_msg_id
    u["awaiting"] = bot.AWAIT_PRICE_FROM
    u["pending"]["min"] = None
    u["pending"]["max"] = None
    return u


# ─────────────────────────────────────────────────────────────────
def test_v_price_01_happy_path_text_input(bot, fresh_state, fake_tg):
    """V-PRICE-01: reply '800' → reply '1500' → Save."""
    state = fresh_state
    chat_id, wiz = 321, 700
    user = _open_wizard(bot, state, chat_id, wiz)

    bot.handle_message(_text_msg(chat_id, "800"), state)
    assert user["pending"]["min"] == 800
    assert user["awaiting"] == bot.AWAIT_PRICE_TO

    bot.handle_message(_text_msg(chat_id, "1500"), state)
    assert user["pending"]["max"] == 1500
    assert user["awaiting"] == bot.AWAIT_PRICE_CONFIRM

    bot.handle_callback(_cb("price:save", chat_id, wiz), state)
    assert user["min_price"] == 800
    assert user["max_price"] == 1500
    assert user["awaiting"] is None


def test_v_price_02_happy_path_quick_pick(bot, fresh_state, fake_tg):
    """V-PRICE-02: tap €1000 → tap €2000 → Save."""
    state = fresh_state
    chat_id, wiz = 321, 700
    user = _open_wizard(bot, state, chat_id, wiz)

    bot.handle_callback(_cb("price:from:1000", chat_id, wiz), state)
    bot.handle_callback(_cb("price:to:2000", chat_id, wiz), state)
    bot.handle_callback(_cb("price:save", chat_id, wiz), state)

    assert user["min_price"] == 1000
    assert user["max_price"] == 2000


def test_v_price_03_no_minimum_no_limit(bot, fresh_state, fake_tg):
    """V-PRICE-03: both bounds disabled → saved as 0/0 → any price matches."""
    state = fresh_state
    chat_id, wiz = 321, 700
    user = _open_wizard(bot, state, chat_id, wiz)

    bot.handle_callback(_cb("price:nomin", chat_id, wiz), state)
    bot.handle_callback(_cb("price:nomax", chat_id, wiz), state)
    bot.handle_callback(_cb("price:save", chat_id, wiz), state)

    assert user["min_price"] == 0
    assert user["max_price"] == 0

    # Prove the gate is fully disabled on matches_user.
    user["cities"] = ["Amersfoort"]
    assert bot.matches_user("Amersfoort", 300.0, user) is True
    assert bot.matches_user("Amersfoort", 3000.0, user) is True


def test_v_price_04_invalid_numeric_input_keeps_step(bot, fresh_state, fake_tg):
    """V-PRICE-04: non-numeric replies do NOT advance the wizard."""
    state = fresh_state
    chat_id, wiz = 321, 700
    user = _open_wizard(bot, state, chat_id, wiz)

    for bad in ("eight hundred", "abc", ""):
        bot.handle_message(_text_msg(chat_id, bad), state)
        assert user["awaiting"] == bot.AWAIT_PRICE_FROM, f"advanced on {bad!r}"
        assert user["pending"]["min"] is None


def test_v_price_05_min_gt_max_auto_swaps(bot, fresh_state, fake_tg):
    """V-PRICE-05: entering From=2000, To=800 → swapped to 800-2000."""
    state = fresh_state
    chat_id, wiz = 321, 700
    user = _open_wizard(bot, state, chat_id, wiz)

    bot.handle_message(_text_msg(chat_id, "2000"), state)
    bot.handle_message(_text_msg(chat_id, "800"), state)

    assert user["pending"]["min"] == 800
    assert user["pending"]["max"] == 2000

    bot.handle_callback(_cb("price:save", chat_id, wiz), state)
    assert user["min_price"] == 800
    assert user["max_price"] == 2000


def test_v_price_06_cancel_preserves_prior_values(bot, fresh_state, fake_tg):
    """V-PRICE-06: Cancel leaves the previously committed range intact."""
    state = fresh_state
    chat_id, wiz = 321, 700
    user = state.get_user(chat_id)
    user["min_price"] = 500
    user["max_price"] = 1200
    user["wizard_message_id"] = wiz
    user["awaiting"] = bot.AWAIT_PRICE_FROM

    bot.handle_callback(_cb("price:from:1000", chat_id, wiz), state)
    bot.handle_callback(_cb("price:cancel", chat_id, wiz), state)

    assert user["min_price"] == 500
    assert user["max_price"] == 1200
    assert user["awaiting"] is None


def test_v_price_07_back_navigation_returns_to_step_one(bot, fresh_state, fake_tg):
    """V-PRICE-07: Back from step 2 returns to AWAIT_PRICE_FROM."""
    state = fresh_state
    chat_id, wiz = 321, 700
    user = _open_wizard(bot, state, chat_id, wiz)

    bot.handle_callback(_cb("price:from:1000", chat_id, wiz), state)
    assert user["awaiting"] == bot.AWAIT_PRICE_TO

    bot.handle_callback(_cb("price:back", chat_id, wiz), state)
    assert user["awaiting"] == bot.AWAIT_PRICE_FROM


# ─────────────── V-PRICE-08 / 09: actual filtering ────────────────
def test_v_price_08_inside_range_alerts(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture
):
    """V-PRICE-08: 500-1500 range → the 1000 and missing-price listings
    must alert (city match + price match / missing)."""
    state = dispatch_state
    u = state.get_user(888)
    u["cities"] = ["Amersfoort"]
    u["min_price"] = 500
    u["max_price"] = 1500

    cycle = load_fixture("price_grid_cycle")["items"]
    stub_fetch(cycle)
    bot.dispatch_new_listings(state)

    photos = fake_tg.calls_of("sendPhoto")
    assert len(photos) == 2, f"expected 2 alerts (inside + no-price), got {len(photos)}"
    captions = [p["caption"] for p in photos]
    assert any("Inside range" in c for c in captions)
    assert any("Price missing" in c for c in captions)


def test_v_price_09_outside_range_silenced(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture
):
    """V-PRICE-09: below/above range produce zero alerts but still enter seen_ids."""
    state = dispatch_state
    u = state.get_user(888)
    u["cities"] = ["Amersfoort"]
    u["min_price"] = 500
    u["max_price"] = 1500

    cycle = load_fixture("price_grid_cycle")["items"]
    stub_fetch(cycle)
    bot.dispatch_new_listings(state)

    # The 300 and 1800 listings must NOT appear in any send call.
    photos = fake_tg.calls_of("sendPhoto")
    messages = fake_tg.calls_of("sendMessage")
    captions = [p.get("caption", "") for p in photos] + \
               [m.get("text", "") for m in messages]
    joined = "\n".join(captions)
    assert "Below range" not in joined
    assert "Above range" not in joined

    # But both skus must be recorded as seen.
    assert "PG-LOW-001" in state.seen_ids
    assert "PG-HIGH-001" in state.seen_ids
