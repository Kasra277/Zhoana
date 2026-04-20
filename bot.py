"""
Holland2Stay Telegram Monitor — always-on edition, dashboard UX
───────────────────────────────────────────────────────────────
Long-running Python 3.11 service. Designed for Railway / VPS.

Workers (non-daemon threads, joined on shutdown):
  tg     — Telegram long-polling (timeout=25s) + command handling
  h2s    — Holland2Stay GraphQL polling on a jittered interval
  saver  — Debounced atomic writer for state.json

UX model:
  • One "dashboard message" per chat, edited in place for all navigation
    (Main, Settings, Cities, Status, Help).
  • One separate "wizard message" used only for the guided price flow
    (From → To → Confirm); created on entry, collapsed on exit.
  • Inline buttons + answerCallbackQuery toasts for every tap.
  • Commands /start /help /cities /price /status /pause /resume still
    work as silent aliases onto the dashboard screens.
  • After each h2s cycle, a bounded "heartbeat" pass re-renders the
    dashboard footer for users active in the last 24h. No new messages
    are ever produced by the heartbeat.

Notes / tradeoffs:
  • Update offset is advanced BEFORE the handler runs (at-most-once).
  • Alert dedup is best-effort across unclean crashes / redeploys.
  • parse_mode=HTML; every interpolated string goes through html.escape.

Persistence on Railway (IMPORTANT — read before deploying):
  The default container filesystem on Railway is ephemeral. state.json
  therefore survives ONLY:
    • normal process restarts inside the same container instance, and
    • typical crash/OOM recoveries that reuse the writable layer.
  state.json is LOST on every:
    • redeploy (manual, CLI, or git-push auto-deploy),
    • image rebuild,
    • environment-variable change that triggers a redeploy,
    • region migration or platform-initiated service replacement.
  When state.json is lost, every persisted field resets to defaults:
    • user cities = [] (user receives no alerts until reconfigured)
    • user min_price/max_price = 0 (price gate disabled → any-priced
      listings are delivered)
    • user paused = False (previously-paused users become active)
    • update_offset = 0 (Telegram may replay up to ~24h of buffered
      updates once, then self-heal)
    • seen_ids = [] (the first_run guard suppresses a mass-alert burst;
      a small number of currently-visible listings may be silently
      re-marked without alerting)
  To make every field survive redeploys, attach a Railway Volume and
  set STATE_FILE=/data/state.json (or any mount path). This is the
  single highest-leverage upgrade for production reliability.
  This service is designed for a SINGLE replica. Scaling beyond one
  replica is unsupported (Telegram returns 409 Conflict on duplicate
  long-polling; divergent state files per replica would also corrupt
  dedup). See .cursor/plans/railway_persistence_coverage_*.plan.md
  and validation/PERSISTENCE_RUNBOOK.md for the full coverage plan.
"""

from __future__ import annotations

import html
import json
import logging
import os
import random
import re
import signal
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import requests

# ─────────────────────────── Config ────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
H2S_GRAPHQL = "https://www.holland2stay.com/graphql"

ALLOWED_CITIES = [
    "Amersfoort", "Arnhem", "Deventer", "Enschede",
    "Nijmegen", "Zutphen", "Zwolle",
]

AVAILABLE_TO_BOOK_OPTION_ID = "179"

H2S_POLL_SECONDS = int(os.environ.get("H2S_POLL_SECONDS", "90"))
H2S_JITTER_SECONDS = int(os.environ.get("H2S_JITTER_SECONDS", "10"))
TG_LONG_POLL_SECONDS = int(os.environ.get("TG_LONG_POLL_SECONDS", "25"))
STATE_SAVE_DEBOUNCE_SECONDS = float(
    os.environ.get("STATE_SAVE_DEBOUNCE_SECONDS", "2")
)
SEEN_IDS_MAX = int(os.environ.get("SEEN_IDS_MAX", "2000"))
HEARTBEAT_ACTIVE_WINDOW_SECONDS = int(
    os.environ.get("HEARTBEAT_ACTIVE_WINDOW_SECONDS", str(24 * 3600))
)
MAX_ALERTS_PER_CYCLE = int(os.environ.get("MAX_ALERTS_PER_CYCLE", "10"))
ALERT_PACING_SECONDS = float(os.environ.get("ALERT_PACING_SECONDS", "0.3"))
CAPTION_MAX_CHARS = 900
DESCRIPTION_MAX_CHARS = 240
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

STATE_SCHEMA_VERSION = 2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Content-Type": "application/json",
    "Origin": "https://www.holland2stay.com",
    "Referer": "https://www.holland2stay.com/residences",
    "Sec-Ch-Ua": "\"Google Chrome\";v=\"131\", \"Chromium\";v=\"131\", \"Not_A Brand\";v=\"24\"",
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": "\"Windows\"",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Store": "nl_en",
}

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
)
log = logging.getLogger("h2s")

if not BOT_TOKEN:
    log.critical("TELEGRAM_BOT_TOKEN env var is required")
    raise SystemExit(1)

# ─────────────────────── HTTP sessions ─────────────────────────
tg_session = requests.Session()
tg_session.headers.update({"Accept": "application/json"})

h2s_session = requests.Session()
h2s_session.headers.update(HEADERS)


# ─────────────────────── Screen constants ──────────────────────
SCR_MAIN = "main"
SCR_SETTINGS = "settings"
SCR_CITIES = "cities"
SCR_STATUS = "status"
SCR_HELP = "help"

AWAIT_PRICE_FROM = "price_from"
AWAIT_PRICE_TO = "price_to"
AWAIT_PRICE_CONFIRM = "price_confirm"

PRICE_FROM_PRESETS = [0, 500, 800, 1000, 1200, 1500]
PRICE_TO_PRESETS = [1000, 1200, 1500, 1800, 2000, 2500]

PULSE_FRAMES = ["·", "•", "●", "•"]


# ─────────────────────── State container ───────────────────────
class State:
    """
    Shared mutable state, protected by `lock`.
    """

    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.update_offset: int = 0
        self.seen_ids: set[str] = set()
        self.seen_order: deque[str] = deque(maxlen=SEEN_IDS_MAX)
        self.first_run: bool = True
        # Dashboard / heartbeat timing (process-local, not persisted):
        self.started_at: float = time.time()
        self.last_check_at: float | None = None
        self.last_check_ok: bool = True
        self.next_check_at: float | None = None
        self.lock = threading.Lock()
        self._dirty = threading.Event()

    def mark_dirty(self) -> None:
        self._dirty.set()

    def take_dirty(self) -> bool:
        was = self._dirty.is_set()
        self._dirty.clear()
        return was

    def add_seen(self, lid: str) -> None:
        if lid in self.seen_ids:
            return
        if len(self.seen_order) == self.seen_order.maxlen:
            self.seen_ids.discard(self.seen_order[0])
        self.seen_order.append(lid)
        self.seen_ids.add(lid)

    def get_user(self, chat_id: int) -> dict:
        key = str(chat_id)
        u = self.users.get(key)
        if u is None:
            u = _default_user()
            self.users[key] = u
        else:
            _ensure_user_defaults(u)
        return u


def _default_user() -> dict:
    return {
        "cities": [],
        "min_price": 0,
        "max_price": 0,
        "paused": False,
        "awaiting": None,
        "pending": {"min": None, "max": None, "cities": None},
        "dashboard_message_id": None,
        "wizard_message_id": None,
        "screen": SCR_MAIN,
        "last_seen_at": None,
    }


