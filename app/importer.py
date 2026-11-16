"""
Moving customers in from a panel.

An owner who sold keys by hand in 3x-ui, Marzban or Remnawave already has
paying people there. The shop reads them from the panel and gives each one
the same end date (and the traffic that was left) as a subscription in the
shop: the person opens the bot and finds it waiting.

  * With a Telegram ID on the panel (3x-ui "tgId", Remnawave "telegramId",
    or a name that is only the ID, like "123456789" or "tg_123456789"), the
    subscription is attached to that ID right away.
  * Without one, the shop makes a personal link (t.me/<bot>?start=imp…).
    The owner sends it to that person; whoever opens it first gets the
    days, once.

The old client on the panel is not touched: it ends on its own date, and
the person moves to the key the shop gives them. Nobody gets more days than
they had — a person who already has a longer subscription in the shop keeps
it as it is.
"""

from __future__ import annotations

import re
import secrets
import threading

from sqlalchemy import update
from sqlalchemy.orm import Session

from . import access, core
from .config import settings as env
from .models import ImportClaim, Server, User, now
from .providers import panel_for

CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
NEVER_DAYS_MAX = 3650
DEVICES_MAX = 50
_ID_NAME = re.compile(r"^(?:tg|id|user|u)?[_\-]?(\d{5,12})$", re.I)


class ImportError_(Exception):
    pass


def _own(name: str) -> bool:
    """A client the shop itself made (tg123, tg123-2, tg123_tg, tgdpicheck)."""
    p = re.escape(env.xui_email_prefix)
    return bool(re.match(rf"^{p}(\d+(-\d+)?(_tg)?|dpicheck)$", name or "", re.I))


def _tg_from_name(name: str) -> int:
    m = _ID_NAME.match((name or "").strip())
    return int(m.group(1)) if m else 0


def read(db: Session, server: Server) -> dict:
    """What can be moved from this panel, with what will happen to each."""
    rows = panel_for(server, timeout=30).clients()
    t = now()
    out, counts = [], {"active": 0, "with_id": 0, "no_id": 0, "in_shop": 0, "ended": 0, "never": 0, "off": 0}
    ids = [r["tg_id"] or _tg_from_name(r["name"]) for r in rows]
    known = {u.tg_id: u for u in db.query(User).filter(User.tg_id.in_([i for i in ids if i]))} if any(ids) else {}
    sent = {c.name: c for c in db.query(ImportClaim).filter(ImportClaim.server == server.name[:64])}
    for r, tg in zip(rows, ids):
        if _own(r["name"]):
            continue
        claim = None if tg else sent.get(r["name"][:128])
        exp = int(r.get("expires") or 0)
        exp = 0 if exp < 0 else exp
        total, used = int(r.get("total") or 0), int(r.get("used") or 0)
        left = max(0, total - used) if total else 0
        if exp and exp <= t:
            state = "ended"
        elif total and left <= 0:
            state = "ended"
        elif tg and tg in known and (known[tg].expires_at or 0) >= (exp or t + 1):
            state = "in_shop"
        elif claim is not None and claim.claimed_by:
            state = "in_shop"                     # opened their link
        elif claim is not None and claim.expires_at >= (exp or t + 1):
            state = "link"                        # the link is made and waits for the person
        elif not r.get("enabled", True):
            state = "off"
        else:
            state = "ok"
        counts["ended" if state == "ended" else "in_shop" if state in ("in_shop", "link") else
               "off" if state == "off" else "active"] += 1
        if state == "ok":
            counts["with_id" if tg else "no_id"] += 1
            if not exp:
                counts["never"] += 1
        out.append({"name": r["name"], "tg_id": tg, "id_from": "panel" if r["tg_id"] else ("name" if tg else ""),
                    "expires": exp, "total": total, "left": left, "state": state})
    out.sort(key=lambda x: (x["state"] != "ok", -(x["expires"] or 10 ** 12)))
    return {"clients": out, "counts": counts}


def _code(db: Session) -> str:
    for _ in range(20):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))
        if not db.query(ImportClaim.id).filter(ImportClaim.code == code).first():
            return code
    raise ImportError_("could not make a code")


def link(code: str, bot: str | None = None) -> str:
    """t.me/<bot>?start=imp<code>. `bot` is passed when making many links,
    so Telegram is asked for the bot's name once, not once per link."""
    if bot is None:
        from . import notify
        bot = notify.username()
    return f"https://t.me/{bot}?start=imp{code}" if bot else f"imp{code}"


def _bot() -> str:
    from . import notify
    return notify.username()


