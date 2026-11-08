"""
Telegram updates (webhook). There is no bot process — Telegram posts
updates here: /start, the two steps of a Stars payment (and the monthly
charges of a Stars subscription), admin commands and the support chat.
"""

from __future__ import annotations

import hmac
import threading
from collections import OrderedDict

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .. import access, botadmin, core, emergency, notify
from ..config import settings as env
from ..db import get_db
from ..models import User
from ..payments import stars

router = APIRouter(prefix="/api/bot", tags=["bot"])


@router.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db),
                  x_telegram_bot_api_secret_token: str = Header(default="")):
    # Without a real SECRET_KEY the webhook secret could be computed from the
    # source code, and Telegram updates (payments!) forged: refuse them.
    if len(env.secret_key) < 32 or not hmac.compare_digest(
            x_telegram_bot_api_secret_token.encode(), notify.webhook_secret().encode()):
        raise HTTPException(403, "forbidden")
    try:
        upd = await request.json()
    except ValueError:
        return {"ok": True}
    upd = upd if isinstance(upd, dict) else {}
    uid = upd.get("update_id")
    # Telegram delivers an update again when the answer was slow or not 200.
    # An update already done (or being done right now) is not done twice:
    # "+30 days" pressed once must add 30 days once.
    if isinstance(uid, int) and not _claim(uid):
        return {"ok": True}
    done = False
    try:
        # Panels and the Bot API are called synchronously: off the event loop.
        await run_in_threadpool(_handle_safe, db, upd)
        done = True
    finally:
        if isinstance(uid, int):
            _release(uid, done)
    return {"ok": True}


# Recent update ids: done, and in progress. One process serves the bot.
_SEEN_MAX = 5000
_seen: "OrderedDict[int, None]" = OrderedDict()
_busy: set[int] = set()
_seen_lock = threading.Lock()


def _claim(uid: int) -> bool:
    with _seen_lock:
        if uid in _seen or uid in _busy:
            return False
        _busy.add(uid)
        return True


def _release(uid: int, done: bool) -> None:
    with _seen_lock:
        _busy.discard(uid)
        if done:
            _seen[uid] = None
            while len(_seen) > _SEEN_MAX:
                _seen.popitem(last=False)


def _handle_safe(db: Session, upd: dict) -> None:
    """One broken update must not come back again and again. Only a
    payment is left to Telegram to repeat: closing an order is safe to
    repeat, and the money must not be lost."""
    try:
        _handle(db, upd)
    except Exception as e:
        db.rollback()
        if (upd.get("message") or {}).get("successful_payment"):
            core.log.exception("payment update %s failed, Telegram will repeat it", upd.get("update_id"))
            raise HTTPException(500, "payment not processed yet")
        core.log.exception("bot update %s failed", upd.get("update_id"))
        try:
            core.audit(db, "Bot update failed", "system", f"{type(e).__name__}: {e}"[:300], level="error")
            cq = upd.get("callback_query")
            if cq:                   # the button stops spinning and says why
                notify.call("answerCallbackQuery", {"callback_query_id": cq.get("id"),
                                                    "text": f"Ошибка / Error: {e}"[:190]})
        except Exception:  # noqa: BLE001
            pass


def _handle(db: Session, upd: dict) -> None:
    # Buttons: the referral captcha, and the admin menus.
    cq = upd.get("callback_query")
    if cq:
        if str(cq.get("data", "")).startswith("cap:"):
            _captcha_answer(db, cq)
        else:
            botadmin.handle_callback(db, cq)
        return

    # Step 1 of a Stars payment: Telegram asks whether to take the money.
    pre = upd.get("pre_checkout_query")
    if pre:
        ok, reason = stars.pre_checkout(db, pre)
        payload = {"pre_checkout_query_id": pre["id"], "ok": ok}
        if not ok:
            payload["error_message"] = reason
        notify.call("answerPreCheckoutQuery", payload, timeout=8)
        return

    msg = upd.get("message") or {}

    # Step 2: the money is in.
    if msg.get("successful_payment"):
        try:
            stars.paid(db, msg["successful_payment"])
        except access.AccessError:
            # Telegram does not repeat this update, but the order is parked
            # as "paid" and the retry job grants it once a server answers.
            notify.admins("Stars payment received, no server accepted the customer yet — "
                          "access will be granted automatically: "
                          f"{msg['successful_payment'].get('invoice_payload')}")
        return

    # Admin commands (/admin, /stats, ...) — only from ADMIN_TELEGRAM_IDS.
    if botadmin.handle_command(db, msg):
        return

    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    if text.startswith("/start") and chat.get("type") == "private":
        _start(db, sender, text[6:].strip())
        return

    # Anything else a person writes goes to support; an admin's reply to a
    # forwarded question goes back to the customer.
    from .. import support
    if support.handle_bot_message(db, msg):
        return
    # Nobody took it (support is off, an unknown command): the bot never
    # stays silent — it shows the way into the app.
    if chat.get("type") == "private" and sender.get("id") and not botadmin.is_admin(sender.get("id")):
        _welcome(db, sender)


