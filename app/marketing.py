"""
Warming customers up: the sale, personal offers, system notices and
campaigns.

* The sale is a discount on every plan for everyone, until a date.
* An offer is a personal discount that one person gets from a notice or a
  campaign. It is applied at checkout by itself while it is valid, and is
  spent by one purchase.
* System notices fire on fixed events (the subscription ends soon, it has
  ended, the trial ends, traffic runs low, somebody never bought). Their
  text, timing and discount are edited in the panel.
* Campaigns are the owner's own: a segment, a text, a discount, a period.

A discount never stacks with another automatic one: the person gets the
largest of the sale and their offer. A promo code is compared with that,
and the lower price wins.
"""

from __future__ import annotations

import datetime as dt
import html

from sqlalchemy import and_, exists, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query, Session

from . import core
from .config import settings as env
from .models import GB, Campaign, Offer, Plan, Transaction, User, WarmSent, now

MAX_DISCOUNT = 90


# ---------------------------------------------------------------------------
#  The sale
# ---------------------------------------------------------------------------
def sale(db: Session) -> dict:
    """The current sale, or percent 0."""
    pct = max(0, min(MAX_DISCOUNT, core.get_int(db, "sale_percent", 0)))
    until = core.get_int(db, "sale_until", 0)
    if pct and until and until <= now():
        pct = 0
    return {"percent": pct, "until": until if pct else 0,
            "title": {"ru": core.get(db, "sale_title_ru"), "en": core.get(db, "sale_title_en")}}


def set_sale(db: Session, percent: int, days: int = 0, title_ru: str | None = None,
             title_en: str | None = None) -> dict:
    percent = max(0, min(MAX_DISCOUNT, int(percent)))
    core.put(db, "sale_percent", str(percent))
    core.put(db, "sale_until", str(now() + int(days) * 86400 if percent and days > 0 else 0))
    if title_ru is not None:
        core.put(db, "sale_title_ru", title_ru.strip()[:60])
    if title_en is not None:
        core.put(db, "sale_title_en", title_en.strip()[:60])
    return sale(db)


# ---------------------------------------------------------------------------
#  Personal offers
# ---------------------------------------------------------------------------
def best_offer(db: Session, user: User) -> Offer | None:
    return (db.query(Offer)
            .filter(Offer.user_id == user.id, Offer.used_at == 0, Offer.expires_at > now())
            .order_by(Offer.percent.desc(), Offer.expires_at).first())


def give_offer(db: Session, user: User, percent: int, days: int, source: str) -> Offer | None:
    """A personal discount. An older, bigger one is kept rather than replaced."""
    percent = max(0, min(MAX_DISCOUNT, int(percent)))
    if not percent:
        return None
    current = best_offer(db, user)
    until = now() + max(1, int(days)) * 86400
    if current and current.percent >= percent and current.expires_at >= until:
        return current
    offer = Offer(user_id=user.id, percent=percent, expires_at=until, source=source[:32])
    db.add(offer)
    db.commit()
    return offer


def auto_discount(db: Session, user: User | None, kind: str = "purchase") -> dict:
    """The automatic discount this person gets now: {percent, source, offer_id}."""
    s = sale(db)
    best = {"percent": s["percent"], "source": "sale" if s["percent"] else "", "offer_id": None,
            "until": s["until"]}
    if user is not None and kind == "purchase":
        o = best_offer(db, user)
        if o and o.percent > best["percent"]:
            best = {"percent": o.percent, "source": "offer", "offer_id": o.id, "until": o.expires_at}
    return best


def spend_offer(db: Session, offer_id: int | None, tx: Transaction) -> bool:
    """Mark the offer as spent by this order — one conditional UPDATE, so
    two orders carrying the same offer cannot both spend it. False when it
    was already spent by another order."""
    if not offer_id:
        return True
    n = db.execute(update(Offer).where(Offer.id == offer_id, Offer.used_at == 0)
                   .values(used_at=now(), tx_id=tx.id)).rowcount
    db.commit()
    return bool(n)


# ---------------------------------------------------------------------------
#  Segments (who a campaign goes to)
# ---------------------------------------------------------------------------
SEGMENTS = ("all", "never", "trial_only", "no_paid", "active", "expiring", "expired")


def _paid_exists():
    return exists().where(and_(Transaction.user_id == User.id, Transaction.status == "success",
                               Transaction.amount > 0))