def give(db: Session, user: User, expires: int, traffic: int, devices: int) -> bool:
    """Bring the person's subscription up to `expires`. False when theirs is
    already as long — nothing is added on top."""
    t = now()
    if (user.expires_at or 0) >= expires:
        return False
    active = (user.expires_at or 0) > t
    user.expires_at = expires
    if active:
        if user.device_limit and devices:
            user.device_limit = max(user.device_limit, devices)
        if user.traffic_limit and traffic:
            user.traffic_limit = max(user.traffic_limit, traffic)
        elif not traffic:
            user.traffic_limit = 0
    else:
        user.device_limit = devices
        user.traffic_limit = traffic
        user.traffic_used = 0
        user.traffic_extra = 0
        user.traffic_blocked = False
    user.sync_error = ""
    db.commit()
    return True


def _push_later(user_ids: list[int]) -> None:
    """Create the shop's clients on the panels in the background: a few
    hundred people take minutes, the owner should not wait for it."""
    def work():
        from .db import SessionLocal
        db = SessionLocal()
        try:
            for uid in user_ids:
                user = db.get(User, uid)
                if user is not None:
                    try:
                        access.push(db, user, reset_traffic=bool(user.traffic_limit))
                    except Exception:  # noqa: BLE001 — the sync job tries again
                        core.log.exception("import: push of %s failed", uid)
                        db.rollback()
        finally:
            db.close()
    if user_ids:
        threading.Thread(target=work, daemon=True).start()


def run(db: Session, server: Server, names: list[str], devices: int, never_days: int, admin: str) -> dict:
    """Move the chosen clients. Returns how many went straight to the shop
    and the personal links for the rest."""
    devices = max(0, min(DEVICES_MAX, int(devices or 0)))
    never_days = max(1, min(NEVER_DAYS_MAX, int(never_days or 30)))
    wanted = {n for n in names if n}
    data = read(db, server)
    t = now()
    added, links, skipped, pushed = 0, [], 0, []
    bot = None
    for c in data["clients"]:
        if c["name"] not in wanted:
            continue
        if c["state"] not in ("ok", "off", "link"):
            skipped += 1
            continue
        exp = c["expires"] or t + never_days * 86400
        if c["tg_id"]:
            user = db.query(User).filter(User.tg_id == c["tg_id"]).first()
            if user is None:
                user = User(tg_id=c["tg_id"], first_name=c["name"][:128], source="import", created_at=t)
                db.add(user)
                db.commit()
            if give(db, user, exp, c["left"], devices):
                added += 1
                pushed.append(user.id)
            else:
                skipped += 1
            continue
        old = db.query(ImportClaim).filter(ImportClaim.server == server.name[:64], ImportClaim.name == c["name"][:128],
                                           ImportClaim.claimed_by.is_(None)).first()
        if old:                                   # moved before and not opened yet: the same link, fresh terms
            old.expires_at, old.traffic_limit, old.device_limit = exp, c["left"], devices
            db.commit()
            claim = old
        else:
            claim = ImportClaim(code=_code(db), name=c["name"][:128], server=server.name[:64],
                                expires_at=exp, traffic_limit=c["left"], device_limit=devices)
            db.add(claim)
            db.commit()
        if bot is None:
            bot = _bot()
        links.append(_claim_out(claim, bot))
    _push_later(pushed)
    core.audit(db, "Customers moved in", admin,
               f"{server.name}: {added} by Telegram ID, {len(links)} by personal link, {skipped} skipped")
    return {"added": added, "links": links, "skipped": skipped}


def _claim_out(c: ImportClaim, bot: str) -> dict:
    return {"id": c.id, "name": c.name, "server": c.server, "expires": c.expires_at, "left": c.traffic_limit,
            "link": link(c.code, bot), "claimed": bool(c.claimed_by), "claimed_at": c.claimed_at}


def claims(db: Session) -> list[dict]:
    rows = db.query(ImportClaim).order_by(ImportClaim.claimed_by.isnot(None), ImportClaim.id.desc()).limit(2000).all()
    bot = _bot() if rows else ""
    return [_claim_out(c, bot) for c in rows]


def claim(db: Session, user: User, code: str) -> str:
    """The person opened their personal link. Returns "ok", "had" (their
    subscription is already longer), "used" or "bad". The capture is one
    conditional UPDATE, so the link works once."""
    code = re.sub(r"[^a-z0-9]", "", (code or "").lower())[:16]
    if len(code) < 8:
        return "bad"
    res = db.execute(update(ImportClaim).where(ImportClaim.code == code, ImportClaim.claimed_by.is_(None))
                     .values(claimed_by=user.id, claimed_at=now()))
    db.commit()
    if not res.rowcount:
        return "used" if db.query(ImportClaim.id).filter(ImportClaim.code == code).first() else "bad"
    c = db.query(ImportClaim).filter(ImportClaim.code == code).first()
    if not give(db, user, c.expires_at, c.traffic_limit, c.device_limit):
        return "had"
    try:
        access.push(db, user, reset_traffic=bool(user.traffic_limit))
    except Exception:  # noqa: BLE001 — the days are in the shop; the sync job reaches the panels
        core.log.exception("import claim: push failed")
        db.rollback()
    core.audit(db, "Customer moved in", f"tg:{user.tg_id}", f"{c.server}: {c.name}")
    return "ok"
