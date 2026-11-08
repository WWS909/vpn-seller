"""
Orders and what a paid order gives.

The one rule that matters: an order becomes "success" only after access
has actually been granted on the servers. If granting fails, the order
goes back to "pending", the gateway gets an error, retries its
notification, and the person gets what they paid for on the next try.
"""

from __future__ import annotations

import datetime as dt
import math
import secrets

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import access, core
from .config import settings as env
from .models import GB, Gift, Plan, Promo, PromoUse, Transaction, User, now


class OrderError(Exception):
    """A request that cannot be fulfilled; the message key is in args[0]."""


_USER_FIELDS = ("expires_at", "plan_id", "device_limit", "traffic_limit",
                "traffic_used", "traffic_blocked", "trial_used", "traffic_extra",
                "bypass_limit", "bypass_used")


# ---------------------------------------------------------------------------
#  Pricing
# ---------------------------------------------------------------------------
def device_price(db: Session) -> float:
    """The price of one extra device for a month."""
    return core.get_float(db, "device_price", 50)


def device_months(days: float) -> float:
    """How many months a device is paid for: whole months for a month and
    longer (365 days = 12), a share of a month for shorter terms (7 days ≈ 0.23)."""
    days = max(0.0, float(days or 0))
    return round(days / 30) if days >= 30 else days / 30


def device_cost(db: Session, days: float, qty: int = 1) -> float:
    return round(int(qty) * device_price(db) * device_months(days), 2)


