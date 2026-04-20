"""
Pytest configuration and shared fixtures for the h2s bot validation suite.

Responsibilities:
  1. Set TELEGRAM_BOT_TOKEN before importing bot.py (module raises SystemExit
     without it).
  2. Point STATE_FILE at an ephemeral path so tests never touch the real state.
  3. Import bot once and expose it as the `bot` fixture.
  4. Provide a FakeTelegram recorder that replaces bot.tg_call and captures
     every outbound API call. Tests assert on this recorder.
  5. Provide a fixture loader that reads validation/fixtures/*.json.
  6. Provide a fresh_state fixture that builds a clean State each test.
  7. Provide time/sleep neutralization so pacing doesn't slow the suite.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pytest

# ── Environment setup MUST happen before `import bot` ─────────────
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-harness-token")
os.environ.setdefault("LOG_LEVEL", "DEBUG")
os.environ.setdefault("ALERT_PACING_SECONDS", "0")
os.environ.setdefault("H2S_POLL_SECONDS", "90")
os.environ.setdefault("H2S_JITTER_SECONDS", "10")
os.environ.setdefault("MAX_ALERTS_PER_CYCLE", "10")

# Put the project root on sys.path so `import bot` works regardless of the
# directory pytest was invoked from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import bot as _bot  # noqa: E402 — must follow env setup above

FIXTURES_DIR = _PROJECT_ROOT / "validation" / "fixtures"


# ── FakeTelegram ─────────────────────────────────────────────────
class FakeTelegram:
    """
    Drop-in replacement for bot.tg_call. Records every call and returns
    realistic Telegram API-shaped responses. Failure policies can be
    programmed per test (broken image URLs, preview failure, 'blocked',
    'not modified', etc.).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._next_message_id = 1000
        # Failure policies:
        self.send_photo_fail_substrings: set[str] = set()
        self.send_photo_blocked_substrings: set[str] = set()
        self.send_message_preview_fails = False  # text fallback #1 fails
        self.send_message_blocked_chats: set[int] = set()
        self.edit_always_not_modified = False
        self.edit_message_gone_ids: set[int] = set()

    def __call__(self, method: str, **params: Any) -> dict:
        # Peel off tg_call-only kwargs so recorded params mirror Telegram-wire.
        params = {k: v for k, v in params.items()
                  if k not in ("session", "timeout", "retries")}
        self.calls.append((method, dict(params)))
        return self._respond(method, params)

    # ---- response builders ----

    def _next_id(self) -> int:
        self._next_message_id += 1
        return self._next_message_id

    def _respond(self, method: str, p: dict) -> dict:
        chat_id = p.get("chat_id")

        if method == "getMe":
            return {"ok": True, "result": {"id": 42, "username": "h2s_test_bot", "first_name": "H2S Test"}}
        if method == "getWebhookInfo":
            return {"ok": True, "result": {"url": ""}}
        if method == "deleteWebhook":
            return {"ok": True, "result": True}
        if method == "setMyCommands":
            return {"ok": True, "result": True}
        if method == "answerCallbackQuery":
            return {"ok": True, "result": True}
        if method == "deleteMessage":
            return {"ok": True, "result": True}

        if method == "sendMessage":
            if isinstance(chat_id, int) and chat_id in self.send_message_blocked_chats:
                return {"ok": False, "error_code": 403,
                        "description": "Forbidden: bot was blocked by the user"}
            preview_on = not p.get("disable_web_page_preview", True)
            if preview_on and self.send_message_preview_fails:
                return {"ok": False, "error_code": 400,
                        "description": "Bad Request: failed to fetch webpage preview"}
            mid = self._next_id()
            return {"ok": True, "result": {
                "message_id": mid,
                "chat": {"id": chat_id},
                "text": p.get("text"),
            }}

        if method == "sendPhoto":
            photo = p.get("photo", "") or ""
            if isinstance(chat_id, int) and chat_id in self.send_message_blocked_chats:
                return {"ok": False, "error_code": 403,
                        "description": "Forbidden: bot was blocked by the user"}
            for s in self.send_photo_blocked_substrings:
                if s and s in photo:
                    return {"ok": False, "error_code": 403,
                            "description": "Forbidden: bot was blocked by the user"}
            for s in self.send_photo_fail_substrings:
                if s and s in photo:
                    return {"ok": False, "error_code": 400,
                            "description": "Bad Request: failed to get HTTP URL content"}
            mid = self._next_id()
            return {"ok": True, "result": {
                "message_id": mid,
                "chat": {"id": chat_id},
                "photo": photo,
            }}

        if method == "editMessageText":
            mid = p.get("message_id")
            if isinstance(mid, int) and mid in self.edit_message_gone_ids:
                return {"ok": False, "error_code": 400,
                        "description": "Bad Request: message to edit not found"}
            if self.edit_always_not_modified:
                return {"ok": False, "error_code": 400,
                        "description": "Bad Request: message is not modified"}
            return {"ok": True, "result": {"message_id": mid, "chat": {"id": chat_id}}}

        # default shape
        return {"ok": True, "result": True}

    # ---- convenience queries ----

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def calls_of(self, method: str) -> list[dict]:
        return [dict(p) for m, p in self.calls if m == method]

    def reset(self) -> None:
        self.calls.clear()