def segment_query(db: Session, segment: str) -> Query:
    t = now()
    q = db.query(User).filter(User.bot_blocked.is_(False), User.is_blocked.is_(False))
    if segment == "never":                      # opened the app, never had access
        q = q.filter(User.expires_at == 0)
    elif segment == "trial_only":               # took the trial, never paid
        q = q.filter(User.trial_used.is_(True), ~_paid_exists())
    elif segment == "no_paid":                  # never paid, trial or not
        q = q.filter(~_paid_exists())
    elif segment == "active":
        q = q.filter(User.expires_at > t)
    elif segment == "expiring":                 # ends within a week
        q = q.filter(User.expires_at > t, User.expires_at <= t + 7 * 86400)
    elif segment == "expired":
        q = q.filter(User.expires_at > 0, User.expires_at <= t)
    return q


# ---------------------------------------------------------------------------
#  System notices
# ---------------------------------------------------------------------------
# A fresh install sends only the two service notices (the subscription
# ends soon, traffic runs low). Every notice with a discount is off until
# the owner turns it on: nobody gets a discount the owner did not decide on.
#
# param means: days before the end (expiring, trial_end), days after it
# (expired, winback), hours after the first visit (no_purchase), percent of
# traffic used (traffic), minutes after a payment was started and not
# finished (unpaid).
TRIGGERS: dict[str, dict] = {
    "expiring": {
        "on": True, "param": 2, "percent": 0, "valid": 3, "button": "renew",
        "ru": "Подписка заканчивается через {days} дн. Продлите заранее — доступ не прервётся.",
        "en": "Your subscription ends in {days} day(s). Renew now to keep your access.",
    },
    "trial_end": {
        "on": False, "param": 1, "percent": 20, "valid": 3, "button": "offer",
        "ru": "Пробный период заканчивается через {days} дн. Оформите подписку со скидкой {percent}% — "
              "предложение действует до {until}.",
        "en": "Your free trial ends in {days} day(s). Subscribe with {percent}% off — the offer is valid "
              "until {until}.",
    },
    "expired": {
        "on": False, "param": 1, "percent": 15, "valid": 3, "button": "offer",
        "ru": "Подписка закончилась. Возвращайтесь со скидкой {percent}% — до {until}.",
        "en": "Your subscription has ended. Come back with {percent}% off — until {until}.",
    },
    "winback": {
        "on": False, "param": 14, "percent": 25, "valid": 5, "button": "offer",
        "ru": "Мы скучаем. Для вас скидка {percent}% на любой тариф до {until}.",
        "en": "We miss you. {percent}% off any plan for you until {until}.",
    },
    "no_purchase": {
        "on": False, "param": 24, "percent": 10, "valid": 3, "button": "offer",
        "ru": "Вы заглянули в {brand}, но ещё не подключились. Первая подписка — со скидкой {percent}% "
              "до {until}.",
        "en": "You looked at {brand} but haven't connected yet. Your first subscription is {percent}% off "
              "until {until}.",
    },
    # A payment started and not finished: most often the person was simply
    # distracted. One reminder, no discount unless the owner adds one.
    "unpaid": {
        "on": True, "param": 60, "percent": 0, "valid": 2, "button": "finish",
        "ru": "Вы начали оплату ({amount}), но не завершили её. Если что-то пошло не так — попробуйте ещё раз, "
              "это займёт минуту. Не получается — напишите в поддержку, поможем.",
        "en": "You started a payment ({amount}) but didn't finish it. If something went wrong, try again — "
              "it takes a minute. Still stuck? Write to support and we'll help.",
    },
    "traffic": {
        "on": True, "param": 80, "percent": 0, "valid": 3, "button": "open",
        "ru": "Израсходовано {used}% трафика. Докупите пакет в приложении, чтобы не остаться без интернета.",
        "en": "You have used {used}% of your traffic. Top it up in the app so you don't run out.",
    },
}
PARAM_LIMITS = {"expiring": (1, 30), "trial_end": (1, 7), "expired": (0, 60), "winback": (3, 180),
                "no_purchase": (1, 720), "unpaid": (15, 1440), "traffic": (50, 99)}
