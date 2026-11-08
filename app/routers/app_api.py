"""API of the mini app. Personal routes require Telegram initData."""

from __future__ import annotations

import re
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import access, billing, channel, clients, core, emergency, legal, marketing, payments, referral, support, support_ai, theme
from ..auth import tg_user
from ..config import settings as env
from ..db import get_db
from ..models import Gift, Payout, Plan, Review, Transaction, User, now

router = APIRouter(prefix="/api/app", tags=["mini app"])


# ---------------------------------------------------------------------------
def current_user(db: Session, tg: dict) -> User:
    """Find or register the Telegram user behind the request."""
    user = db.query(User).filter(User.tg_id == tg["id"]).first()
    if user is None:
        from .. import sources
        user = User(tg_id=tg["id"], username=(tg.get("username") or "")[:64],
                    first_name=(tg.get("first_name") or "")[:128],
                    lang=core.lang_of(tg.get("language_code")),
                    source=sources.code_from_start(str(tg.get("_start") or "")))
        db.add(user)
        try:
            db.commit()
        except Exception:  # noqa: BLE001 — a parallel request created it first
            db.rollback()
            user = db.query(User).filter(User.tg_id == tg["id"]).first()
        else:
            core.audit(db, "New user", f"tg:{user.tg_id}",
                       f"@{user.username}" if user.username else user.first_name)
            start = str(tg.get("_start") or "")
            if start.startswith("r"):
                _join_referral(db, user, start)
    changed = False
    for field, value in (("username", (tg.get("username") or "")[:64]),
                         ("first_name", (tg.get("first_name") or "")[:128])):
        if value and getattr(user, field) != value:
            setattr(user, field, value)
            changed = True
    if user.bot_blocked:
        user.bot_blocked = False          # they are here, so the bot can write again
        changed = True
    if changed:
        db.commit()
    return user


def _join_referral(db: Session, user: User, code: str) -> None:
    """A newcomer through somebody's link. With the captcha on, the bot asks
    it first; the referral counts once it is solved."""
    if referral.attach(db, user, code) == "captcha":
        send_captcha(db, user)


def send_captcha(db: Session, user: User) -> None:
    """A fresh captcha (new numbers, new answer) to the person in the bot."""
    from .. import notify
    text, options = referral.captcha(db, user)
    notify.call("sendMessage", {"chat_id": user.tg_id, "text": text, "reply_markup": {
        "inline_keyboard": [[{"text": o, "callback_data": f"cap:{o}"} for o in options]]}})


def fail(user: User | None, key: str, code: int = 400, **kw):
    raise HTTPException(code, core.t((user.lang if user else None) or env.default_lang, key, **kw))


def order_error(user: User | None, e: Exception, code: int = 400):
    """An OrderError as a message: args are (key) or (key, {values})."""
    kw = e.args[1] if len(e.args) > 1 and isinstance(e.args[1], dict) else {}
    fail(user, e.args[0], code, **kw)


def plan_out(p: Plan) -> dict:
    return {"id": p.id, "name": p.name, "name_en": p.name_en or p.name, "days": p.days,
            "price": p.price, "old_price": p.old_price or 0, "badge": p.badge or "",
            "devices": p.devices or 0, "traffic_gb": p.traffic_gb or 0,
            "bypass_gb": p.bypass_gb or 0, "is_trial": bool(p.is_trial)}


