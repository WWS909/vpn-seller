"""
Emergency mode: something is broken (a server, a payment gateway, a block)
and the owner wants to tell everyone and keep things from getting worse.

While it is on:
  * every customer sees the owner's message at the top of the app and
    after /start in the bot;
  * if "stop sales" is chosen, no new orders and no free trials are taken
    and no Stars invoice is accepted. Orders already paid are still granted —
    a gateway's late notice is never refused;
  * the warm-up notices with discounts wait (they would invite people to a
    checkout that is closed);
  * if "keep access" is chosen, nobody who still had the VPN when the mode
    was turned on is cut off while it lasts — their end on the panels is
    kept a few days ahead and moved on every day.

Turning it off puts everything back: the real end dates go to the panels.
"""

from __future__ import annotations

import os
import threading

from sqlalchemy.orm import Session

from . import core
from .models import now

KEEP_AHEAD = 3 * 86400          # "keep access" holds the end this far ahead; the daily sync moves it
TEXT_MAX = 300


def is_on(db: Session) -> bool:
    return core.get(db, "emergency") == "1"


def sales_stopped(db: Session) -> bool:
    return is_on(db) and core.get(db, "emergency_stop_sales") == "1"


def hold_since(db: Session) -> int:
    """When the "nobody is cut off" hold began, 0 when there is none."""
    if is_on(db) and core.get(db, "emergency_keep_access") == "1":
        return core.get_int(db, "emergency_since", 0) or now()
    return 0


def message(db: Session, lang: str) -> str:
    lang = lang if lang in ("ru", "en") else "en"
    own = core.get(db, f"emergency_text_{lang}") or core.get(db, "emergency_text_ru")
    if own:
        return own
    # Turned on by itself: say what happened, not a generic "maintenance".
    return core.t(lang, "em_auto" if core.get(db, "emergency_auto_on") == "1" else "em_default")


def bot_line(db: Session, lang: str) -> str:
    """The message under the bot's greeting, as HTML (the owner's text is escaped)."""
    if not is_on(db):
        return ""
    import html
    head = "Внимание" if lang == "ru" else "Notice"
    return f"\n\n<b>{head}:</b> {html.escape(message(db, lang))}"


def state(db: Session) -> dict:
    return {"on": is_on(db), "since": core.get_int(db, "emergency_since", 0) if is_on(db) else 0,
            "auto": core.get(db, "emergency_auto") == "1",
            "auto_minutes": core.get_int(db, "emergency_auto_minutes", 10),
            "auto_on": is_on(db) and core.get(db, "emergency_auto_on") == "1",
            "text_ru": core.get(db, "emergency_text_ru"), "text_en": core.get(db, "emergency_text_en"),
            "stop_sales": core.get(db, "emergency_stop_sales") == "1",
            "keep_access": core.get(db, "emergency_keep_access") == "1",
            "default_ru": core.t("ru", "em_default"), "default_en": core.t("en", "em_default")}


def public(db: Session) -> dict | None:
    """What the app shows (no settings, only what the customer needs)."""
    if not is_on(db):
        return None
    return {"text": {"ru": message(db, "ru"), "en": message(db, "en")},
            "stop_sales": sales_stopped(db)}


