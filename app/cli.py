"""
Small commands for the server menu (install.sh), run inside the container:

    python -m app.cli summary                  key=value lines for the menu header
    python -m app.cli report                   the day's results to the owners now
    python -m app.cli auto-update on|off [H]   the night update, at hour H (server time)

Each prints plain lines and never raises: the menu only shows what it got.
"""

from __future__ import annotations

import datetime as dt
import sys

from sqlalchemy import func


def _db():
    from . import db as dbmod
    dbmod.init_engine()
    return dbmod.SessionLocal()


def summary() -> int:
    from . import backup, core, release, updates
    from .config import settings as env
    from .models import Transaction, User, now
    db = _db()
    try:
        t = now()
        day = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        paid = db.query(Transaction).filter(Transaction.status == "success", Transaction.amount > 0,
                                            Transaction.paid_at >= day)
        revenue = float(paid.with_entities(func.coalesce(func.sum(Transaction.amount), 0)).scalar() or 0)
        info = updates.info(db)
        latest = info.get("latest") or ""
        last = backup.listing()[:1]
        lang = env.default_lang
        out = {
            "version": release.version(),
            "latest": latest if release.newer(latest, release.version()) else "",
            "orders": paid.count(),
            "revenue": core.money(revenue, lang),
            "new": db.query(User).filter(User.created_at >= day).count(),
            "users": db.query(User).count(),
            "active": db.query(User).filter(User.expires_at > t, User.is_blocked.is_(False)).count(),
            "backup": dt.datetime.fromtimestamp(last[0]["at"]).strftime("%d.%m %H:%M") if last else "",
            "auto": core.get(db, "auto_update"),
            "hour": core.get_int(db, "auto_update_hour", 1) % 24,
            "report": core.get(db, "report_on"),
        }
    finally:
        db.close()
    for k, v in out.items():
        print(f"{k}={v}")
    return 0


def report() -> int:
    from . import report as rep
    db = _db()
    try:
        n = rep.send(db)
    finally:
        db.close()
    print(f"sent={n}")
    return 0 if n else 1


def auto_update(argv: list[str]) -> int:
    from . import core
    if not argv or argv[0] not in ("on", "off"):
        print("usage: python -m app.cli auto-update on|off [hour]")
        return 2
    db = _db()
    try:
        core.put(db, "auto_update", "1" if argv[0] == "on" else "0")
        if len(argv) > 1 and argv[1].isdigit() and 0 <= int(argv[1]) <= 23:
            core.put(db, "auto_update_hour", str(int(argv[1])))
        core.audit(db, "Auto update " + argv[0], "server menu")
        print(f"auto={core.get(db, 'auto_update')} hour={core.get(db, 'auto_update_hour')}")
    finally:
        db.close()
    return 0


def main(argv: list[str]) -> int:
    try:
        if argv[:1] == ["summary"]:
            return summary()
        if argv[:1] == ["report"]:
            return report()
        if argv[:1] == ["auto-update"]:
            return auto_update(argv[1:])
    except Exception as e:  # noqa: BLE001 — the menu shows a short line, not a traceback
        print(f"error={type(e).__name__}: {str(e)[:200]}")
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
