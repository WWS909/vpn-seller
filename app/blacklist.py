"""
Shared blacklists: Telegram accounts other shops caught abusing free
trials, charging back or reselling keys.

The owner lists URLs of plain text files — one Telegram ID per line,
anything after "#" is the reason:

    123456789   # chargeback
    987654321

Every six hours (and on "Refresh now") each list is downloaded and its
rows replaced as a whole. What happens to a listed account is the owner's
choice: nothing, no free trial, or no trial and no purchases. Access that
was already paid for is never taken away by a list — only an admin blocks
a customer.

A shop can share its own list the same way: with publishing on,
/blacklist.txt shows the IDs of the customers its admins blocked (IDs
only, no names), so other shops can follow it.
"""

from __future__ import annotations

import json
import re

import httpx
from sqlalchemy.orm import Session

from . import core
from .config import settings as env
from .models import Blacklisted, User, now

MAX_LISTS = 10
MAX_BYTES = 4 * 1024 * 1024
MAX_IDS = 200_000
LINE = re.compile(r"^\s*(\d{3,15})\b\s*(?:#\s*(.*))?$")
ACTIONS = ("off", "trial", "block")


def urls(db: Session) -> list[str]:
    out = []
    for line in (core.get(db, "blacklist_urls") or "").splitlines():
        u = line.strip()
        if u.startswith("https://") and u not in out:
            out.append(u[:255])
    return out[:MAX_LISTS]


def action(db: Session) -> str:
    a = core.get(db, "blacklist_action")
    return a if a in ACTIONS else "trial"


def parse(text: str) -> list[tuple[int, str]]:
    seen, out = set(), []
    for line in text.splitlines():
        m = LINE.match(line)
        if m and int(m.group(1)) not in seen:
            seen.add(int(m.group(1)))
            out.append((int(m.group(1)), (m.group(2) or "").strip()[:200]))
            if len(out) >= MAX_IDS:
                break
    return out


def _download(url: str) -> str:
    with httpx.stream("GET", url, timeout=20, follow_redirects=True) as r:
        if r.status_code != 200:
            raise ValueError(f"HTTP {r.status_code}")
        data = b""
        for chunk in r.iter_bytes():
            data += chunk
            if len(data) > MAX_BYTES:
                raise ValueError("larger than 4 MB")
    return data.decode("utf-8", "replace")


def refresh(db: Session) -> dict:
    """Download every list and replace its rows. A list that fails keeps
    its previous rows (a hiccup must not unblock everyone on it)."""
    status = {}
    want = urls(db)
    for url in want:
        try:
            rows = parse(_download(url))
        except (httpx.HTTPError, ValueError) as e:
            status[url] = {"ok": False, "error": str(e)[:160] or e.__class__.__name__,
                           "count": db.query(Blacklisted).filter(Blacklisted.source == url).count()}
            continue
        db.query(Blacklisted).filter(Blacklisted.source == url).delete()
        db.bulk_save_objects([Blacklisted(tg_id=i, source=url, reason=r) for i, r in rows])
        db.commit()
        status[url] = {"ok": True, "count": len(rows)}
    # Lists the owner removed are forgotten.
    db.query(Blacklisted).filter(Blacklisted.source.notin_(want or [""])).delete(synchronize_session=False)
    db.commit()
    core.put(db, "blacklist_status_json", json.dumps(status))
    core.put(db, "blacklist_updated_at", str(now()))
    return status


def entry(db: Session, tg_id: int) -> Blacklisted | None:
    if int(tg_id) in env.admin_telegram_ids:
        return None                     # an admin is never refused by somebody else's list
    return db.query(Blacklisted).filter(Blacklisted.tg_id == int(tg_id)).first()


def refuses(db: Session, user: User, what: str) -> bool:
    """Is this person refused `what` ("trial" or "purchase") by a list?"""
    a = action(db)
    if a == "off" or (a == "trial" and what != "trial"):
        return False
    return entry(db, user.tg_id) is not None


def info(db: Session) -> dict:
    try:
        st = json.loads(core.get(db, "blacklist_status_json") or "{}")
    except ValueError:
        st = {}
    return {"urls": urls(db), "action": action(db), "publish": core.get(db, "blacklist_publish") == "1",
            "updated_at": core.get_int(db, "blacklist_updated_at", 0), "status": st,
            "total": db.query(Blacklisted.tg_id).distinct().count(),
            "own": db.query(User).filter(User.is_blocked.is_(True)).count(),
            "own_url": f"{env.public_url}/blacklist.txt"}


def own_list(db: Session) -> str:
    rows = db.query(User.tg_id).filter(User.is_blocked.is_(True)).order_by(User.tg_id).all()
    head = (f"# Blocked by {core.get(db, 'brand_name')} — Telegram IDs, one per line\n"
            f"# updated {now()}\n")
    return head + "".join(f"{tg}\n" for (tg,) in rows if int(tg) not in env.admin_telegram_ids)
