"""
Mandatory channel subscription — one channel or several (up to five).

The owner adds channels (the bot must be an admin in each, otherwise
Telegram does not tell who is subscribed). Then either the whole service
or only the free trial is available to people subscribed to ALL of them.

If a check itself fails (the bot was removed from a channel, Telegram is
down), people are let through and the admin sees the problem — a broken
setting must not lock every customer out.

The list lives in channels_json; an older single channel (channel_id,
channel_link, channel_title) still counts until the list is saved.
"""

from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

from . import core, notify
from .models import User, now

MODES = ("off", "trial", "all")
CACHE = 30 * 60            # a confirmed subscription is trusted for half an hour
MEMBER = ("creator", "administrator", "member")
MAX = 5
ID = re.compile(r"@[A-Za-z0-9_]{4,64}|-100\d{5,20}")
LINK = re.compile(r"https://t\.me/[A-Za-z0-9_+/-]{2,200}")


def clean(raw) -> list[dict]:
    """A list from the panel, checked: ids in the right form, links to t.me
    only, at most MAX, no repeats. Raises ValueError with the reason."""
    if not isinstance(raw, list):
        raise ValueError("channels: a list")
    out, seen = [], set()
    for c in raw:
        if not isinstance(c, dict):
            raise ValueError("channels: objects")
        cid = str(c.get("id", "")).strip()
        link = str(c.get("link", "")).strip()
        if not cid:
            continue
        if not ID.fullmatch(cid):
            raise ValueError(f"channel {cid[:40]}: @username or -100…")
        if link and not LINK.fullmatch(link):
            raise ValueError(f"channel link {link[:60]}: https://t.me/…")
        if cid.lower() in seen:
            continue
        seen.add(cid.lower())
        out.append({"id": cid, "link": link, "title": str(c.get("title", ""))[:128]})
    if len(out) > MAX:
        raise ValueError(f"channels: at most {MAX}")
    return out


def channels(db: Session) -> list[dict]:
    raw = core.get_json(db, "channels_json", None)
    try:
        out = clean(raw) if raw is not None else []
    except ValueError:
        out = []
    if raw is None and core.get(db, "channel_id"):         # the older single channel
        out = [{"id": core.get(db, "channel_id"), "link": core.get(db, "channel_link"),
                "title": core.get(db, "channel_title")}]
    for c in out:
        if not c["link"] and c["id"].startswith("@"):
            c["link"] = f"https://t.me/{c['id'][1:]}"
    return out


def _store(db: Session, items: list[dict]) -> None:
    core.put(db, "channels_json", json.dumps(items, ensure_ascii=False))


def mode(db: Session) -> str:
    m = core.get(db, "channel_mode")
    return m if m in MODES and channels(db) else "off"


def info(db: Session) -> dict:
    cs = channels(db)
    first = cs[0] if cs else {"id": "", "link": "", "title": ""}
    return {"mode": mode(db), "id": first["id"], "link": first["link"], "title": first["title"],
            "channels": [{"link": c["link"], "title": c["title"]} for c in cs]}


def _joined(chat: str, user: User, strict: bool) -> bool:
    res = notify.call("getChatMember", {"chat_id": chat, "user_id": user.tg_id}, timeout=8)
    if not res.get("ok"):
        desc = str(res.get("description", ""))
        if "user not found" in desc.lower() or "participant_id_invalid" in desc.lower():
            return False
        if strict and res.get("error_code") == 429:
            return False
        core.log.warning("channel check failed (%s): %s", chat, desc)
        return True                     # fail open, the admin sees it in the panel
    member = res.get("result")
    if not isinstance(member, dict):
        return True                     # an answer we do not understand: fail open
    return member.get("status") in MEMBER or \
        bool(member.get("status") == "restricted" and member.get("is_member"))


def missing(db: Session, user: User, fresh: bool = False, strict: bool = False) -> list[dict]:
    """The channels the person has not joined yet ([] — all of them, or none required)."""
    cs = channels(db)
    if not cs:
        return []
    if not fresh and user.channel_ok_at and now() - user.channel_ok_at < CACHE:
        return []
    out = [c for c in cs if not _joined(c["id"], user, strict)]
    user.channel_ok_at = 0 if out else now()
    db.commit()
    return out


def is_member(db: Session, user: User, fresh: bool = False, strict: bool = False) -> bool:
    """Whether the person is in every channel. True when nothing is required.
    strict: Telegram saying "too many requests" counts as "no" (for the
    free trial — flooding the check must not be a way to skip it)."""
    return not missing(db, user, fresh, strict)


def gate(db: Session, user: User, what: str = "all", fresh: bool = False) -> dict | None:
    """None when allowed; otherwise what the app shows: the channels to join."""
    m = mode(db)
    if m == "off" or (m == "trial" and what != "trial"):
        return None
    left = missing(db, user, fresh, strict=what == "trial")
    if not left:
        return None
    return {"link": left[0]["link"], "title": left[0]["title"], "mode": m,
            "channels": [{"link": c["link"], "title": c["title"]} for c in left]}


def _check_one(c: dict, me_id: int) -> dict:
    res = notify.call("getChat", {"chat_id": c["id"]}, timeout=8)
    if not res.get("ok"):
        return {"id": c["id"], "ok": False, "error": str(res.get("description", "chat not found"))}
    chat = res["result"]
    role = notify.call("getChatMember", {"chat_id": c["id"], "user_id": me_id}, timeout=8)
    status = ((role.get("result") or {}).get("status")) if role.get("ok") else ""
    title = str(chat.get("title", ""))[:128]
    link = c.get("link") or chat.get("invite_link") or (f"https://t.me/{chat['username']}"
                                                         if chat.get("username") else "")
    if status not in ("administrator", "creator"):
        return {"id": c["id"], "ok": False, "title": title, "link": link,
                "error": "the bot is not an administrator of the channel"}
    return {"id": c["id"], "ok": True, "title": title, "link": link}


def check_setup(db: Session) -> dict:
    """Can the bot see members of every channel? Also keeps their titles and,
    for a private channel, an invite link."""
    cs = channels(db)
    if not cs:
        return {"ok": False, "error": "no channel set", "channels": []}
    me = notify.call("getMe", timeout=8).get("result") or {}
    results = [_check_one(c, me.get("id", 0)) for c in cs]
    for c, r in zip(cs, results):
        if r.get("title"):
            c["title"] = r["title"]
        if not c["link"] and r.get("link"):
            c["link"] = r["link"]
    _store(db, cs)
    bad = [r for r in results if not r["ok"]]
    first = results[0]
    return {"ok": not bad, "title": first.get("title", ""), "link": first.get("link", ""),
            "error": "; ".join(f"{r['id']}: {r['error']}" for r in bad), "channels": results}
