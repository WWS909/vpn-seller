"""
Support chat inside the bot.

A customer writes to the bot (or from the app's Support sheet) — the message
goes to every admin from ADMIN_TELEGRAM_IDS with the customer's card. An
admin answers by REPLYING to that message in the bot: the answer goes to the
customer from the bot, no personal account is exposed. The whole thread is
kept and can be read and answered in the admin panel too.

Photos, files and voice messages are passed on as they are (copyMessage).
"""

from __future__ import annotations

import html

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import core, notify
from .config import settings as env
from .models import SupportMessage, User, now

MAX_PER_HOUR = 20
ACK_EVERY = 600            # tell the customer "sent" at most once per 10 minutes
MEDIA = ("photo", "document", "video", "voice", "audio", "sticker", "animation", "video_note")


def enabled(db: Session) -> bool:
    return core.get(db, "support_enabled") == "1" and bool(env.admin_telegram_ids)


def _media_of(msg: dict) -> str:
    return next((k for k in MEDIA if msg.get(k)), "")


def _header(db: Session, user: User) -> str:
    lang = env.default_lang
    t = now()
    if user.expires_at and user.expires_at > t:
        state = core.t(lang, "sup_active", date=_date(user.expires_at))
    elif user.expires_at:
        state = core.t(lang, "sup_expired", date=_date(user.expires_at))
    else:
        state = core.t(lang, "sup_never")
    name = html.escape(user.first_name or "—")
    uname = f" · @{html.escape(user.username)}" if user.username else ""
    return core.t(lang, "sup_header", name=name, uname=uname, tg=user.tg_id, state=state)


def _date(ts: int) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(int(ts or 0)).strftime("%d.%m.%Y")


def _card_button(user: User) -> dict:
    """The card right in the chat, and the same customer in the panel (opened
    as a mini app: Telegram signs the admin in, no password)."""
    lang = env.default_lang
    row = [{"text": core.t(lang, "sup_card"), "callback_data": f"a:uc:{user.tg_id}"}]
    if env.public_url.startswith("https://"):
        row.append({"text": core.t(lang, "sup_panel"),
                    "web_app": {"url": f"{env.public_url}/admin#users?u={user.tg_id}"}})
    return {"inline_keyboard": [row]}


def _too_many(db: Session, user: User) -> bool:
    n = (db.query(func.count(SupportMessage.id))
         .filter(SupportMessage.user_id == user.id, SupportMessage.from_admin.is_(False),
                 SupportMessage.at > now() - 3600).scalar())
    return (n or 0) >= MAX_PER_HOUR


def incoming(db: Session, user: User, *, text: str = "", msg: dict | None = None,
             source: str = "bot") -> SupportMessage | None:
    """A customer's message: store it and pass it to the admins."""
    msg = msg or {}
    media = _media_of(msg)
    text = (text or msg.get("text") or msg.get("caption") or "").strip()[:4000]
    if not text and not media:
        return None
    lang = user.lang or env.default_lang
    if _too_many(db, user):
        if source == "bot":
            notify.send_safe(db, user, core.t(lang, "sup_slow"), button_key="")
        return None
    last = user.support_at or 0
    entry = SupportMessage(user_id=user.id, from_admin=False, text=text, media=media, at=now(),
                           source=source)
    db.add(entry)
    user.support_at = entry.at
    db.commit()
    from . import support_ai
    if support_ai.take(db, user, entry):
        return entry                # the AI helper answers; people get it if it hands off
    forward(db, user, entry, msg=msg)
    if source == "bot" and entry.at - last > ACK_EVERY:
        notify.send_safe(db, user, core.t(lang, "sup_sent"), button_key="")
    return entry


def forward(db: Session, user: User, entry: SupportMessage, *, msg: dict | None = None,
            note: str = "") -> None:
    """Pass a customer's message to every admin, with the customer's card."""
    msg = msg or {}
    text, media = entry.text or "", entry.media or ""
    # Cut before escaping: a cut through "&amp;" would make Telegram refuse it.
    body = _header(db, user) + ("\n<i>" + html.escape(note) + "</i>" if note else "") + "\n\n" \
        + (html.escape(text[:3500]) if text else f"[{media}]")
    refs = []
    from . import team
    for admin in team.support_ids(db):
        if admin == user.tg_id and entry.source == "bot":
            continue
        res = notify.call("sendMessage", {"chat_id": admin, "text": body[:4096], "parse_mode": "HTML",
                                          "disable_web_page_preview": True,
                                          "reply_markup": _card_button(user)})
        if _mid(res):
            refs.append(f"{admin}:{_mid(res)}")
        if media and msg.get("message_id"):
            res = notify.call("copyMessage", {"chat_id": admin, "from_chat_id": user.tg_id,
                                              "message_id": msg["message_id"]})
            if _mid(res):
                refs.append(f"{admin}:{_mid(res)}")
    entry.refs = "," + ",".join(refs) + "," if refs else ""
    db.commit()


def _mid(res: dict):
    """The message id of a sent message (Bot API answer), or None."""
    result = res.get("result") if res.get("ok") else None
    return result.get("message_id") if isinstance(result, dict) else None


