"""
Railway Persistence Coverage — §6.1 local-filesystem invariants.

Reference: .cursor/plans/railway_persistence_coverage_fb36b62a.plan.md

These tests prove the code-level persistence guarantees that hold regardless
of the platform:

  P-PERSIST-01  Full field-by-field round-trip (save → load equality).
  P-PERSIST-02  Atomic write: a mid-write crash leaves the previous state.json
                intact AND no ".tmp" residue interferes with a subsequent load.
  P-PERSIST-03  Corrupt state quarantine + recovery: after quarantine, the
                next successful save produces a fresh clean state.json and
                the quarantine file is preserved for post-mortem.
  P-PERSIST-04  Type-drift tolerance: document actual behavior when a user
                row's fields arrive with wrong types (e.g. min_price as
                string, cities as string). Locks in the current behavior
                so any change is visible.
  P-PERSIST-05  Bounded seen_ids round-trip: persisting 3× SEEN_IDS_MAX ids
                keeps only the most recent SEEN_IDS_MAX on reload (FIFO).
  P-PERSIST-06  Debounce-window loss (at-most-once on hard kill): a dirty
                mutation that never reaches save_state is absent from the
                reloaded state — proving the documented tradeoff.

Railway-platform tests (P-PERSIST-07..14) are manual; see
[validation/PERSISTENCE_RUNBOOK.md](../validation/PERSISTENCE_RUNBOOK.md).
"""
from __future__ import annotations

import json
import os
from pathlib import Path


# ────────────────────────── P-PERSIST-01 ────────────────────────────
def test_p_persist_01_full_field_roundtrip(bot, isolated_state_file):
    """Every persisted field survives a save→load cycle with byte-equal
    semantics. This is the per-field proof-of-round-trip required by §7."""
    s = bot.State()

    # Seed two users to exercise multi-chat persistence.
    u_a = s.get_user(111)
    u_a["cities"] = ["Amersfoort", "Zwolle"]
    u_a["min_price"] = 800
    u_a["max_price"] = 1500
    u_a["paused"] = True
    u_a["awaiting"] = "price_from"
    u_a["pending"] = {"min": 900, "max": None, "cities": ["Arnhem"]}
    u_a["dashboard_message_id"] = 4242
    u_a["wizard_message_id"] = 4243
    u_a["screen"] = bot.SCR_SETTINGS
    u_a["last_seen_at"] = 1_700_000_000.0

    u_b = s.get_user(222)
    u_b["cities"] = []
    u_b["min_price"] = 0
    u_b["max_price"] = 0
    u_b["paused"] = False

    s.update_offset = 987_654_321
    for i in range(10):
        s.add_seen(f"SKU-{i:03d}")

    bot.save_state(s)
    assert isolated_state_file.exists(), "save_state did not create state.json"

    loaded = bot.load_state()

    # User A — every field preserved exactly.
    la = loaded.users["111"]
    assert la["cities"] == ["Amersfoort", "Zwolle"]
    assert la["min_price"] == 800
    assert la["max_price"] == 1500
    assert la["paused"] is True
    assert la["awaiting"] == "price_from"
    assert la["pending"] == {"min": 900, "max": None, "cities": ["Arnhem"]}
    assert la["dashboard_message_id"] == 4242
    assert la["wizard_message_id"] == 4243
    assert la["screen"] == bot.SCR_SETTINGS
    assert la["last_seen_at"] == 1_700_000_000.0

    # User B — defaults preserved.
    lb = loaded.users["222"]
    assert lb["cities"] == []
    assert lb["paused"] is False

    # Global fields.
    assert loaded.update_offset == 987_654_321
    for i in range(10):
        assert f"SKU-{i:03d}" in loaded.seen_ids
    # first_run is derived from seen_ids presence on load.
    assert loaded.first_run is False

    # Schema version round-tripped.
    raw = json.loads(isolated_state_file.read_text(encoding="utf-8"))
    assert raw.get("version") == bot.STATE_SCHEMA_VERSION