def update(db: Session, actor: str, *, on: bool | None = None, text_ru: str | None = None,
           text_en: str | None = None, stop_sales: bool | None = None,
           keep_access: bool | None = None, auto: bool | None = None,
           auto_minutes: int | None = None, _by_itself: bool = False) -> dict:
    """Change the mode. When what the panels must hold changes (the mode
    goes on or off with "keep access", or that option flips while on), the
    customers are pushed to the panels again in the background."""
    before = hold_since(db)
    was_on = is_on(db)
    if text_ru is not None:
        core.put(db, "emergency_text_ru", text_ru.strip()[:TEXT_MAX])
    if text_en is not None:
        core.put(db, "emergency_text_en", text_en.strip()[:TEXT_MAX])
    if stop_sales is not None:
        core.put(db, "emergency_stop_sales", "1" if stop_sales else "0")
    if keep_access is not None:
        core.put(db, "emergency_keep_access", "1" if keep_access else "0")
    if auto is not None:
        core.put(db, "emergency_auto", "1" if auto else "0")
    if auto_minutes is not None:
        core.put(db, "emergency_auto_minutes", str(max(2, min(240, int(auto_minutes)))))
    if on is not None and on != was_on:
        # Who turned it on decides who may turn it off: a mode the owner
        # turned on is never switched off by the automatic check.
        core.put(db, "emergency_auto_on", "1" if (on and _by_itself) else "0")
        core.put(db, "emergency", "1" if on else "0")
        core.put(db, "emergency_since", str(now()) if on else "0")
        core.audit(db, "Emergency mode " + ("on" if on else "off"), actor,
                   (core.get(db, "emergency_text_ru") or "")[:120], level="warn")
        from . import notify
        notify.admins(("Режим аварии включён / Emergency mode is ON" if on
                       else "Режим аварии выключен / Emergency mode is off") + f" — {actor}")
    elif on is not None and on and not _by_itself:
        core.put(db, "emergency_auto_on", "0")      # the owner confirmed it: it stays until they turn it off
    elif text_ru is not None or stop_sales is not None or keep_access is not None or auto is not None:
        core.audit(db, "Emergency mode changed", actor)
    if bool(before) != bool(hold_since(db)):
        resync_soon()
    return state(db)


# ---------------------------------------------------------------------------
#  By itself: every server down for a while — on; one of them back — off
# ---------------------------------------------------------------------------
def server_down_since(server) -> int:
    """Since when this server does not work for customers: its panel does
    not answer (the alert mark) or its VPN does not. 0 — it works."""
    marks = [m for m in (int(server.down_alerted_at or 0), int(server.vpn_down_at or 0)) if m]
    return min(marks) if marks else 0


def auto_check(db: Session) -> str:
    """Run after every health round. Returns what it did: "on", "off" or ""."""
    if core.get(db, "emergency_auto") != "1":
        return ""
    from . import access
    rows = access.servers(db)
    if not rows:
        return ""                   # nothing configured is not an outage
    t = now()
    minutes = max(2, core.get_int(db, "emergency_auto_minutes", 10))
    downs = [server_down_since(s) for s in rows]
    all_down = all(downs) and t - max(downs) >= minutes * 60
    if all_down and not is_on(db):
        update(db, "auto", on=True, _by_itself=True)
        return "on"
    if is_on(db) and core.get(db, "emergency_auto_on") == "1" and not all(downs):
        update(db, "auto", on=False)
        return "off"
    return ""


# ---------------------------------------------------------------------------
#  Pushing everyone again, once, in the background
# ---------------------------------------------------------------------------
_resync_lock = threading.Lock()
_resync = {"running": False, "again": False}


def resync_soon() -> bool:
    """Push every customer whose end on the panels may have changed. One run
    at a time; a change during a run asks for one more. Off in tests and
    without background jobs (SHOP_NO_JOBS) — there the daily sync does it."""
    if os.getenv("SHOP_NO_JOBS"):
        return False
    with _resync_lock:
        if _resync["running"]:
            _resync["again"] = True
            return True
        _resync["running"] = True
    threading.Thread(target=_resync_worker, name="resync", daemon=True).start()
    return True


def _resync_worker() -> None:
    from . import access
    from .db import SessionLocal
    while True:
        db = SessionLocal()
        try:
            res = access.sync_users(db, only_failed=False, limit=100000)
            core.audit(db, "Customers re-sent to the panels", "system",
                       f"{res['users']} customers, {res['with_errors']} with errors")
        except Exception as e:  # noqa: BLE001 — the daily sync will try again
            core.log.exception("resync failed: %s", e)
        finally:
            db.close()
        with _resync_lock:
            if not _resync["again"]:
                _resync["running"] = False
                return
            _resync["again"] = False
