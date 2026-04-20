"""
§7 — Exact-link validation (release-critical).

  V-LINK-01  Per-listing URL construction.
  V-LINK-02  Special-char slug behavior (documented).
  V-LINK-03  Missing url_key falls back ONLY to /residences.
  V-LINK-04  Caption link and button URL are byte-for-byte equal in the
             text-fallback path.
  V-LINK-05  Photo-path caption does NOT include a redundant
             'View on Holland2Stay' anchor (button serves that role).
  V-LINK-06  Full-cycle no-silent-fallback scan: given a mix of listings
             with/without url_key, exactly N buttons carry per-listing
             URLs and M carry the /residences URL AND emit the WARN log.
"""
from __future__ import annotations

import re


RESIDENCES = "https://www.holland2stay.com/residences"


def test_v_link_01_per_listing_url_construction(bot, load_fixture):
    item = load_fixture("happy_listing")["item"]
    url = bot.listing_url(item)
    expected = "https://www.holland2stay.com/residences/luxury-studio-amersfoort-a1.html"
    assert url == expected, f"expected exact byte match; got {url}"


def test_v_link_02_special_char_slug_behavior(bot):
    """V-LINK-02: documented behavior — slug is substituted verbatim. The
    test locks in the current behavior so a silent change in construction
    is caught on CI."""
    item = {"url_key": "weird slug/with-special"}
    url = bot.listing_url(item)
    # Current behavior: verbatim substitution. Telegram URL-encodes in the UI.
    assert url == "https://www.holland2stay.com/residences/weird slug/with-special.html"


def test_v_link_03_missing_url_key_falls_back_only_to_residences(
    bot, load_fixture
):
    item = load_fixture("missing_url_key")["item"]
    url = bot.listing_url(item)
    assert url == RESIDENCES, f"fallback must be exactly {RESIDENCES}; got {url}"


def test_v_link_04_caption_link_matches_button_url(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture
):
    """V-LINK-04: text-fallback path. Caption's <a href=...> and keyboard
    button's url must be byte-for-byte equal."""
    state = dispatch_state
    u = state.get_user(222)
    u["cities"] = ["Zutphen"]

    # no_image fixture forces text-fallback #1.
    item = load_fixture("no_image")["item"]
    stub_fetch([item])
    bot.dispatch_new_listings(state)

    messages = fake_tg.calls_of("sendMessage")
    assert len(messages) == 1
    msg = messages[0]

    # The inline keyboard's URL
    buttons = msg["reply_markup"]["inline_keyboard"][0]
    button_url = buttons[0]["url"]
    # The caption's trailing <a href="..."> anchor
    m = re.search(r'href="([^"]+)"', msg["text"])
    assert m, "expected an <a href=...> anchor in the text-fallback body"
    caption_url = m.group(1)

    assert button_url == caption_url, (
        f"button url {button_url!r} must equal caption url {caption_url!r}"
    )
    assert button_url.endswith("zutphen-flat-noimg-001.html"), button_url


def test_v_link_05_photo_path_caption_has_no_link_line(bot, load_fixture):
    """V-LINK-05: caption built with include_url=False (photo path) MUST
    NOT contain 'View on Holland2Stay'. The caption built with include_url
    (text fallback) MUST contain it exactly once."""
    item = load_fixture("happy_listing")["item"]

    photo_cap = bot.build_alert_caption(item, "Amersfoort", include_url=False)
    text_cap = bot.build_alert_caption(item, "Amersfoort", include_url=True)

    assert "View on Holland2Stay" not in photo_cap
    assert text_cap.count("View on Holland2Stay") == 1


def test_v_link_06_full_cycle_no_silent_fallback(
    bot, dispatch_state, fake_tg, stub_fetch, load_fixture, caplog_bot
):
    """V-LINK-06: fixture has 3 with url_key + 7 without. After a full
    cycle, exactly 3 per-listing URLs, exactly 7 fallback URLs, and 7
    matching WARN log lines — one per missing slug."""
    state = dispatch_state
    u = state.get_user(999)
    u["cities"] = ["Amersfoort"]

    items = load_fixture("link_cycle_mixed")["items"]
    stub_fetch(items)
    bot.dispatch_new_listings(state)

    photos = fake_tg.calls_of("sendPhoto")
    assert len(photos) == 10

    per_listing = 0
    fallback = 0
    for p in photos:
        btn = p["reply_markup"]["inline_keyboard"][0][0]["url"]
        if btn == RESIDENCES:
            fallback += 1
        elif btn.startswith("https://www.holland2stay.com/residences/") and btn.endswith(".html"):
            per_listing += 1
        else:
            raise AssertionError(f"unexpected URL {btn!r}")

    assert per_listing == 3, per_listing
    assert fallback == 7, fallback

    warn_lines = [
        rec.message for rec in caplog_bot.records
        if "missing url_key" in rec.message and "using residences fallback URL" in rec.message
    ]
    assert len(warn_lines) == 7, (
        f"expected 7 WARN lines for missing url_key; got {len(warn_lines)}"
    )
    # Each must carry the sku.
    for sku in ("L-NONE-001", "L-NONE-002", "L-NONE-003", "L-NONE-004",
                "L-NONE-005", "L-NONE-006", "L-NONE-007"):
        assert any(sku in line for line in warn_lines), (
            f"expected a WARN line carrying sku={sku}"
        )
