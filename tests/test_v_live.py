"""
§13 — Heartbeat / bot-is-alive validation.

Automatable portions:
  V-LIVE-03  heartbeat_refresh NEVER sends new messages, only edits. It
             picks only users that meet all gating criteria (not paused,
             not awaiting, on main screen, active window, live dashboard).
  V-LIVE-04  gating: paused / awaiting / non-main / stale-last-seen /
             no dashboard id are all correctly skipped.
  V-LIVE-05  On edit failure (message gone / blocked), dashboard id is
             pruned.

Manual portions (live sandbox / canary):
  V-LIVE-01  visible running-state dashboard text after 30min observation.
  V-LIVE-02  Last check / Next check timestamps consistent with poll cadence.
"""
from __future__ import annotations

import time


def _user_on_main(bot, state, chat_id: int, msg_id: int) -> dict:
    u = state.get_user(chat_id)
    u["dashboard_message_id"] = msg_id
    u["screen"] = bot.SCR_MAIN
    u["last_seen_at"] = time.time()
    return u


def test_v_live_03_heartbeat_never_sends_new_messages(
    bot, fresh_state, fake_tg
):
    """V-LIVE-03: heartbeat_refresh emits only editMessageText calls.
    sendMessage / sendPhoto counts must remain at 0."""
    state = fresh_state
    _user_on_main(bot, state, 1, 1001)
    _user_on_main(bot, state, 2, 1002)

    bot.heartbeat_refresh(state)

    assert fake_tg.calls_of("sendMessage") == []
    assert fake_tg.calls_of("sendPhoto") == []
    assert len(fake_tg.calls_of("editMessageText")) == 2


def test_v_live_04_gating_rules(bot, fresh_state, fake_tg):
    """V-LIVE-04: paused / awaiting / non-main / stale / no-dashboard-id
    must all be skipped by heartbeat."""
    state = fresh_state

    # Eligible user — gets refreshed.
    _user_on_main(bot, state, 1, 1001)
    # Paused — skipped.
    u2 = _user_on_main(bot, state, 2, 1002); u2["paused"] = True
    # Awaiting — skipped.
    u3 = _user_on_main(bot, state, 3, 1003); u3["awaiting"] = bot.AWAIT_PRICE_FROM
    # On a non-main screen — skipped.
    u4 = _user_on_main(bot, state, 4, 1004); u4["screen"] = bot.SCR_SETTINGS
    # No last_seen_at — skipped.
    u5 = _user_on_main(bot, state, 5, 1005); u5["last_seen_at"] = None
    # Stale last_seen_at — skipped.
    u6 = _user_on_main(bot, state, 6, 1006)
    u6["last_seen_at"] = time.time() - (bot.HEARTBEAT_ACTIVE_WINDOW_SECONDS + 60)
    # No dashboard id — skipped.
    u7 = _user_on_main(bot, state, 7, 1007); u7["dashboard_message_id"] = None

    bot.heartbeat_refresh(state)

    edits = fake_tg.calls_of("editMessageText")
    # Only chat 1 is eligible.
    assert len(edits) == 1
    assert edits[0]["chat_id"] == 1


def test_v_live_05_stale_dashboard_pruned_on_gone(bot, fresh_state, fake_tg):
    """V-LIVE-05: if edit returns 'message to edit not found', the stored
    dashboard_message_id is cleared."""
    state = fresh_state
    u = _user_on_main(bot, state, 99, 9900)

    fake_tg.edit_message_gone_ids.add(9900)
    bot.heartbeat_refresh(state)

    assert u["dashboard_message_id"] is None


def test_v_live_05_stale_dashboard_pruned_on_blocked(bot, fresh_state, fake_tg):
    """Also prune when the user blocked the bot."""
    state = fresh_state
    u = _user_on_main(bot, state, 100, 9901)

    fake_tg.send_message_blocked_chats.add(100)
    # Block check uses edit_message_text; we simulate via a custom override.
    original = fake_tg._respond

    def override_edit(method, p):
        if method == "editMessageText" and p.get("chat_id") == 100:
            return {"ok": False, "error_code": 403,
                    "description": "Forbidden: bot was blocked by the user"}
        return original(method, p)
    fake_tg._respond = override_edit  # type: ignore[assignment]

    bot.heartbeat_refresh(state)
    assert u["dashboard_message_id"] is None


def test_v_live_heartbeat_idempotent_on_not_modified(bot, fresh_state, fake_tg):
    """If two heartbeat cycles produce identical text, the second edit
    returns 'message is not modified' and heartbeat silently accepts it
    without pruning the dashboard."""
    state = fresh_state
    u = _user_on_main(bot, state, 50, 5000)

    # First cycle — edits succeed.
    bot.heartbeat_refresh(state)
    assert u["dashboard_message_id"] == 5000

    # Second cycle — force "not modified".
    fake_tg.edit_always_not_modified = True
    bot.heartbeat_refresh(state)
    # Still has dashboard id.
    assert u["dashboard_message_id"] == 5000