def find_by_ref(db: Session, chat: int, message_id: int) -> SupportMessage | None:
    return (db.query(SupportMessage).filter(SupportMessage.refs.like(f"%,{int(chat)}:{int(message_id)},%"))
            .order_by(SupportMessage.id.desc()).first())


def answer(db: Session, user: User, *, text: str = "", admin_id: int = 0, msg: dict | None = None,
           source: str = "bot", answered: bool = True) -> bool:
    """An answer to the customer: an admin's (from the bot or the panel), or
    the AI helper's (source="ai", always signed as the helper)."""
    msg = msg or {}
    media = _media_of(msg)
    text = (text or msg.get("text") or msg.get("caption") or "").strip()[:4000]
    if not text and not media:
        return False
    lang = user.lang or env.default_lang
    ok = False
    if media and msg.get("message_id"):
        head = core.t(lang, "sup_reply", text="") .strip()
        try:
            notify.send(user.tg_id, head)
        except notify.Blocked:
            user.bot_blocked = True
            db.commit()
            return False
        res = notify.call("copyMessage", {"chat_id": user.tg_id, "from_chat_id": msg["chat"]["id"],
                                          "message_id": msg["message_id"]})
        ok = bool(res.get("ok"))
    elif source == "ai":
        name = (core.get(db, "ai_name") or "").strip()[:40] or core.t(lang, "ai_default_name")
        ok = notify.send_safe(db, user, core.t(lang, "ai_reply", name=html.escape(name),
                                               text=html.escape(text[:3500])), button_key="")
    else:
        ok = notify.send_safe(db, user, core.t(lang, "sup_reply", text=html.escape(text[:3500])),
                              button_key="")
    if not ok:
        return False
    entry = SupportMessage(user_id=user.id, from_admin=True, text=text, media=media, at=now(),
                           admin_tg=admin_id or None, source=source)
    db.add(entry)
    if answered:
        user.support_answered_at = entry.at
    db.commit()
    return True


def handle_bot_message(db: Session, msg: dict) -> bool:
    """Called for every private message that is not a command. Returns True
    if support took it."""
    from . import botadmin
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    if chat.get("type") != "private" or not sender.get("id"):
        return False
    text = (msg.get("text") or "").strip()
    reply = msg.get("reply_to_message") or {}
    from . import team
    helper = team.can(db, sender.get("id"), "support")
    # An admin (or a support member of the team) answering a forwarded question.
    if helper and reply:
        entry = find_by_ref(db, chat.get("id"), reply.get("message_id"))
        if entry:
            user = db.get(User, entry.user_id)
            lang = botadmin._lang(sender)
            ok = user and answer(db, user, admin_id=sender["id"], msg=msg)
            notify.call("sendMessage", {"chat_id": chat["id"], "reply_to_message_id": msg.get("message_id"),
                                        "text": core.t(lang, "sup_delivered" if ok else "sup_undelivered")})
            return True
    if text.startswith("/") or not enabled(db):
        return False
    if helper:
        return False            # an admin's own notes to the bot are not questions
    if not text and not _media_of(msg):
        return False            # a contact, a location…: not a question
    from .routers.app_api import current_user
    user = current_user(db, sender)
    incoming(db, user, msg=msg)  # stored and passed on, or refused with "too many"
    return True


def threads(db: Session, limit: int = 100) -> list[dict]:
    """Conversations, the unanswered first, then the latest."""
    last = (db.query(SupportMessage.user_id, func.max(SupportMessage.id).label("mid"),
                     func.count(SupportMessage.id).label("n"))
            .group_by(SupportMessage.user_id).subquery())
    rows = (db.query(SupportMessage, User, last.c.n)
            .join(last, SupportMessage.id == last.c.mid)
            .join(User, User.id == SupportMessage.user_id)
            .order_by(SupportMessage.at.desc()).limit(limit).all())
    out = []
    for m, u, n in rows:
        out.append({"tg_id": u.tg_id, "name": u.first_name or "", "username": u.username or "",
                    "last": m.text or (f"[{m.media}]" if m.media else ""), "at": m.at,
                    "from_admin": bool(m.from_admin), "count": int(n),
                    "waiting": (u.support_at or 0) > (u.support_answered_at or 0)})
    out.sort(key=lambda r: (not r["waiting"], -r["at"]))
    return out


def waiting(db: Session) -> int:
    return db.query(func.count(User.id)).filter(User.support_at > User.support_answered_at).scalar() or 0


def history(db: Session, user: User, limit: int = 100) -> list[dict]:
    rows = (db.query(SupportMessage).filter(SupportMessage.user_id == user.id)
            .order_by(SupportMessage.id.desc()).limit(limit).all())
    return [{"id": m.id, "from_admin": bool(m.from_admin), "text": m.text or "", "media": m.media or "",
             "at": m.at, "source": m.source or ""} for m in reversed(rows)]


def close(db: Session, user: User) -> None:
    """Mark as answered without writing (the question was solved elsewhere)."""
    user.support_answered_at = now()
    db.commit()