# ── Pytest fixtures ──────────────────────────────────────────────
@pytest.fixture
def bot():
    """The imported bot module. Fresh per-test (module import is cached)."""
    return _bot


@pytest.fixture
def fake_tg(monkeypatch):
    """Install a FakeTelegram that intercepts every bot.tg_call."""
    fake = FakeTelegram()
    monkeypatch.setattr(_bot, "tg_call", fake)
    # Also neutralize any bare time.sleep that bot might call for pacing.
    monkeypatch.setattr(_bot.time, "sleep", lambda _s: None)
    return fake


@pytest.fixture
def isolated_state_file(tmp_path, monkeypatch):
    """Redirect bot.STATE_FILE to a fresh path in tmp_path."""
    p = tmp_path / "state.json"
    monkeypatch.setattr(_bot, "STATE_FILE", p)
    return p


@pytest.fixture
def fresh_state(bot):
    """A brand-new bot.State with no users, no seen ids, first_run=True."""
    return bot.State()


@pytest.fixture
def dispatch_state(bot):
    """
    A State that is ready for dispatch_new_listings — i.e. first_run is False
    so the seeding branch is skipped and matched alerts actually go out.
    """
    s = bot.State()
    s.first_run = False
    return s


@pytest.fixture
def load_fixture():
    """Read a JSON fixture from validation/fixtures/<name>.json."""
    def _load(name: str) -> dict:
        path = FIXTURES_DIR / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8"))
    return _load


@pytest.fixture
def make_user(bot):
    """Build a user dict with the requested filter config."""
    def _make(
        cities: list[str] | None = None,
        min_price: int = 0,
        max_price: int = 0,
        paused: bool = False,
    ) -> dict:
        u = bot._default_user()
        u["cities"] = list(cities or [])
        u["min_price"] = int(min_price)
        u["max_price"] = int(max_price)
        u["paused"] = paused
        return u
    return _make


@pytest.fixture
def caplog_bot(caplog):
    """Capture log records from the 'h2s' logger at DEBUG and above."""
    caplog.set_level(logging.DEBUG, logger="h2s")
    return caplog


@pytest.fixture
def stub_fetch(bot, monkeypatch):
    """Monkey-patch bot.fetch_listings to return a programmed list AND emit
    the real log signature 'h2s fetched N listing(s)', so log-observability
    assertions still hold against the stub."""
    def _stub(items: list[dict] | None):
        def _fake():
            if items is not None:
                bot.log.info("h2s fetched %d listing(s)", len(items))
            return items
        monkeypatch.setattr(bot, "fetch_listings", _fake)
    return _stub