# ---------------------------------------------------------------------------
#  Public: what the shop sells. Shown before sign-in too.
# ---------------------------------------------------------------------------
@router.get("/config")
def config(db: Session = Depends(get_db)):
    plans = (db.query(Plan).filter(Plan.enabled.is_(True))
             .order_by(Plan.is_trial, Plan.sort, Plan.id).all())
    faq = core.get_json(db, "faq_json", [])
    show_load = core.get(db, "servers_load") == "1"
    return {
        "brand": core.get(db, "brand_name"),
        "currency": env.currency,
        "symbol": env.currency_symbol,
        "whole_prices": env.whole_prices,
        "default_lang": env.default_lang,
        "methods": payments.methods(),
        "pay_labels": payments.labels(db),
        "plans": [plan_out(p) for p in plans],
        "servers": [{"name": s.name, "country": (s.country or "").upper(), "bypass": bool(s.bypass),
                     "load": access.load_of(s) if show_load else None}
                    for s in access.servers(db)],
        "traffic_packs": billing.traffic_packs(db),
        # The whitelist-bypass servers: their gigabytes are sold apart.
        "bypass": bool(access.bypass_servers(db)),
        "bypass_packs": billing.bypass_packs(db),
        "device_price": billing.device_price(db),           # a month; × months of the term
        "max_devices": core.get_int(db, "max_devices", 10),
        "stars_rate": core.stars_rate(db),
        "support": core.get(db, "support_url"),
        "about": {"ru": core.get(db, "about_ru"), "en": core.get(db, "about_en")},
        "faq": faq if isinstance(faq, list) else [],
        "gifts": core.get(db, "gifts_enabled") == "1",
        "reviews": core.get(db, "reviews_enabled") == "1",
        "admin_link": core.get(db, "admin_link"),
        "test_mode": env.payments_test_mode,
        "logo": theme.logo_url(db),
        "tagline": {"ru": core.get(db, "tagline_ru"), "en": core.get(db, "tagline_en")},
        "sale": marketing.sale(db),
        "referral": {"enabled": referral.enabled(db), "mode": referral.mode(db),
                     "days": core.get_int(db, "ref_days", 0),
                     "percent": referral.percent(db),
                     "friend_days": core.get_int(db, "ref_friend_days", 0),
                     "every": core.get(db, "ref_every") == "1",
                     "top": core.get(db, "top_enabled") == "1"},
        "balance_pay": core.get(db, "ref_balance_pay") == "1",
        "docs": legal.exists(db),
        "channel": {k: v for k, v in channel.info(db).items() if k != "id"},
        "links": theme.links(db),
        "support_chat": support.enabled(db),
        "support_ai": support.enabled(db) and support_ai.enabled(db),
        "emergency": emergency.public(db),
        "apps": clients.offered(core.get(db, "connect_apps"), core.get(db, "require_hwid") == "1"),
    }


# ---------------------------------------------------------------------------
#  The person
# ---------------------------------------------------------------------------
@router.get("/me")
def me(tg: dict = Depends(tg_user), db: Session = Depends(get_db)):
    user = current_user(db, tg)
    plan = user.plan
    history = (db.query(Transaction)
               .filter(Transaction.user_id == user.id, Transaction.status == "success")
               .order_by(Transaction.id.desc()).limit(20).all())
    from .. import team
    is_admin = user.tg_id in env.admin_telegram_ids or team.member(db, user.tg_id) is not None
    link_rule = core.get(db, "admin_link")
    if not user.app_opened_at:
        user.app_opened_at = now()              # the funnel: the app was opened
        db.commit()
    return {
        "tg_id": user.tg_id,
        "username": user.username,
        "first_name": user.first_name,
        "lang": user.lang or core.lang_of(tg.get("language_code")),
        "active": user.is_active,
        "has_subscription": user.has_subscription,
        "blocked": bool(user.is_blocked),
        "traffic_blocked": bool(user.traffic_blocked),
        "expires_at": user.expires_at or 0,
        # The paid time is over but the VPN still works (the days after the
        # end, or an emergency hold): until when. 0 otherwise.
        "grace_until": access.grace_until(db, user),
        # After that: only Telegram works until then (0 — not now or not set up).
        "tg_only_until": access.tg_until(db, user) if access.tg_servers(db) else 0,
        "plan": plan_out(plan) if plan else None,
        "device_limit": user.device_limit or 0,
        "devices": access.devices(db, user),
        "removed_devices": access.removed_devices(db, user),
        "traffic_used": user.traffic_used or 0,
        "traffic_limit": user.traffic_limit or 0,
        "bypass_used": user.bypass_used or 0,
        "bypass_limit": user.bypass_limit or 0,
        "sub_url": f"{env.public_url}/sub/{user.sub_token}" if user.expires_at else "",
        "trial_available": billing.trial_available(db, user),
        "history": [{"id": t.id, "kind": t.kind, "amount": t.amount, "currency": t.currency,
                     "method": t.method, "at": t.paid_at or t.created_at} for t in history],
        "spent": round(sum(t.amount for t in history), 2),
        "show_admin": link_rule == "all" or (link_rule == "admins" and is_admin),
        "is_admin": is_admin,
        "offer": _offer_out(db, user),
        "review_bonus": _review_bonus(db, user),
        "gate": channel.gate(db, user, "all"),
        "trial_gate": channel.gate(db, user, "trial") if billing.trial_available(db, user) else None,
        "start": tg.get("_start", ""),
        "ref_balance": max(0.0, round(float(user.ref_balance or 0), 2)),
    }