def _ensure_user_defaults(u: dict) -> None:
    """Backfill any missing fields on older state rows."""
    u.setdefault("cities", [])
    u.setdefault("min_price", 0)
    u.setdefault("max_price", 0)
    u.setdefault("paused", False)
    # Old value "price_range" is no longer valid; clear it.
    if u.get("awaiting") == "price_range":
        u["awaiting"] = None
    u.setdefault("awaiting", None)
    if not isinstance(u.get("pending"), dict):
        u["pending"] = {"min": None, "max": None, "cities": None}
    else:
        u["pending"].setdefault("min", None)
        u["pending"].setdefault("max", None)
        u["pending"].setdefault("cities", None)
    u.setdefault("dashboard_message_id", None)
    u.setdefault("wizard_message_id", None)
    u.setdefault("screen", SCR_MAIN)
    u.setdefault("last_seen_at", None)


# ─────────────────────── State persistence ─────────────────────
def load_state() -> State:
    s = State()
    if not STATE_FILE.exists():
        log.info("No state file; starting fresh")
        return s
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        quarantine = STATE_FILE.with_suffix(
            f".json.corrupt.{int(time.time())}"
        )
        try:
            STATE_FILE.rename(quarantine)
            log.error("state.json unreadable (%s); moved to %s", e, quarantine)
        except OSError:
            log.exception("failed to quarantine corrupt state file")
        return s

    s.users = raw.get("users") or {}
    s.update_offset = int(raw.get("update_offset") or 0)

    seen_list = raw.get("seen_ids") or []
    for lid in seen_list[-SEEN_IDS_MAX:]:
        s.add_seen(str(lid))
    s.first_run = not seen_list

    for u in s.users.values():
        _ensure_user_defaults(u)

    log.info(
        "Loaded state: version=%s users=%d seen=%d offset=%d first_run=%s",
        raw.get("version"), len(s.users), len(s.seen_ids),
        s.update_offset, s.first_run,
    )
    return s


def save_state(s: State) -> None:
    """Atomic write via write-temp-then-rename. Caller holds s.lock."""
    payload = {
        "version": STATE_SCHEMA_VERSION,
        "update_offset": s.update_offset,
        "users": s.users,
        "seen_ids": list(s.seen_order),
    }
    data = json.dumps(payload, indent=2, ensure_ascii=False)
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    try:
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except OSError:
        log.exception("Failed to persist state")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# ──────────────────────── Telegram API ─────────────────────────
def tg_call(
    method: str,
    *,
    session: requests.Session = tg_session,
    timeout: float = 30.0,
    retries: int = 3,
    **params: Any,
) -> dict:
    """
    POST to Telegram with retry on transient errors. Honors 429 retry_after.
    Returns the parsed response dict, or {"ok": False, "error": "..."} on
    definitive failure. Never raises.
    """
    url = f"{TELEGRAM_API}/{method}"
    backoff = 1.0
    last_err = "unknown"
    for attempt in range(1, retries + 1):
        try:
            r = session.post(url, json=params, timeout=timeout)
        except requests.RequestException as e:
            last_err = f"net:{e.__class__.__name__}"
            log.warning("tg %s attempt %d network error: %s", method, attempt, e)
            time.sleep(backoff)
            backoff = min(backoff * 2, 15.0)
            continue

        if r.status_code == 429:
            try:
                retry_after = int(r.json().get("parameters", {}).get("retry_after", 1))
            except ValueError:
                retry_after = 1
            log.warning("tg %s rate-limited, sleeping %ds", method, retry_after)
            time.sleep(min(retry_after, 60))
            continue

        if r.status_code >= 500:
            last_err = f"http:{r.status_code}"
            log.warning("tg %s http %d, retrying", method, r.status_code)
            time.sleep(backoff)
            backoff = min(backoff * 2, 15.0)
            continue

        try:
            return r.json()
        except ValueError:
            last_err = "bad-json"
            log.warning("tg %s non-JSON response (status=%d)", method, r.status_code)
            return {"ok": False, "error": last_err}

    return {"ok": False, "error": last_err}


def send_message(
    chat_id: int | str,
    text: str,
    reply_markup: dict | None = None,
    disable_preview: bool = True,
) -> dict:
    params: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    res = tg_call("sendMessage", **params)
    if not res.get("ok"):
        log.warning("sendMessage to %s failed: %s", chat_id, res)
    return res


def edit_message_text(
    chat_id: int | str,
    message_id: int,
    text: str,
    reply_markup: dict | None = None,
    disable_preview: bool = True,
) -> dict:
    params: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    return tg_call("editMessageText", **params)


def delete_message(chat_id: int | str, message_id: int) -> dict:
    return tg_call("deleteMessage", chat_id=chat_id, message_id=message_id)


def answer_callback(callback_id: str, text: str = "", show_alert: bool = False) -> dict:
    return tg_call(
        "answerCallbackQuery",
        callback_query_id=callback_id,
        text=text,
        show_alert=show_alert,
    )


# Telegram error-description helpers.
def _err_desc(res: dict) -> str:
    return (res.get("description") or res.get("error") or "").lower()


def _is_not_modified(res: dict) -> bool:
    return "message is not modified" in _err_desc(res)


def _is_message_gone(res: dict) -> bool:
    d = _err_desc(res)
    return (
        "message to edit not found" in d
        or "message_id_invalid" in d
        or "message can't be edited" in d
        or "message to delete not found" in d
    )


def _is_blocked(res: dict) -> bool:
    d = _err_desc(res)
    return (
        "bot was blocked by the user" in d
        or "user is deactivated" in d
        or "chat not found" in d
        or "bot can't initiate conversation" in d
    )


# ──────────────── Startup preflight for polling ────────────────
def telegram_preflight() -> None:
    """
    Verify token, log identity, clear any webhook so long polling doesn't
    immediately 409, and register the command menu.
    """
    me = tg_call("getMe", retries=5, timeout=15)
    if me.get("ok"):
        u = me.get("result", {})
        log.info(
            "Telegram auth ok: @%s (id=%s, name=%s)",
            u.get("username"), u.get("id"), u.get("first_name"),
        )
    else:
        log.error("Telegram getMe failed: %s", me)

    info = tg_call("getWebhookInfo", retries=2, timeout=15)
    webhook_url = info.get("result", {}).get("url") if info.get("ok") else None
    if webhook_url:
        log.warning("A webhook was set (%s); deleting to enable long polling", webhook_url)
    else:
        log.info("No webhook set; long polling clear to proceed")

    res = tg_call(
        "deleteWebhook",
        drop_pending_updates=False,
        retries=2,
        timeout=15,
    )
    if not res.get("ok"):
        log.warning("deleteWebhook failed (will continue; 409 handler in tg loop): %s", res)

    # Advertise a minimal command menu; other commands still work as aliases.
    commands = [
        {"command": "start", "description": "Open the dashboard"},
        {"command": "help", "description": "How this bot works"},
    ]
    res = tg_call("setMyCommands", commands=commands, retries=2, timeout=15)
    if not res.get("ok"):
        log.warning("setMyCommands failed: %s", res)


# ────────────────────── Formatting helpers ─────────────────────
def _fmt_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        h, m = divmod(s, 3600)
        m = m // 60
        return f"{h}h {m}m" if m else f"{h}h"
    d, rem = divmod(s, 86400)
    h = rem // 3600
    return f"{d}d {h}h" if h else f"{d}d"


def _price_range_str(user: dict) -> str:
    mn = user.get("min_price") or 0
    mx = user.get("max_price") or 0
    mn_s = f"€{int(mn)}" if mn else "€0"
    mx_s = f"€{int(mx)}" if mx else "no limit"
    return f"{mn_s} – {mx_s}"


def _cities_str(cities: list[str]) -> str:
    if not cities:
        return "— none —"
    return ", ".join(html.escape(c) for c in cities)


