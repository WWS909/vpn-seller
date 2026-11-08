"""
The owner's team: people who help with the shop but should not see
everything. Each member signs in to the panel through Telegram (from the
bot or the mini app) and gets only the sections the owner ticked:

    stats    — overview, analytics, launch steps, the day's results
    sales    — orders, plans, trial, promo codes, gifts, warm-up, sources, sale
    people   — customers, referrals and payouts, broadcasts, reviews, blacklists
    support  — the support chat (also: questions arrive to them in the bot)
    system   — servers, look, documents, settings, log, backups, emergency

The team itself and anything not listed here are the owner's only.
The check is made on the server for every request — hiding a menu item is
only a convenience; a member who calls an address of another section gets
403. Taking a member off (or a section away) works at once: the token is
checked against this table each time, not trusted for what it says.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from .config import settings as env
from .models import TeamMember, now

SECTIONS = ("stats", "sales", "people", "support", "system")
SECTION_OF = {
    "overview": "stats", "analytics": "stats", "setup": "stats", "report": "stats",
    "orders": "sales", "plans": "sales", "trial": "sales", "promos": "sales", "gifts": "sales",
    "warm": "sales", "campaigns": "sales", "sources": "sales", "sale": "sales", "receipts": "sales",
    "users": "people", "referrals": "people", "payouts": "people", "broadcasts": "people",
    "audiences": "people", "reviews": "people", "blacklist": "people",
    "support": "support",
    "servers": "system", "theme": "system", "brand": "system", "templates": "system",
    "settings": "system", "logs": "system", "backups": "system", "emergency": "system",
    "diagnostics": "system", "probe": "system", "dpicheck": "system", "bot": "system",
    "channel": "system",
    "me": "any",
}


def section_of(path: str) -> str:
    """/api/admin/users/123 → "people"; anything unknown → "owner"."""
    rest = path.split("/api/admin/", 1)[-1]
    return SECTION_OF.get(rest.split("/", 1)[0], "owner")


def clean_sections(raw) -> str:
    items = raw if isinstance(raw, list) else str(raw or "").split(",")
    return ",".join(s for s in SECTIONS if s in {str(x).strip() for x in items})


def member(db: Session, tg_id: int) -> TeamMember | None:
    m = db.query(TeamMember).filter(TeamMember.tg_id == int(tg_id)).first()
    return m if m and m.enabled else None


def allowed(m: TeamMember, section: str) -> bool:
    return section == "any" or section in (m.sections or "").split(",")


def is_owner_tg(tg_id) -> bool:
    try:
        return int(tg_id) in env.admin_telegram_ids
    except (TypeError, ValueError):
        return False


def can(db: Session, tg_id, section: str) -> bool:
    """For the bot: may this Telegram account do what `section` covers?"""
    if is_owner_tg(tg_id):
        return True
    try:
        m = member(db, int(tg_id))
    except (TypeError, ValueError):
        return False
    return bool(m) and allowed(m, section)


def support_ids(db: Session) -> list[int]:
    """Who gets customers' questions in the bot: the owners and the members with "support"."""
    ids = set(env.admin_telegram_ids)
    for m in db.query(TeamMember).filter(TeamMember.enabled.is_(True)).all():
        if "support" in (m.sections or "").split(","):
            ids.add(int(m.tg_id))
    return sorted(ids)


def out(m: TeamMember) -> dict:
    return {"id": m.id, "tg_id": m.tg_id, "name": m.name, "sections": [s for s in (m.sections or "").split(",") if s],
            "enabled": bool(m.enabled), "created_at": m.created_at, "seen_at": m.seen_at or 0}


def touch(db: Session, m: TeamMember) -> None:
    if now() - int(m.seen_at or 0) > 300:
        m.seen_at = now()
        db.commit()