def _offer_out(db: Session, user: User) -> dict | None:
    o = marketing.best_offer(db, user)
    return {"percent": o.percent, "until": o.expires_at} if o else None


class SupportBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


@router.get("/support")
def support_history(tg: dict = Depends(tg_user), db: Session = Depends(get_db)):
    user = current_user(db, tg)
    if not support.enabled(db):
        return {"enabled": False, "messages": []}
    return {"enabled": True, "messages": support.history(db, user, 50)}


@router.post("/support")
def support_write(body: SupportBody, tg: dict = Depends(tg_user), db: Session = Depends(get_db)):
    """A question from the app; the answer comes to the bot chat (and shows here)."""
    user = current_user(db, tg)
    if not support.enabled(db):
        fail(user, "support_off", 404)
    if not body.text.strip():
        fail(user, "support_empty")
    entry = support.incoming(db, user, text=body.text, source="app")
    if entry is None:
        fail(user, "sup_slow", 429)
    return {"ok": True, "messages": support.history(db, user, 50)}


class LangBody(BaseModel):
    lang: str


@router.post("/lang")
def set_lang(body: LangBody, tg: dict = Depends(tg_user), db: Session = Depends(get_db)):
    user = current_user(db, tg)
    user.lang = "en" if body.lang == "en" else "ru"
    db.commit()
    return {"ok": True, "lang": user.lang}


# ---------------------------------------------------------------------------
#  Orders
# ---------------------------------------------------------------------------
class OrderBody(BaseModel):
    kind: str = "purchase"              # purchase | gift | devices | traffic | bypass
    plan_id: int | None = None
    devices: int = 0
    qty: int = 0
    gb: int = 0
    promo: str = ""
    method: str = "stars"


class QuoteBody(BaseModel):
    plan_id: int
    devices: int = 0
    promo: str = Field("", max_length=32)


@router.post("/quote")
def quote(body: QuoteBody, tg: dict = Depends(tg_user), db: Session = Depends(get_db)):
    user = current_user(db, tg)
    plan = db.get(Plan, body.plan_id)
    if not plan or not plan.enabled:
        fail(user, "plan_not_found", 404)
    if body.promo:
        _throttle(user, "quote", 20)        # promo codes are not to be guessed by brute force
    try:
        q = billing.quote(db, user, plan, body.devices, body.promo)
    except billing.OrderError as e:
        order_error(user, e)
    q.pop("promo_id", None)
    q.pop("offer_id", None)
    return q


_recent: dict[tuple[str, int], list[float]] = {}


def _throttle(user: User, what: str = "order", per_minute: int = 12) -> None:
    """At most 12 orders a minute per person — each one may create an
    invoice at a gateway, and those should not be spammed. The same for
    trying promo codes and for asking Telegram about the channel."""
    stamp = time.time()
    key = (what, user.tg_id)
    hist = [t for t in _recent.get(key, []) if stamp - t < 60]
    if len(hist) >= per_minute:
        fail(user, "too_many", 429)
    hist.append(stamp)
    _recent[key] = hist
    if len(_recent) > 20000:
        for k in [k for k, v in _recent.items() if not v or stamp - v[-1] > 60]:
            _recent.pop(k, None)