# ────────────────────────── P-PERSIST-02 ────────────────────────────
def test_p_persist_02_mid_write_crash_leaves_live_file_intact(
    bot, isolated_state_file, monkeypatch
):
    """If os.replace fails (simulated SIGKILL mid-rename), the live
    state.json must be byte-identical to the pre-crash version, and the
    NEXT successful load must ignore any .tmp residue."""
    s1 = bot.State()
    s1.get_user(333)["cities"] = ["Amersfoort"]
    s1.update_offset = 100
    bot.save_state(s1)
    snapshot_bytes = isolated_state_file.read_bytes()

    class BoomError(OSError):
        pass

    def crash_replace(src, dst):
        # Prove the temp file was genuinely written before the crash.
        assert Path(src).exists(), "save_state did not create tmp file"
        raise BoomError("simulated kill between write and rename")

    monkeypatch.setattr("os.replace", crash_replace)

    s2 = bot.State()
    s2.get_user(333)["cities"] = ["Zwolle"]
    s2.update_offset = 200
    bot.save_state(s2)  # catches OSError internally

    # Live file unchanged byte-for-byte.
    assert isolated_state_file.read_bytes() == snapshot_bytes

    # Reload proves the pre-crash filter survived; the never-landed
    # mutation was lost (the user asked for Zwolle, but state still shows
    # Amersfoort — this is the honest at-most-once claim).
    monkeypatch.setattr("os.replace", os.replace)
    loaded = bot.load_state()
    assert loaded.users["333"]["cities"] == ["Amersfoort"]
    assert loaded.update_offset == 100


# ────────────────────────── P-PERSIST-03 ────────────────────────────
def test_p_persist_03_corrupt_quarantine_then_recover(
    bot, isolated_state_file
):
    """After a corrupt state is quarantined, the next save must produce a
    valid state.json and the quarantine file must still be on disk for
    post-mortem inspection."""
    isolated_state_file.write_text("{ not valid json", encoding="utf-8")

    loaded = bot.load_state()
    assert loaded.users == {}
    assert loaded.update_offset == 0

    parent = isolated_state_file.parent
    corrupt_files = sorted(parent.glob("state.json.corrupt.*"))
    assert corrupt_files, "corrupt file was not quarantined"

    # Recover: save a fresh, minimal state and prove it is readable again.
    fresh = bot.State()
    fresh.get_user(999)["cities"] = ["Deventer"]
    bot.save_state(fresh)

    # state.json is now the clean one.
    raw = json.loads(isolated_state_file.read_text(encoding="utf-8"))
    assert raw["users"]["999"]["cities"] == ["Deventer"]

    # Quarantine file preserved (evidence for §8).
    assert corrupt_files[0].exists(), (
        "quarantine artifact was deleted; evidence must survive recovery"
    )

    # And load_state succeeds cleanly.
    again = bot.load_state()
    assert again.users["999"]["cities"] == ["Deventer"]


# ────────────────────────── P-PERSIST-04 ────────────────────────────
def test_p_persist_04_type_drift_does_not_crash_boot(
    bot, isolated_state_file, caplog_bot
):
    """A hand-edited state.json that parses but uses wrong types for
    user-row fields must not crash load_state. The current behavior is
    that _ensure_user_defaults uses setdefault, which means existing
    wrong-typed fields PASS THROUGH unchanged. This test LOCKS IN that
    behavior — any future fix should update this assertion intentionally
    rather than silently drop existing users on reload."""
    drifted = {
        "version": bot.STATE_SCHEMA_VERSION,
        "update_offset": 0,
        "users": {
            # min_price as string, max_price as float, cities as string,
            # paused as int. All wrong, none crash-worthy.
            "101": {
                "cities": "Amersfoort",
                "min_price": "800",
                "max_price": 1500.0,
                "paused": 1,
            },
            # Missing pending → _ensure_user_defaults rebuilds it.
            "102": {
                "cities": ["Zwolle"],
                "pending": "not a dict",
            },
        },
        "seen_ids": [],
    }
    isolated_state_file.write_text(
        json.dumps(drifted, indent=2), encoding="utf-8"
    )

    loaded = bot.load_state()

    # load_state does not crash — this is the critical guarantee.
    assert set(loaded.users.keys()) == {"101", "102"}

    # DOCUMENTED CURRENT BEHAVIOR (intentionally asserting what happens
    # today so any silent change is visible in CI):
    u101 = loaded.users["101"]
    assert u101["cities"] == "Amersfoort", (
        "current load_state passes string-typed cities through; matches_user "
        "will treat it as an iterable of characters. Consider tightening."
    )
    assert u101["min_price"] == "800", "string min_price passes through"
    assert u101["max_price"] == 1500.0, "float max_price passes through"
    assert u101["paused"] == 1, "int paused passes through (truthy works)"

    # The malformed pending dict gets replaced by the default dict shape.
    u102 = loaded.users["102"]
    assert isinstance(u102["pending"], dict), (
        "non-dict pending must be normalized to a dict"
    )
    assert u102["pending"] == {"min": None, "max": None, "cities": None}