# A payment counts as unfinished while it waits (or was declined) and nothing
# newer was ordered; one reminder per person in UNPAID_GAP at most.
UNPAID_STATES = ("pending", "failed")
UNPAID_GAP = 3 * 86400
# How far back an event may be caught up. Turning a notice on must not
# send it to everybody whose subscription ended a year ago.
CATCH_UP = 3 * 86400


def trigger(db: Session, key: str) -> dict:
    base = dict(TRIGGERS[key])
    saved = core.get_json(db, f"warm_{key}", {})
    if isinstance(saved, dict):
        for k in ("on", "param", "percent", "valid", "ru", "en"):
            if k in saved:
                base[k] = saved[k]
    return base


def set_trigger(db: Session, key: str, data: dict) -> dict:
    if key not in TRIGGERS:
        raise KeyError(key)
    lo, hi = PARAM_LIMITS[key]
    cur = trigger(db, key)
    out = {
        "on": bool(data.get("on", cur["on"])),
        "param": max(lo, min(hi, int(data.get("param", cur["param"])))),
        "percent": max(0, min(MAX_DISCOUNT, int(data.get("percent", cur["percent"])))),
        "valid": max(1, min(30, int(data.get("valid", cur["valid"])))),
        "ru": str(data.get("ru", cur["ru"]))[:1500].strip() or TRIGGERS[key]["ru"],
        "en": str(data.get("en", cur["en"]))[:1500].strip() or TRIGGERS[key]["en"],
    }
    import json
    core.put(db, f"warm_{key}", json.dumps(out, ensure_ascii=False))
    return trigger(db, key)


def reset_trigger(db: Session, key: str) -> dict:
    core.put(db, f"warm_{key}", "")
    return trigger(db, key)


def _date(ts: int, lang: str) -> str:
    return dt.datetime.fromtimestamp(int(ts)).strftime("%d.%m" if lang == "ru" else "%b %d")