@router.post("/order")
def order(body: OrderBody, tg: dict = Depends(tg_user), db: Session = Depends(get_db)):
    user = current_user(db, tg)
    _throttle(user)
    if emergency.sales_stopped(db):
        fail(user, "em_sales", 503)
    from .. import blacklist
    if blacklist.refuses(db, user, "purchase"):
        fail(user, "bl_blocked", 403)
    plan = db.get(Plan, body.plan_id) if body.plan_id else None
    if channel.gate(db, user, "all"):
        fail(user, "channel_required", 403)
    if plan and plan.is_trial and body.kind == "purchase" and channel.gate(db, user, "trial"):
        fail(user, "channel_trial", 403)
    # the usual four, and a second card / crypto gateway by its name; an
    # unconfigured one is refused further on ("method_unavailable")
    method = body.method if body.method in {m["id"] for m in payments.methods()} | {
        "stars", "card", "crypto", "balance"} else "stars"
    if method == "balance" and core.get(db, "ref_balance_pay") != "1":
        fail(user, "balance_off")
    try:
        tx = billing.new_order(db, user, body.kind, method, plan=plan, devices=body.devices,
                               qty=body.qty, gb=body.gb, promo_code=body.promo)
        pay = payments.start(db, tx, user)
    except billing.OrderError as e:
        order_error(user, e)
    except payments.PaymentError as e:
        fail(user, e.args[0], 502)
    except access.AccessError:
        fail(user, "no_servers", 503)
    db.refresh(tx)
    return {"order_id": tx.id, "amount": tx.amount, "currency": tx.currency,
            "status": tx.status, "pay": pay}


@router.get("/order/{order_id}")
def order_status(order_id: int, tg: dict = Depends(tg_user), db: Session = Depends(get_db)):
    user = current_user(db, tg)
    tx = db.get(Transaction, order_id)
    if not tx or tx.user_id != user.id:
        fail(user, "order_not_found", 404)
    if tx.status == "pending":
        # The gateway's webhook may be late or lost — ask the gateway itself.
        try:
            payments.poll(db, tx)
        except access.AccessError:
            pass                # paid, parked as "paid"; the retry job grants it
        db.refresh(tx)
    out = {"order_id": tx.id, "status": tx.status, "kind": tx.kind}
    gift = billing.gift_of_order(db, tx) if tx.kind == "gift" and tx.status == "success" else None
    if gift:
        out["gift"] = {"code": gift.code, "link": billing.gift_link(gift.code)}
    return out


# ---------------------------------------------------------------------------
#  Referrals and the channel
# ---------------------------------------------------------------------------
@router.get("/referrals")
def referrals(tg: dict = Depends(tg_user), db: Session = Depends(get_db)):
    user = current_user(db, tg)
    st = referral.stats(db, user)
    history = [referral.payout_out(p) for p in db.query(Payout).filter(Payout.user_id == user.id)
               .order_by(Payout.id.desc()).limit(20).all()]
    wallet = {"balance": st["balance"], "earned": st["earned"], "withdrawn": st["withdrawn"],
              "pending": st["pending"], "spent": st["spent"],
              "min": max(0.0, core.get_float(db, "ref_min_payout", 0)),
              "methods": [{k: m[k] for k in ("id", "kind", "ru", "en", "hint_ru", "hint_en")}
                          for m in referral.payout_methods(db)],
              "open": referral.payouts_open(db),
              "balance_pay": core.get(db, "ref_balance_pay") == "1",
              "history": history}
    if not referral.enabled(db):
        # The program was turned off: money already earned is still the
        # person's — it can be withdrawn or spent, nothing new is earned.
        has_money = st["balance"] > 0 or st["pending"] > 0 or history
        return {"enabled": False, "wallet": wallet if has_money else None}
    return {"enabled": True, "mode": referral.mode(db), "link": referral.link(db, user),
            "invited": st["invited"], "paid": st["paid"], "days": st["days"],
            "reward_days": core.get_int(db, "ref_days", 0),
            "percent": referral.percent(db),
            "friend_days": core.get_int(db, "ref_friend_days", 0),
            "every": core.get(db, "ref_every") == "1",
            "friends": referral.friends(db, user),
            "wallet": wallet if referral.mode(db) == "money" or st["balance"] > 0 or history else None,
            "top": referral.top(db) if core.get(db, "top_enabled") == "1" else None,
            "top_type": core.get(db, "top_type"), "top_period": core.get(db, "top_period")}


