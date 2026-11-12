"""
Talking to Telegram: the Bot API client, the webhook/menu setup done at
start, and messages to customers and admins.

There is no long-running bot process. Telegram delivers updates to our
webhook (/api/bot/webhook), and we send messages with plain HTTP calls.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time

import httpx
from sqlalchemy.orm import Session

from . import core
from .config import settings as env

API_BASE = os.getenv("TG_API_BASE", "https://api.telegram.org").rstrip("/")

_me: dict = {}
_me_lock = threading.Lock()


class Blocked(Exception):
    """The person blocked the bot or deleted the account."""


def call(method: str, payload: dict | None = None, timeout: float = 15) -> dict:
    """One Bot API call. Never raises on Telegram errors — returns the answer."""
    if not env.bot_token:
        return {"ok": False, "description": "BOT_TOKEN is not set"}
    try:
        r = httpx.post(f"{API_BASE}/bot{env.bot_token}/{method}", json=payload or {},
                       timeout=timeout)
    except httpx.HTTPError as e:
        return {"ok": False, "description": f"network: {e.__class__.__name__}"}
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "description": f"HTTP {r.status_code}"}


def username() -> str:
    """The bot's @username, asked once from Telegram."""
    with _me_lock:
        if not _me.get("username") and env.bot_token:
            res = call("getMe", timeout=8)
            if res.get("ok"):
                _me.update(res["result"])
        return _me.get("username", "")


def webhook_secret() -> str:
    """Secret Telegram puts into X-Telegram-Bot-Api-Secret-Token. Derived
    from SECRET_KEY so it survives restarts and never appears in the URL."""
    return hashlib.sha256(("tg-webhook:" + env.secret_key).encode()).hexdigest()[:48]


def app_url(query: str = "") -> str:
    return f"{env.public_url}/app" + (f"?{query}" if query else "")


def open_button(text: str, query: str = "") -> dict:
    return {"inline_keyboard": [[{"text": text, "web_app": {"url": app_url(query)}}]]}


# ---------------------------------------------------------------------------
def setup(db: Session) -> list[str]:
    """
    Register the webhook, the menu button and the /start command.
    Runs at start when BOT_SETUP is on and PUBLIC_URL is https.
    """
    done = []
    if not (env.bot_token and env.https):
        return done
    res = call("setWebhook", {
        "url": f"{env.public_url}/api/bot/webhook",
        "secret_token": webhook_secret(),
        "allowed_updates": ["message", "pre_checkout_query", "callback_query"],
        "drop_pending_updates": False,
    })
    done.append(f"webhook: {'ok' if res.get('ok') else res.get('description')}")
    brand = core.get(db, "brand_name")
    # The menu button is one for everybody, so it speaks the default language.
    res = call("setChatMenuButton", {"menu_button": {
        "type": "web_app", "text": core.t(env.default_lang, "open_app"),
        "web_app": {"url": app_url()}}})
    done.append(f"menu button: {'ok' if res.get('ok') else res.get('description')}")
    for lang in ("ru", "en"):
        payload = {"commands": [{"command": "start", "description": brand[:256]},
                                {"command": "support", "description": core.t(lang, "cmd_support")}]}
        if lang != env.default_lang:
            payload["language_code"] = lang
        call("setMyCommands", payload)
    from . import botadmin
    botadmin.setup_commands(env.default_lang)
    done.append(f"admin commands: {len(env.admin_telegram_ids)} admin(s)")
    username()
    return done


def set_descriptions(db: Session) -> dict:
    """The bot's texts in Telegram itself: the short description (profile,
    shared links) and the description in an empty chat, in ru and en.
    Empty = Telegram's own default. Returns what Telegram answered."""
    out = {}
    for lang in ("ru", "en"):
        code = "ru" if lang == "ru" else ""           # "" = every other language
        about = core.get(db, f"bot_about_{lang}")
        desc = core.get(db, f"bot_desc_{lang}")
        a = call("setMyShortDescription", {"short_description": about, "language_code": code})
        d = call("setMyDescription", {"description": desc, "language_code": code})
        out[lang] = bool(a.get("ok") and d.get("ok"))
    return out


def tunnel_address() -> str:
    """The current https address of the Cloudflare quick tunnel, or ""."""
    if not env.tunnel_metrics_url:
        return ""
    try:
        # trust_env=False: the tunnel is on the Docker network, never behind
        # a proxy from the environment.
        r = httpx.get(f"{env.tunnel_metrics_url}/quicktunnel", timeout=5, trust_env=False)
        host = str(r.json().get("hostname") or "").strip().lower()
    except (httpx.HTTPError, ValueError, AttributeError):
        return ""
    if not host or "/" in host or " " in host:
        return ""
    return f"https://{host}"


def follow_tunnel(db: Session) -> bool:
    """
    Tunnel mode: a quick tunnel gets a new address every time it restarts.
    When it does, switch PUBLIC_URL to it and register the webhook and the
    menu button again, so the bot keeps working without anyone editing .env.
    Returns True when the address changed.
    """
    url = tunnel_address()
    if not url or url == env.public_url:
        return False
    old = env.public_url
    env.public_url = url
    core.log.info("tunnel address: %s (was %s)", url, old)
    if env.bot_setup and env.bot_token:
        for line in setup(db):
            core.log.info("bot %s", line)
    core.audit(db, "Tunnel address changed", "system", url)
    return True


# ---------------------------------------------------------------------------
def send(chat_id: int, text: str, button: str = "", query: str = "") -> bool:
    """Send a message; raises Blocked if the person blocked the bot."""
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if button and env.https:
        payload["reply_markup"] = open_button(button, query)
    for _ in range(3):
        res = call("sendMessage", payload)
        if res.get("ok"):
            return True
        desc = str(res.get("description", ""))
        code = res.get("error_code")
        if code == 403 or "blocked" in desc or "deactivated" in desc:
            raise Blocked(desc)
        if code == 429:
            time.sleep(min(int((res.get("parameters") or {}).get("retry_after", 2)), 30))
            continue
        if "parse entities" in desc and payload.get("parse_mode"):
            # A stray "<" or "&" (an error text, a broadcast typed without
            # HTML in mind): send it as plain text rather than not at all.
            payload.pop("parse_mode")
            continue
        core.log.warning("telegram refused %s: %s", chat_id, desc)
        return False
    return False


def send_safe(db: Session, user, text: str, button_key: str = "open_app", query: str = "") -> bool:
    lang = user.lang or env.default_lang
    try:
        return send(user.tg_id, text, core.t(lang, button_key), query)
    except Blocked:
        user.bot_blocked = True
        db.commit()
        return False


def admins(text: str) -> None:
    """A short note to every admin — payments, errors. Failures are ignored."""
    for tg_id in env.admin_telegram_ids:
        try:
            send(tg_id, text)
        except Blocked:
            pass