def _pulse(now_ts: float) -> str:
    return PULSE_FRAMES[int(now_ts // 2) % len(PULSE_FRAMES)]


def _last_check_str(state: State, now_ts: float) -> str:
    if state.last_check_at is None:
        return "not yet"
    delta = now_ts - state.last_check_at
    suffix = "" if state.last_check_ok else "  (fetch failing)"
    return f"{_fmt_duration(delta)} ago{suffix}"


def _next_check_str(state: State, now_ts: float) -> str:
    if state.next_check_at is None:
        return "soon"
    delta = state.next_check_at - now_ts
    if delta <= 0:
        return "any moment"
    return f"in {_fmt_duration(delta)}"


# ─────────────────────── Screen renderers ──────────────────────
def render_main(user: dict, state: State) -> tuple[str, dict]:
    now_ts = time.time()
    status = "⏸ Paused" if user["paused"] else "▶️ Active"
    text = (
        "<b>🏠 Holland2Stay Alerts</b>\n\n"
        f"<b>Status:</b>   {status}\n"
        f"<b>Cities:</b>   {_cities_str(user['cities'])}\n"
        f"<b>Price:</b>    {html.escape(_price_range_str(user))}\n\n"
        f"<i>Last check:  {html.escape(_last_check_str(state, now_ts))}</i>\n"
        f"<i>Next check:  {html.escape(_next_check_str(state, now_ts))}  "
        f"{_pulse(now_ts)}</i>"
    )
    pause_btn = (
        {"text": "▶️ Resume", "callback_data": "toggle:paused"}
        if user["paused"]
        else {"text": "⏸ Pause", "callback_data": "toggle:paused"}
    )
    markup = {
        "inline_keyboard": [
            [pause_btn],
            [
                {"text": "⚙️ Settings", "callback_data": "nav:settings"},
                {"text": "📊 Status", "callback_data": "nav:status"},
            ],
            [
                {"text": "🔄 Refresh", "callback_data": "refresh"},
                {"text": "❓ Help", "callback_data": "nav:help"},
            ],
        ]
    }
    return text, markup


def render_settings(user: dict) -> tuple[str, dict]:
    text = (
        "<b>⚙️ Settings</b>\n\n"
        f"<b>Cities:</b>  {_cities_str(user['cities'])}\n"
        f"<b>Price:</b>   {html.escape(_price_range_str(user))}"
    )
    markup = {
        "inline_keyboard": [
            [
                {"text": "📍 Cities", "callback_data": "nav:cities"},
                {"text": "💶 Price range", "callback_data": "nav:price"},
            ],
            [{"text": "← Back", "callback_data": "nav:main"}],
        ]
    }
    return text, markup


def render_cities(user: dict) -> tuple[str, dict]:
    pending = user["pending"].get("cities")
    selected = list(pending) if pending is not None else list(user["cities"])
    selected_set = {c for c in selected if c in ALLOWED_CITIES}

    sel_str = ", ".join(html.escape(c) for c in sorted(selected_set)) or "— none —"
    text = (
        "<b>📍 Cities to watch</b>\n\n"
        "Tap to toggle, then <b>Save</b> when done.\n\n"
        f"<b>Selected:</b> {sel_str}"
    )

    rows: list[list[dict]] = []
    pair: list[dict] = []
    for city in ALLOWED_CITIES:
        mark = "☑" if city in selected_set else "◻"
        pair.append({
            "text": f"{mark} {city}",
            "callback_data": f"city:{city}",
        })
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([
        {"text": "Select all", "callback_data": "cities:all"},
        {"text": "Clear", "callback_data": "cities:clear"},
    ])
    rows.append([
        {"text": "✓ Save", "callback_data": "cities:save"},
        {"text": "✕ Cancel", "callback_data": "cities:cancel"},
    ])
    return text, {"inline_keyboard": rows}


def render_status(user: dict, state: State) -> tuple[str, dict]:
    now_ts = time.time()
    status = "⏸ Paused" if user["paused"] else "▶️ Active"
    uptime = _fmt_duration(now_ts - state.started_at)
    seen_n = len(state.seen_ids)
    n_users = len(state.users)
    text = (
        "<b>📊 Status</b>\n\n"
        f"<b>Your alerts:</b> {status}\n"
        f"<b>Cities:</b>      {_cities_str(user['cities'])}\n"
        f"<b>Price:</b>       {html.escape(_price_range_str(user))}\n\n"
        f"<b>Last check:</b>  {html.escape(_last_check_str(state, now_ts))}\n"
        f"<b>Next check:</b>  {html.escape(_next_check_str(state, now_ts))}\n"
        f"<b>Bot uptime:</b>  {uptime}\n"
        f"<b>Listings seen:</b> {seen_n}\n"
        f"<b>Subscribers:</b>   {n_users}"
    )
    markup = {"inline_keyboard": [[{"text": "← Back", "callback_data": "nav:main"}]]}
    return text, markup


HELP_BODY = (
    "<b>❓ Help</b>\n\n"
    "I watch Holland2Stay and ping you when a new apartment matching "
    "your filters becomes available to book.\n\n"
    "<b>How to use:</b>\n"
    "• Open <b>Settings</b> → pick your <b>Cities</b>.\n"
    "• Open <b>Settings</b> → set your <b>Price range</b>.\n"
    "• Use <b>Pause</b> / <b>Resume</b> to stop or start alerts.\n"
    "• Tap <b>Refresh</b> to update the dashboard.\n\n"
    "Alerts arrive as new messages as soon as listings appear."
)


def render_help() -> tuple[str, dict]:
    markup = {"inline_keyboard": [[{"text": "← Back", "callback_data": "nav:main"}]]}
    return HELP_BODY, markup


def _price_preset_rows(
    presets: list[int], prefix: str, extra_last: dict
) -> list[list[dict]]:
    rows: list[list[dict]] = []
    row: list[dict] = []
    for p in presets:
        label = f"€{p}" if p > 0 else "€0"
        row.append({"text": label, "callback_data": f"{prefix}:{p}"})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([extra_last])
    return rows


def render_wizard_from(user: dict) -> tuple[str, dict]:
    text = (
        "<b>💶 Price range (1/3)</b>\n\n"
        "What's your <b>minimum</b> budget per month?\n\n"
        "Reply with a number, or tap a preset."
    )
    rows = _price_preset_rows(
        PRICE_FROM_PRESETS,
        "price:from",
        {"text": "No minimum", "callback_data": "price:nomin"},
    )
    rows.append([{"text": "✕ Cancel", "callback_data": "price:cancel"}])
    return text, {"inline_keyboard": rows}


def render_wizard_to(user: dict) -> tuple[str, dict]:
    mn = user["pending"].get("min") or 0
    mn_s = f"€{int(mn)}" if mn else "€0"
    text = (
        "<b>💶 Price range (2/3)</b>\n\n"
        f"Minimum: <b>{html.escape(mn_s)}</b>\n\n"
        "What's your <b>maximum</b> budget per month?\n\n"
        "Reply with a number, or tap a preset."
    )
    rows = _price_preset_rows(
        PRICE_TO_PRESETS,
        "price:to",
        {"text": "No limit", "callback_data": "price:nomax"},
    )
    rows.append([
        {"text": "← Back", "callback_data": "price:back"},
        {"text": "✕ Cancel", "callback_data": "price:cancel"},
    ])
    return text, {"inline_keyboard": rows}


def render_wizard_confirm(user: dict) -> tuple[str, dict]:
    mn = user["pending"].get("min") or 0
    mx = user["pending"].get("max") or 0
    mn_s = f"€{int(mn)}" if mn else "€0"
    mx_s = f"€{int(mx)}" if mx else "no limit"
    text = (
        "<b>💶 Price range (3/3)</b>\n\n"
        "Save this range?\n\n"
        f"<b>{html.escape(mn_s)} – {html.escape(mx_s)}</b>"
    )
    markup = {
        "inline_keyboard": [
            [
                {"text": "✓ Save", "callback_data": "price:save"},
                {"text": "✎ Edit", "callback_data": "price:edit"},
            ],
            [{"text": "✕ Cancel", "callback_data": "price:cancel"}],
        ]
    }
    return text, markup


# ─────────────────────── Dashboard plumbing ────────────────────
def _render_screen(screen: str, user: dict, state: State) -> tuple[str, dict]:
    if screen == SCR_SETTINGS:
        return render_settings(user)
    if screen == SCR_CITIES:
        return render_cities(user)
    if screen == SCR_STATUS:
        return render_status(user, state)
    if screen == SCR_HELP:
        return render_help()
    return render_main(user, state)


def upsert_dashboard(
    chat_id: int,
    user: dict,
    screen: str,
    state: State,
) -> bool:
    """
    Render `screen` as the user's dashboard. Edit in place if we have a
    live dashboard_message_id; otherwise send a new message and record
    its id. Handles Telegram edit failures gracefully.

    Returns True on success, False if the user appears unreachable
    (blocked / chat gone). The returned flag lets callers prune.
    """
    text, markup = _render_screen(screen, user, state)
    msg_id = user.get("dashboard_message_id")

    if msg_id:
        res = edit_message_text(chat_id, msg_id, text, reply_markup=markup)
        if res.get("ok"):
            user["screen"] = screen
            return True
        if _is_not_modified(res):
            user["screen"] = screen
            return True
        if _is_blocked(res):
            log.info("chat %s unreachable (%s); clearing dashboard id", chat_id, _err_desc(res))
            user["dashboard_message_id"] = None
            return False
        if _is_message_gone(res):
            log.info("chat %s dashboard message gone; creating new one", chat_id)
            user["dashboard_message_id"] = None
        else:
            log.warning("chat %s editMessageText failed: %s", chat_id, res)
            user["dashboard_message_id"] = None

    res = send_message(chat_id, text, reply_markup=markup)
    if res.get("ok"):
        result = res.get("result") or {}
        new_id = result.get("message_id")
        if new_id:
            user["dashboard_message_id"] = int(new_id)
            user["screen"] = screen
            return True
    if _is_blocked(res):
        log.info("chat %s unreachable on send; leaving dashboard cleared", chat_id)
    return False


def clear_wizard_message(chat_id: int, user: dict, note: str | None = None) -> None:
    """Delete the wizard message, or edit it to a one-line summary on Save."""
    msg_id = user.get("wizard_message_id")
    if not msg_id:
        return
    if note is not None:
        res = edit_message_text(chat_id, msg_id, note, reply_markup={"inline_keyboard": []})
        if res.get("ok") or _is_not_modified(res):
            user["wizard_message_id"] = None
            return
    res = delete_message(chat_id, msg_id)
    user["wizard_message_id"] = None
    if not res.get("ok") and not _is_message_gone(res):
        log.debug("wizard cleanup for %s: %s", chat_id, res)


def show_wizard(chat_id: int, user: dict, step: str) -> bool:
    """Render the price wizard at `step` in the dedicated wizard message."""
    if step == AWAIT_PRICE_FROM:
        text, markup = render_wizard_from(user)
    elif step == AWAIT_PRICE_TO:
        text, markup = render_wizard_to(user)
    elif step == AWAIT_PRICE_CONFIRM:
        text, markup = render_wizard_confirm(user)
    else:
        log.warning("unknown wizard step %r", step)
        return False

    user["awaiting"] = step
    msg_id = user.get("wizard_message_id")
    if msg_id:
        res = edit_message_text(chat_id, msg_id, text, reply_markup=markup)
        if res.get("ok") or _is_not_modified(res):
            return True
        if _is_blocked(res):
            user["wizard_message_id"] = None
            return False
        # message gone or some other error — fall through to resend
        user["wizard_message_id"] = None

    res = send_message(chat_id, text, reply_markup=markup)
    if res.get("ok"):
        new_id = (res.get("result") or {}).get("message_id")
        if new_id:
            user["wizard_message_id"] = int(new_id)
            return True
    return False


def toast(cq_id: str, text: str = "", alert: bool = False) -> None:
    answer_callback(cq_id, text=text, show_alert=alert)


# ────────────────────────── Handlers ───────────────────────────
def _parse_int_message(text: str) -> int | None:
    m = re.search(r"\d+", text)
    if not m:
        return None
    try:
        return int(m.group(0))
    except ValueError:
        return None


def _commit_cities_pending(user: dict) -> None:
    pending = user["pending"].get("cities")
    if pending is None:
        return
    clean = [c for c in pending if c in ALLOWED_CITIES]
    # Preserve ALLOWED_CITIES ordering for display stability.
    user["cities"] = [c for c in ALLOWED_CITIES if c in set(clean)]
    user["pending"]["cities"] = None


def _abort_cities_pending(user: dict) -> None:
    user["pending"]["cities"] = None


def _abort_price_wizard(user: dict) -> None:
    user["awaiting"] = None
    user["pending"]["min"] = None
    user["pending"]["max"] = None


def handle_command(chat_id: int, text: str, user: dict, state: State) -> None:
    """Route /commands to dashboard screens or wizard starts."""
    cmd = text.split()[0].lower() if text else ""
    # Strip @botname suffix if present.
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]

    if cmd == "/start":
        upsert_dashboard(chat_id, user, SCR_MAIN, state)
    elif cmd == "/help":
        upsert_dashboard(chat_id, user, SCR_HELP, state)
    elif cmd == "/status":
        upsert_dashboard(chat_id, user, SCR_STATUS, state)
    elif cmd == "/cities":
        user["pending"]["cities"] = list(user["cities"])
        upsert_dashboard(chat_id, user, SCR_CITIES, state)
    elif cmd == "/price":
        user["pending"]["min"] = None
        user["pending"]["max"] = None
        show_wizard(chat_id, user, AWAIT_PRICE_FROM)
    elif cmd == "/pause":
        user["paused"] = True
        upsert_dashboard(chat_id, user, SCR_MAIN, state)
    elif cmd == "/resume":
        user["paused"] = False
        upsert_dashboard(chat_id, user, SCR_MAIN, state)
    else:
        # Unknown command — just show the dashboard. Less abrasive than a
        # "Unknown command" spam message.
        upsert_dashboard(chat_id, user, SCR_MAIN, state)


def handle_wizard_text(
    chat_id: int, text: str, user: dict, state: State
) -> None:
    """Handle a text reply while inside the price wizard."""
    awaiting = user.get("awaiting")
    n = _parse_int_message(text)
    if n is None:
        # Gentle inline reminder via the wizard message itself.
        if awaiting == AWAIT_PRICE_FROM:
            note = (
                "<b>💶 Price range (1/3)</b>\n\n"
                "Please send a number like <code>800</code>, "
                "or tap a preset."
            )
            _, markup = render_wizard_from(user)
        elif awaiting == AWAIT_PRICE_TO:
            mn = user["pending"].get("min") or 0
            mn_s = f"€{int(mn)}" if mn else "€0"
            note = (
                "<b>💶 Price range (2/3)</b>\n\n"
                f"Minimum: <b>{html.escape(mn_s)}</b>\n\n"
                "Please send a number like <code>1500</code>, "
                "or tap a preset."
            )
            _, markup = render_wizard_to(user)
        else:
            return
        msg_id = user.get("wizard_message_id")
        if msg_id:
            edit_message_text(chat_id, msg_id, note, reply_markup=markup)
        else:
            show_wizard(chat_id, user, awaiting)
        return

    if awaiting == AWAIT_PRICE_FROM:
        user["pending"]["min"] = max(0, n)
        show_wizard(chat_id, user, AWAIT_PRICE_TO)
    elif awaiting == AWAIT_PRICE_TO:
        mx = max(0, n)
        mn = user["pending"].get("min") or 0
        if mx and mn and mx < mn:
            mn, mx = mx, mn
            user["pending"]["min"] = mn
        user["pending"]["max"] = mx
        show_wizard(chat_id, user, AWAIT_PRICE_CONFIRM)


def handle_message(update: dict, state: State) -> None:
    msg = update.get("message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    user = state.get_user(chat_id)
    user["last_seen_at"] = time.time()

    text = (msg.get("text") or "").strip()
    if not text:
        return

    if text.startswith("/"):
        # Commands always override any pending wizard input.
        if user.get("awaiting"):
            _abort_price_wizard(user)
            clear_wizard_message(chat_id, user, note="Cancelled.")
        handle_command(chat_id, text, user, state)
        return

    if user.get("awaiting") in (AWAIT_PRICE_FROM, AWAIT_PRICE_TO):
        handle_wizard_text(chat_id, text, user, state)
        return

    # Not a command and no wizard open — gently point to the dashboard.
    upsert_dashboard(chat_id, user, SCR_MAIN, state)


def handle_callback(update: dict, state: State) -> None:
    cq = update["callback_query"]
    data = cq.get("data") or ""
    msg = cq.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        toast(cq["id"])
        return

    user = state.get_user(int(chat_id))
    user["last_seen_at"] = time.time()
    dash_id = user.get("dashboard_message_id")
    wizard_id = user.get("wizard_message_id")
    msg_id = msg.get("message_id")

    # If the callback was triggered on a stale message we no longer track,
    # silently accept the tap.
    is_dashboard_cb = (msg_id == dash_id) if dash_id else False
    is_wizard_cb = (msg_id == wizard_id) if wizard_id else False

    try:
        _route_callback(chat_id, cq, data, user, state, is_dashboard_cb, is_wizard_cb)
    except Exception:
        log.exception("callback handler failed data=%r", data)
        toast(cq["id"], "Something went wrong")


def _route_callback(
    chat_id: int,
    cq: dict,
    data: str,
    user: dict,
    state: State,
    is_dashboard_cb: bool,
    is_wizard_cb: bool,
) -> None:
    cq_id = cq["id"]

    # ── Navigation ───────────────────────────────────────────
    if data == "nav:main":
        _abort_cities_pending(user)
        upsert_dashboard(chat_id, user, SCR_MAIN, state)
        toast(cq_id)
        return
    if data == "nav:settings":
        _abort_cities_pending(user)
        upsert_dashboard(chat_id, user, SCR_SETTINGS, state)
        toast(cq_id)
        return
    if data == "nav:status":
        upsert_dashboard(chat_id, user, SCR_STATUS, state)
        toast(cq_id)
        return
    if data == "nav:help":
        upsert_dashboard(chat_id, user, SCR_HELP, state)
        toast(cq_id)
        return
    if data == "nav:cities":
        user["pending"]["cities"] = list(user["cities"])
        upsert_dashboard(chat_id, user, SCR_CITIES, state)
        toast(cq_id)
        return
    if data == "nav:price":
        user["pending"]["min"] = None
        user["pending"]["max"] = None
        show_wizard(chat_id, user, AWAIT_PRICE_FROM)
        toast(cq_id)
        return

    # ── Dashboard toggles ────────────────────────────────────
    if data == "toggle:paused":
        user["paused"] = not user["paused"]
        upsert_dashboard(chat_id, user, SCR_MAIN, state)
        toast(cq_id, "Alerts paused" if user["paused"] else "Alerts resumed")
        return

    if data == "refresh":
        upsert_dashboard(chat_id, user, user.get("screen") or SCR_MAIN, state)
        toast(cq_id, "Refreshed")
        return

    # ── Cities selection ─────────────────────────────────────
    if data.startswith("city:"):
        city = data[5:]
        if city not in ALLOWED_CITIES:
            toast(cq_id, "Unknown city")
            return
        pending = user["pending"].get("cities")
        if pending is None:
            pending = list(user["cities"])
        if city in pending:
            pending.remove(city)
            toast_text = f"Removed {city}"
        else:
            pending.append(city)
            toast_text = f"Added {city}"
        user["pending"]["cities"] = pending
        upsert_dashboard(chat_id, user, SCR_CITIES, state)
        toast(cq_id, toast_text)
        return

    if data == "cities:all":
        user["pending"]["cities"] = list(ALLOWED_CITIES)
        upsert_dashboard(chat_id, user, SCR_CITIES, state)
        toast(cq_id, f"All {len(ALLOWED_CITIES)} selected")
        return

    if data == "cities:clear":
        user["pending"]["cities"] = []
        upsert_dashboard(chat_id, user, SCR_CITIES, state)
        toast(cq_id, "Cleared")
        return

    if data == "cities:save":
        _commit_cities_pending(user)
        upsert_dashboard(chat_id, user, SCR_SETTINGS, state)
        toast(cq_id, "Saved")
        return

    if data == "cities:cancel":
        _abort_cities_pending(user)
        upsert_dashboard(chat_id, user, SCR_SETTINGS, state)
        toast(cq_id, "Discarded")
        return

    # ── Price wizard ─────────────────────────────────────────
    if data.startswith("price:from:"):
        try:
            n = int(data.split(":", 2)[2])
        except ValueError:
            toast(cq_id)
            return
        user["pending"]["min"] = max(0, n)
        show_wizard(chat_id, user, AWAIT_PRICE_TO)
        toast(cq_id)
        return

    if data == "price:nomin":
        user["pending"]["min"] = 0
        show_wizard(chat_id, user, AWAIT_PRICE_TO)
        toast(cq_id, "No minimum")
        return

    if data.startswith("price:to:"):
        try:
            n = int(data.split(":", 2)[2])
        except ValueError:
            toast(cq_id)
            return
        mx = max(0, n)
        mn = user["pending"].get("min") or 0
        if mx and mn and mx < mn:
            mn, mx = mx, mn
            user["pending"]["min"] = mn
        user["pending"]["max"] = mx
        show_wizard(chat_id, user, AWAIT_PRICE_CONFIRM)
        toast(cq_id)
        return

    if data == "price:nomax":
        user["pending"]["max"] = 0
        show_wizard(chat_id, user, AWAIT_PRICE_CONFIRM)
        toast(cq_id, "No limit")
        return

    if data == "price:back":
        show_wizard(chat_id, user, AWAIT_PRICE_FROM)
        toast(cq_id)
        return

    if data == "price:edit":
        show_wizard(chat_id, user, AWAIT_PRICE_FROM)
        toast(cq_id)
        return

    if data == "price:save":
        mn = user["pending"].get("min") or 0
        mx = user["pending"].get("max") or 0
        user["min_price"] = int(mn)
        user["max_price"] = int(mx)
        _abort_price_wizard(user)
        mn_s = f"€{int(mn)}" if mn else "€0"
        mx_s = f"€{int(mx)}" if mx else "no limit"
        summary = f"💶 Saved: <b>{html.escape(mn_s)} – {html.escape(mx_s)}</b>"
        clear_wizard_message(chat_id, user, note=summary)
        upsert_dashboard(chat_id, user, SCR_SETTINGS, state)
        toast(cq_id, "Saved")
        return

    if data == "price:cancel":
        _abort_price_wizard(user)
        clear_wizard_message(chat_id, user, note="Cancelled.")
        upsert_dashboard(chat_id, user, SCR_SETTINGS, state)
        toast(cq_id, "Cancelled")
        return

    toast(cq_id)


def handle_update(update: dict, state: State) -> None:
    if "callback_query" in update:
        handle_callback(update, state)
        return
    if "message" in update:
        handle_message(update, state)


# ─────────────────────── Telegram worker ───────────────────────
ALLOWED_UPDATES = ["message", "callback_query"]


def telegram_loop(state: State, stop: threading.Event) -> None:
    log.info("telegram loop started (long poll=%ds)", TG_LONG_POLL_SECONDS)
    backoff = 1.0

    while not stop.is_set():
        try:
            with state.lock:
                offset = state.update_offset

            url = f"{TELEGRAM_API}/getUpdates"
            params = {
                "offset": offset,
                "timeout": TG_LONG_POLL_SECONDS,
                "allowed_updates": ALLOWED_UPDATES,
            }
            try:
                r = tg_session.post(
                    url, json=params, timeout=TG_LONG_POLL_SECONDS + 5
                )
            except requests.RequestException as e:
                log.warning("getUpdates network error: %s (backoff %.1fs)", e, backoff)
                stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                continue

            if r.status_code == 409:
                log.error(
                    "getUpdates 409 Conflict — another instance is polling "
                    "or a webhook is set. Sleeping 30s. Body: %s",
                    r.text[:300],
                )
                stop.wait(30)
                continue

            if r.status_code == 429:
                try:
                    retry_after = int(
                        r.json().get("parameters", {}).get("retry_after", 5)
                    )
                except ValueError:
                    retry_after = 5
                log.warning("getUpdates 429, sleeping %ds", retry_after)
                stop.wait(min(retry_after, 60))
                continue

            if r.status_code != 200:
                log.warning("getUpdates http %d, backoff %.1fs", r.status_code, backoff)
                stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                continue

            try:
                body = r.json()
            except ValueError:
                log.warning("getUpdates non-JSON response")
                stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                continue

            if not body.get("ok"):
                log.warning("getUpdates not ok: %s", body)
                stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                continue

            backoff = 1.0
            updates = body.get("result") or []
            if not updates:
                continue

            log.info("received %d telegram update(s)", len(updates))
            for u in updates:
                with state.lock:
                    # At-most-once: commit offset BEFORE handling.
                    state.update_offset = u["update_id"] + 1
                    state.mark_dirty()
                    try:
                        handle_update(u, state)
                    except Exception:
                        log.exception(
                            "handler failed for update %s", u.get("update_id")
                        )

        except Exception:
            log.exception("telegram loop iteration crashed")
            stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)

    log.info("telegram loop stopped")


# ───────────────── Holland2Stay GraphQL polling ────────────────
# Rich query: today's fields + media + description + optional custom attrs
# (bedrooms / living_area / available_from). If the H2S schema rejects any
# of these, we permanently degrade to GRAPHQL_QUERY for the rest of the
# process lifetime — captions then omit the corresponding rows.
RICH_QUERY = """
query GetAvailable($pageSize: Int!) {
  products(
    filter: { available_to_book: { eq: "%s" } }
    pageSize: $pageSize
  ) {
    total_count
    items {
      sku
      name
      url_key
      city
      bedrooms
      living_area
      available_from
      short_description { html }
      small_image { url label }
      thumbnail { url label }
      media_gallery { url label position disabled }
      price_range {
        minimum_price {
          final_price { value currency }
        }
      }
    }
  }
}
""" % AVAILABLE_TO_BOOK_OPTION_ID

# Safe query: every field is in ProductInterface or already known to work
# against the H2S schema. Used as the fallback when RICH_QUERY is rejected.
GRAPHQL_QUERY = """
query GetAvailable($pageSize: Int!) {
  products(
    filter: { available_to_book: { eq: "%s" } }
    pageSize: $pageSize
  ) {
    total_count
    items {
      sku
      name
      url_key
      city
      price_range {
        minimum_price {
          final_price { value currency }
        }
      }
    }
  }
}
""" % AVAILABLE_TO_BOOK_OPTION_ID

MINIMAL_QUERY = """
query { products(filter: {available_to_book: {eq: "%s"}}, pageSize: 100) {
  items { sku name url_key price_range { minimum_price { final_price { value currency } } } }
} }
""" % AVAILABLE_TO_BOOK_OPTION_ID

# Process-lifetime flag: cleared on the first RICH_QUERY schema rejection.
_rich_query_usable = True


def _post_graphql(query: str, variables: dict | None = None) -> dict | None:
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables
    try:
        r = h2s_session.post(H2S_GRAPHQL, json=payload, timeout=30)
    except requests.RequestException as e:
        log.warning("h2s network error: %s", e)
        return None

    if r.status_code != 200:
        log.warning(
            "h2s http %d (body head: %r)",
            r.status_code, r.text[:200],
        )
        return None

    ctype = r.headers.get("Content-Type", "")
    if "application/json" not in ctype.lower():
        log.warning(
            "h2s non-JSON Content-Type=%r (body head: %r)",
            ctype, r.text[:300],
        )
        return None

    try:
        return r.json()
    except ValueError:
        log.warning("h2s JSON decode failed (body head: %r)", r.text[:300])
        return None


def _post_with_retry(query: str, variables: dict | None) -> dict | None:
    data = _post_graphql(query, variables)
    if data is None:
        time.sleep(2)
        data = _post_graphql(query, variables)
    return data


def fetch_listings() -> list[dict] | None:
    global _rich_query_usable
    data: dict | None = None

    if _rich_query_usable:
        data = _post_with_retry(RICH_QUERY, {"pageSize": 100})
        if data is not None and "errors" in data:
            err_msg = (data["errors"][0] or {}).get("message", "unknown")
            log.warning(
                "h2s rich query rejected (%s); degrading to safe query for "
                "the rest of this process lifetime",
                err_msg,
            )
            _rich_query_usable = False
            data = None

    if data is None:
        data = _post_with_retry(GRAPHQL_QUERY, {"pageSize": 100})
        if data is None:
            return None

    if "errors" in data:
        err_msg = (data["errors"][0] or {}).get("message", "unknown")
        log.warning("h2s graphql errors (%s), retrying minimal query", err_msg)
        data = _post_graphql(MINIMAL_QUERY)
        if data is None or "errors" in data:
            log.error("h2s minimal query also failed: %s", data)
            return None

    items = (((data.get("data") or {}).get("products") or {}).get("items")) or []
    log.info("h2s fetched %d listing(s)", len(items))
    return items


def listing_city(item: dict) -> str:
    c = item.get("city")
    if c:
        if isinstance(c, dict):
            return c.get("label") or c.get("name") or ""
        return str(c)
    haystack = (item.get("name") or "") + " " + (item.get("url_key") or "")
    for city in ALLOWED_CITIES:
        if city.lower() in haystack.lower():
            return city
    return ""


def listing_price(item: dict) -> float | None:
    try:
        return float(item["price_range"]["minimum_price"]["final_price"]["value"])
    except (KeyError, TypeError, ValueError):
        return None


H2S_RESIDENCES_URL = "https://www.holland2stay.com/residences"

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def listing_url(item: dict) -> str:
    url_key = item.get("url_key") or ""
    if not url_key:
        return H2S_RESIDENCES_URL
    # url_key is a slug; still treat defensively.
    return f"https://www.holland2stay.com/residences/{url_key}.html"


def _coerce_url(v: Any) -> str | None:
    """media_gallery entries / image fields may be dict or str."""
    if not v:
        return None
    if isinstance(v, dict):
        u = v.get("url")
        return str(u) if u else None
    return str(v)


def listing_image_candidates(item: dict) -> list[str]:
    """
    Return ordered image URLs to try with sendPhoto:
      1. media_gallery entries with disabled == False, sorted by position
      2. small_image.url
      3. thumbnail.url
    """
    urls: list[str] = []
    seen_urls: set[str] = set()

    gallery = item.get("media_gallery")
    if isinstance(gallery, list):
        enabled = [
            g for g in gallery
            if isinstance(g, dict) and not g.get("disabled")
        ]
        try:
            enabled.sort(key=lambda g: int(g.get("position") or 0))
        except (TypeError, ValueError):
            pass
        for g in enabled:
            u = _coerce_url(g)
            if u and u not in seen_urls:
                seen_urls.add(u)
                urls.append(u)

    for field in ("small_image", "thumbnail", "image"):
        u = _coerce_url(item.get(field))
        if u and u not in seen_urls:
            seen_urls.add(u)
            urls.append(u)

    return urls


def listing_bedrooms(item: dict) -> str | None:
    for k in ("bedrooms", "bedrooms_count", "bedroom_count",
              "number_of_bedrooms", "no_of_rooms"):
        v = item.get(k)
        if v:
            return str(v)
    return None


def listing_size(item: dict) -> str | None:
    for k in ("living_area", "square_meters", "floor_space",
              "surface", "surface_area", "area"):
        v = item.get(k)
        if v:
            return str(v)
    return None


def listing_available_from(item: dict) -> str | None:
    for k in ("available_from", "available_from_date", "available_on"):
        v = item.get(k)
        if not v:
            continue
        s = str(v).strip()
        # Trim ISO datetimes to the date portion.
        if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
            return s[:10]
        return s
    return None


def _extract_short_description_text(item: dict) -> str:
    sd = item.get("short_description")
    if isinstance(sd, dict):
        raw = sd.get("html") or sd.get("text") or ""
    else:
        raw = sd or ""
    if not raw:
        return ""
    # Strip HTML tags, then unescape entities, then collapse whitespace.
    text = _HTML_TAG_RE.sub(" ", str(raw))
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"


def build_alert_caption(item: dict, city: str, *, include_url: bool) -> str:
    """
    Build the HTML-safe alert caption (also used as the text-fallback body).
    All interpolated values are html.escape'd. Fields degrade individually:
    any missing field drops its cell; an empty facts row drops entirely.
    Final output is hard-capped at CAPTION_MAX_CHARS.
    """
    name = html.escape(item.get("name") or "New listing")
    city_s = html.escape(city) if city else "Location on Holland2Stay"

    price = listing_price(item)
    price_s = f"€{int(price)}/month" if price else "price on site"

    lines: list[str] = [f"🏠 <b>{name}</b>"]
    lines.append(f"📍 {city_s}  ·  💶 {html.escape(price_s)}")

    facts: list[str] = []
    beds = listing_bedrooms(item)
    if beds:
        facts.append(f"🛏 {html.escape(beds)} bed")
    size = listing_size(item)
    if size:
        facts.append(f"📐 {html.escape(size)} m²")
    avail = listing_available_from(item)
    if avail:
        facts.append(f"📅 from {html.escape(avail)}")
    if facts:
        lines.append("  ·  ".join(facts))

    desc = _extract_short_description_text(item)
    if desc:
        desc = _truncate(desc, DESCRIPTION_MAX_CHARS)
        lines.append("")
        lines.append(f"<i>{html.escape(desc)}</i>")

    if include_url:
        url = listing_url(item)
        lines.append("")
        lines.append(
            f"<a href=\"{html.escape(url, quote=True)}\">"
            f"→ View on Holland2Stay</a>"
        )

    caption = "\n".join(lines)
    if len(caption) > CAPTION_MAX_CHARS:
        caption = caption[: CAPTION_MAX_CHARS - 1].rstrip() + "…"
    return caption


def alert_keyboard(open_url: str, *, open_label: str = "🔗 Open listing") -> dict:
    return {
        "inline_keyboard": [
            [{"text": open_label, "url": open_url}],
            [
                {"text": "⏸ Pause alerts", "callback_data": "toggle:paused"},
                {"text": "⚙ Settings", "callback_data": "nav:settings"},
            ],
        ]
    }


def send_photo(
    chat_id: int | str,
    photo: str,
    caption: str,
    reply_markup: dict | None = None,
) -> dict:
    params: dict[str, Any] = {
        "chat_id": chat_id,
        "photo": photo,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    return tg_call("sendPhoto", **params)


def send_alert(chat_id: int, item: dict, city: str) -> dict:
    """
    Deliver one listing alert using the photo-first fallback chain:
      1. sendPhoto with each candidate image (stop trying images on a
         non-photo error — e.g. user blocked).
      2. sendMessage with caption + trailing link line, webpage preview on.
      3. Terminal sendMessage with preview disabled.

    Returns the final tg_call response (ok or the last failure). The
    returned dict is what the caller inspects for _is_blocked pruning.
    """
    url = listing_url(item)
    keyboard = alert_keyboard(url)
    photo_caption = build_alert_caption(item, city, include_url=False)
    text_body = build_alert_caption(item, city, include_url=True)

    last_res: dict = {"ok": False, "error": "no attempt"}

    for image_url in listing_image_candidates(item):
        res = send_photo(chat_id, image_url, photo_caption, reply_markup=keyboard)
        last_res = res
        if res.get("ok"):
            return res
        if _is_blocked(res):
            # User-side failure: no point trying more images or text.
            return res
        desc = _err_desc(res)
        log.info(
            "sendPhoto to %s failed (%s); trying next image / fallback",
            chat_id, desc or "unknown",
        )
        # Loop to next candidate image.

    # Text fallback #1: allow Telegram to scrape a webpage preview.
    res = send_message(
        chat_id, text_body, reply_markup=keyboard, disable_preview=False
    )
    if res.get("ok") or _is_blocked(res):
        return res
    last_res = res

    # Text fallback #2: preview off. The URL button still opens the listing.
    res = send_message(
        chat_id, text_body, reply_markup=keyboard, disable_preview=True
    )
    return res if res.get("ok") else last_res


def matches_user(city: str, price: float | None, user: dict) -> bool:
    if user["paused"] or not user["cities"]:
        return False
    if not city or not any(c.lower() == city.lower() for c in user["cities"]):
        return False
    if price is not None:
        if user["min_price"] and price < user["min_price"]:
            return False
        if user["max_price"] and price > user["max_price"]:
            return False
    return True


def dispatch_new_listings(state: State) -> None:
    items = fetch_listings()
    if items is None:
        with state.lock:
            state.last_check_at = time.time()
            state.last_check_ok = False
            state.mark_dirty()
        log.info("h2s fetch failed; keeping seen_ids untouched this cycle")
        return

    with state.lock:
        state.last_check_at = time.time()
        state.last_check_ok = True

        new_items: list[tuple[str, dict]] = []
        batch_seen: set[str] = set()
        for it in items:
            lid = str(it.get("sku") or it.get("url_key") or "")
            if not lid:
                continue
            if lid in state.seen_ids or lid in batch_seen:
                continue
            batch_seen.add(lid)
            new_items.append((lid, it))

        first_run = state.first_run and not state.seen_ids
        if first_run:
            log.info(
                "first run — seeding %d listing(s) without sending alerts",
                len(new_items),
            )
            for lid, _ in new_items:
                state.add_seen(lid)
            state.first_run = False
            state.mark_dirty()
            return

        to_send: list[tuple[str, dict, str, float | None, list[int]]] = []
        for lid, item in new_items:
            city = listing_city(item)
            if not city:
                log.warning(
                    "could not resolve city for listing sku=%s name=%r",
                    item.get("sku"), item.get("name"),
                )
            if not item.get("url_key"):
                log.warning(
                    "listing sku=%s missing url_key; using residences fallback URL",
                    item.get("sku"),
                )
            price = listing_price(item)
            recipients = [
                int(chat_id_str)
                for chat_id_str, user in state.users.items()
                if matches_user(city, price, user)
            ]
            state.add_seen(lid)
            to_send.append((lid, item, city, price, recipients))
        if new_items:
            state.mark_dirty()

    # Aggregate alerts per user so we can enforce a per-user soft cap and
    # a clean summary message for any excess on burst cycles.
    user_alerts: dict[int, list[tuple[str, dict, str]]] = {}
    for lid, item, city, _price, recipients in to_send:
        for chat_id in recipients:
            user_alerts.setdefault(chat_id, []).append((lid, item, city))

    log.info(
        "h2s cycle: new=%d total=%d users_with_matches=%d",
        len(to_send), len(items), len(user_alerts),
    )

    for chat_id, alerts in user_alerts.items():
        _deliver_alerts_to_user(state, chat_id, alerts)


def _deliver_alerts_to_user(
    state: State,
    chat_id: int,
    alerts: list[tuple[str, dict, str]],
) -> None:
    cap = max(1, MAX_ALERTS_PER_CYCLE)
    if len(alerts) > cap:
        # Send cap-1 full alerts + one summary describing the remainder.
        head = alerts[: cap - 1]
        tail = alerts[cap - 1 :]
    else:
        head = alerts
        tail = []

    sent = 0
    for lid, item, city in head:
        res = send_alert(chat_id, item, city)
        if res.get("ok"):
            sent += 1
        else:
            log.warning(
                "alert to %s failed for listing %s: %s", chat_id, lid, res
            )
            if _is_blocked(res):
                with state.lock:
                    u = state.users.get(str(chat_id))
                    if u is not None:
                        u["dashboard_message_id"] = None
                        state.mark_dirty()
                log.info(
                    "chat %s unreachable; aborting remaining %d alert(s)",
                    chat_id, len(head) - sent - 1 + len(tail),
                )
                return
        time.sleep(ALERT_PACING_SECONDS)

    if tail:
        summary = (
            f"🔔 <b>+{len(tail)} more new listings</b> matched your filters "
            f"this cycle. You can browse them all on Holland2Stay."
        )
        kb = alert_keyboard(H2S_RESIDENCES_URL, open_label="🔗 Browse listings")
        res = send_message(
            chat_id, summary, reply_markup=kb, disable_preview=True
        )
        if res.get("ok"):
            sent += 1
            log.info(
                "chat %s: delivered %d alerts + summary for %d more",
                chat_id, len(head), len(tail),
            )
        else:
            log.warning("summary to %s failed: %s", chat_id, res)
            if _is_blocked(res):
                with state.lock:
                    u = state.users.get(str(chat_id))
                    if u is not None:
                        u["dashboard_message_id"] = None
                        state.mark_dirty()

    if sent:
        log.debug("chat %s: %d alert message(s) delivered", chat_id, sent)


# ────────────────────── Heartbeat refresher ────────────────────
def heartbeat_refresh(state: State) -> None:
    """
    After each h2s cycle, re-render the main dashboard for users that:
      • have a live dashboard_message_id, AND
      • are currently on the main screen (not mid-flow), AND
      • interacted in the last HEARTBEAT_ACTIVE_WINDOW_SECONDS, AND
      • are not paused.

    Never creates a new message. On edit failure we prune the stale id.
    """
    now_ts = time.time()
    cutoff = now_ts - HEARTBEAT_ACTIVE_WINDOW_SECONDS

    with state.lock:
        targets: list[tuple[int, int]] = []
        for chat_id_str, user in state.users.items():
            if user.get("paused"):
                continue
            if user.get("awaiting"):
                continue
            if (user.get("screen") or SCR_MAIN) != SCR_MAIN:
                continue
            msg_id = user.get("dashboard_message_id")
            if not msg_id:
                continue
            last_seen = user.get("last_seen_at")
            if last_seen is None or last_seen < cutoff:
                continue
            try:
                targets.append((int(chat_id_str), int(msg_id)))
            except (TypeError, ValueError):
                continue

    if not targets:
        return

    log.info("heartbeat: refreshing %d dashboard(s)", len(targets))

    for chat_id, msg_id in targets:
        with state.lock:
            user = state.users.get(str(chat_id))
            if not user or user.get("dashboard_message_id") != msg_id:
                continue
            # Only heartbeat main screen; user may have moved since snapshot.
            if (user.get("screen") or SCR_MAIN) != SCR_MAIN or user.get("awaiting"):
                continue
            text, markup = render_main(user, state)

        res = edit_message_text(chat_id, msg_id, text, reply_markup=markup)
        if res.get("ok") or _is_not_modified(res):
            continue
        if _is_blocked(res) or _is_message_gone(res):
            with state.lock:
                user = state.users.get(str(chat_id))
                if user and user.get("dashboard_message_id") == msg_id:
                    user["dashboard_message_id"] = None
                    state.mark_dirty()
            log.info("heartbeat: pruned dashboard id for chat %s (%s)",
                     chat_id, _err_desc(res))
        else:
            log.debug("heartbeat edit failed for %s: %s", chat_id, res)


# ───────────────────────── H2S worker ──────────────────────────
def _jittered_interval() -> float:
    base = H2S_POLL_SECONDS
    jitter = random.uniform(-H2S_JITTER_SECONDS, H2S_JITTER_SECONDS)
    return max(30.0, float(base) + jitter)


def h2s_loop(state: State, stop: threading.Event) -> None:
    log.info(
        "h2s loop started (base=%ds jitter=±%ds)",
        H2S_POLL_SECONDS, H2S_JITTER_SECONDS,
    )
    stop.wait(min(5.0, _jittered_interval()))
    while not stop.is_set():
        try:
            dispatch_new_listings(state)
        except Exception:
            log.exception("h2s cycle crashed")

        try:
            heartbeat_refresh(state)
        except Exception:
            log.exception("heartbeat refresh crashed")

        interval = _jittered_interval()
        with state.lock:
            state.next_check_at = time.time() + interval
        stop.wait(interval)
    log.info("h2s loop stopped")


# ────────────────────── State saver worker ─────────────────────
def state_saver_loop(state: State, stop: threading.Event) -> None:
    log.info(
        "state saver started (debounce=%.1fs path=%s)",
        STATE_SAVE_DEBOUNCE_SECONDS, STATE_FILE,
    )
    while not stop.is_set():
        stop.wait(STATE_SAVE_DEBOUNCE_SECONDS)
        if state.take_dirty():
            try:
                with state.lock:
                    save_state(state)
            except Exception:
                log.exception("saver: save_state failed")
    log.info("state saver stopped")


# ─────────────────────────── Main ──────────────────────────────
def install_signal_handlers(stop: threading.Event) -> None:
    def _handler(signum: int, _frame: Any) -> None:
        log.info("received signal %s; beginning graceful shutdown", signum)
        stop.set()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            log.debug("could not install handler for %s", sig_name)


def run() -> None:
    log.info(
        "starting h2s bot | poll=%ds jitter=±%ds tg_long_poll=%ds",
        H2S_POLL_SECONDS, H2S_JITTER_SECONDS, TG_LONG_POLL_SECONDS,
    )
    state = load_state()
    stop = threading.Event()
    install_signal_handlers(stop)

    try:
        telegram_preflight()
    except Exception:
        log.exception("telegram preflight crashed; continuing anyway")

    workers: list[tuple[threading.Thread, float]] = [
        (threading.Thread(target=telegram_loop, args=(state, stop), name="tg"), 10.0),
        (threading.Thread(target=h2s_loop, args=(state, stop), name="h2s"), 5.0),
        (threading.Thread(target=state_saver_loop, args=(state, stop), name="saver"), 3.0),
    ]
    for w, _ in workers:
        w.start()

    try:
        while not stop.is_set():
            stop.wait(1.0)
            for w, _ in workers:
                if not w.is_alive():
                    log.error("worker %s died unexpectedly; initiating shutdown", w.name)
                    stop.set()
                    break
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt; shutting down")
        stop.set()

    log.info("joining workers…")
    any_alive = False
    for w, timeout in workers:
        w.join(timeout=timeout)
        if w.is_alive():
            any_alive = True
            log.warning("worker %s did not exit within %.0fs", w.name, timeout)

    try:
        with state.lock:
            save_state(state)
        log.info("final state flushed")
    except Exception:
        log.exception("final state flush failed")

    if any_alive:
        log.warning("forcing process exit due to stuck worker(s)")
        os._exit(0)

    log.info("h2s bot exited cleanly")


if __name__ == "__main__":
    run()