class PayoutBody(BaseModel):
    method: str = Field(..., max_length=32)
    account: str = Field(..., max_length=200)
    bank: str = Field("", max_length=40)
    amount: float | None = Field(None, gt=0)


@router.post("/referrals/payout")
def payout_request(body: PayoutBody, tg: dict = Depends(tg_user), db: Session = Depends(get_db)):
    user = current_user(db, tg)
    _throttle(user)
    try:
        p = referral.request_payout(db, user, body.method, body.account, body.bank, body.amount)
    except billing.OrderError as e:
        order_error(user, e)
    lang = user.lang or env.default_lang
    return {"ok": True, "payout": referral.payout_out(p),
            "text": core.t(lang, "payout_sent", amount=core.money(p.amount, lang))}


@router.post("/referrals/payout/{payout_id}/cancel")
def payout_cancel(payout_id: int, tg: dict = Depends(tg_user), db: Session = Depends(get_db)):
    user = current_user(db, tg)
    if not referral.cancel_payout(db, user, payout_id):
        fail(user, "order_not_found", 404)
    return {"ok": True}


@router.post("/channel/check")
def channel_check(tg: dict = Depends(tg_user), db: Session = Depends(get_db)):
    """"I have subscribed" — checked with Telegram right now, not from cache."""
    user = current_user(db, tg)
    _throttle(user, "channel", 6)
    gate = channel.gate(db, user, "all", fresh=True)
    trial = channel.gate(db, user, "trial", fresh=True) if not gate else gate
    return {"ok": gate is None and trial is None, "gate": gate, "trial_gate": trial}


# ---------------------------------------------------------------------------
#  Devices and the link
# ---------------------------------------------------------------------------
@router.post("/devices/{device_id}/forget")
def forget(device_id: int, tg: dict = Depends(tg_user), db: Session = Depends(get_db)):
    user = current_user(db, tg)
    if not access.forget_device(db, user, device_id):
        fail(user, "device_not_found", 404)
    return {"ok": True, "devices": access.devices(db, user),
            "removed_devices": access.removed_devices(db, user)}


@router.post("/devices/{device_id}/restore")
def restore(device_id: int, tg: dict = Depends(tg_user), db: Session = Depends(get_db)):
    user = current_user(db, tg)
    err = access.restore_device(db, user, device_id)
    if err:
        fail(user, err, 404 if err == "device_not_found" else 400)
    return {"ok": True, "devices": access.devices(db, user),
            "removed_devices": access.removed_devices(db, user)}


_reissued: dict[int, float] = {}


@router.post("/reissue")
def reissue(tg: dict = Depends(tg_user), db: Session = Depends(get_db)):
    """New link and new keys — when the old link leaked."""
    user = current_user(db, tg)
    if not user.has_subscription:
        fail(user, "no_subscription")
    if time.time() - _reissued.get(user.tg_id, 0) < 60:
        fail(user, "too_many", 429)
    _reissued[user.tg_id] = time.time()
    res = access.reissue(db, user)
    if res.get("reverted"):
        # No server took the new key: the old link stays, and the person is
        # told so instead of getting a "new" link with the same keys.
        _reissued.pop(user.tg_id, None)
        core.audit(db, "Link reissue failed", f"tg:{user.tg_id}", "; ".join(res.get("errors") or [])[:300],
                   level="warn")
        fail(user, "reissue_failed", 503)
    core.audit(db, "Link reissued", f"tg:{user.tg_id}", f"servers ok: {res['ok']}, failed: {res['failed']}")
    return {"ok": True, "sub_url": f"{env.public_url}/sub/{user.sub_token}", "partial": bool(res["failed"])}


# ---------------------------------------------------------------------------
#  Gifts
# ---------------------------------------------------------------------------
class RedeemBody(BaseModel):
    code: str = Field(..., max_length=16)