def render(db: Session, text: str, user: User, **values) -> str:
    """Fill {name} {brand} {days} {percent} {until} {used}; unknown braces stay."""
    lang = user.lang or env.default_lang
    data = {"name": html.escape(user.first_name or ""), "brand": html.escape(core.get(db, "brand_name")),
            "days": "", "percent": "", "until": "", "used": ""}
    data.update({k: v for k, v in values.items() if v is not None})
    if isinstance(data.get("until"), int):
        data["until"] = _date(data["until"], lang) if data["until"] else ""
    out = text
    for k, v in data.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _candidates(db: Session, key: str, p: int) -> list[tuple[User, int, dict]]:
    """(user, stamp, values) for everyone the notice is due to now."""
    t = now()
    base = db.query(User).filter(User.bot_blocked.is_(False), User.is_blocked.is_(False))

    def not_sent(stamp):
        # People who already got this notice are left out in the query
        # itself: a portion of 300 must not be filled with them again and
        # again while everyone after them waits forever.
        return ~exists().where(and_(WarmSent.user_id == User.id, WarmSent.trigger == key,
                                    WarmSent.stamp == stamp))

    rows: list = []
    if key in ("expiring", "trial_end"):
        q = (base.outerjoin(Plan, Plan.id == User.plan_id)
             .filter(User.expires_at > t, User.expires_at <= t + p * 86400, not_sent(User.expires_at)))
        q = q.filter(Plan.is_trial.is_(True)) if key == "trial_end" else \
            q.filter(or_(Plan.is_trial.is_(False), Plan.id.is_(None)))
        for u in q.order_by(User.expires_at).limit(300).all():
            rows.append((u, u.expires_at, {"days": max(1, (u.expires_at - t + 3600) // 86400)}))
    elif key in ("expired", "winback"):
        edge = t - p * 86400
        for u in base.filter(User.expires_at > 0, User.expires_at <= edge,
                             User.expires_at > edge - CATCH_UP, not_sent(User.expires_at)
                             ).order_by(User.expires_at).limit(300).all():
            rows.append((u, u.expires_at, {}))
    elif key == "no_purchase":
        edge = t - p * 3600
        for u in base.filter(User.expires_at == 0, User.created_at <= edge,
                             User.created_at > edge - CATCH_UP, not_sent(0)
                             ).order_by(User.created_at).limit(300).all():
            rows.append((u, 0, {}))
    elif key == "unpaid":
        from sqlalchemy.orm import aliased
        edge = t - p * 60
        later = aliased(Transaction)
        q = (db.query(Transaction, User).join(User, User.id == Transaction.user_id)
             .filter(User.bot_blocked.is_(False), User.is_blocked.is_(False),
                     Transaction.status.in_(UNPAID_STATES), Transaction.amount > 0,
                     Transaction.method.notin_(("free", "admin", "balance")),
                     Transaction.created_at <= edge, Transaction.created_at > edge - 86400,
                     # tried again, paid another way, or bought something since: not abandoned
                     ~exists().where(and_(later.user_id == Transaction.user_id, later.id > Transaction.id)),
                     ~exists().where(and_(WarmSent.user_id == User.id, WarmSent.trigger == key,
                                          or_(WarmSent.stamp == Transaction.id,
                                              WarmSent.created_at > t - UNPAID_GAP))))
             .order_by(Transaction.id).limit(300))
        seen: set[int] = set()
        for tx, u in q.all():
            if u.id in seen:
                continue
            seen.add(u.id)
            rows.append((u, tx.id, {"amount": core.money(tx.amount, u.lang or env.default_lang)}))
    elif key == "traffic":
        for u in base.filter(User.traffic_limit > 0, User.traffic_blocked.is_(False),
                             User.expires_at > t,
                             User.traffic_used * 100 >= User.traffic_limit * p,
                             not_sent(User.traffic_limit + User.expires_at)).limit(300).all():
            used = int(u.traffic_used * 100 // max(1, u.traffic_limit))
            rows.append((u, int(u.traffic_limit) + int(u.expires_at), {"used": used}))
    return rows


def run_triggers(db: Session, only: str | None = None) -> dict:
    """Send every notice that is due. Each event goes out once per person."""
    from . import notify
    sent: dict[str, int] = {}
    for key in TRIGGERS:
        if only and key != only:
            continue
        tr = trigger(db, key)
        if not tr["on"]:
            continue
        n = 0
        for user, stamp, values in _candidates(db, key, int(tr["param"])):
            mark = WarmSent(user_id=user.id, trigger=key, stamp=stamp)
            db.add(mark)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                continue            # already sent for this event
            offer = give_offer(db, user, tr["percent"], tr["valid"], f"trigger:{key}") \
                if tr["percent"] else None
            lang = user.lang or env.default_lang
            text = render(db, tr[lang if lang in ("ru", "en") else "en"], user,
                          percent=offer.percent if offer else tr["percent"],
                          until=offer.expires_at if offer else 0, **values)
            if key == "expired":
                from . import access
                text += access.grace_line(db, user, lang)
            if key in RENEW_KEYS and renewal_invoice(db, user, text):
                n += 1
                continue
            button = {"renew": "renew", "offer": "take_offer", "open": "open_app",
                      "finish": "take_offer" if offer else "finish_pay"}[tr["button"]]
            if notify.send_safe(db, user, text, button, "go=sub" if button != "open_app" else ""):
                n += 1
        if n:
            sent[key] = n
            core.audit(db, "Warm-up notices sent", "system", f"{key}: {n}")
    return sent


# Notices about the end of a subscription carry an invoice for the same plan.
RENEW_KEYS = ("expiring", "expired")


def renewal_invoice(db: Session, user: User, text: str) -> bool:
    """
    The reminder as a Telegram Stars invoice for the plan the person has
    (with the same devices and any discount they are due): one tap on
    "Pay" in the chat renews it, no app to open. Returns False when it does
    not apply (turned off, no Stars, no paid plan, the plan is hidden) —
    then the ordinary message goes out instead.
    """
    import re

    from . import billing, notify
    from .payments import stars
    if core.get(db, "renew_invoice") != "1" or not stars.configured():
        return False
    plan = db.get(Plan, user.plan_id) if user.plan_id else None
    if not plan or plan.is_trial or not plan.enabled or plan.price <= 0:
        return False
    try:
        tx = billing.new_order(db, user, "purchase", "stars", plan=plan,
                               devices=int(user.device_limit or plan.devices or 0))
    except billing.OrderError:
        return False
    if tx.amount <= 0:
        return False
    tx.stars, tx.gateway = billing.stars_for(db, tx.amount), "stars"
    db.commit()
    lang = user.lang or env.default_lang
    name = plan.name_en if lang == "en" and plan.name_en else plan.name
    title = core.t(lang, "renew_title", plan=name)[:32]
    plain = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
    rows = [[{"text": core.t(lang, "renew_pay", n=tx.stars), "pay": True}]]
    if env.https:
        rows.append([{"text": core.t(lang, "renew_other"), "web_app": {"url": notify.app_url("go=sub")}}])
    res = notify.call("sendInvoice", {
        "chat_id": user.tg_id, "title": title, "description": plain[:255] or title,
        "payload": f"tx:{tx.id}", "currency": "XTR",
        "prices": [{"label": title, "amount": tx.stars}],
        "reply_markup": {"inline_keyboard": rows}})
    if not res.get("ok"):
        tx.status, tx.error = "cancelled", str(res.get("description", ""))[:250]
        if res.get("error_code") == 403:
            user.bot_blocked = True
        db.commit()
        return False
    return True


def preview_trigger(db: Session, key: str, admin_tg: int) -> bool:
    """Send the notice as it would look to the admin themselves."""
    from . import notify
    tr = trigger(db, key)
    fake = User(tg_id=admin_tg, first_name="Admin", lang=env.default_lang)
    lang = env.default_lang
    text = render(db, tr[lang], fake, percent=tr["percent"], until=now() + tr["valid"] * 86400,
                  days=tr["param"], used=tr["param"], amount=core.money(299, lang))
    try:
        return notify.send(admin_tg, text, core.t(lang, "take_offer" if tr["percent"] else "open_app"),
                           "go=sub")
    except notify.Blocked:
        return False


# ---------------------------------------------------------------------------
#  Campaigns
# ---------------------------------------------------------------------------
def campaign_stats(db: Session, c: Campaign) -> dict:
    src = f"campaign:{c.id}"
    used = db.query(Offer).filter(Offer.source == src, Offer.used_at > 0)
    tx_ids = [o.tx_id for o in used.all() if o.tx_id]
    revenue = sum(t.amount for t in db.query(Transaction).filter(Transaction.id.in_(tx_ids)).all()) \
        if tx_ids else 0
    return {"offers": db.query(Offer).filter(Offer.source == src).count(),
            "bought": len(tx_ids), "revenue": round(revenue, 2)}


def run_campaigns(db: Session) -> None:
    """Send the due campaign in portions; the cursor survives restarts."""
    from . import notify
    t = now()
    c = (db.query(Campaign).filter(Campaign.status.in_(("scheduled", "sending")),
                                   Campaign.start_at <= t)
         .order_by(Campaign.id).first())
    if not c:
        return
    if c.status == "scheduled":
        c.status = "sending"
        c.total = segment_query(db, c.segment).count()
        db.commit()
        core.audit(db, "Campaign started", "system", f"#{c.id} {c.name}: {c.total}")
    batch = (segment_query(db, c.segment).filter(User.id > (c.cursor or 0))
             .order_by(User.id).limit(25).all())
    if not batch:
        c.status = "done"
        db.commit()
        core.audit(db, "Campaign finished", "system", f"#{c.id}: sent {c.sent}, failed {c.failed}")
        return
    for user in batch:
        db.refresh(c)
        if c.status != "sending":
            return                      # cancelled from the panel
        offer = give_offer(db, user, c.percent, c.valid_days, f"campaign:{c.id}") if c.percent else None
        lang = user.lang or env.default_lang
        body = (c.text_en if lang == "en" and c.text_en else c.text_ru) or c.text_en
        text = render(db, body, user, percent=offer.percent if offer else c.percent,
                      until=offer.expires_at if offer else 0)
        try:
            ok = notify.send(user.tg_id, text,
                             core.t(lang, "take_offer" if c.percent else "open_app"), "go=sub")
        except notify.Blocked:
            user.bot_blocked = True
            ok = False
        c.sent += 1 if ok else 0
        c.failed += 0 if ok else 1
        c.cursor = user.id
        db.commit()


def traffic_share(user: User) -> int:
    return int((user.traffic_used or 0) * 100 // max(1, user.traffic_limit or 0)) if user.traffic_limit else 0


__all__ = ["sale", "set_sale", "best_offer", "give_offer", "auto_discount", "spend_offer",
           "SEGMENTS", "segment_query", "TRIGGERS", "trigger", "set_trigger", "run_triggers",
           "run_campaigns", "campaign_stats", "GB"]
