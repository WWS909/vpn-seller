"""
Background jobs. One small loop per job, each in a worker thread so the
web server stays responsive while panels are slow.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.orm import Session

from . import access, billing, core, notify
from .db import SessionLocal
from .models import Broadcast, User, now


# The last run of every job: when, how long, and how it ended — for the
# diagnostics page (a job that stopped running or keeps failing shows there).
STATE: dict[str, dict] = {}
INTERVALS: dict[str, int] = {}


def _run(fn) -> None:
    import time
    name = getattr(fn, "__name__", str(fn))
    db = SessionLocal()
    started = time.time()
    try:
        fn(db)
        STATE[name] = {"at": int(started), "ms": int((time.time() - started) * 1000), "error": ""}
    except Exception as e:  # noqa: BLE001 — a job must never die
        core.log.exception("job %s failed: %s", name, e)
        STATE[name] = {"at": int(started), "ms": int((time.time() - started) * 1000),
                       "error": (str(e) or e.__class__.__name__)[:200]}
    finally:
        db.close()


async def _every(seconds: int, fn, delay: int = 5) -> None:
    await asyncio.sleep(delay)
    while True:
        await asyncio.to_thread(_run, fn)
        await asyncio.sleep(seconds)


def start() -> list[asyncio.Task]:
    jobs = [
        (300, job_traffic, 20),
        (120, job_retry_paid, 30),
        (180, job_check_pending, 45),
        (600, job_sync_failed, 60),
        (86400, job_sync_all, 3600),
        (86400, job_cleanup, 600),
        (600, job_backup, 300),
        (5, job_broadcast, 15),
        (900, job_warm, 120),
        (10, job_campaigns, 25),
        (900, job_health, 10),
        (120, job_load, 40),
        (120, job_vpn, 50),
        (60, job_receipts, 35),
        (21600, job_blacklist, 90),
        (300, job_tg_only, 75),
        (60, job_updates, 30),
        (300, job_report, 100),
    ]
    from .config import settings as env
    if env.tunnel_metrics_url:
        jobs.append((30, job_tunnel, 3))
    INTERVALS.update({fn.__name__: sec + delay for sec, fn, delay in jobs})
    return [asyncio.create_task(_every(sec, fn, delay)) for sec, fn, delay in jobs]


def job_report(db: Session) -> None:
    """The day's results to the owners, once a day at the chosen hour."""
    from . import report
    report.run_daily(db)


def job_updates(db: Session) -> None:
    """New versions on GitHub, the end of an update, the night auto-update."""
    from . import updates
    updates.tick(db)


def job_tunnel(db: Session) -> None:
    notify.follow_tunnel(db)


# ---------------------------------------------------------------------------
def job_traffic(db: Session) -> None:
    res = access.collect_traffic(db)
    for tg_id in res.get("over", []):
        user = db.query(User).filter(User.tg_id == tg_id).first()
        if user and not user.bot_blocked:
            notify.send_safe(db, user, core.t(user.lang or "ru", "traffic_out"))


def job_retry_paid(db: Session) -> None:
    n = billing.retry_paid(db)
    if n:
        core.audit(db, "Delayed orders granted", "system", str(n))


def job_check_pending(db: Session) -> None:
    """Orders the person paid and then closed the app on, while the
    gateway's webhook never came: ask the gateway about recent open ones."""
    from . import payments
    from .models import Transaction
    t = now()
    rows = (db.query(Transaction)
            .filter(Transaction.status == "pending",
                    Transaction.gateway.in_(tuple(payments.POLLABLE)),
                    Transaction.external_id != "",
                    Transaction.created_at.between(t - 3 * 3600, t - 60))
            .order_by(Transaction.id.desc()).limit(100).all())
    for tx in rows:
        try:
            payments.poll(db, tx, min_interval=120)
        except access.AccessError:
            continue            # parked as "paid"


def job_sync_failed(db: Session) -> None:
    access.sync_users(db, only_failed=True)


def job_sync_all(db: Session) -> None:
    access.sync_users(db, only_failed=False, limit=100000)


def job_backup(db: Session) -> None:
    """The daily archive to the admins, at the hour set in the panel."""
    from . import backup
    backup.run_daily(db)


def job_cleanup(db: Session) -> None:
    """The journal would grow forever; keep half a year of it."""
    from .models import Log
    db.query(Log).filter(Log.created_at < now() - 180 * 86400).delete()
    db.commit()


