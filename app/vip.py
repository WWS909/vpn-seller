"""
The owner's own access.

Whoever runs the shop (the Telegram IDs in ADMIN_TELEGRAM_IDS) gets a
subscription with no end, no device limit and no traffic limit. It is not
a plan: nobody can buy it, it is in no list and no price, the analytics do
not count it. The app shows "∞" on the owner's own screen and nothing
anywhere else.

It is put in place when the owner opens the app, and kept there: an admin
edit that shortens it is undone on the next opening.
"""

from __future__ import annotations

import threading

from sqlalchemy.orm import Session

from . import access, core
from .config import settings as env
from .models import User

FOREVER = 2145916800            # 1 January 2038: "no end". Not later: the column is a 32-bit
                                # integer in PostgreSQL, a bigger number breaks the save


def is_vip(user: User | None) -> bool:
    return bool(user) and int(user.tg_id or 0) in env.admin_telegram_ids


def ids() -> set[int]:
    return set(env.admin_telegram_ids)


def ensure(db: Session, user: User) -> bool:
    """Make the owner's access what it should be. True for the owner."""
    if not is_vip(user):
        return False
    if ((user.expires_at or 0) >= FOREVER and not user.device_limit and not user.traffic_limit
            and not user.bypass_limit and not user.is_blocked and not user.traffic_blocked):
        return True
    user.expires_at = FOREVER
    user.device_limit = 0
    user.traffic_limit = 0
    user.bypass_limit = 0
    user.traffic_blocked = False
    user.is_blocked = False
    user.plan_id = None
    db.commit()
    core.audit(db, "Owner access set: no end, no limits", f"tg:{user.tg_id}", "")
    uid = user.id

    def work():
        from .db import SessionLocal
        s = SessionLocal()
        try:
            u = s.get(User, uid)
            if u is not None:
                access.push(s, u, reset_traffic=True)
        except Exception:  # noqa: BLE001 — the sync job reaches the panels later
            core.log.exception("vip: push failed")
        finally:
            s.close()
    threading.Thread(target=work, daemon=True).start()
    return True
