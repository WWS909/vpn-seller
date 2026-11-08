"""
Source links: which video, blogger or ad brings customers — and money.

The owner makes a link with a label (t.me/<bot>?start=src_<code>). Whoever
comes through it for the first time gets that code stored (first touch:
a person who already knew the bot is not counted again). The report shows,
per source, how many came, opened the app, took the trial, paid, and how
much money they brought — and, with what the source cost, whether it paid
off.
"""

from __future__ import annotations

import re

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import Source, Transaction, User, now

PREFIX = "src_"
_CODE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
DIRECT, REFERRAL = "_direct", "_ref"

_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p",
                      "r", "s", "t", "u", "f", "h", "c", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def code_from_start(start: str) -> str:
    """The source code in a start parameter ("src_tiktok5" → "tiktok5"), or ""."""
    start = (start or "").strip()
    if not start.lower().startswith(PREFIX):
        return ""
    code = start[len(PREFIX):].lower()
    return code if _CODE.match(code) else ""


def valid(code: str) -> bool:
    return bool(_CODE.match(code or ""))


def slug(name: str) -> str:
    """A code from a name: "Ролик №5 в TikTok" → "rolik-5-v-tiktok"."""
    out = "".join(_TRANSLIT.get(ch, ch) for ch in (name or "").lower())
    out = re.sub(r"[^a-z0-9]+", "-", out).strip("-")
    return out[:32].rstrip("-")


def link(code: str) -> str:
    from . import notify
    name = notify.username()
    return f"https://t.me/{name}?start={PREFIX}{code}" if name else ""


def report(db: Session, since: int = 0) -> list[dict]:
    """One row per source of the people who came since `since`, with the
    money they brought (all their payments). Sources that brought nobody
    yet are listed too. Ordered by revenue."""
    cohort = db.query(User.id, User.source, User.referrer_id, User.app_opened_at,
                      User.trial_used).filter(User.created_at >= since).all()
    money = dict(db.query(Transaction.user_id, func.sum(Transaction.amount))
                 .filter(Transaction.status == "success", Transaction.amount > 0)
                 .group_by(Transaction.user_id).all())
    rows: dict[str, dict] = {}

    def row(key: str) -> dict:
        return rows.setdefault(key, {"code": key, "came": 0, "opened": 0, "trial": 0, "paid": 0,
                                     "revenue": 0.0})

    for uid, source, referrer, opened, trial in cohort:
        key = source or (REFERRAL if referrer else DIRECT)
        r = row(key)
        r["came"] += 1
        r["opened"] += 1 if opened else 0
        r["trial"] += 1 if trial else 0
        if uid in money:
            r["paid"] += 1
            r["revenue"] += float(money[uid] or 0)
    known = {s.code: s for s in db.query(Source).all()}
    for code in known:
        row(code)
    out = []
    for key, r in rows.items():
        s = known.get(key)
        r["revenue"] = round(r["revenue"], 2)
        r["name"] = s.name if s else ""
        r["id"] = s.id if s else None
        r["cost"] = round(float(s.cost or 0), 2) if s else 0.0
        r["known"] = bool(s) or key in (DIRECT, REFERRAL)
        r["conversion"] = round(100 * r["paid"] / r["came"], 1) if r["came"] else 0
        r["per_customer"] = round(r["cost"] / r["paid"], 2) if s and r["paid"] and r["cost"] else 0
        r["link"] = link(key) if s else ""
        out.append(r)
    return sorted(out, key=lambda r: (r["code"] in (DIRECT, REFERRAL), -r["revenue"], -r["came"]))


def create(db: Session, name: str, code: str = "", cost: float = 0) -> Source:
    name = (name or "").strip()[:80]
    code = (code or "").strip().lower() or slug(name) or f"s{now() % 100000}"
    if not valid(code):
        raise ValueError("code: latin letters, digits, - and _, up to 40")
    if db.query(Source).filter(Source.code == code).first():
        raise ValueError("this code is taken")
    s = Source(code=code, name=name or code, cost=max(0.0, float(cost or 0)))
    db.add(s)
    db.commit()
    return s