@router.post("/gift/redeem")
def redeem(body: RedeemBody, tg: dict = Depends(tg_user), db: Session = Depends(get_db)):
    user = current_user(db, tg)
    _throttle(user, "code", 20)          # the same budget as the one code field
    try:
        plan = billing.redeem(db, user, body.code)
    except billing.OrderError as e:
        order_error(user, e)
    except access.AccessError:
        fail(user, "no_servers", 503)
    return {"ok": True, "plan": plan_out(plan)}


@router.get("/gifts")
def my_gifts(tg: dict = Depends(tg_user), db: Session = Depends(get_db)):
    user = current_user(db, tg)
    rows = (db.query(Gift).filter(Gift.buyer_id == user.id)
            .order_by(Gift.id.desc()).limit(20).all())
    return [{"code": g.code, "link": billing.gift_link(g.code),
             "plan": plan_out(g.plan) if g.plan else None,
             "used": bool(g.used_by_id), "created_at": g.created_at} for g in rows]


class CodeBody(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)


@router.post("/code")
def any_code(body: CodeBody, tg: dict = Depends(tg_user), db: Session = Depends(get_db)):
    """One field for every code a person may have: a gift code activates the
    subscription right away, a promo code is checked and handed back to be
    used on the plans screen (it is spent only by a paid order)."""
    user = current_user(db, tg)
    _throttle(user, "code", 20)          # codes are not to be guessed by brute force
    code = body.code.strip()
    gift_like = re.fullmatch(r"[A-Za-z0-9]{4}-?[A-Za-z0-9]{4}", code.replace(" ", ""))
    if gift_like:
        try:
            plan = billing.redeem(db, user, code)
            return {"ok": True, "kind": "gift", "plan": plan_out(plan)}
        except billing.OrderError as e:
            if e.args[0] != "gift_invalid":
                order_error(user, e)
        except access.AccessError:
            fail(user, "no_servers", 503)
    try:
        promo = billing.check_promo(db, user, code)
    except billing.OrderError as e:
        if e.args[0] == "promo_not_found":
            fail(user, "code_not_found", 404)
        order_error(user, e)
    return {"ok": True, "kind": "promo",
            "promo": {"code": promo.code, "kind": promo.kind, "value": promo.value}}


# ---------------------------------------------------------------------------
#  Reviews
# ---------------------------------------------------------------------------
class ReviewBody(BaseModel):
    stars: int = 5
    text: str = Field("", max_length=1000)


@router.get("/reviews")
def reviews(db: Session = Depends(get_db)):
    if core.get(db, "reviews_enabled") != "1":
        return {"average": 0, "count": 0, "items": []}
    return _reviews_summary(db)


def _reviews_summary(db: Session, limit: int = 50) -> dict:
    rows = (db.query(Review).filter(Review.status == "published")
            .order_by(Review.id.desc()).limit(limit).all())
    total = db.query(Review).filter(Review.status == "published").count()
    stars = db.query(func.avg(Review.stars)).filter(Review.status == "published").scalar()
    return {"average": round(float(stars), 1) if stars else 0, "count": total,
            "items": [{"name": (r.user.first_name or r.user.username or "—")[:32],
                       "stars": r.stars, "text": r.text, "at": r.created_at} for r in rows]}


def _review_bonus(db: Session, user: User) -> int:
    from .. import reviews as reviews_mod
    if core.get(db, "reviews_enabled") != "1":
        return 0
    return reviews_mod.bonus_for(db, user)


@router.post("/reviews")
def add_review(body: ReviewBody, tg: dict = Depends(tg_user), db: Session = Depends(get_db)):
    user = current_user(db, tg)
    if core.get(db, "reviews_enabled") != "1":
        fail(user, "reviews_off")
    if not user.expires_at:
        fail(user, "review_customers")          # only people who have used the service
    if len(body.text.strip()) < 3:
        fail(user, "review_short")
    recent = (db.query(Review).filter(Review.user_id == user.id,
                                      Review.created_at > now() - 86400).count())
    if recent >= 3:
        fail(user, "too_many", 429)
    db.add(Review(user_id=user.id, stars=max(1, min(5, int(body.stars))),
                  text=body.text.strip()[:1000]))
    db.commit()
    return {"ok": True, "status": "pending"}