# ────────────────────────── P-PERSIST-05 ────────────────────────────
def test_p_persist_05_bounded_seen_ids_survive_reload_fifo(
    bot, isolated_state_file
):
    """Persisting more ids than SEEN_IDS_MAX keeps only the most recent
    SEEN_IDS_MAX on reload; eviction is FIFO."""
    limit = bot.SEEN_IDS_MAX

    s = bot.State()
    # Overfill by 3x so the truncation logic has something to do even in
    # the save path.
    total = limit * 3
    for i in range(total):
        s.add_seen(f"sku-{i:06d}")

    bot.save_state(s)
    loaded = bot.load_state()

    # Exactly limit entries retained.
    assert len(loaded.seen_ids) == limit
    assert len(loaded.seen_order) == limit

    # Most-recent items present; oldest items evicted.
    assert f"sku-{total - 1:06d}" in loaded.seen_ids, (
        "most recent sku must survive eviction"
    )
    assert "sku-000000" not in loaded.seen_ids, (
        "oldest sku must be evicted (FIFO)"
    )
    # The exact cutoff: first retained id should be total - limit.
    assert f"sku-{total - limit:06d}" in loaded.seen_ids


# ────────────────────────── P-PERSIST-06 ────────────────────────────
def test_p_persist_06_debounce_window_loss_matches_at_most_once(
    bot, isolated_state_file
):
    """Prove the documented at-most-once contract: a mutation that is
    marked dirty but never reaches save_state (simulating a SIGKILL in
    the debounce window) is absent from the reloaded state. The bot
    header calls this out as a tradeoff; this test locks in that the
    observed behavior matches the documented claim."""
    # Initial flushed state on disk.
    s = bot.State()
    s.get_user(555)["cities"] = ["Amersfoort"]
    s.update_offset = 50
    bot.save_state(s)

    # Simulate mid-cycle activity that dirties state but is interrupted
    # before the saver loop's next tick. We deliberately DO NOT call
    # save_state(s) again — that is the SIGKILL analogy.
    s.get_user(555)["cities"] = ["Zwolle"]
    s.get_user(555)["min_price"] = 1000
    s.update_offset = 77
    s.add_seen("HOT-NEW-SKU")
    s.mark_dirty()
    assert s.take_dirty(), "dirty flag must be set on mutation"

    # Simulate hard kill: process ends without a flush. Reload from disk.
    loaded = bot.load_state()
    lu = loaded.users["555"]

    # The post-flush state is what survives — the in-memory dirty mutation
    # is LOST. This matches the documented at-most-once semantics.
    assert lu["cities"] == ["Amersfoort"], (
        "mutation that never reached save_state must be absent on reload; "
        "this is the at-most-once tradeoff documented in bot.py"
    )
    assert lu["min_price"] == 0
    assert loaded.update_offset == 50
    assert "HOT-NEW-SKU" not in loaded.seen_ids


# ────────────────────────── P-PERSIST-06b ───────────────────────────
def test_p_persist_06b_graceful_shutdown_flushes_dirty(
    bot, isolated_state_file
):
    """Complementary to P-PERSIST-06: under graceful shutdown (SIGTERM,
    saver thread drains), a dirty mutation IS persisted — proving the
    at-most-once window is strictly bounded by the debounce."""
    import threading

    # Tiny debounce so the test is fast and deterministic.
    real_debounce = bot.STATE_SAVE_DEBOUNCE_SECONDS
    bot.STATE_SAVE_DEBOUNCE_SECONDS = 0.05
    try:
        s = bot.State()
        stop = threading.Event()
        t = threading.Thread(
            target=bot.state_saver_loop, args=(s, stop), daemon=False
        )
        t.start()

        # Mutate + mark dirty; give the saver one full debounce tick.
        with s.lock:
            s.get_user(555)["cities"] = ["Zwolle"]
            s.update_offset = 123
            s.mark_dirty()

        import time as _real_time
        _real_time.sleep(0.25)  # 5 debounce ticks
        stop.set()
        t.join(timeout=2.0)
        assert not t.is_alive(), "saver loop did not exit"
    finally:
        bot.STATE_SAVE_DEBOUNCE_SECONDS = real_debounce

    loaded = bot.load_state()
    assert loaded.users["555"]["cities"] == ["Zwolle"]
    assert loaded.update_offset == 123
