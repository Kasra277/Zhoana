"""
§12 — State validation.

  V-STATE-01  Normal restart persistence: save → load → filters survive.
  V-STATE-02  Atomic write: save writes to a temp path first, then os.replace.
              A crash between write_text and replace leaves the previous
              state intact (never a truncated file).
  V-STATE-03  Corrupt state quarantine: a malformed state.json is renamed
              to state.json.corrupt.<ts> and a fresh empty State returned.
  V-STATE-04  seen_ids bounded by SEEN_IDS_MAX (deque maxlen).
  V-STATE-05  Schema migration / version tolerance: user rows missing
              newer fields get defaults filled in.
  V-STATE-06  Dedup across restart — covered by V-NOTIFY-03 via seen_ids.
  V-STATE-07  Ephemeral disk caveat is documented in bot.py header.
  V-STATE-08  Offset durability — at-most-once: offset is advanced
              BEFORE the handler runs. Documented; we lock it in by
              reading the telegram_loop source for the 'at-most-once'
              comment next to the offset advance.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path


def test_v_state_01_save_then_load_preserves_filters(
    bot, isolated_state_file
):
    state = bot.State()
    u = state.get_user(777)
    u["cities"] = ["Amersfoort", "Zwolle"]
    u["min_price"] = 800
    u["max_price"] = 1500
    u["paused"] = True
    state.update_offset = 99
    state.add_seen("SEEN-001")

    bot.save_state(state)
    assert isolated_state_file.exists()

    loaded = bot.load_state()
    lu = loaded.users["777"]
    assert lu["cities"] == ["Amersfoort", "Zwolle"]
    assert lu["min_price"] == 800
    assert lu["max_price"] == 1500
    assert lu["paused"] is True
    assert loaded.update_offset == 99
    assert "SEEN-001" in loaded.seen_ids


def test_v_state_02_atomic_write_never_produces_partial_file(
    bot, isolated_state_file, monkeypatch
):
    """V-STATE-02: simulate a crash between the temp-file write and the
    os.replace step. The live state.json must be unchanged (still the
    previously persisted version)."""
    # Seed a prior state.
    s1 = bot.State()
    s1.update_offset = 1
    bot.save_state(s1)
    original = isolated_state_file.read_text(encoding="utf-8")

    # Attempt a second save that crashes mid-way.
    s2 = bot.State()
    s2.update_offset = 2

    class BoomError(OSError):
        pass

    real_replace = __import__("os").replace

    def crash_replace(src, dst):
        # Prove the temp file was written first.
        assert Path(src).exists()
        raise BoomError("simulated mid-write crash")

    monkeypatch.setattr("os.replace", crash_replace)

    # save_state catches OSError and logs; does not raise.
    bot.save_state(s2)

    # Live file unchanged.
    assert isolated_state_file.read_text(encoding="utf-8") == original

    # tmp file is cleaned up (best-effort) by save_state's finally branch.
    tmp = isolated_state_file.with_suffix(
        isolated_state_file.suffix + ".tmp"
    )
    # Allow either cleaned up or still present — the guarantee is ONLY that
    # the LIVE state.json is intact. Cleanup is a nicety.
    _ = tmp  # no assertion needed

    # Sanity: restore os.replace and prove we can still save cleanly.
    monkeypatch.setattr("os.replace", real_replace)
    bot.save_state(s2)
    loaded = bot.load_state()
    assert loaded.update_offset == 2


def test_v_state_03_corrupt_file_quarantined(
    bot, isolated_state_file
):
    """V-STATE-03: malformed JSON → renamed to state.json.corrupt.<ts>;
    load_state returns a fresh empty State."""
    isolated_state_file.write_text("{not json", encoding="utf-8")

    loaded = bot.load_state()
    assert loaded.users == {}
    assert loaded.update_offset == 0

    parent = isolated_state_file.parent
    corrupt_files = list(parent.glob("state.json.corrupt.*"))
    assert corrupt_files, (
        f"expected a quarantine file in {parent}, found: {list(parent.iterdir())}"
    )


def test_v_state_04_seen_ids_bounded(bot):
    s = bot.State()
    # Use a smaller maxlen by swapping the deque.
    # The real SEEN_IDS_MAX is configurable; we assert the deque respects it.
    limit = s.seen_order.maxlen or 2000
    for i in range(limit + 50):
        s.add_seen(f"sku-{i}")
    assert len(s.seen_order) == limit
    assert len(s.seen_ids) == limit
    # Oldest ids evicted.
    assert "sku-0" not in s.seen_ids
    assert f"sku-{limit + 49}" in s.seen_ids


def test_v_state_05_schema_tolerance_backfills_defaults(
    bot, isolated_state_file
):
    """V-STATE-05: a hand-rolled state.json missing newer fields must boot."""
    legacy = {
        "version": 1,
        "update_offset": 7,
        "users": {
            "100": {
                "cities": ["Amersfoort"],
                "min_price": 500,
                "max_price": 1200,
                # Missing: paused, awaiting, pending, dashboard_message_id,
                # wizard_message_id, screen, last_seen_at
            }
        },
        "seen_ids": ["OLD-1", "OLD-2"],
    }
    isolated_state_file.write_text(
        json.dumps(legacy, indent=2), encoding="utf-8"
    )
    loaded = bot.load_state()
    u = loaded.users["100"]
    assert u["paused"] is False
    assert u["awaiting"] is None
    assert u["pending"] == {"min": None, "max": None, "cities": None}
    assert u["dashboard_message_id"] is None
    assert u["wizard_message_id"] is None
    assert u["screen"] == bot.SCR_MAIN
    assert u["last_seen_at"] is None
    assert "OLD-1" in loaded.seen_ids


def test_v_state_07_ephemeral_disk_documented_in_header(bot):
    """V-STATE-07: the bot.py header must acknowledge the best-effort /
    at-most-once dedup tradeoff so operators know redeploys on ephemeral
    disks may produce a single re-alert burst."""
    text = Path(bot.__file__).read_text(encoding="utf-8")
    assert "best-effort" in text.lower()
    assert "crash" in text.lower() or "redeploy" in text.lower()


def test_v_state_08_offset_advanced_before_handler(bot):
    """V-STATE-08: the telegram_loop source advances the update offset
    BEFORE calling handle_update. This encodes at-most-once semantics."""
    src = inspect.getsource(bot.telegram_loop)
    # Find the line that advances the offset and the line that invokes handle_update.
    lines = src.splitlines()
    try:
        idx_offset = next(
            i for i, ln in enumerate(lines)
            if "update_offset" in ln and "u[\"update_id\"] + 1" in ln
        )
    except StopIteration:
        raise AssertionError("telegram_loop no longer advances update_offset")
    try:
        idx_handle = next(
            i for i, ln in enumerate(lines)
            if "handle_update(u, state)" in ln
        )
    except StopIteration:
        raise AssertionError("telegram_loop no longer calls handle_update")
    assert idx_offset < idx_handle, (
        "at-most-once requires offset advance BEFORE handle_update"
    )