def traffic_packs(db: Session) -> list[dict]:
    packs = core.get_json(db, "traffic_packs", [])
    out = []
    for row in packs if isinstance(packs, list) else []:
        try:
            gb, price = int(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if gb > 0 and price >= 0:
            out.append({"gb": gb, "price": price})
    return out


def bypass_packs(db: Session) -> list[dict]:
    """Gigabytes for the whitelist-bypass servers, as the owner sells them."""
    packs = core.get_json(db, "bypass_packs", [])
    out = []
    for row in packs if isinstance(packs, list) else []:
        try:
            gb, price = int(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if gb > 0 and price >= 0:
            out.append({"gb": gb, "price": price})
    return out


def check_promo(db: Session, user: User, code: str) -> Promo:
    promo = (db.query(Promo)
             .filter(Promo.code == (code or "").strip().upper(), Promo.enabled.is_(True))
             .first())
    if not promo:
        raise OrderError("promo_not_found")
    if promo.limit and promo.used >= promo.limit:
        raise OrderError("promo_used_up")
    if promo.expires_at and promo.expires_at < now():
        raise OrderError("promo_expired")
    if promo.new_only and paid_before(db, user):
        raise OrderError("promo_new_only")
    if db.query(PromoUse).filter(PromoUse.promo_id == promo.id,
                                 PromoUse.user_id == user.id).first():
        raise OrderError("promo_already")
    return promo


def trial_available(db: Session, user: User) -> bool:
    """A trial is for the first contact only: never used, never had any
    subscription (bought, gifted or granted by an admin)."""
    from . import blacklist
    return (not user.trial_used and not user.expires_at and not paid_before(db, user)
            and not blacklist.refuses(db, user, "trial"))


# ---------------------------------------------------------------------------
#  The free trial: one hidden plan with is_trial, managed as a setting
# ---------------------------------------------------------------------------
def trial_plan(db: Session) -> Plan | None:
    return db.query(Plan).filter(Plan.is_trial.is_(True)).order_by(Plan.id).first()


def trial_info(db: Session) -> dict:
    p = trial_plan(db)
    return {"enabled": bool(p and p.enabled), "days": p.days if p else 3,
            "devices": p.devices if p else 1, "traffic_gb": p.traffic_gb if p else 0}


def set_trial(db: Session, *, enabled: bool, days: int, devices: int = 1,
              traffic_gb: int = 0) -> dict:
    """Turn the free trial on or off and set what it gives."""
    days = max(1, min(int(days), 90))
    p = trial_plan(db)
    if p is None:
        p = Plan(name="Пробный период", name_en="Free trial", price=0, is_trial=True, sort=0)
        db.add(p)
    p.enabled, p.days, p.price = bool(enabled), days, 0
    p.devices = max(0, min(int(devices), 100))
    p.traffic_gb = max(0, int(traffic_gb))
    # Only one trial exists: older duplicates from hand-made plans are hidden.
    for other in db.query(Plan).filter(Plan.is_trial.is_(True), Plan.id != (p.id or 0)).all():
        other.enabled = False
    db.commit()
    return trial_info(db)


def paid_before(db: Session, user: User) -> bool:
    return db.query(Transaction).filter(Transaction.user_id == user.id,
                                        Transaction.status == "success",
                                        Transaction.amount > 0).first() is not None


def round_price(value: float) -> float:
    """A discounted price: whole units (159 ₽, not 159,2 ₽), cents only in
    currencies where they are normal. Never rounds a price up."""
    import math
    return float(math.floor(value + 1e-9)) if env.whole_prices else round(value, 2)


def quote(db: Session, user: User, plan: Plan, devices: int = 0,
          promo_code: str = "", kind: str = "purchase") -> dict:
    """
    Price of a subscription with extra devices, the automatic discount (the
    sale or a personal offer, whichever is bigger) and an optional promo
    code. A promo code does not stack with the automatic discount: the
    lower price wins. Bonus days of a promo code are given either way.
    """
    from . import marketing
    included = int(plan.devices or 0)
    devices = included if not included else max(included, min(int(devices or included),
                                                               core.get_int(db, "max_devices", 10)))
    if plan.is_trial:
        devices = included          # the trial is free: no extra devices on it
    extra = devices - included if included else 0
    base = float(plan.price) + device_cost(db, plan.days or 0, extra)
    auto = marketing.auto_discount(db, user, kind) if not plan.is_trial else {"percent": 0}
    auto_price = base * (1 - auto["percent"] / 100)
    amount, bonus_days, promo, promo_price = auto_price, 0, None, None
    if promo_code:
        promo = check_promo(db, user, promo_code)
        if promo.kind == "percent":
            promo_price = base * (1 - min(100.0, max(0.0, promo.value)) / 100)
        elif promo.kind == "amount":
            promo_price = max(0.0, base - promo.value)
        elif promo.kind == "days":
            bonus_days = int(promo.value)
    if auto["percent"]:
        auto_price = round_price(auto_price)
    if promo_price is not None and promo_price != base:
        promo_price = round_price(promo_price)
    use_auto = auto["percent"] > 0 and (promo_price is None or auto_price <= promo_price)
    if promo_price is not None and not use_auto:
        amount = promo_price
    elif use_auto:
        amount = auto_price
    amount = max(0.0, round(amount, 2))
    # The code is spent only when it gave something: its price won, or it
    # adds days. When the sale is cheaper the code stays unused.
    promo_used = bool(promo) and (bonus_days > 0 or (promo_price is not None and not use_auto))
    return {"amount": amount, "base": round(base, 2), "devices": devices,
            "extra_devices": extra, "bonus_days": bonus_days,
            "promo": promo.code if promo else "", "promo_id": promo.id if promo_used else None,
            "promo_applied": promo_used,
            "discount": auto["percent"] if use_auto else 0,
            "discount_source": auto.get("source", "") if use_auto else "",
            "offer_id": auto.get("offer_id") if use_auto else None}


# ---------------------------------------------------------------------------
#  Orders
# ---------------------------------------------------------------------------
def new_order(db: Session, user: User, kind: str, method: str, *, plan: Plan | None = None,
              devices: int = 0, qty: int = 0, gb: int = 0, promo_code: str = "") -> Transaction:
    """Validate a request and create a pending order with a server-side price."""
    if user.is_blocked:
        raise OrderError("blocked")
    if kind != "gift" and not access.servers(db):
        # Nothing to give yet: do not take money for it. (A gift is only a
        # code; it is activated later, when servers exist.)
        raise OrderError("no_servers")
    tx = Transaction(user_id=user.id, kind=kind, method=method, currency=env.currency)

    if kind in ("purchase", "gift"):
        if not plan or not plan.enabled:
            raise OrderError("plan_not_found")
        if plan.is_trial and kind == "gift":
            raise OrderError("plan_not_found")
        if kind == "gift" and core.get(db, "gifts_enabled") != "1":
            raise OrderError("gifts_off")
        if plan.is_trial and not trial_available(db, user):
            raise OrderError("trial_used")
        q = quote(db, user, plan, devices, promo_code if kind == "purchase" else "", kind)
        tx.plan_id = plan.id
        tx.devices = q["devices"]
        tx.bonus_days = q["bonus_days"]
        tx.promo_id = q["promo_id"]
        tx.discount = q["discount"]
        tx.offer_id = q["offer_id"]
        tx.days = int(plan.days or 0)
        tx.traffic_gb = int(plan.traffic_gb or 0)
        tx.bypass_gb = int(plan.bypass_gb or 0)
        tx.amount = 0.0 if plan.is_trial else q["amount"]

    elif kind == "devices":
        if not user.is_active:
            raise OrderError("no_subscription")
        if not user.device_limit:
            raise OrderError("unlimited_devices")     # 0 means no limit: nothing to add
        qty = max(1, min(int(qty or 1), core.get_int(db, "max_devices", 10)))
        tx.qty = qty
        # For the time the subscription has left, at least one month.
        left = max(0, int(user.expires_at or 0) - now()) / 86400
        tx.amount = device_cost(db, max(30.0, left), qty)

    elif kind == "traffic":
        if not user.has_subscription:
            raise OrderError("no_subscription")
        if not user.traffic_limit:
            raise OrderError("unlimited_traffic")
        pack = next((p for p in traffic_packs(db) if p["gb"] == int(gb or 0)), None)
        if not pack:
            raise OrderError("plan_not_found")
        tx.gb = pack["gb"]
        tx.amount = pack["price"]

    elif kind == "bypass":
        if not user.has_subscription:
            raise OrderError("no_subscription")
        if not access.bypass_servers(db):
            raise OrderError("plan_not_found")
        pack = next((p for p in bypass_packs(db) if p["gb"] == int(gb or 0)), None)
        if not pack:
            raise OrderError("plan_not_found")
        tx.bypass_gb = pack["gb"]
        tx.amount = pack["price"]
    else:
        raise OrderError("plan_not_found")

    if tx.amount <= 0:
        tx.method = "free"
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return tx


def finish(db: Session, tx: Transaction, *, external_id: str = "",
           confirmed: bool = True, manual: bool = False) -> bool:
    """
    Close a paid order: grant what was bought, then mark it success.

    Safe to call many times and from several gateway notifications at once:
    the order is captured with one conditional UPDATE, so only one caller
    does the work. Returns True if this call completed the order.

    confirmed=True means the money is known to be received (a verified
    gateway notification). If access then cannot be granted, the order is
    parked as "paid" and the retry job grants it as soon as a server
    answers — the money is never forgotten.
    """
    if external_id:
        # Only the first payment id is kept: a second charge for the same
        # order must stay recognisable as a different one.
        db.execute(update(Transaction).where(Transaction.id == tx.id, Transaction.external_id == "")
                   .values(external_id=external_id[:128]))
        db.commit()
    # A verified payment on an order that was cancelled meanwhile (the person
    # paid an old invoice) is still money received: it is granted, not lost.
    open_states = ("pending", "failed", "paid", "cancelled") if confirmed else ("pending", "failed", "paid")
    was_cancelled = tx.status == "cancelled"
    # The admin's "grant" on an order no gateway confirmed: access is given,
    # but whether money came is only the admin's word — its receipt waits
    # for them instead of going to the tax office by itself.
    hold = manual and db.execute(select(Transaction.status).where(Transaction.id == tx.id)).scalar() != "paid"
    res = db.execute(update(Transaction)
                     .where(Transaction.id == tx.id, Transaction.status.in_(open_states))
                     .values(status="processing"))
    db.commit()
    if not res.rowcount:
        db.refresh(tx)
        return False
    if confirmed:
        db.execute(update(Transaction).where(Transaction.id == tx.id, Transaction.money_at == 0)
                   .values(money_at=now()))
        db.commit()
    if was_cancelled:
        core.audit(db, "Payment for a cancelled order — granted", f"tx:{tx.id}", "", level="warn")
    # Two orders of one person closing at once (a parked order granted by
    # the retry job while a new payment comes in, a referral reward) must
    # not read the same expiry and both write "+30 days" over it: the
    # customer's part is done under their lock, one order after another.
    try:
        with access.user_lock(tx.user_id):
            user, extra = _grant(db, tx, confirmed, hold)
    except Exception:
        # Whatever went wrong, a captured order must not stay "processing":
        # nothing would ever pick it up again. Paid money goes back to the
        # retry queue.
        db.rollback()
        db.execute(update(Transaction).where(Transaction.id == tx.id, Transaction.status == "processing")
                   .values(status="paid" if confirmed else "pending"))
        db.commit()
        raise
    from . import marketing, referral
    if tx.offer_id and not marketing.spend_offer(db, tx.offer_id, tx):
        core.audit(db, "Personal discount used twice", f"tg:{user.tg_id}",
                   f"#{tx.id}: the offer was already spent by another order", level="warn")
    try:
        referral.reward(db, tx, user)
    except Exception as e:  # noqa: BLE001 — the purchase itself is done and stays done
        core.audit(db, "Referral reward failed", f"tg:{user.tg_id}", f"#{tx.id}: {e}", level="error")
    core.audit(db, "Paid", f"tg:{user.tg_id}",
               f"#{tx.id} {tx.kind} {tx.amount:g} {tx.currency} via {tx.gateway or tx.method}")
    _after_paid(db, tx, user, extra)
    return True


def _grant(db: Session, tx: Transaction, confirmed: bool, hold: bool = False) -> tuple[User, dict]:
    """The part of finish() that changes the customer. Runs under their lock."""
    db.refresh(tx)
    user = db.get(User, tx.user_id)
    db.refresh(user)            # fresh values: the snapshot below may be written back
    trial = tx.kind == "purchase" and bool(getattr(db.get(Plan, tx.plan_id), "is_trial", False))
    if trial:
        # Two trial orders opened at once (a double tap) both passed the
        # check in new_order. The trial is taken here with one conditional
        # UPDATE: only one of them gets it.
        took = db.execute(update(User).where(User.id == user.id, User.trial_used.is_(False))
                          .values(trial_used=True)).rowcount
        db.commit()
        if not took:
            db.execute(update(Transaction).where(Transaction.id == tx.id)
                       .values(status="cancelled", error="the trial was already taken"))
            db.commit()
            raise OrderError("trial_used")
        db.refresh(user)
    # access.push() commits along the way (server health, sync status), so a
    # failed grant cannot be undone with a rollback alone — the customer's
    # fields are put back explicitly.
    before = {f: getattr(user, f) for f in _USER_FIELDS}
    if trial:
        before["trial_used"] = False      # not granted after all: the trial can be tried again

    # A promo code is taken here, with one conditional UPDATE: several
    # orders opened with a one-time code before any was paid get it once.
    claimed = _claim_promo(db, tx, user) if tx.promo_id else False
    try:
        extra = _apply(db, tx, user, promo_ok=claimed, hold=hold)
    except Exception as e:
        db.rollback()
        if claimed:
            _release_promo(db, tx.promo_id, user.id)     # the retry takes it again
        for field, value in before.items():
            setattr(user, field, value)
        db.execute(update(Transaction).where(Transaction.id == tx.id)
                   .values(status="paid" if confirmed else "pending", paid_at=0, error=str(e)[:250]))
        db.commit()
        core.audit(db, "Order not granted", f"tg:{user.tg_id}",
                   f"#{tx.id} {tx.kind}: {e}", level="error")
        raise

    tx.status = "success"
    tx.paid_at = tx.paid_at or now()
    tx.error = ""
    db.commit()
    return user, extra


def _apply(db: Session, tx: Transaction, user: User, promo_ok: bool = True, hold: bool = False) -> dict:
    """Change the customer according to the order and push it to the panels."""
    extra: dict = {}
    # The «Мой налог» receipt is claimed in the same commit that closes the
    # order: an order can never end up done without its receipt (a crash
    # right after), nor get a second one.
    from . import nalog
    nalog.claim(db, tx, hold=hold)
    if tx.kind == "gift":
        plan = db.get(Plan, tx.plan_id)
        if not plan:
            raise RuntimeError("plan is gone")
        # Marked done in the same commit as the gift code: a crash between
        # the two would otherwise make the retry job create a second code.
        tx.status, tx.paid_at = "success", now()
        extra["code"] = _create_gift(db, user, plan, tx)
        return extra

    reset = False
    if tx.kind == "purchase":
        plan = db.get(Plan, tx.plan_id)
        if not plan:
            raise RuntimeError("plan is gone")
        start = max(now(), int(user.expires_at or 0))
        from . import referral
        extra["friend_days"] = referral.friend_bonus(db, tx, user)
        bonus = int(tx.bonus_days or 0)
        if tx.promo_id and not promo_ok:
            # Several orders were opened with one code and paid one by one:
            # the code went with the first of them.
            bonus = 0
            core.audit(db, "Promo code was already used", f"tg:{user.tg_id}",
                       f"#{tx.id}: paid {tx.amount:g} {tx.currency} with a code that was spent "
                       "by another order", level="warn")
        days = int(tx.days) if tx.days is not None else int(plan.days or 0)
        traffic_gb = int(tx.traffic_gb) if tx.traffic_gb is not None else int(plan.traffic_gb or 0)
        # Renewing the same plan while it runs keeps devices bought on top
        # of it (an invoice made before they were bought knows nothing of them).
        keep = user.plan_id == plan.id and int(user.expires_at or 0) > now() and int(user.device_limit or 0) \
            and int(tx.devices or 0)
        user.expires_at = start + (days + bonus + extra["friend_days"]) * 86400
        user.plan_id = plan.id
        user.device_limit = max(int(tx.devices or 0), int(user.device_limit or 0)) if keep \
            else int(tx.devices or 0)
        new_limit = traffic_gb * GB
        # Gigabytes from traffic packs that are still unused move into the
        # new period (the plan's own traffic is used up first).
        carry = 0
        if new_limit and user.traffic_limit and user.traffic_extra:
            left = int(user.traffic_limit) - int(user.traffic_used or 0)
            carry = max(0, min(int(user.traffic_extra), left))
        extra["carry_gb"] = round(carry / GB, 1)
        new_limit += carry
        user.traffic_extra = carry
        # A plan with a traffic limit starts a new period: counters go to zero.
        reset = bool(new_limit)
        user.traffic_limit = new_limit
        user.traffic_used = 0 if reset else user.traffic_used
        user.traffic_blocked = False
        # Bypass gigabytes: what is left moves on, the plan's own are added.
        # When the panels' counters are reset (a plan with a traffic limit)
        # "used" starts from zero too.
        add = int(tx.bypass_gb) if tx.bypass_gb is not None else int(plan.bypass_gb or 0)
        b_used = int(user.bypass_used or 0)
        b_left = max(0, int(user.bypass_limit or 0) - b_used)
        if reset:
            b_used = 0
        user.bypass_used = b_used
        user.bypass_limit = (b_used + b_left + add * GB) if (b_left or add) else 0
        if plan.is_trial:
            user.trial_used = True
        extra["plan"] = plan
    elif tx.kind == "devices":
        # 0 is "no limit": an old order paid after a switch to an unlimited
        # plan must not turn "unlimited" into "N devices".
        if user.device_limit:
            user.device_limit = int(user.device_limit) + int(tx.qty or 0)
    elif tx.kind == "bypass":
        b_used = int(user.bypass_used or 0)
        user.bypass_limit = max(int(user.bypass_limit or 0), b_used) + int(tx.bypass_gb or 0) * GB
    elif tx.kind == "traffic":
        if user.traffic_limit:              # the same for unlimited traffic
            user.traffic_limit = int(user.traffic_limit) + int(tx.gb or 0) * GB
            user.traffic_extra = int(user.traffic_extra or 0) + int(tx.gb or 0) * GB
            if user.traffic_used < user.traffic_limit:
                user.traffic_blocked = False

    # The order is marked done in the same commit that stores the new access
    # (push() commits only when a server took it): a crash can no longer
    # leave the days given and the order open, which the retry job would
    # then give a second time.
    tx.status = "success"
    tx.paid_at = now()
    # Raises AccessError (and rolls the session back) when not a single
    # server accepted the customer.
    access.push(db, user, required=True, reset_traffic=reset)
    return extra


def _claim_promo(db: Session, tx: Transaction, user: User) -> bool:
    """Take one use of the order's promo code for this person. False when
    the person already used it, or its limit is reached."""
    db.add(PromoUse(promo_id=tx.promo_id, user_id=user.id))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return False
    took = db.execute(update(Promo).where(Promo.id == tx.promo_id,
                                          or_(Promo.limit == 0, Promo.used < Promo.limit))
                      .values(used=Promo.used + 1)).rowcount
    if not took:
        db.rollback()
        return False
    db.commit()
    return True


def _release_promo(db: Session, promo_id: int, user_id: int) -> None:
    n = db.query(PromoUse).filter(PromoUse.promo_id == promo_id, PromoUse.user_id == user_id).delete()
    if n:
        db.execute(update(Promo).where(Promo.id == promo_id, Promo.used > 0).values(used=Promo.used - 1))
    db.commit()


def promo_open(db: Session, promo_id: int, user_id: int) -> bool:
    """The code can still be used by this person (checked before Stars are taken)."""
    promo = db.get(Promo, promo_id)
    if not promo or (promo.limit and promo.used >= promo.limit):
        return False
    return not db.query(PromoUse).filter(PromoUse.promo_id == promo_id,
                                         PromoUse.user_id == user_id).first()


ORDER_TTL = 10 * 86400          # an unpaid order older than this cannot be paid with Stars


def stale_reason(db: Session, tx: Transaction) -> str:
    """Why an open order should no longer be paid, or "" when it may.

    Checked before Telegram takes the Stars, so a reminder invoice that lay
    in the chat for weeks, or one already covered by another purchase,
    cannot be paid by mistake. Text keys of core.TEXTS."""
    from . import marketing
    # A reminder sent N days before the end must stay payable until then.
    ttl = max(ORDER_TTL, (int(marketing.trigger(db, "expiring").get("param") or 0) + 2) * 86400)
    if now() - int(tx.created_at or 0) > ttl:
        return "order_old"
    if tx.kind in ("purchase", "gift"):
        plan = db.get(Plan, tx.plan_id) if tx.plan_id else None
        if not plan or not plan.enabled:
            return "order_old"
    if tx.promo_id and not promo_open(db, tx.promo_id, tx.user_id):
        return "order_promo_gone"
    if tx.offer_id:
        from .models import Offer
        o = db.get(Offer, tx.offer_id)
        if not o or o.used_at or o.expires_at < now():
            return "order_offer_gone"
    # A subscription paid for since this one was made: paying this one too
    # would renew twice (a reminder invoice after renewing in the app). A
    # free trial, a gift code, devices or traffic bought meanwhile do not count.
    if tx.kind == "purchase":
        newer = db.query(Transaction.id).filter(
            Transaction.user_id == tx.user_id, Transaction.id != tx.id,
            Transaction.status == "success", Transaction.kind == "purchase", Transaction.amount > 0,
            Transaction.paid_at > int(tx.created_at or 0)).first()
        if newer:
            return "order_old"
    return ""


def _after_paid(db: Session, tx: Transaction, user: User, extra: dict) -> None:
    from . import notify
    lang = user.lang or env.default_lang
    if tx.kind == "gift":
        what = core.t(lang, "paid_gift", code=extra.get("code", ""),
                      link=gift_link(extra.get("code", "")))
    elif tx.kind == "purchase":
        plan = extra.get("plan")
        name = (plan.name_en if lang == "en" and plan and plan.name_en else plan.name if plan else "")
        what = core.t(lang, "paid_plan", plan=name, date=_date(user.expires_at, lang))
        if extra.get("carry_gb"):
            what += " " + core.t(lang, "paid_carry", gb=f"{extra['carry_gb']:g}")
    elif tx.kind == "devices":
        what = core.t(lang, "paid_devices", n=user.device_limit)
    elif tx.kind == "bypass":
        what = core.t(lang, "paid_bypass", gb=tx.bypass_gb)
    else:
        what = core.t(lang, "paid_traffic", gb=tx.gb)
    if tx.amount > 0 or tx.kind == "gift":
        notify.send_safe(db, user, core.t(lang, "paid", what=what))
    if tx.amount > 0:
        notify.admins(f"Оплата / Payment: {tx.amount:g} {tx.currency} · {tx.kind} · "
                      f"{'@' + user.username if user.username else user.tg_id} · {tx.gateway or tx.method}")


def _date(ts: int, lang: str) -> str:
    return dt.datetime.fromtimestamp(int(ts or 0)).strftime("%d.%m.%Y" if lang == "ru" else "%Y-%m-%d")


# ---------------------------------------------------------------------------
#  Gifts
# ---------------------------------------------------------------------------
GIFT_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"      # no 0/O, 1/I/L


def _gift_code() -> str:
    body = "".join(secrets.choice(GIFT_ALPHABET) for _ in range(8))
    return f"{body[:4]}-{body[4:]}"


def _create_gift(db: Session, buyer: User, plan: Plan, tx: Transaction) -> str:
    for _ in range(10):
        code = _gift_code()
        if not db.query(Gift).filter(Gift.code == code).first():
            break
    db.add(Gift(code=code, tx_id=tx.id, plan_id=plan.id, buyer_id=buyer.id, amount=tx.amount))
    db.commit()
    return code


def gift_link(code: str) -> str:
    from . import notify
    name = notify.username()
    clean = code.replace("-", "")
    return f"https://t.me/{name}?start=gift{clean}" if name else code


def redeem(db: Session, user: User, code: str) -> Plan:
    """Activate a gift code. The capture is one conditional UPDATE, so two
    people racing with the same code cannot both get a subscription."""
    code = (code or "").strip().upper().replace(" ", "")
    if "-" not in code and len(code) == 8:
        code = f"{code[:4]}-{code[4:]}"
    res = db.execute(update(Gift).where(Gift.code == code, Gift.used_by_id.is_(None))
                     .values(used_by_id=user.id, used_at=now()))
    db.commit()
    if not res.rowcount:
        raise OrderError("gift_invalid")
    gift = db.query(Gift).filter(Gift.code == code).first()
    plan = db.get(Plan, gift.plan_id) if gift and gift.plan_id else None
    if not plan:
        raise OrderError("gift_invalid")
    tx = Transaction(user_id=user.id, kind="purchase", plan_id=plan.id, amount=0,
                     currency=env.currency, method="gift", devices=plan.devices or 0,
                     external_id=code)
    db.add(tx)
    db.commit()
    try:
        finish(db, tx, confirmed=False)
    except Exception:
        # Access could not be granted right now: give the code back so it
        # can be used again, instead of burning it.
        db.execute(update(Gift).where(Gift.code == code).values(used_by_id=None, used_at=0))
        db.commit()
        raise
    return plan


def gift_of_order(db: Session, tx: Transaction) -> Gift | None:
    return db.query(Gift).filter(Gift.tx_id == tx.id).first()


# ---------------------------------------------------------------------------
#  Telegram Stars
# ---------------------------------------------------------------------------
def stars_for(db: Session, amount: float) -> int:
    rate = core.stars_rate(db)
    return max(1, int(math.ceil(float(amount) / max(0.0001, rate))))


def recover_interrupted(db: Session) -> int:
    """On start: orders caught mid-way by a restart or crash ("processing")
    were already confirmed or free, so they are handed to the retry job
    instead of hanging forever. Runs before any request is served."""
    res = db.execute(update(Transaction).where(Transaction.status == "processing")
                     .values(status="paid"))
    db.commit()
    return res.rowcount or 0


RETRY_MAX_WAIT = 3600            # a stubborn order is still tried at least hourly
RETRY_ALERT = 5                 # after this many failed tries the admins are told


def retry_paid(db: Session) -> int:
    """Grant orders whose money arrived while every server was down.

    Each failure makes the next try wait longer (2, 4, 8 … up to 60 min),
    and the order goes to the back of the queue: one order a panel keeps
    refusing does not hold up the newer ones."""
    from . import notify
    done = 0
    t = now()
    for tx in (db.query(Transaction).filter(Transaction.status == "paid",
                                            Transaction.next_try_at <= t)
               .order_by(Transaction.next_try_at, Transaction.id).limit(50).all()):
        try:
            if finish(db, tx):
                done += 1
            continue
        except Exception as e:  # noqa: BLE001 — stays "paid", next round tries again
            error = str(e)[:200]
        tries = int(tx.tries or 0) + 1
        db.execute(update(Transaction).where(Transaction.id == tx.id)
                   .values(tries=tries, next_try_at=now() + min(RETRY_MAX_WAIT, 60 * 2 ** tries)))
        db.commit()
        if tries == RETRY_ALERT:
            notify.admins(f"Оплаченный заказ #{tx.id} не удаётся выдать уже {tries} раз / "
                          f"Paid order #{tx.id} still not granted: {error}. "
                          "Проверьте серверы в панели; магазин продолжит пытаться сам.")
    return done
