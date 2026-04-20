"""
§11 — Always-on runtime validation.

The majority of V-RUN tests are inherently manual (>=24h uptime, Railway
restart metrics, live SIGTERM).
This module automates what is testable in-process:

  V-RUN-04  fetch_listings transient failure does not kill the process —
            dispatch returns cleanly and logs the documented WARN signature
            'h2s fetch failed; keeping seen_ids untouched this cycle'.
  V-RUN-07  Graceful shutdown logic: the saver loop exits when stop event
            is set; final save flushes state to disk.
  V-RUN-08  Webhook preflight: telegram_preflight calls getMe, getWebhookInfo
            and deleteWebhook in the right order and respects each result.
  _jittered_interval bounds (always within [poll-jitter, poll+jitter] and
   never below 30s safety floor).
  Signal handler wiring.
"""
from __future__ import annotations

import threading
import time as real_time


def test_v_run_04_fetch_failure_does_not_crash(
    bot, dispatch_state, fake_tg, monkeypatch, caplog_bot
):
    """V-RUN-04: fetch_listings returns None (transient failure). dispatch
    logs WARN and does not raise; seen_ids is not advanced this cycle."""
    monkeypatch.setattr(bot, "fetch_listings", lambda: None)
    # Pre-populate a seen id so we can prove it survives.
    dispatch_state.add_seen("ALREADY-SEEN-001")

    bot.dispatch_new_listings(dispatch_state)  # must not raise

    assert "ALREADY-SEEN-001" in dispatch_state.seen_ids
    assert dispatch_state.last_check_ok is False
    # Documented log signature.
    assert any(
        "keeping seen_ids untouched" in rec.message
        for rec in caplog_bot.records
    )


def test_v_run_07_saver_loop_exits_on_stop(bot, monkeypatch, tmp_path):
    """V-RUN-07: state_saver_loop exits promptly when stop.set() is called
    and persists dirty state on the final flush path. The orchestration
    itself is verified via the run() function's explicit final save_state."""
    monkeypatch.setattr(bot, "STATE_FILE", tmp_path / "state.json")
    # Shorten the debounce so the test runs quickly.
    monkeypatch.setattr(bot, "STATE_SAVE_DEBOUNCE_SECONDS", 0.05)

    state = bot.State()
    state.update_offset = 42
    state.mark_dirty()

    stop = threading.Event()
    t = threading.Thread(target=bot.state_saver_loop, args=(state, stop))
    t.start()
    real_time.sleep(0.3)  # give loop a few ticks to flush
    stop.set()
    t.join(timeout=2.0)
    assert not t.is_alive(), "saver loop did not exit on stop"

    import json
    raw = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert raw["update_offset"] == 42


def test_v_run_08_webhook_preflight_call_sequence(bot, fake_tg):
    """V-RUN-08: preflight calls getMe → getWebhookInfo → deleteWebhook →
    setMyCommands exactly once in that order."""
    bot.telegram_preflight()

    methods = fake_tg.methods()
    # Filter down to preflight-relevant methods.
    pre = [m for m in methods
           if m in ("getMe", "getWebhookInfo", "deleteWebhook", "setMyCommands")]
    assert pre == ["getMe", "getWebhookInfo", "deleteWebhook", "setMyCommands"], (
        f"unexpected preflight order: {pre}"
    )


def test_v_run_jittered_interval_bounds(bot):
    """_jittered_interval must always be within [base-jitter, base+jitter],
    clamped at the 30s safety floor."""
    base = bot.H2S_POLL_SECONDS
    jitter = bot.H2S_JITTER_SECONDS
    for _ in range(200):
        iv = bot._jittered_interval()
        assert iv >= 30.0
        if base - jitter >= 30.0:
            assert base - jitter - 1e-6 <= iv <= base + jitter + 1e-6


def test_v_run_signal_handler_sets_stop_event(bot, monkeypatch):
    """Signal handler, when invoked, must set the stop event. Signal
    delivery is not portable across OS, so we invoke the handler
    directly."""
    stop = threading.Event()
    # Capture installed handler by monkey-patching signal.signal.
    installed: dict[str, object] = {}

    def fake_signal(signum, handler):
        installed[str(signum)] = handler
        return None

    monkeypatch.setattr(bot.signal, "signal", fake_signal)
    bot.install_signal_handlers(stop)
    # At least one handler was installed.
    assert installed, "no signal handlers installed"

    # Invoke each handler manually.
    for handler in installed.values():
        stop.clear()
        handler(0, None)  # type: ignore[misc]
        assert stop.is_set(), "handler did not set stop event"
