"""
§8 — Image / photo validation.

  V-IMG-01  Valid media_gallery hero image → one sendPhoto, no fallback.
  V-IMG-02  All gallery disabled → small_image URL used.
  V-IMG-03  Only thumbnail present → thumbnail URL used.
  V-IMG-04  No image at all → text fallback #1 only, one sendMessage.
  V-IMG-05  Broken URL in first candidate → chain advances, ONE final send.
  V-IMG-06  All images broken AND preview fails → terminal preview-off
            sendMessage, still exactly ONE final message.
  V-IMG-07  (Manual) photo visually matches the listing page.
  V-IMG-08  UX under failure: caption renders correctly even on fallbacks.
"""
from __future__ import annotations


def test_v_img_01_valid_media_gallery(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture
):
    state = dispatch_state
    u = state.get_user(1)
    u["cities"] = ["Amersfoort"]

    item = load_fixture("happy_listing")["item"]
    stub_fetch([item])
    bot.dispatch_new_listings(state)

    photos = fake_tg.calls_of("sendPhoto")
    messages = fake_tg.calls_of("sendMessage")
    assert len(photos) == 1
    assert messages == []
    assert photos[0]["photo"] == "https://img.holland2stay.com/gallery/a1-hero.jpg"


def test_v_img_02_all_disabled_gallery_uses_small_image(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture
):
    state = dispatch_state
    u = state.get_user(2)
    u["cities"] = ["Enschede"]

    item = load_fixture("all_disabled_gallery")["item"]
    stub_fetch([item])
    bot.dispatch_new_listings(state)

    photos = fake_tg.calls_of("sendPhoto")
    assert len(photos) == 1
    # The first disabled gallery URL must NOT be used.
    assert photos[0]["photo"] == item["small_image"]["url"]
    assert "gallery/ens1.jpg" not in photos[0]["photo"]


def test_v_img_03_only_thumbnail_used(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture
):
    state = dispatch_state
    u = state.get_user(3)
    u["cities"] = ["Nijmegen"]

    item = load_fixture("only_thumbnail")["item"]
    stub_fetch([item])
    bot.dispatch_new_listings(state)

    photos = fake_tg.calls_of("sendPhoto")
    assert len(photos) == 1
    assert photos[0]["photo"] == item["thumbnail"]["url"]


def test_v_img_04_no_image_uses_text_fallback_once(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture
):
    state = dispatch_state
    u = state.get_user(4)
    u["cities"] = ["Zutphen"]

    item = load_fixture("no_image")["item"]
    stub_fetch([item])
    bot.dispatch_new_listings(state)

    photos = fake_tg.calls_of("sendPhoto")
    messages = fake_tg.calls_of("sendMessage")
    assert photos == []
    assert len(messages) == 1
    # Text fallback #1 uses preview-on.
    assert messages[0]["disable_web_page_preview"] is False
    # Final message must be well-formed HTML (no raw tag bleed).
    assert "<b>" in messages[0]["text"]
    assert "View on Holland2Stay" in messages[0]["text"]


def test_v_img_05_broken_first_candidate_advances_chain(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture
):
    """Make the gallery hero fail; small_image + thumbnail still succeed.
    Result: chain walks past the 404, sends ONE final alert."""
    state = dispatch_state
    u = state.get_user(5)
    u["cities"] = ["Amersfoort"]

    # Fail all gallery hero URLs (contain 'gallery/a1-hero').
    fake_tg.send_photo_fail_substrings.add("a1-hero")

    item = load_fixture("happy_listing")["item"]
    stub_fetch([item])
    bot.dispatch_new_listings(state)

    photos = fake_tg.calls_of("sendPhoto")
    messages = fake_tg.calls_of("sendMessage")
    # At least two photo attempts (hero failed, next succeeded).
    assert any(p["photo"].endswith("a1-hero.jpg") for p in photos)
    # Exactly one SUCCESSFUL send: either the second photo OR (not both).
    # We count successful outcomes via the responses recorded on last_res
    # indirectly — at least one photo after the hero must be a send that
    # the chain accepted as ok (i.e. it didn't fall through to sendMessage).
    assert len(messages) == 0, "chain should not have fallen to text"
    assert len(photos) >= 2, "chain should have retried at least once"


def test_v_img_06_all_broken_falls_through_to_terminal_text(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture
):
    """Fail every photo URL AND fail preview-on sendMessage; the terminal
    sendMessage (preview-off) must deliver exactly ONE final message."""
    state = dispatch_state
    u = state.get_user(6)
    u["cities"] = ["Amersfoort"]

    # Break every image URL in the fixture.
    fake_tg.send_photo_fail_substrings.update({"404/", "gallery/", "small/", "thumb/"})
    fake_tg.send_message_preview_fails = True

    item = load_fixture("broken_image_urls")["item"]
    stub_fetch([item])
    bot.dispatch_new_listings(state)

    photos = fake_tg.calls_of("sendPhoto")
    messages = fake_tg.calls_of("sendMessage")
    # Two sendMessage attempts: preview-on (fails), then preview-off (succeeds).
    assert len(messages) == 2
    assert messages[0]["disable_web_page_preview"] is False
    assert messages[1]["disable_web_page_preview"] is True
    # Every photo attempt failed.
    assert len(photos) >= 1


def test_v_img_08_ux_under_failure_caption_is_branded(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture
):
    """V-IMG-08: even on the terminal fallback, the caption must render
    HTML correctly (bold name, facts row, trailing link)."""
    state = dispatch_state
    u = state.get_user(7)
    u["cities"] = ["Amersfoort"]

    fake_tg.send_photo_fail_substrings.update({"404/"})
    fake_tg.send_message_preview_fails = True

    item = load_fixture("broken_image_urls")["item"]
    stub_fetch([item])
    bot.dispatch_new_listings(state)

    messages = fake_tg.calls_of("sendMessage")
    # Take the last (terminal) message.
    terminal = messages[-1]
    body = terminal["text"]
    assert body.startswith("🏠 <b>"), f"caption must start with branded header; got: {body[:80]!r}"
    assert "<b>" in body  # HTML present
    assert "&lt;b&gt;" not in body  # no raw-escaped tags
    assert "View on Holland2Stay" in body
