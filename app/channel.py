"""
Mandatory channel subscription.

The owner binds a channel (the bot must be its admin, otherwise Telegram
does not tell who is subscribed). Then either the whole service or only
the free trial is available to subscribers.

If the check itself fails (the bot was removed from the channel, Telegram
is down), people are let through and the admin sees the problem — a broken
setting must not lock every customer out.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from . import core, notify
from .models import User, now

MODES = ("off", "trial", "all")
CACHE = 30 * 60            # a confirmed subscription is trusted for half an hour
MEMBER = ("creator", "administrator", "member")


def mode(db: Session) -> str:
    m = core.get(db, "channel_mode")
    return m if m in MODES and core.get(db, "channel_id") else "off"


def info(db: Session) -> dict:
    chat = core.get(db, "channel_id")
    link = core.get(db, "channel_link")
    if not link and chat.startswith("@"):
        link = f"https://t.me/{chat[1:]}"
    return {"mode": mode(db), "id": chat, "link": link, "title": core.get(db, "channel_title")}


def is_member(db: Session, user: User, fresh: bool = False, strict: bool = False) -> bool:
    """Whether the person is in the channel. True when nothing is required.
    strict: Telegram saying "too many requests" counts as "no" (for the
    free trial — flooding the check must not be a way to skip it)."""
    chat = core.get(db, "channel_id")
    if not chat:
        return True
    if not fresh and user.channel_ok_at and now() - user.channel_ok_at < CACHE:
        return True
    res = notify.call("getChatMember", {"chat_id": chat, "user_id": user.tg_id}, timeout=8)
    if not res.get("ok"):
        desc = str(res.get("description", ""))
        if "user not found" in desc.lower() or "participant_id_invalid" in desc.lower():
            return False
        if strict and res.get("error_code") == 429:
            return False
        core.log.warning("channel check failed: %s", desc)
        return True                     # fail open, the admin sees it in the panel
    member = res.get("result")
    if not isinstance(member, dict):
        return True                     # an answer we do not understand: fail open
    ok = member.get("status") in MEMBER or \
        (member.get("status") == "restricted" and member.get("is_member"))
    user.channel_ok_at = now() if ok else 0
    db.commit()
    return bool(ok)


def gate(db: Session, user: User, what: str = "all", fresh: bool = False) -> dict | None:
    """None when allowed; otherwise what the app shows: the channel to join."""
    m = mode(db)
    if m == "off" or (m == "trial" and what != "trial"):
        return None
    if is_member(db, user, fresh, strict=what == "trial"):
        return None
    i = info(db)
    return {"link": i["link"], "title": i["title"], "mode": m}


def check_setup(db: Session) -> dict:
    """Is the bot able to see members of the channel? Also caches its title
    and, for a private channel, an invite link."""
    chat = core.get(db, "channel_id")
    if not chat:
        return {"ok": False, "error": "no channel set"}
    res = notify.call("getChat", {"chat_id": chat}, timeout=8)
    if not res.get("ok"):
        return {"ok": False, "error": str(res.get("description", "chat not found"))}
    c = res["result"]
    me = notify.call("getMe", timeout=8).get("result") or {}
    role = notify.call("getChatMember", {"chat_id": chat, "user_id": me.get("id", 0)}, timeout=8)
    status = ((role.get("result") or {}).get("status")) if role.get("ok") else ""
    if status not in ("administrator", "creator"):
        return {"ok": False, "title": c.get("title", ""),
                "error": "the bot is not an administrator of the channel"}
    core.put(db, "channel_title", str(c.get("title", ""))[:128])
    if not core.get(db, "channel_link") and c.get("invite_link"):
        core.put(db, "channel_link", c["invite_link"])
    return {"ok": True, "title": c.get("title", ""), "username": c.get("username", ""),
            "link": core.get(db, "channel_link") or (f"https://t.me/{c['username']}"
                                                     if c.get("username") else "")}