def _welcome(db: Session, sender: dict) -> None:
    from .app_api import current_user
    user = current_user(db, sender)
    lang = user.lang or core.lang_of(sender.get("language_code"))
    text = core.get(db, f"welcome_{lang}") or core.t(lang, "welcome", brand=core.get(db, "brand_name"))
    text += emergency.bot_line(db, lang)
    try:
        notify.send(sender["id"], text, core.t(lang, "open_app"))
    except notify.Blocked:
        pass


def _start(db: Session, sender: dict, param: str) -> None:
    from .app_api import current_user, send_captcha
    known = db.query(User.id).filter(User.tg_id == sender.get("id")).first() is not None
    # The person is registered right here, so a referral link works even if
    # the app is opened later — and the captcha can be asked in this chat.
    user = current_user(db, {**sender, "_start": param[:64]})
    if known and user.ref_pending:
        # Still owes the captcha (the first message may be deleted or lost):
        # ask it again instead of saying nothing.
        send_captcha(db, user)
        return
    lang = user.lang or core.lang_of(sender.get("language_code"))
    brand = core.get(db, "brand_name")
    custom = core.get(db, f"welcome_{lang}")
    text = custom or core.t(lang, "welcome", brand=brand)
    query = ""
    if param.startswith("gift") and len(param) >= 12:
        code = param[4:12].upper()
        query = f"gift={code}"
        text = core.t(lang, "gift_hint")
    if user.ref_pending and user.captcha:
        return                  # the captcha went out first; the app button follows it
    text += emergency.bot_line(db, lang)
    try:
        notify.send(sender["id"], text, core.t(lang, "open_app"), query)
    except notify.Blocked:
        pass


def _captcha_answer(db: Session, cq: dict) -> None:
    from .. import referral
    sender = cq.get("from") or {}
    msg = cq.get("message") or {}
    user = db.query(User).filter(User.tg_id == sender.get("id")).first()
    lang = (user.lang if user else "") or core.lang_of(sender.get("language_code"))
    res = referral.solve(db, user, str(cq.get("data", ""))[4:]) if user else None
    if res is None:
        # An old captcha pressed again: nothing is pending, nothing to greet.
        notify.call("answerCallbackQuery", {"callback_query_id": cq.get("id")})
        notify.call("deleteMessage", {"chat_id": msg.get("chat", {}).get("id"),
                                      "message_id": msg.get("message_id")})
        return
    if res is False:
        notify.call("answerCallbackQuery", {"callback_query_id": cq.get("id"),
                                            "text": core.t(lang, "captcha_bad")})
        text, options = referral.captcha(db, user)
        notify.call("editMessageText", {
            "chat_id": msg.get("chat", {}).get("id"), "message_id": msg.get("message_id"), "text": text,
            "reply_markup": {"inline_keyboard": [[{"text": o, "callback_data": f"cap:{o}"}
                                                  for o in options]]}})
        return
    notify.call("answerCallbackQuery", {"callback_query_id": cq.get("id"),
                                        "text": core.t(lang, "captcha_ok")})
    notify.call("deleteMessage", {"chat_id": msg.get("chat", {}).get("id"),
                                  "message_id": msg.get("message_id")})
    brand = core.get(db, "brand_name")
    text = core.get(db, f"welcome_{lang}") or core.t(lang, "welcome", brand=brand)
    try:
        notify.send(sender["id"], text, core.t(lang, "open_app"))
    except notify.Blocked:
        pass