def job_health(db: Session) -> None:
    """Keeps the health column in the admin panel fresh even with no sales,
    and tells the admins once when a panel stops answering (and when it is
    back). Other jobs write the last error too, so the message goes by its
    own mark, not by whether an error was already noted."""
    from .providers import panel_for
    rows = access.servers(db)
    access.release(db)
    for server in rows:
        try:
            res = panel_for(access._snap(server), timeout=10).test()
        except Exception as e:  # noqa: BLE001 — one broken panel must not stop the round
            server.last_error = str(e)[:250] or e.__class__.__name__
            if not server.down_alerted_at:
                server.down_alerted_at = now()
                notify.admins(f"Сервер недоступен / Server down — {server.name}: {server.last_error}")
            db.commit()
            continue
        wanted = set(server.inbounds)
        server.clients_count = res["clients"] if "clients" in res else \
            sum(i["clients"] for i in res["inbounds"] if i["id"] in wanted)
        server.api_version = res["version"]
        server.last_ok_at, server.last_error = now(), ""
        if server.down_alerted_at:
            server.down_alerted_at = 0
            notify.admins(f"Сервер снова отвечает / Server is back — {server.name}")
        db.commit()
    from . import emergency
    emergency.auto_check(db)


def job_load(db: Session) -> None:
    """People online on each server, for the load shown in the app."""
    access.refresh_load(db)


def job_vpn(db: Session) -> None:
    """Is the VPN of each server reachable (not the panel — the VPN itself)."""
    from . import vpncheck
    vpncheck.check(db)
    from . import emergency
    emergency.auto_check(db)


def job_broadcast(db: Session) -> None:
    """Send the current broadcast in portions; the cursor survives restarts."""
    from .routers.admin_api import audience_query
    cast = (db.query(Broadcast).filter(Broadcast.status == "sending")
            .order_by(Broadcast.id).first())
    if not cast:
        return
    batch = (audience_query(db, cast.audience).filter(User.id > (cast.cursor or 0))
             .order_by(User.id).limit(25).all())
    if not batch:
        cast.status = "done"
        db.commit()
        core.audit(db, "Broadcast finished", "system",
                   f"#{cast.id}: sent {cast.sent}, failed {cast.failed}")
        return
    for user in batch:
        db.refresh(cast)
        if cast.status != "sending":
            return                      # cancelled from the panel
        try:
            ok = notify.send(user.tg_id, cast.text,
                             core.t(user.lang or "ru", "open_app") if cast.button_text else "")
        except notify.Blocked:
            user.bot_blocked = True
            ok = False
        cast.sent += 1 if ok else 0
        cast.failed += 0 if ok else 1
        cast.cursor = user.id
        db.commit()


def job_warm(db: Session) -> None:
    """Warm-up notices: the subscription ends soon, has ended, the trial
    ends, traffic runs low, somebody never bought."""
    from . import emergency, marketing
    if emergency.sales_stopped(db):
        return              # no "buy now" while the checkout is closed; they go out after
    marketing.run_triggers(db)


def job_campaigns(db: Session) -> None:
    from . import emergency, marketing
    if emergency.sales_stopped(db):
        return
    marketing.run_campaigns(db)


def job_tg_only(db: Session) -> None:
    """Those whose VPN has just stopped get the Telegram-only client (when the
    owner turned it on and made such inbounds), so they can still reach the bot."""
    hours = access.tg_hours(db)
    if not hours or not access.tg_servers(db):
        return
    t = now()
    back = access.grace_days(db) * 86400 + hours * 3600 + 3600
    ids = [i for (i,) in db.query(User.id).filter(User.expires_at > t - back, User.expires_at <= t,
                                                  User.is_blocked.is_(False))
           .order_by(User.id).limit(500).all()]
    access.release(db)
    for uid in ids:
        with access.user_lock(uid):
            user = db.get(User, uid)
            if user is None:
                continue
            db.refresh(user)
            end = access.tg_until(db, user)
            if end and int(user.tg_only_until or 0) != end:
                access.push(db, user)


def job_blacklist(db: Session) -> None:
    """The shared blacklists the owner follows, downloaded again."""
    from . import blacklist
    if blacklist.urls(db):
        blacklist.refresh(db)


def job_receipts(db: Session) -> None:
    """«Мой налог» receipts for paid orders."""
    from . import nalog
    nalog.run(db)
