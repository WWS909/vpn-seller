"""API of the admin panel. Everything except /login requires a token."""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import threading

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, exists, func, or_
from sqlalchemy.orm import Session

from .. import access, auth, billing, clients, core, notify, payments
from ..config import settings as env
from ..db import get_db
from ..models import (GB, Broadcast, SupportMessage, Device, Gift, Log, Payout, Plan, Promo, Review,
                      Receipt, Server, Transaction, User, now)
from ..providers import KINDS, PanelError, drop_session, normalize_url, panel_for

router = APIRouter(prefix="/api/admin", tags=["admin"])
A = Depends(auth.admin_required)


# ================================================================ login
class LoginBody(BaseModel):
    login: str = Field(..., max_length=128)
    password: str = Field(..., max_length=256)


def _tell_admins(text: str) -> None:
    """A note to the admins about sign-ins. Sent aside, so neither the owner
    nor someone hammering the form waits for Telegram longer than a moment."""
    th = threading.Thread(target=notify.admins, args=(text,), daemon=True)
    th.start()
    th.join(timeout=0.5)


def _sign_in_notes(ip: str, ua: str) -> dict[str, str]:
    where = html.escape(ip or "?") + (f" ({html.escape(auth.device(ua))})" if auth.device(ua) else "")
    mins = auth.LOCK_SECONDS // 60
    return {
        "new": f"Вход в панель по паролю с нового адреса: {where}.\n"
               "Если это были не вы — смените пароль: на сервере <code>vpn-seller</code> → «Сменить токен, ID или пароль».\n\n"
               f"Panel sign-in with the password from a new address: {where}.\n"
               "If it was not you, change the password on the server: <code>vpn-seller</code> → Change token, IDs or password.",
        "ip": f"Кто-то подбирает пароль к панели: {auth.MAX_ATTEMPTS} неверных попыток подряд с {where}. "
              f"Этот адрес закрыт на {mins} минут.\n\n"
              f"Someone is guessing the panel password: {auth.MAX_ATTEMPTS} wrong tries in a row from {where}. "
              f"The address is shut for {mins} minutes.",
        "all": f"Много неверных паролей к панели с разных адресов ({auth.MAX_ALL} за {mins} минут). "
               f"Вход по паролю закрыт для всех на {mins} минут; из бота (/admin) войти можно.\n\n"
               f"Many wrong panel passwords from different addresses ({auth.MAX_ALL} in {mins} minutes). "
               f"Password sign-in is shut for everyone for {mins} minutes; signing in from the bot (/admin) still works.",
    }


@router.post("/login")
def login(body: LoginBody, request: Request, db: Session = Depends(get_db)):
    ip = auth.client_ip(request)
    ua = request.headers.get("user-agent", "")
    left = auth.locked(ip)
    if left:
        raise HTTPException(429, f"too many attempts, wait {left // 60 + 1} min")
    if not auth.check_credentials(body.login, body.password):
        shut = auth.failed(ip)
        core.audit(db, "Admin login failed", ip, body.login[:64], level="warn")
        if shut:                                   # told once, when the address (or everyone) is shut
            _tell_admins(_sign_in_notes(ip, ua)[shut])
        raise HTTPException(401, "wrong login or password")
    if len(env.secret_key) < 32:
        raise HTTPException(503, "SECRET_KEY is not set on the server")
    auth.succeeded(ip)
    # A new address: the password was used before, but never from here (the
    # log keeps half a year). The very first sign-in after install is not news.
    seen = (db.query(Log.actor).filter(Log.action == "Admin login").distinct().limit(500).all())
    known = {a for (a,) in seen}
    core.audit(db, "Admin login", ip, body.login[:64] + (f" · {auth.device(ua)}" if auth.device(ua) else ""))
    if known and ip[:64] not in known:
        _tell_admins(_sign_in_notes(ip, ua)["new"])
    return {"token": auth.create_token(body.login)}


@router.post("/tg-login")
def tg_login(request: Request, db: Session = Depends(get_db)):
    """The panel opened from the bot: Telegram's signature proves who it is,
    so an admin from ADMIN_TELEGRAM_IDS gets in without a password."""
    ip = auth.client_ip(request)
    if auth.locked(ip, password=False):
        raise HTTPException(429, "too many attempts")
    # A fresh signature only: an initData copied from somewhere old is refused.
    user = auth.verify_init_data(request.headers.get("x-init-data", ""), max_age=3600)
    from .. import team
    member = None if int(user["id"]) in env.admin_telegram_ids else team.member(db, int(user["id"]))
    if int(user["id"]) not in env.admin_telegram_ids and member is None:
        auth.failed(ip, password=False)
        core.audit(db, "Panel from Telegram refused", f"tg:{user['id']}", "not an admin", level="warn")
        raise HTTPException(403, "not an admin")
    if len(env.secret_key) < 32:
        raise HTTPException(503, "SECRET_KEY is not set on the server")
    name = "@" + user["username"] if user.get("username") else f"tg:{user['id']}"
    if member is not None:
        name = (member.name or name)[:40] + f" (tg:{user['id']})"
        core.audit(db, "Team member signed in", name, ip)
        return {"token": auth.create_token(name, team_tg=int(user["id"])), "name": name}
    core.audit(db, "Admin login via Telegram", name, ip)
    return {"token": auth.create_token(name), "name": name}


@router.get("/me")
def whoami(authorization: str = Header(default=""), db: Session = Depends(get_db), name=A):
    """Who is signed in and which sections they may open (the menu shows only those)."""
    from .. import team
    claims = auth.token_claims(authorization)
    if claims.get("team"):
        m = team.member(db, int(claims["team"]))
        return {"name": name, "owner": False, "sections": (m.sections or "").split(",") if m else []}
    return {"name": name, "owner": True, "sections": list(team.SECTIONS)}


# ================================================================ team (the owner only)
class TeamBody(BaseModel):
    tg_id: int = Field(..., ge=1)
    name: str = Field("", max_length=64)
    sections: list[str] = Field(default_factory=list)
    enabled: bool = True


@router.get("/team")
def team_list(db: Session = Depends(get_db), _=A):
    from .. import team
    from ..models import TeamMember
    return {"members": [team.out(m) for m in db.query(TeamMember).order_by(TeamMember.id).all()],
            "sections": list(team.SECTIONS), "owners": sorted(env.admin_telegram_ids)}


@router.post("/team")
def team_save(body: TeamBody, db: Session = Depends(get_db), admin=A):
    from .. import team
    from ..models import TeamMember
    if body.tg_id in env.admin_telegram_ids:
        raise HTTPException(400, "this account is an owner already (ADMIN_TELEGRAM_IDS)")
    m = db.query(TeamMember).filter(TeamMember.tg_id == body.tg_id).first() or TeamMember(tg_id=body.tg_id)
    m.name = body.name.strip()
    m.sections = team.clean_sections(body.sections)
    m.enabled = body.enabled
    db.add(m)
    db.commit()
    core.audit(db, "Team member saved", admin, f"tg:{m.tg_id} {m.name}: {m.sections or '—'}"
               + ("" if m.enabled else " (off)"))
    return team.out(m)


@router.delete("/team/{tg_id}")
def team_delete(tg_id: int, db: Session = Depends(get_db), admin=A):
    from ..models import TeamMember
    n = db.query(TeamMember).filter(TeamMember.tg_id == tg_id).delete()
    db.commit()
    if n:
        core.audit(db, "Team member removed", admin, f"tg:{tg_id}")
    return {"removed": n}


@router.post("/logout")
def logout(authorization: str = Header(default="")):
    """Sign out: the token of this browser stops working right away."""
    if authorization.startswith("Bearer "):
        auth.revoke(authorization[7:])
    return {"ok": True}


# ================================================================ overview
def _day(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


@router.get("/analytics")
def analytics(days: int = 30, db: Session = Depends(get_db), _=A):
    """The sales funnel of the people who came in the period, and where the
    money came from."""
    days = max(1, min(int(days), 3650))
    t = now()
    start = t - days * 86400
    paid = (Transaction.status == "success") & (Transaction.amount > 0)

    per_user = (db.query(Transaction.user_id, func.count(Transaction.id).label("n"),
                         func.min(Transaction.paid_at).label("first"))
                .filter(paid).group_by(Transaction.user_id).subquery())
    cohort = db.query(User).filter(User.created_at >= start)
    started = cohort.count()
    opened = cohort.filter(User.app_opened_at > 0).count()
    trial = cohort.filter(User.trial_used.is_(True)).count()
    connected = cohort.filter(User.connected_at > 0).count()
    paid_c = cohort.join(per_user, per_user.c.user_id == User.id).count()
    renewed = cohort.join(per_user, per_user.c.user_id == User.id).filter(per_user.c.n >= 2).count()
    trial_paid = (cohort.filter(User.trial_used.is_(True))
                  .join(per_user, per_user.c.user_id == User.id).count())
    referred = cohort.filter(User.referrer_id.isnot(None)).count()
    referred_paid = (cohort.filter(User.referrer_id.isnot(None))
                     .join(per_user, per_user.c.user_id == User.id).count())

    txs = db.query(Transaction).filter(paid, Transaction.paid_at >= start)
    by_gateway: dict[str, dict] = {}
    by_plan: dict[str, dict] = {}
    by_kind: dict[str, dict] = {}
    plans = {p.id: p.name for p in db.query(Plan).all()}
    revenue = 0.0
    payers = set()
    for tx in txs.all():
        revenue += tx.amount
        payers.add(tx.user_id)
        for store, key in ((by_gateway, tx.gateway or tx.method or "other"),
                           (by_kind, tx.kind),
                           (by_plan, plans.get(tx.plan_id, "—") if tx.kind in ("purchase", "gift") else None)):
            if key is None:
                continue
            row = store.setdefault(key, {"name": key, "amount": 0.0, "count": 0})
            row["amount"] += tx.amount
            row["count"] += 1
    new_payers = db.query(per_user).filter(per_user.c.first >= start).count()

    ever_paid = db.query(per_user.c.user_id)
    churned = (db.query(User).filter(User.id.in_(ever_paid), User.expires_at >= start,
                                     User.expires_at < t).count())
    active_paying = db.query(User).filter(User.id.in_(ever_paid), User.expires_at >= t).count()
    total_rev = float(db.query(func.coalesce(func.sum(Transaction.amount), 0)).filter(paid).scalar() or 0)
    total_payers = db.query(per_user).count()

    def rows(store):
        return sorted(({**r, "amount": round(r["amount"], 2)} for r in store.values()),
                      key=lambda r: -r["amount"])

    def pct(a, b):
        return round(100 * a / b, 1) if b else 0

    from .. import support
    return {
        "days": days,
        "funnel": [{"key": "started", "n": started}, {"key": "opened", "n": opened},
                   {"key": "trial", "n": trial}, {"key": "connected", "n": connected},
                   {"key": "paid", "n": paid_c}, {"key": "renewed", "n": renewed}],
        "conversion": {"to_paid": pct(paid_c, started), "trial_to_paid": pct(trial_paid, trial),
                       "repeat": pct(renewed, paid_c), "referred": referred,
                       "referred_paid": referred_paid},
        "revenue": round(revenue, 2), "orders": txs.count(), "payers": len(payers),
        "new_payers": new_payers, "arppu": round(revenue / len(payers), 2) if payers else 0,
        "ltv": round(total_rev / total_payers, 2) if total_payers else 0,
        "churn": {"ended": churned, "active": active_paying, "rate": pct(churned, churned + active_paying)},
        "support_waiting": support.waiting(db),
        "by_gateway": rows(by_gateway), "by_plan": rows(by_plan), "by_kind": rows(by_kind),
        "currency": env.currency, "symbol": env.currency_symbol,
    }


# ---------------------------------------------------------------------------
#  Source links: which video or blogger brings customers and money
# ---------------------------------------------------------------------------
class SourceBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    code: str = Field("", max_length=40)
    cost: float = Field(0, ge=0, le=100_000_000)


@router.get("/sources")
def sources_report(days: int = 0, db: Session = Depends(get_db), _=A):
    from .. import sources
    since = now() - max(1, min(int(days), 3650)) * 86400 if days else 0
    return {"items": sources.report(db, since), "bot": notify.username(), "prefix": sources.PREFIX,
            "currency": env.currency, "symbol": env.currency_symbol}


@router.post("/sources")
def create_source(body: SourceBody, db: Session = Depends(get_db), admin=A):
    from .. import sources
    try:
        s = sources.create(db, body.name, body.code, body.cost)
    except ValueError as e:
        raise HTTPException(400, str(e))
    core.audit(db, "Source link created", admin, f"{s.code} {s.name}")
    return {"id": s.id, "code": s.code, "name": s.name, "cost": s.cost, "link": sources.link(s.code)}


@router.put("/sources/{source_id}")
def update_source(source_id: int, body: SourceBody, db: Session = Depends(get_db), admin=A):
    from ..models import Source
    s = db.get(Source, source_id)
    if not s:
        raise HTTPException(404, "not found")
    s.name, s.cost = body.name.strip()[:80], body.cost   # the code stays: links are already out
    db.commit()
    return {"ok": True}


@router.delete("/sources/{source_id}")
def delete_source(source_id: int, db: Session = Depends(get_db), admin=A):
    """The label goes; people who came through it keep their code."""
    from ..models import Source
    s = db.get(Source, source_id)
    if not s:
        raise HTTPException(404, "not found")
    db.delete(s)
    db.commit()
    core.audit(db, "Source link deleted", admin, s.code)
    return {"ok": True}


# ---------------------------------------------------------------------------
#  Support chat
# ---------------------------------------------------------------------------
class SupportReply(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


def _support_user(db: Session, tg_id: int) -> User:
    user = db.query(User).filter(User.tg_id == tg_id).first()
    if not user:
        raise HTTPException(404, "user not found")
    return user


@router.get("/support")
def support_threads(db: Session = Depends(get_db), _=A):
    from .. import support, support_ai
    return {"enabled": support.enabled(db), "waiting": support.waiting(db),
            "threads": support.threads(db),
            "ai": {"configured": support_ai.configured(), "on": support_ai.enabled(db),
                   "model": env.ai_model if support_ai.configured() else "",
                   "name": core.get(db, "ai_name"), "prompt": core.get(db, "ai_prompt")}}


class AISettings(BaseModel):
    on: bool = False
    name: str = Field("", max_length=40)
    prompt: str = Field("", max_length=3000)


@router.post("/support/ai")
def support_ai_save(body: AISettings, db: Session = Depends(get_db), admin=A):
    from .. import support_ai
    if body.on and not support_ai.configured():
        raise HTTPException(400, "AI_API_KEY is not set on the server")
    core.put(db, "ai_enabled", "1" if body.on else "0")
    core.put(db, "ai_name", body.name.strip())
    core.put(db, "ai_prompt", body.prompt.strip())
    core.audit(db, "AI helper " + ("on" if body.on else "off"), admin, env.ai_model if body.on else "")
    return {"ok": True}


@router.post("/support/ai/check")
def support_ai_check(_=A):
    from .. import support_ai
    if not support_ai.configured():
        raise HTTPException(400, "AI_API_KEY is not set on the server")
    try:
        support_ai.check()
    except support_ai.AIError as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "model": env.ai_model}


@router.get("/support/{tg_id}")
def support_thread(tg_id: int, db: Session = Depends(get_db), _=A):
    from .. import support
    user = _support_user(db, tg_id)
    return {"user": {"tg_id": user.tg_id, "name": user.first_name, "username": user.username,
                     "expires_at": user.expires_at or 0, "bot_blocked": bool(user.bot_blocked)},
            "messages": support.history(db, user, 200)}


@router.post("/support/{tg_id}")
def support_reply(tg_id: int, body: SupportReply, db: Session = Depends(get_db), admin: str = A):
    from .. import support
    user = _support_user(db, tg_id)
    if not support.answer(db, user, text=body.text, source="panel"):
        raise HTTPException(409, "not delivered: the customer blocked the bot")
    core.audit(db, "Support answer", admin, f"tg:{tg_id}")
    return {"ok": True, "messages": support.history(db, user, 200)}


@router.post("/support/{tg_id}/close")
def support_close(tg_id: int, db: Session = Depends(get_db), _=A):
    from .. import support
    support.close(db, _support_user(db, tg_id))
    return {"ok": True}


@router.get("/overview")
def overview(db: Session = Depends(get_db), _=A):
    t = now()
    day0 = int(dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    month0 = t - 30 * 86400
    paid = (db.query(Transaction)
            .filter(Transaction.status == "success", Transaction.amount > 0))
    revenue_month = paid.filter(Transaction.paid_at >= month0).with_entities(
        func.coalesce(func.sum(Transaction.amount), 0)).scalar() or 0
    orders_month = paid.filter(Transaction.paid_at >= month0).count()
    revenue_today = paid.filter(Transaction.paid_at >= day0).with_entities(
        func.coalesce(func.sum(Transaction.amount), 0)).scalar() or 0

    series = {}
    for tx in paid.filter(Transaction.paid_at >= month0).all():
        key = _day(tx.paid_at)
        series[key] = series.get(key, 0) + tx.amount
    days = [_day(t - i * 86400) for i in range(29, -1, -1)]

    by_plan = (db.query(Plan.name, func.count(User.id))
               .join(User, User.plan_id == Plan.id)
               .filter(User.expires_at > t)
               .group_by(Plan.name).all())

    # The 30 days before, to show which way things go.
    prev0 = month0 - 30 * 86400
    in_prev = paid.filter(Transaction.paid_at >= prev0, Transaction.paid_at < month0)
    revenue_prev = in_prev.with_entities(func.coalesce(func.sum(Transaction.amount), 0)).scalar() or 0
    orders_prev = in_prev.count()
    new_month = db.query(User).filter(User.created_at >= month0).count()
    new_prev = db.query(User).filter(User.created_at >= prev0, User.created_at < month0).count()
    # Of those who took the trial, how many paid afterwards.
    payers = db.query(Transaction.user_id).filter(Transaction.status == "success", Transaction.amount > 0)
    tried = db.query(User).filter(User.trial_used.is_(True)).count()
    tried_paid = db.query(User).filter(User.trial_used.is_(True), User.id.in_(payers)).count()
    # Churn: subscriptions that ended in these 30 days and were not renewed,
    # against everyone who had one at their start.
    ended = db.query(User).filter(User.expires_at > month0, User.expires_at <= t).count()
    had = db.query(User).filter(User.expires_at > month0).count()
    recent = (db.query(Transaction, User).join(User, User.id == Transaction.user_id)
              .filter(Transaction.status == "success", Transaction.amount > 0)
              .order_by(Transaction.paid_at.desc()).limit(6).all())
    names = {p.id: p.name for p in db.query(Plan).all()}
    return {
        "revenue_prev": round(float(revenue_prev), 2),
        "avg_prev": round(float(revenue_prev) / orders_prev, 2) if orders_prev else 0,
        "new_month": new_month, "new_prev": new_prev,
        "trial_conv": round(tried_paid / tried * 100, 1) if tried else None,
        "churn": round(ended / had * 100, 1) if had else None,
        "recent": [{"id": tx.id, "tg_id": u.tg_id,
                    "who": ("@" + u.username) if u.username else (u.first_name or str(u.tg_id)),
                    "kind": tx.kind, "plan": names.get(tx.plan_id, ""), "devices": tx.devices or 0,
                    "qty": tx.qty or 0, "gb": tx.gb or tx.bypass_gb or 0,
                    "gateway": tx.gateway or tx.method or "", "amount": round(float(tx.amount), 2),
                    "at": tx.paid_at or 0} for tx, u in recent],
        "users": db.query(User).count(),
        "active": db.query(User).filter(User.expires_at > t, User.is_blocked.is_(False)).count(),
        "new_today": db.query(User).filter(User.created_at >= day0).count(),
        "revenue_month": round(float(revenue_month), 2),
        "revenue_today": round(float(revenue_today), 2),
        "orders_month": orders_month,
        "avg_check": round(float(revenue_month) / orders_month, 2) if orders_month else 0,
        "traffic_bytes": int(db.query(func.coalesce(func.sum(User.traffic_used), 0)).scalar() or 0),
        "stuck_orders": db.query(Transaction).filter(Transaction.status == "paid").count(),
        "receipts_attention": db.query(Receipt).filter(Receipt.status.in_(("failed", "uncertain"))).count(),
        "payouts_waiting": db.query(Payout).filter(Payout.status == "pending").count(),
        "support_waiting": db.query(User).filter(User.support_at > User.support_answered_at).count(),
        "sync_errors": db.query(User).filter(User.sync_error != "",
                                             User.expires_at > t).count(),
        "revenue_days": [{"day": d, "amount": round(series.get(d, 0), 2)} for d in days],
        "plans": [{"name": n, "count": c} for n, c in by_plan],
        "servers": [_server_out(s) for s in access.servers(db, include_disabled=True)],
        "problems": env.problems() + _problems(db),
        "currency": env.currency, "symbol": env.currency_symbol,
        "setup": _setup_steps(db),
        "setup_hidden": core.get(db, "setup_hidden") == "1",
        "bot": notify.username(),
    }


_hook_cache: dict = {"at": 0.0, "info": {}}


def _webhook_info() -> dict:
    """What Telegram itself says about our webhook (asked at most once a minute)."""
    import time as _time
    if _time.time() - _hook_cache["at"] > 60:
        res = notify.call("getWebhookInfo", timeout=6)
        _hook_cache.update(at=_time.time(),
                           info=res.get("result") if isinstance(res.get("result"), dict) else {})
    return _hook_cache["info"]


def _setup_steps(db: Session) -> list[dict]:
    """
    The launch checklist: what the shop needs before it can sell
    ("required") and what makes it better ("recommended"), in order.
    Every tick comes from a fact, never from "the page was opened":
    a panel answered, a plan exists, a payment method is configured, the
    documents are written. (The bot's webhook is checked by the installer
    and "vpn-seller status"; a broken one shows as a problem on the overview.)
    """
    from .. import legal, referral
    from ..models import Campaign, Setting
    plans = db.query(Plan).filter(Plan.enabled.is_(True), Plan.is_trial.is_(False), Plan.price > 0).count()
    methods = [m["gateway"] for m in payments.methods()]
    faq = core.get_json(db, "faq_json", [])
    ref_on = referral.enabled(db)
    ref_gap = ref_on and referral.mode(db) == "money" and not referral.payout_methods(db)
    warm_touched = (db.query(Setting).filter(Setting.key.like("warm\\_%", escape="\\"),
                                             Setting.value != "").first() is not None
                    or db.query(Campaign).first() is not None
                    or core.get_int(db, "sale_percent", 0) > 0)
    tunnel = "trycloudflare.com" in env.public_url

    def step(id_: str, group: str, done: bool, note: str = "") -> dict:
        return {"id": id_, "group": group, "done": bool(done), "note": note}

    return [
        step("server", "required", any(s.last_ok_at and not s.last_error for s in access.servers(db))),
        step("plans", "required", plans, str(plans) if plans else ""),
        step("pay", "required", methods, ", ".join(methods)),
        step("docs", "required", legal.complete(db)),
        step("brand", "recommended", core.get(db, "brand_name") != core.DEFAULTS["brand_name"]),
        step("logo", "recommended", core.get(db, "logo")),
        step("trial", "recommended", billing.trial_info(db)["enabled"]),
        step("contacts", "recommended", core.get(db, "legal_owner").strip()
             and (core.get(db, "legal_contact").strip() or core.get(db, "support_url").strip())),
        step("bot_texts", "recommended", core.get(db, "bot_about_ru") or core.get(db, "bot_about_en")),
        step("faq", "recommended", isinstance(faq, list) and len(faq) > 0),
        step("referral", "recommended", ref_on and not ref_gap, "methods" if ref_gap else ""),
        step("warm", "recommended", warm_touched),
        step("domain", "recommended", env.https and not tunnel, "tunnel" if tunnel else ""),
        step("backup", "recommended", core.get(db, "backup_enabled") == "1"),
    ]


@router.get("/setup")
def setup(db: Session = Depends(get_db), _=A):
    return {"steps": _setup_steps(db), "hidden": core.get(db, "setup_hidden") == "1",
            "bot": notify.username()}


def _problems(db: Session) -> list[dict]:
    out = []
    if not access.servers(db):
        out.append({"code": "no_servers",
                    "text": "No enabled servers — nothing to sell. Add a 3x-ui server."})
    if not payments.methods():
        out.append({"code": "no_methods", "text": "No payment method is configured."})
    if env.bot_token:
        info = _webhook_info()
        want = f"{env.public_url}/api/bot/webhook"
        if info and info.get("url") != want:
            out.append({"code": "bot_hook", "text": "The bot is not connected to the shop: Telegram sends its "
                        "messages elsewhere. On the server: vpn-seller → Status."})
        elif info and info.get("last_error_message") and now() - int(info.get("last_error_date") or 0) < 3600:
            out.append({"code": "bot_hook_err", "arg": str(info["last_error_message"])[:120],
                        "text": f"Telegram cannot deliver messages to the shop: {info['last_error_message']}"})
    if env.card_gateway and env.card_gateway != "off" and not payments.card_gateway():
        out.append({"code": "card_keys",
                    "text": f"CARD_GATEWAY={env.card_gateway}, but its keys are not set.",
                    "arg": env.card_gateway})
    from .. import emergency
    if emergency.is_on(db):
        out.append({"code": "emergency", "text": "Emergency mode is on."})
    if env.crypto_gateway and env.crypto_gateway != "off" and not payments.crypto_gateway():
        out.append({"code": "crypto_keys",
                    "text": f"CRYPTO_GATEWAY={env.crypto_gateway}, but its keys are not set.",
                    "arg": env.crypto_gateway})
    return out


# ================================================================ users
def _user_row(u: User, db: Session | None = None) -> dict:
    return {"tg_id": u.tg_id, "username": u.username, "first_name": u.first_name,
            "lang": u.lang, "expires_at": u.expires_at or 0, "active": u.is_active,
            "blocked": bool(u.is_blocked), "traffic_blocked": bool(u.traffic_blocked),
            "plan": u.plan.name if u.plan else "", "device_limit": u.device_limit or 0,
            "traffic_used": u.traffic_used or 0, "traffic_limit": u.traffic_limit or 0,
            "bypass_used": u.bypass_used or 0, "bypass_limit": u.bypass_limit or 0,
            "created_at": u.created_at, "sync_error": u.sync_error or "",
            "bot_blocked": bool(u.bot_blocked), "grace_until": access.grace_until(db, u) if db else 0}


@router.get("/users")
def users(q: str = "", status: str = "", limit: int = 50, offset: int = 0,
          db: Session = Depends(get_db), _=A):
    t = now()
    query = db.query(User)
    if q:
        term = q.strip().lstrip("@")
        conds = [User.username.ilike(f"%{term}%"), User.first_name.ilike(f"%{term}%")]
        if term.isdigit():
            conds.append(User.tg_id == int(term))
        query = query.filter(or_(*conds))
    if status == "active":
        query = query.filter(User.expires_at > t, User.is_blocked.is_(False))
    elif status == "expired":
        query = query.filter(User.expires_at > 0, User.expires_at <= t)
    elif status == "never":
        query = query.filter(User.expires_at == 0)
    elif status == "blocked":
        query = query.filter(User.is_blocked.is_(True))
    elif status == "errors":
        query = query.filter(User.sync_error != "")
    total = query.count()
    rows = query.order_by(User.id.desc()).offset(max(0, offset)).limit(min(200, limit)).all()
    return {"total": total, "items": [_user_row(u, db) for u in rows]}


def _get_user(db: Session, tg_id: int) -> User:
    user = db.query(User).filter(User.tg_id == tg_id).first()
    if not user:
        raise HTTPException(404, "user not found")
    return user


@router.get("/users/{tg_id}")
def user_card(tg_id: int, db: Session = Depends(get_db), _=A):
    user = _get_user(db, tg_id)
    orders = (db.query(Transaction).filter(Transaction.user_id == user.id,
                                           Transaction.status.notin_(UNPAID))
              .order_by(Transaction.id.desc()).limit(30).all())
    return {**_user_row(user, db),
            "bypass_servers": len(access.bypass_servers(db)),
            "sub_url": f"{env.public_url}/sub/{user.sub_token}",
            "email": access.email_for(user),
            "devices": access.devices(db, user),
            "orders": [_tx_out(t) for t in orders],
            "spent": round(sum(t.amount for t in orders if t.status == "success"), 2),
            "synced_at": user.synced_at or 0,
            "support": db.query(SupportMessage).filter(SupportMessage.user_id == user.id).count(),
            "connected_at": user.connected_at or 0,
            "source": user.source or "",
            "blacklisted": _bl_out(db, user)}


def _bl_out(db: Session, user: User) -> dict | None:
    from .. import blacklist
    e = blacklist.entry(db, user.tg_id)
    return {"source": e.source, "reason": e.reason, "action": blacklist.action(db)} if e else None


class UserAction(BaseModel):
    action: str
    days: int = 0
    gb: int = 0
    devices: int = 0
    plan_id: int | None = None
    text: str = Field("", max_length=4000)


@router.post("/users/{tg_id}/action")
def user_action(tg_id: int, body: UserAction, db: Session = Depends(get_db), admin=A):
    user = _get_user(db, tg_id)
    a = body.action
    result: dict = {"ok": True}
    if a == "extend":
        if not body.days:
            raise HTTPException(400, "days required")
        base = max(now(), user.expires_at or 0) if body.days > 0 else (user.expires_at or now())
        user.expires_at = max(0, base + body.days * 86400)
        if body.plan_id:
            user.plan_id = body.plan_id
        if body.devices:
            user.device_limit = body.devices
        db.commit()
        result["sync"] = access.push(db, user)
    elif a == "block":
        user.is_blocked = True
        db.commit()
        result["sync"] = access.push(db, user)
    elif a == "unblock":
        user.is_blocked = False
        db.commit()
        result["sync"] = access.push(db, user)
    elif a == "devices":
        user.device_limit = max(0, body.devices)
        db.commit()
        result["sync"] = access.push(db, user)
    elif a == "traffic":
        user.traffic_limit = max(0, body.gb) * GB
        user.traffic_extra = 0            # the admin sets the whole number
        user.traffic_blocked = bool(user.traffic_limit and user.traffic_used >= user.traffic_limit)
        db.commit()
        result["sync"] = access.push(db, user)
    elif a == "reset_traffic":
        user.traffic_used = 0
        user.traffic_blocked = False
        # the panels' counters go to zero on the bypass servers too: what is left stays
        user.bypass_limit = access.bypass_left(user)
        user.bypass_used = 0
        db.commit()
        result["sync"] = access.push(db, user, reset_traffic=True)
    elif a == "bypass":
        # The admin sets how many bypass gigabytes are left from now on.
        user.bypass_limit = int(user.bypass_used or 0) + max(0, body.gb) * GB if body.gb > 0 else 0
        db.commit()
        result["sync"] = access.push(db, user)
    elif a == "sync":
        result["sync"] = access.push(db, user)
    elif a == "reissue":
        result["sync"] = access.reissue(db, user)
        if result["sync"].get("reverted"):
            raise HTTPException(503, "no server took the new key — the old link stays: "
                                + "; ".join(result["sync"].get("errors") or [])[:300])
    elif a == "clear_devices":
        db.query(Device).filter(Device.user_id == user.id).delete()
        db.commit()
    elif a == "message":
        if not body.text.strip():
            raise HTTPException(400, "empty message")
        try:
            result["delivered"] = notify.send(user.tg_id, body.text.strip())
        except notify.Blocked:
            user.bot_blocked = True
            db.commit()
            result["delivered"] = False
    else:
        raise HTTPException(400, "unknown action")
    core.audit(db, f"Admin: {a}", admin, f"tg:{tg_id} {body.days or ''}{body.gb or ''}".strip())
    return result


# ================================================================ plans
class PlanBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    name_en: str = Field("", max_length=64)
    days: int = Field(30, ge=1, le=3650)
    price: float = Field(0, ge=0)
    old_price: float = Field(0, ge=0)
    badge: str = Field("", max_length=24)
    devices: int = Field(3, ge=0, le=100)
    traffic_gb: int = Field(0, ge=0)
    bypass_gb: int = Field(0, ge=0, le=100000)      # whitelist-bypass GB included
    is_trial: bool = False
    enabled: bool = True
    sort: int = 0


def _plan_out(p: Plan) -> dict:
    return {c: getattr(p, c) for c in ("id", "name", "name_en", "days", "price", "old_price",
                                       "badge", "devices", "traffic_gb", "bypass_gb", "is_trial",
                                       "enabled", "sort")}


@router.get("/plans")
def plans(db: Session = Depends(get_db), _=A):
    """Paid plans. The trial has its own card: /trial."""
    return [_plan_out(p) for p in db.query(Plan).filter(Plan.is_trial.is_(False))
            .order_by(Plan.sort, Plan.id).all()]


class TrialBody(BaseModel):
    enabled: bool = True
    days: int = Field(3, ge=1, le=90)
    devices: int = Field(1, ge=0, le=100)
    traffic_gb: int = Field(0, ge=0, le=100000)


@router.get("/trial")
def get_trial(db: Session = Depends(get_db), _=A):
    info = billing.trial_info(db)
    t = now()
    info["used"] = db.query(User).filter(User.trial_used.is_(True)).count()
    info["used_week"] = (db.query(Transaction)
                         .join(Plan, Plan.id == Transaction.plan_id)
                         .filter(Plan.is_trial.is_(True), Transaction.status == "success",
                                 Transaction.created_at >= t - 7 * 86400).count())
    return info


@router.put("/trial")
def put_trial(body: TrialBody, db: Session = Depends(get_db), admin=A):
    info = billing.set_trial(db, **body.model_dump())
    core.audit(db, "Trial changed", admin,
               f"{'on' if info['enabled'] else 'off'}, {info['days']} d, {info['devices']} dev, "
               f"{info['traffic_gb'] or '∞'} GB")
    return info


@router.post("/plans")
def create_plan(body: PlanBody, db: Session = Depends(get_db), admin=A):
    # The free trial is not made here — it has its own setting (/trial).
    plan = Plan(**{**body.model_dump(), "is_trial": False})
    db.add(plan)
    db.commit()
    core.audit(db, "Plan created", admin, plan.name)
    return _plan_out(plan)


@router.put("/plans/{plan_id}")
def update_plan(plan_id: int, body: PlanBody, db: Session = Depends(get_db), admin=A):
    plan = db.get(Plan, plan_id)
    if not plan:
        raise HTTPException(404, "plan not found")
    for k, v in body.model_dump().items():
        if k != "is_trial":
            setattr(plan, k, v)
    db.commit()
    core.audit(db, "Plan updated", admin, plan.name)
    return _plan_out(plan)


@router.delete("/plans/{plan_id}")
def delete_plan(plan_id: int, db: Session = Depends(get_db), admin=A):
    plan = db.get(Plan, plan_id)
    if not plan:
        raise HTTPException(404, "plan not found")
    # Keep history intact: a plan somebody bought is hidden, not deleted.
    if db.query(Transaction).filter(Transaction.plan_id == plan_id).first():
        plan.enabled = False
        db.commit()
        core.audit(db, "Plan hidden", admin, plan.name)
        return {"ok": True, "hidden": True}
    db.delete(plan)
    db.commit()
    core.audit(db, "Plan deleted", admin, plan.name)
    return {"ok": True, "hidden": False}


# ================================================================ servers
class ServerBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    country: str = Field("", max_length=2)
    url: str = Field(..., min_length=3, max_length=255)
    username: str = Field("", max_length=128)
    password: str | None = Field(None, max_length=255)     # None = keep the stored one
    api_token: str | None = Field(None, max_length=1024)
    inbound_ids: str = Field("", max_length=1024)
    tg_inbounds: str = Field("", max_length=1024)       # the "Telegram only" inbounds / squads
    sub_url: str = Field("", max_length=255)
    verify_tls: bool = True
    enabled: bool = True
    sort: int = 0
    kind: str = "3xui"                  # 3xui | marzban | remnawave
    capacity: int = Field(0, ge=0, le=100000)         # people online the server is meant for, 0 = default
    bypass: bool = False                # gets through whitelists; its traffic is sold by the GB


def _server_out(s: Server) -> dict:
    return {"id": s.id, "name": s.name, "country": s.country, "url": s.url,
            "username": s.username, "has_password": bool(s.password),
            "has_token": bool(s.api_token), "inbound_ids": s.inbound_ids,
            "sub_url": s.sub_url, "verify_tls": s.verify_tls, "enabled": s.enabled,
            "sort": s.sort, "api_version": s.api_version, "last_ok_at": s.last_ok_at,
            "last_error": s.last_error, "clients_count": s.clients_count, "kind": s.kind or "3xui",
            "capacity": s.capacity or 0, "capacity_default": access.DEFAULT_CAPACITY,
            "online": s.online if s.online is not None else -1, "online_at": s.online_at or 0,
            "load": access.load_of(s),
            "endpoints": s.endpoints or "", "vpn_down_at": s.vpn_down_at or 0, "vpn_ok_at": s.vpn_ok_at or 0,
            "vpn_fails": s.vpn_fails or 0, "tg_inbounds": s.tg_inbounds or "", "bypass": bool(s.bypass)}


def _apply_server(s: Server, body: ServerBody) -> None:
    if body.kind not in KINDS:
        raise PanelError("unknown panel type")
    s.kind = body.kind
    s.name = body.name.strip()
    s.country = body.country.strip().upper()
    s.url = normalize_url(body.url, body.kind)
    s.username = body.username.strip()
    if body.password is not None:
        s.password = body.password
    if body.api_token is not None:
        s.api_token = body.api_token.strip()
    def clean(raw: str) -> str:
        if body.kind in ("marzban", "remnawave"):   # inbound tags / internal squad uuids
            return ",".join(t.strip() for t in raw.split(",") if t.strip())
        return ",".join(str(i) for i in [int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()])
    s.inbound_ids = clean(body.inbound_ids)
    s.tg_inbounds = clean(body.tg_inbounds)
    if set(s.tg_inbounds.split(",")) & set(s.inbound_ids.split(",")) - {""}:
        # The Telegram-only client must never land on a full-VPN inbound.
        raise PanelError("the Telegram-only inbounds must be other than the customers' inbounds")
    s.sub_url = body.sub_url.strip()
    s.verify_tls = body.verify_tls
    s.enabled = body.enabled
    s.sort = body.sort
    s.capacity = body.capacity
    s.bypass = body.bypass
    s.api_version = ""          # re-detect with the new settings


def _check_tg_inbounds(s: Server) -> None:
    """The "Telegram only" inbounds must exist on the panel: a wrong number
    (an example copied from the form) would quietly leave people without
    the Telegram fallback. Checked only when the panel answers."""
    want = [x for x in (s.tg_inbounds or "").split(",") if x]
    if not want:
        return
    from types import SimpleNamespace
    snap = SimpleNamespace(id=-(s.id or 1) - 2000, url=s.url, username=s.username, password=s.password,
                           api_token=s.api_token, inbound_ids=s.inbound_ids, sub_url=s.sub_url,
                           verify_tls=s.verify_tls, api_version="", kind=s.kind)
    try:
        have = {str(i.get("id")) for i in panel_for(snap, timeout=8).test().get("inbounds", [])}
    except PanelError:
        return
    finally:
        drop_session(snap.id)
    missing = [x for x in want if x not in have]
    if missing:
        raise HTTPException(400, "«Только Telegram»: в панели нет " + ", ".join(missing)
                            + " — исправьте или оставьте поле пустым. / “Telegram only”: the panel has no "
                            + ", ".join(missing) + " — fix it or leave the field empty.")


@router.get("/servers")
def list_servers(db: Session = Depends(get_db), _=A):
    return [_server_out(s) for s in access.servers(db, include_disabled=True)]


@router.post("/servers/test")
def test_server(body: ServerBody, server_id: int | None = None,
                db: Session = Depends(get_db), _=A):
    """Try the connection with the entered settings BEFORE saving them."""
    from types import SimpleNamespace
    stored = db.get(Server, server_id) if server_id else None
    try:
        snap = SimpleNamespace(
            id=-(server_id or 1) - 1000, url=normalize_url(body.url, body.kind), username=body.username,
            password=body.password if body.password is not None else (stored.password if stored else ""),
            api_token=body.api_token if body.api_token is not None else (stored.api_token if stored else ""),
            inbound_ids=body.inbound_ids, sub_url=body.sub_url, verify_tls=body.verify_tls,
            api_version="", kind=body.kind)
        res = panel_for(snap, timeout=12).test()
        drop_session(snap.id)
    except PanelError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, **res}


def _refuse_same_panel(db: Session, s: Server) -> None:
    """One panel, one server. The panel keeps one account per customer:
    two server rows would overwrite each other's settings on it and count
    its traffic twice. Locations inside one panel (Remnawave or Marzban
    nodes, several 3x-ui inbounds) are chosen within the one server."""
    key = access.panel_key(s)
    for other in db.query(Server).filter(Server.id != (s.id or 0)).all():
        if access.panel_key(other) == key:
            raise HTTPException(409, f"Эта панель уже добавлена как «{other.name}». Выберите нужные "
                                     f"входы/сквады в нём — одна панель, один сервер. / This panel is "
                                     f"already added as “{other.name}”: pick its inbounds there.")


@router.post("/servers")
def create_server(body: ServerBody, db: Session = Depends(get_db), admin=A):
    s = Server()
    try:
        _apply_server(s, body)
    except PanelError as e:
        raise HTTPException(400, str(e))
    _refuse_same_panel(db, s)
    _check_tg_inbounds(s)
    db.add(s)
    db.commit()
    core.audit(db, "Server added", admin, f"{s.name} {s.url}")
    return _server_out(s)


@router.put("/servers/{server_id}")
def update_server(server_id: int, body: ServerBody, db: Session = Depends(get_db), admin=A):
    s = db.get(Server, server_id)
    if not s:
        raise HTTPException(404, "server not found")
    try:
        _apply_server(s, body)
    except PanelError as e:
        raise HTTPException(400, str(e))
    try:
        _refuse_same_panel(db, s)
        _check_tg_inbounds(s)
    except HTTPException:
        db.rollback()
        raise
    db.commit()
    drop_session(s.id)
    core.audit(db, "Server updated", admin, s.name)
    return _server_out(s)


@router.delete("/servers/{server_id}")
def delete_server(server_id: int, db: Session = Depends(get_db), admin=A):
    s = db.get(Server, server_id)
    if not s:
        raise HTTPException(404, "server not found")
    db.delete(s)
    db.commit()
    drop_session(server_id)
    core.audit(db, "Server deleted", admin, s.name)
    return {"ok": True}


@router.post("/servers/{server_id}/check")
def check_server(server_id: int, db: Session = Depends(get_db), _=A):
    s = db.get(Server, server_id)
    if not s:
        raise HTTPException(404, "server not found")
    try:
        res = panel_for(s, timeout=12).test()
        s.last_ok_at, s.last_error = now(), ""
        wanted = set(s.inbounds)
        s.clients_count = res["clients"] if "clients" in res else \
            sum(i["clients"] for i in res["inbounds"] if i["id"] in wanted)
        db.commit()
        access.refresh_load(db, [s])
        from .. import vpncheck
        vpn = None
        if s.endpoints:
            vpn = any(vpncheck.reachable(e) for e in s.endpoints.split(",") if e)
        return {"ok": True, **res, "online": s.online, "vpn": vpn}
    except PanelError as e:
        s.last_error = str(e)[:250]
        db.commit()
        return {"ok": False, "error": str(e)}


_server_sync: dict[int, dict] = {}


def _sync_worker(server_id: int, name: str, admin: str) -> None:
    from ..db import SessionLocal
    state = _server_sync[server_id]
    db = SessionLocal()
    try:
        users = [u.id for u in db.query(User).filter(User.expires_at > access.sync_floor(db))
                 .order_by(User.id).all()]
        state["total"] = len(users)
        for uid in users:
            user = db.get(User, uid)
            if user is not None:
                res = access.push(db, user, only=[server_id])
                state["errors"] += 1 if res["failed"] else 0
            state["done"] += 1
        core.audit(db, "Server synced", admin,
                   f"{name}: {state['done']} customers, {state['errors']} with errors")
    except Exception as e:  # noqa: BLE001
        state["error"] = str(e)[:200]
        core.log.exception("server sync failed")
    finally:
        state["running"] = False
        db.close()


@router.post("/servers/{server_id}/sync")
def sync_server(server_id: int, db: Session = Depends(get_db), admin=A):
    """Push every current customer to this server — after adding it. Runs
    in the background (thousands of customers take minutes); progress is
    read with GET."""
    import threading
    s = db.get(Server, server_id)
    if not s:
        raise HTTPException(404, "server not found")
    state = _server_sync.get(server_id)
    if state and state["running"]:
        return state
    state = {"running": True, "done": 0, "total": 0, "errors": 0, "error": ""}
    _server_sync[server_id] = state
    threading.Thread(target=_sync_worker, args=(server_id, s.name, admin), daemon=True).start()
    return state


@router.get("/servers/{server_id}/sync")
def sync_progress(server_id: int, _=A):
    return _server_sync.get(server_id) or {"running": False, "done": 0, "total": 0,
                                           "errors": 0, "error": ""}


# ================================================================ orders
def _tx_out(t: Transaction) -> dict:
    return {"id": t.id, "tg_id": t.user.tg_id if t.user else None,
            "username": t.user.username if t.user else "", "kind": t.kind,
            "plan_id": t.plan_id, "amount": t.amount, "currency": t.currency,
            "method": t.method, "gateway": t.gateway, "status": t.status,
            "external_id": t.external_id, "error": t.error, "created_at": t.created_at,
            "paid_at": t.paid_at, "devices": t.devices, "qty": t.qty,
            "gb": t.gb or (t.bypass_gb if t.kind == "bypass" else 0)}


# ================================================================ payment gateways
class PayEnvBody(BaseModel):
    values: dict[str, str | None] = {}


@router.get("/payments")
def payments_view(db: Session = Depends(get_db), _=A):
    from .. import paycfg
    return paycfg.view(db)


@router.post("/payments")
def payments_save(body: PayEnvBody, db: Session = Depends(get_db), admin=A):
    from .. import paycfg
    try:
        changed = paycfg.save(db, body.values)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if changed:
        # names only: the values are secrets
        core.audit(db, "Payment settings changed", admin, ", ".join(sorted(changed))[:250], level="warn")
    return paycfg.view(db)


# ================================================================ updates (the owner only)
class AutoUpdateBody(BaseModel):
    auto: bool = False
    hour: int = Field(1, ge=0, le=23)


@router.get("/updates")
def updates_view(db: Session = Depends(get_db), _=A):
    from .. import updates
    return updates.view(db)


@router.post("/updates/check")
def updates_check(db: Session = Depends(get_db), _=A):
    from .. import updates
    return updates.check(db)


@router.post("/updates/run")
def updates_run(db: Session = Depends(get_db), admin=A):
    from .. import updates
    try:
        return updates.request(db, admin)
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/updates/auto")
def updates_auto(body: AutoUpdateBody, db: Session = Depends(get_db), admin=A):
    from .. import updates
    core.put(db, "auto_update", "1" if body.auto else "0")
    core.put(db, "auto_update_hour", str(body.hour))
    core.audit(db, "Auto update " + ("on" if body.auto else "off"), admin, f"{body.hour}:00")
    return updates.view(db)


UNPAID = ("pending", "failed", "cancelled")


@router.get("/orders")
def orders(status: str = "", limit: int = 100, offset: int = 0,
           db: Session = Depends(get_db), _=A):
    # By default only what was paid: a tap on a plan makes an order too, and
    # those unpaid ones are kept apart ("unpaid") so they do not look like sales.
    q = db.query(Transaction)
    if status == "unpaid":
        q = q.filter(Transaction.status.in_(UNPAID))
    elif status == "all":
        pass
    elif status:
        q = q.filter(Transaction.status == status)
    else:
        q = q.filter(Transaction.status.notin_(UNPAID))
    total = q.count()
    rows = q.order_by(Transaction.id.desc()).offset(offset).limit(min(300, limit)).all()
    return {"total": total, "items": [_tx_out(t) for t in rows]}


@router.post("/orders/{order_id}/grant")
def grant_order(order_id: int, db: Session = Depends(get_db), admin=A):
    """Give what an order contains — e.g. money arrived but the notification
    did not. The admin takes responsibility; it is written to the log."""
    tx = db.get(Transaction, order_id)
    if not tx:
        raise HTTPException(404, "order not found")
    if tx.status == "success":
        return {"ok": True, "already": True}
    if tx.status == "processing":
        raise HTTPException(409, "the order is being processed right now")
    if tx.status == "cancelled":
        # The money came after all (a late payment on a cancelled order).
        tx.status = "pending"
        db.commit()
    try:
        done = billing.finish(db, tx, confirmed=True, manual=True)
    except access.AccessError as e:
        raise HTTPException(503, f"access not granted: {e}")
    if not done:
        raise HTTPException(409, "the order was closed by someone else meanwhile")
    core.audit(db, "Admin granted an order", admin, f"#{tx.id}", level="warn")
    return {"ok": True}


# ================================================================ receipts («Мой налог»)
@router.get("/receipts")
def receipts(status: str = "", limit: int = 200, db: Session = Depends(get_db), _=A):
    from .. import nalog
    q = db.query(Receipt)
    if status:
        q = q.filter(Receipt.status == status)
    rows = q.order_by(Receipt.id.desc()).limit(max(1, min(limit, 500))).all()
    counts = dict(db.query(Receipt.status, func.count(Receipt.id)).group_by(Receipt.status).all())
    return {"on": nalog.configured(), "paused": nalog.paused(db), "gateways": list(env.nalog_gateways),
            "counts": counts, "items": [nalog.out(r) for r in rows]}


class ReceiptDecision(BaseModel):
    status: str                    # sent | skipped | pending
    uuid: str = Field("", max_length=64)


def _receipt(db: Session, rid: int):
    r = db.get(Receipt, rid)
    if not r:
        raise HTTPException(404, "receipt not found")
    return r


@router.post("/receipts/{rid}/retry")
def receipt_retry(rid: int, db: Session = Depends(get_db), admin=A):
    """Send again a receipt FNS refused for sure. Never for an uncertain
    one: that could make a second receipt — see /resolve."""
    from sqlalchemy import update as _upd
    r = _receipt(db, rid)
    done = db.execute(_upd(Receipt).where(Receipt.id == r.id, Receipt.status == "failed")
                      .values(status="pending", next_try_at=0, error="", updated_at=now())).rowcount
    db.commit()
    if not done:
        raise HTTPException(409, "only a refused receipt can be sent again")
    core.audit(db, "Receipt resent", admin, f"tx:{r.tx_id}")
    return {"ok": True}


@router.post("/receipts/{rid}/resolve")
def receipt_resolve(rid: int, body: ReceiptDecision, db: Session = Depends(get_db), admin=A):
    """The admin looked in «Мой налог»: the receipt is there (sent), it is
    not there and should be made (pending), or it is not needed (skipped)."""
    from sqlalchemy import update as _upd
    r = _receipt(db, rid)
    allowed = {"uncertain": ("sent", "pending", "skipped"), "failed": ("skipped",),
               "pending": ("skipped",)}.get(r.status, ())
    if r.status == "skipped":
        allowed = ("pending",)            # the admin decided it is needed after all
    if body.status not in allowed:
        raise HTTPException(409, f"a {r.status} receipt cannot become {body.status}")
    values = {"status": body.status, "next_try_at": 0,
              "updated_at": now()}
    if body.status in ("sent", "pending"):
        values["error"] = ""
    if body.status == "sent":
        values["uuid"] = body.uuid.strip()
    done = db.execute(_upd(Receipt).where(Receipt.id == r.id, Receipt.status == r.status)
                      .values(**values)).rowcount
    db.commit()
    if not done:
        raise HTTPException(409, "the receipt changed meanwhile")
    core.audit(db, f"Receipt marked {body.status}", admin, f"tx:{r.tx_id} {body.uuid}".strip(), level="warn")
    return {"ok": True}


@router.post("/receipts/check")
def receipt_check(db: Session = Depends(get_db), admin=A):
    """Try to sign in to «Мой налог» with the keys from the server settings."""
    from .. import nalog
    if not nalog.configured():
        raise HTTPException(400, "NALOG_INN and NALOG_PASSWORD are not set")
    try:
        nalog.login(env.nalog_inn, env.nalog_password, force=True)
    except nalog.AuthError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e)[:200])
    resumed = nalog.paused(db)
    if resumed:
        core.put(db, "nalog_paused", "")          # the password works again: the queue goes on
        core.audit(db, "Receipts resumed", admin, "")
    return {"ok": True, "resumed": resumed}


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, db: Session = Depends(get_db), admin=A):
    tx = db.get(Transaction, order_id)
    if not tx or tx.status not in ("pending", "failed"):
        raise HTTPException(400, "only unpaid orders can be cancelled")
    tx.status = "cancelled"
    db.commit()
    core.audit(db, "Order cancelled", admin, f"#{tx.id}")
    return {"ok": True}


# ================================================================ promos
class PromoBody(BaseModel):
    code: str = Field(..., min_length=2, max_length=32)
    kind: str = "percent"
    value: float = Field(0, ge=0)
    limit: int = Field(0, ge=0)
    new_only: bool = False
    expires_at: int = 0
    enabled: bool = True


def _promo_out(p: Promo) -> dict:
    return {c: getattr(p, c) for c in ("id", "code", "kind", "value", "limit", "used",
                                       "new_only", "expires_at", "enabled")}


@router.get("/promos")
def promos(db: Session = Depends(get_db), _=A):
    return [_promo_out(p) for p in db.query(Promo).order_by(Promo.id.desc()).all()]


@router.post("/promos")
def create_promo(body: PromoBody, db: Session = Depends(get_db), admin=A):
    if body.kind not in ("percent", "amount", "days"):
        raise HTTPException(400, "kind must be percent, amount or days")
    code = body.code.strip().upper()
    if db.query(Promo).filter(Promo.code == code).first():
        raise HTTPException(400, "such a code already exists")
    p = Promo(**{**body.model_dump(), "code": code})
    db.add(p)
    db.commit()
    core.audit(db, "Promo created", admin, code)
    return _promo_out(p)


@router.put("/promos/{promo_id}")
def update_promo(promo_id: int, body: PromoBody, db: Session = Depends(get_db), admin=A):
    p = db.get(Promo, promo_id)
    if not p:
        raise HTTPException(404, "promo not found")
    if body.kind not in ("percent", "amount", "days"):
        raise HTTPException(400, "kind must be percent, amount or days")
    code = body.code.strip().upper()
    if db.query(Promo).filter(Promo.code == code, Promo.id != promo_id).first():
        raise HTTPException(400, "such a code already exists")
    for k, v in body.model_dump().items():
        setattr(p, k, v.strip().upper() if k == "code" else v)
    db.commit()
    return _promo_out(p)


@router.delete("/promos/{promo_id}")
def delete_promo(promo_id: int, db: Session = Depends(get_db), admin=A):
    p = db.get(Promo, promo_id)
    if not p:
        raise HTTPException(404, "promo not found")
    db.delete(p)
    db.commit()
    core.audit(db, "Promo deleted", admin, p.code)
    return {"ok": True}


# ================================================================ gifts
@router.get("/gifts")
def gifts(db: Session = Depends(get_db), _=A):
    rows = db.query(Gift).order_by(Gift.id.desc()).limit(200).all()
    return [{"code": g.code, "plan": g.plan.name if g.plan else "", "amount": g.amount,
             "buyer_id": g.buyer_id, "used": bool(g.used_by_id), "created_at": g.created_at,
             "used_at": g.used_at} for g in rows]


# ================================================================ reviews
@router.get("/reviews")
def reviews(status: str = "", db: Session = Depends(get_db), _=A):
    q = db.query(Review)
    if status:
        q = q.filter(Review.status == status)
    return [{"id": r.id, "stars": r.stars, "text": r.text, "status": r.status,
             "created_at": r.created_at, "tg_id": r.user.tg_id if r.user else None,
             "name": (r.user.username or r.user.first_name) if r.user else ""}
            for r in q.order_by(Review.id.desc()).limit(200).all()]


@router.post("/reviews/{review_id}/{decision}")
def moderate(review_id: int, decision: str, db: Session = Depends(get_db), admin=A):
    r = db.get(Review, review_id)
    if not r:
        raise HTTPException(404, "review not found")
    if decision == "delete":
        db.delete(r)
    elif decision in ("published", "rejected", "pending"):
        r.status = decision
    else:
        raise HTTPException(400, "unknown decision")
    db.commit()
    days = 0
    if decision == "published":
        from .. import reviews as reviews_mod
        days = reviews_mod.reward(db, r)
    return {"ok": True, "reward_days": days}


# ================================================================ broadcasts
AUDIENCES = ("all", "active", "expiring", "inactive", "never")


def audience_query(db: Session, audience: str):
    t = now()
    q = db.query(User).filter(User.bot_blocked.is_(False))
    if audience == "active":
        q = q.filter(User.expires_at > t)
    elif audience == "expiring":
        q = q.filter(User.expires_at.between(t, t + 3 * 86400))
    elif audience == "inactive":
        q = q.filter(User.expires_at > 0, User.expires_at <= t)
    elif audience == "never":
        q = q.filter(User.expires_at == 0)
    return q


@router.get("/audiences")
def audiences(db: Session = Depends(get_db), _=A):
    return {a: audience_query(db, a).count() for a in AUDIENCES}


class CastBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    audience: str = "all"
    button: bool = True


@router.post("/broadcasts")
def create_broadcast(body: CastBody, db: Session = Depends(get_db), admin=A):
    if body.audience not in AUDIENCES:
        raise HTTPException(400, "unknown audience")
    if not env.bot_token:
        raise HTTPException(400, "BOT_TOKEN is not set")
    cast = Broadcast(text=body.text.strip(), audience=body.audience,
                     button_text="1" if body.button else "",
                     total=audience_query(db, body.audience).count())
    db.add(cast)
    db.commit()
    core.audit(db, "Broadcast started", admin, f"{body.audience}: {cast.total}")
    return {"id": cast.id, "total": cast.total}


@router.get("/broadcasts")
def list_broadcasts(db: Session = Depends(get_db), _=A):
    return [{"id": b.id, "text": b.text, "audience": b.audience, "sent": b.sent,
             "failed": b.failed, "total": b.total, "status": b.status,
             "created_at": b.created_at}
            for b in db.query(Broadcast).filter(Broadcast.status != "draft")
            .order_by(Broadcast.id.desc()).limit(50).all()]


@router.post("/broadcasts/{cast_id}/cancel")
def cancel_broadcast(cast_id: int, db: Session = Depends(get_db), admin=A):
    b = db.get(Broadcast, cast_id)
    if b and b.status == "sending":
        b.status = "cancelled"
        db.commit()
    return {"ok": True}


# ================================================================ settings
# Kept by the server for itself: never shown, never set through the settings form.
PRIVATE_SETTINGS = ("probe_token", "dpi_uuid", "dpi_sub", "dpi_last_json", "blacklist_status_json",
                    "emergency_auto_on", "emergency_since",
                    # updates are the owner's: changed only through /updates
                    "update_info", "update_notified", "update_announced", "auto_update", "auto_update_hour",
                    "auto_update_tried", "report_sent")


@router.get("/settings")
def get_settings(db: Session = Depends(get_db), _=A):
    return {"values": {k: core.get(db, k) for k in core.DEFAULTS if k not in PRIVATE_SETTINGS},
            "env": {
                "public_url": env.public_url, "currency": env.currency,
                "default_lang": env.default_lang, "card_gateway": env.card_gateway,
                "methods": payments.methods(), "test_mode": env.payments_test_mode,
                "bot": notify.username(), "email_prefix": env.xui_email_prefix,
                "admin_ids": sorted(env.admin_telegram_ids),
                "nalog": {"on": bool(env.nalog_inn and env.nalog_password),
                          "gateways": list(env.nalog_gateways)},
                "webhooks": {
                    "yookassa": f"{env.public_url}/api/pay/yookassa",
                    "freekassa": f"{env.public_url}/api/pay/freekassa",
                    "platega": f"{env.public_url}/api/pay/platega",
                    "paypalych": f"{env.public_url}/api/pay/paypalych",
                    "cryptobot": f"{env.public_url}/api/pay/cryptobot",
                    "lava": f"{env.public_url}/api/pay/lava",
                    "wata": f"{env.public_url}/api/pay/wata",
                    "heleket": f"{env.public_url}/api/pay/heleket",
                    "tribute": f"{env.public_url}/api/pay/tribute",
                    "cloudpayments": f"{env.public_url}/api/pay/cloudpayments",
                    "mulenpay": f"{env.public_url}/api/pay/mulenpay",
                    "lavepay": f"{env.public_url}/api/pay/lavepay",
                },
                "problems": env.problems() + _problems(db)},
            "tg_servers": len(access.tg_servers(db)),
            "apps": [{"id": k, "name": a["name"], "hwid": a["hwid"]} for k, a in clients.APPS.items()]}


@router.post("/settings")
def save_settings(data: dict, db: Session = Depends(get_db), admin=A):
    from .. import referral, theme
    changed = []
    for key, value in data.items():
        if key not in core.DEFAULTS or key in ("logo", "logo_info") or key in PRIVATE_SETTINGS:
            continue
        value = "" if value is None else str(value)
        if key == "theme_accent" and not theme.HEX.match(value):
            raise HTTPException(400, "theme_accent: a colour like #F2B441")
        if key == "theme_surface" and value not in theme.SURFACES:
            raise HTTPException(400, "theme_surface: " + " | ".join(theme.SURFACES))
        if key == "theme_preset" and value not in theme.PRESETS and value != "custom":
            raise HTTPException(400, "theme_preset: unknown theme")
        if key == "theme_mode" and value not in theme.MODES:
            raise HTTPException(400, "theme_mode: " + " | ".join(theme.MODES))
        if key == "font" and value not in theme.FONTS and value != "custom":
            raise HTTPException(400, "font: " + " | ".join(theme.FONTS) + " | custom")
        if key == "font_custom":
            value = " ".join(value.split())
            if value and not theme.CUSTOM_FONT.match(value):
                raise HTTPException(400, "font_custom: a Google Fonts family name, like PT Sans")
        if key == "radius" and value not in theme.RADII:
            raise HTTPException(400, "radius: " + " | ".join(theme.RADII))
        for opt, allowed in (("backdrop", theme.BACKDROPS), ("icons", theme.ICONS),
                             ("motion", theme.MOTIONS), ("effect", theme.EFFECTS),
                             ("cards", theme.CARDS), ("splash", ("0", "1")),
                             ("splash_kind", theme.SPLASHES), *theme.SPLASH_OPTS.items()):
            if key == opt and value not in allowed:
                raise HTTPException(400, f"{opt}: " + " | ".join(allowed))
        if key == "connect_apps":
            try:
                value = clients.clean(value)
            except ValueError as e:
                raise HTTPException(400, str(e))
        if key == "splash_color":
            value = value.strip()
            if value not in ("style", "logo") and not theme.HEX.match(value):
                raise HTTPException(400, "splash_color: style | logo | #RRGGBB")
            value = value.upper() if value.startswith("#") else value
        if key in ("splash_text_ru", "splash_text_en"):
            value = value.strip()[:60]
        if key in ("bot_about_ru", "bot_about_en"):
            value = value.strip()[:120]           # Telegram's limit for the short description
        if key in ("bot_desc_ru", "bot_desc_en"):
            value = value.strip()[:512]           # and for the description
        if key == "channel_mode" and value not in ("off", "trial", "all"):
            raise HTTPException(400, "channel_mode: off | trial | all")
        if key == "channel_id":
            value = value.strip()
            if value and not re.fullmatch(r"@[A-Za-z0-9_]{4,64}|-100\d{5,20}", value):
                raise HTTPException(400, "channel_id: @username or -100…")
        if key == "channels_json":
            from .. import channel
            try:
                value = json.dumps(channel.clean(json.loads(value or "[]")), ensure_ascii=False)
            except ValueError as e:
                raise HTTPException(400, str(e))
        if key == "grace_days":
            value = value.strip() or "0"
            if not (value.isdigit() and 0 <= int(value) <= access.GRACE_MAX):
                raise HTTPException(400, f"grace_days: 0–{access.GRACE_MAX}")
        if key == "tg_only_hours":
            value = value.strip() or "0"
            if not (value.isdigit() and 0 <= int(value) <= access.TG_MAX_HOURS):
                raise HTTPException(400, f"tg_only_hours: 0–{access.TG_MAX_HOURS}")
        if key.startswith("emergency"):
            continue        # the emergency mode has its own route: it audits and re-syncs
        if key in ("backup_hour", "report_hour") and not (value.isdigit() and 0 <= int(value) <= 23):
            raise HTTPException(400, f"{key}: 0–23")
        if key in ("top_period",) and value not in ("all", "month", "week"):
            raise HTTPException(400, "top_period: all | month | week")
        if key in ("top_type",) and value not in ("days", "paid", "invited"):
            raise HTTPException(400, "top_type: days | paid | invited")
        if key == "pay_labels":
            try:
                parsed = json.loads(value or "{}")
            except ValueError:
                raise HTTPException(400, "pay_labels: not valid JSON")
            if not isinstance(parsed, dict):
                raise HTTPException(400, "pay_labels: an object")
            value = json.dumps({m: {k: str((parsed.get(m) or {}).get(k) or "").strip()[:40 if k.startswith("title") else 60]
                                    for k in payments.LABEL_PARTS}
                                for m in payments.LABEL_METHODS if isinstance(parsed.get(m), dict)},
                               ensure_ascii=False)
        if key == "review_reward_days":
            value = value.strip() or "0"
            if not value.isdigit() or int(value) > 365:
                raise HTTPException(400, "review_reward_days: a whole number from 0 to 365")
        if key in ("ref_percent", "ref_days", "ref_friend_days", "ref_min_payout"):
            value = value.strip() or "0"
        if key == "ref_mode" and value not in ("days", "money"):
            raise HTTPException(400, "ref_mode: days | money")
        if key == "ref_percent" and not (value.isdigit() and 0 <= int(value) <= referral.MAX_PERCENT):
            raise HTTPException(400, f"ref_percent: 0–{referral.MAX_PERCENT}")
        if key in ("ref_days", "ref_friend_days") and not (value.isdigit() and int(value) <= 365):
            raise HTTPException(400, f"{key}: 0–365")
        if key == "ref_min_payout":
            try:
                if not 0 <= float(value or 0) <= 10_000_000:
                    raise ValueError
            except ValueError:
                raise HTTPException(400, "ref_min_payout: a number, 0 or more")
        if key == "ref_payout_methods":
            try:
                raw = json.loads(value or "[]")
            except ValueError:
                raise HTTPException(400, "ref_payout_methods: not valid JSON")
            value = json.dumps(referral.clean_methods(raw), ensure_ascii=False)
        if key in ("terms_ru", "terms_en", "privacy_ru", "privacy_en"):
            value = value.replace("\r\n", "\n").strip()
            if len(value) > 20000:
                raise HTTPException(400, f"{key}: at most 20000 characters")
        if key.endswith("_json") or key in ("traffic_packs", "bypass_packs"):
            try:
                json.loads(value or "null")
            except ValueError:
                raise HTTPException(400, f"{key}: not valid JSON")
        if core.get(db, key) != value:
            core.put(db, key, value[:20000])
            changed.append(key)
    bot = {}
    if any(k.startswith(("bot_about_", "bot_desc_")) for k in changed):
        bot = notify.set_descriptions(db)
    if "grace_days" in changed:
        # The end on the panels moves for everyone near it: send them again.
        from .. import emergency
        emergency.resync_soon()
    if changed:
        core.audit(db, "Settings changed", admin, ", ".join(changed))
    return {"ok": True, "changed": changed, "bot": bot}


# ================================================================ shared blacklists
class BlacklistBody(BaseModel):
    urls: list[str] = Field(default_factory=list, max_length=10)
    action: str = "trial"
    publish: bool = False


@router.get("/blacklist")
def blacklist_get(db: Session = Depends(get_db), _=A):
    from .. import blacklist
    return blacklist.info(db)


@router.post("/blacklist")
def blacklist_set(body: BlacklistBody, db: Session = Depends(get_db), admin=A):
    from .. import blacklist
    if body.action not in blacklist.ACTIONS:
        raise HTTPException(400, "action: " + " | ".join(blacklist.ACTIONS))
    clean = []
    for u in body.urls:
        u = u.strip()
        if not u:
            continue
        if not re.fullmatch(r"https://[^\s]{4,250}", u):
            raise HTTPException(400, f"not an https:// address: {u[:80]}")
        clean.append(u)
    core.put(db, "blacklist_urls", "\n".join(clean))
    core.put(db, "blacklist_action", body.action)
    core.put(db, "blacklist_publish", "1" if body.publish else "0")
    core.audit(db, "Blacklists changed", admin, f"{len(clean)} lists, {body.action}")
    status = blacklist.refresh(db)
    return {**blacklist.info(db), "refreshed": status}


@router.post("/blacklist/refresh")
def blacklist_refresh(db: Session = Depends(get_db), _=A):
    from .. import blacklist
    blacklist.refresh(db)
    return blacklist.info(db)


# ================================================================ reachability from Russia
@router.get("/probe")
def probe_status(db: Session = Depends(get_db), _=A):
    from .. import dpicheck, probe
    return {**probe.status(db), "dpi": {"on": dpicheck.configured(), "last": dpicheck.last(db)},
            "script_url": f"{env.public_url}/probe/ru-probe.py", "shop": env.public_url}


@router.post("/probe/token")
def probe_token_new(db: Session = Depends(get_db), admin=A):
    """A new token: the old one (and every checker using it) stops at once."""
    from .. import probe
    return {"token": probe.new_token(db, admin)}


@router.delete("/probe/token")
def probe_token_drop(db: Session = Depends(get_db), admin=A):
    from .. import probe
    probe.drop_token(db, admin)
    return {"ok": True}


class ProbeName(BaseModel):
    name: str = Field(..., max_length=60)


@router.post("/probe/forget")
def probe_forget(body: ProbeName, db: Session = Depends(get_db), admin=A):
    from .. import probe
    n = probe.forget(db, body.name)
    core.audit(db, "Russia checker forgotten", admin, body.name)
    return {"removed": n}


class DpiBody(BaseModel):
    server_id: int


@router.get("/dpicheck/account")
def dpi_account(_=A):
    from .. import dpicheck
    try:
        return dpicheck.account()
    except dpicheck.DpiError as e:
        raise HTTPException(502, str(e))


@router.post("/dpicheck/estimate")
def dpi_estimate(body: DpiBody, db: Session = Depends(get_db), _=A):
    from .. import dpicheck
    try:
        return dpicheck.estimate(db, body.server_id)
    except dpicheck.DpiError as e:
        raise HTTPException(502, str(e))


@router.post("/dpicheck/start")
def dpi_start(body: DpiBody, db: Session = Depends(get_db), admin=A):
    from .. import dpicheck
    try:
        return dpicheck.start(db, body.server_id, admin)
    except dpicheck.DpiError as e:
        raise HTTPException(502, str(e))


@router.get("/dpicheck/{check_id}")
def dpi_result(check_id: int, db: Session = Depends(get_db), _=A):
    from .. import dpicheck
    try:
        return dpicheck.result(db, check_id)
    except dpicheck.DpiError as e:
        raise HTTPException(502, str(e))


# ================================================================ emergency and diagnostics
class EmergencyBody(BaseModel):
    on: bool | None = None
    text_ru: str | None = Field(None, max_length=300)
    text_en: str | None = Field(None, max_length=300)
    stop_sales: bool | None = None
    keep_access: bool | None = None
    auto: bool | None = None
    auto_minutes: int | None = Field(None, ge=2, le=240)


@router.get("/emergency")
def emergency_get(db: Session = Depends(get_db), _=A):
    from .. import emergency
    return emergency.state(db)


@router.post("/emergency")
def emergency_set(body: EmergencyBody, db: Session = Depends(get_db), admin=A):
    from .. import emergency
    return emergency.update(db, admin, **body.model_dump())


@router.get("/diagnostics")
def diagnostics_last(_=A):
    from .. import diagnostics
    return diagnostics.LAST or None


@router.post("/diagnostics")
def diagnostics_run(db: Session = Depends(get_db), _=A):
    from .. import diagnostics
    return diagnostics.run(db)


# ================================================================ look
@router.get("/theme")
def theme_info(db: Session = Depends(get_db), _=A):
    """Colour shortcuts with their computed colours, the parts and what is chosen now."""
    from .. import theme
    presets = []
    for key, p in theme.PRESETS.items():
        th = theme.resolve(p["accent"], p["surface"])
        presets.append({"id": key, "name": p["name"], "accent": th["fill"], "surface": p["surface"],
                        "ground": th["s_ground"], "card": th["s_card"], "on": th["on_fill"]})
    return {"presets": presets, "surfaces": {k: v["card"] for k, v in theme.SURFACES.items()},
            "current": theme.current(db), "logo": theme.logo_url(db),
            "look": theme.look(db),
            "options": {"backdrop": theme.BACKDROPS, "icons": theme.ICONS,
                        "motion": theme.MOTIONS, "effect": theme.EFFECTS,
                        "cards": theme.CARDS}}


@router.post("/theme/preview")
def theme_preview(body: dict, _=A):
    """Colours an accent would really get (after the contrast checks)."""
    from .. import theme
    if "font" in body:
        # The whole look for the live preview of the mini app.
        try:
            return theme.preview(body)
        except ValueError as e:
            raise HTTPException(400, str(e))
    accent = str(body.get("accent", ""))
    surface = str(body.get("surface", "black"))
    if not theme.HEX.match(accent) or surface not in theme.SURFACES:
        raise HTTPException(400, "bad colour or surface")
    return theme.resolve(accent, surface)


# ================================================================ backups
@router.get("/backups")
def backups(db: Session = Depends(get_db), _=A):
    from .. import backup
    return {"items": backup.listing(), "last": core.get_int(db, "backup_last", 0),
            "admins": len(env.admin_telegram_ids)}


@router.post("/backups")
def make_backup(db: Session = Depends(get_db), admin=A):
    """Make an archive now and send it to the admins."""
    from .. import backup
    path = backup.make(db)
    sent = backup.send(db, path) if env.bot_token else 0
    core.audit(db, "Backup on demand", admin, f"{path.name}, sent to {sent}")
    return {"name": path.name, "size": path.stat().st_size, "sent": sent}


@router.get("/backups/{name}")
def download_backup(name: str, _=A):
    from fastapi.responses import FileResponse

    from .. import backup
    path = backup.file_of(name)
    if not path:
        raise HTTPException(404, "no such backup")
    return FileResponse(path, media_type="application/zip", filename=name)


@router.post("/brand/reset")
def reset_brand(db: Session = Depends(get_db), admin=A):
    from .. import theme
    theme.reset_brand(db)
    core.audit(db, "Brand reset", admin, "")
    return {"ok": True, "brand": core.get(db, "brand_name")}


@router.get("/theme/fonts")
def theme_fonts(_=A):
    from .. import theme
    return [{"id": k, "name": v["name"], "d": v["d"], "b": v["b"], "load": v["load"], "kind": v["kind"],
             "app_only": bool(v.get("app_only"))}
            for k, v in theme.FONTS.items()]


# ================================================================ warm-up
class SaleBody(BaseModel):
    percent: int = Field(0, ge=0, le=90)
    days: int = Field(0, ge=0, le=365)             # 0 = until turned off
    title_ru: str = Field("", max_length=60)
    title_en: str = Field("", max_length=60)


@router.get("/sale")
def get_sale(db: Session = Depends(get_db), _=A):
    from .. import marketing
    s = marketing.sale(db)
    t = now()
    sold = (db.query(Transaction).filter(Transaction.status == "success", Transaction.discount > 0,
                                         Transaction.offer_id.is_(None), Transaction.paid_at >= t - 30 * 86400))
    return {**s, "max": marketing.MAX_DISCOUNT, "orders_30d": sold.count(),
            "revenue_30d": round(sum(x.amount for x in sold.all()), 2)}


@router.put("/sale")
def put_sale(body: SaleBody, db: Session = Depends(get_db), admin=A):
    from .. import marketing
    s = marketing.set_sale(db, body.percent, body.days, body.title_ru, body.title_en)
    core.audit(db, "Sale changed", admin, f"{s['percent']}% until {s['until'] or '—'}")
    return s


def _trigger_out(db: Session, key: str) -> dict:
    from .. import marketing
    from ..models import Offer, WarmSent
    tr = marketing.trigger(db, key)
    t = now()
    week = t - 7 * 86400
    return {"key": key, **tr, "default_ru": marketing.TRIGGERS[key]["ru"],
            "default_en": marketing.TRIGGERS[key]["en"],
            "limits": marketing.PARAM_LIMITS[key],
            "sent_7d": db.query(WarmSent).filter(WarmSent.trigger == key, WarmSent.created_at >= week).count(),
            "bought_7d": _paid_after_reminder(db, week) if key == "unpaid" else
            db.query(Offer).filter(Offer.source == f"trigger:{key}", Offer.used_at >= week).count()}


def _paid_after_reminder(db: Session, since: int) -> int:
    """People reminded of an unfinished payment who then paid."""
    from ..models import WarmSent
    return (db.query(func.count(func.distinct(WarmSent.user_id)))
            .filter(WarmSent.trigger == "unpaid", WarmSent.created_at >= since,
                    exists().where(and_(Transaction.user_id == WarmSent.user_id, Transaction.status == "success",
                                        Transaction.created_at >= WarmSent.created_at)))
            .scalar() or 0)


@router.get("/warm")
def warm(db: Session = Depends(get_db), _=A):
    from .. import marketing
    return {"triggers": [_trigger_out(db, k) for k in marketing.TRIGGERS],
            "segments": {s: marketing.segment_query(db, s).count() for s in marketing.SEGMENTS}}


@router.put("/warm/{key}")
def put_warm(key: str, body: dict, db: Session = Depends(get_db), admin=A):
    from .. import marketing
    if key not in marketing.TRIGGERS:
        raise HTTPException(404, "unknown notice")
    try:
        marketing.set_trigger(db, key, body)
    except (TypeError, ValueError):
        raise HTTPException(400, "bad values")
    core.audit(db, "Warm-up notice changed", admin, key)
    return _trigger_out(db, key)


@router.post("/warm/{key}/reset")
def reset_warm(key: str, db: Session = Depends(get_db), admin=A):
    from .. import marketing
    if key not in marketing.TRIGGERS:
        raise HTTPException(404, "unknown notice")
    marketing.reset_trigger(db, key)
    return _trigger_out(db, key)


def _admin_chat(admin: str) -> int | None:
    """Where to send a test: the admin's own chat when they came from
    Telegram, otherwise the first admin from ADMIN_TELEGRAM_IDS."""
    if admin.startswith("tg:") and admin[3:].isdigit():
        return int(admin[3:])
    return sorted(env.admin_telegram_ids)[0] if env.admin_telegram_ids else None


@router.post("/report/send")
def report_send(db: Session = Depends(get_db), admin=A):
    """The day's results right now, to the admin who asked (or the owners)."""
    from .. import report
    chat = _admin_chat(admin)
    n = report.send(db, [chat] if chat else None)
    if not n:
        raise HTTPException(502, "Telegram did not take the message: check BOT_TOKEN and that you started the bot")
    return {"ok": True, "sent": n}


@router.post("/warm/{key}/test")
def test_warm(key: str, db: Session = Depends(get_db), admin=A):
    from .. import marketing
    if key not in marketing.TRIGGERS:
        raise HTTPException(404, "unknown notice")
    chat = _admin_chat(admin)
    if not chat:
        raise HTTPException(400, "set ADMIN_TELEGRAM_IDS to receive tests")
    return {"ok": marketing.preview_trigger(db, key, chat), "chat": chat}


class CampaignBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    segment: str = "all"
    text_ru: str = Field(..., min_length=1, max_length=3500)
    text_en: str = Field("", max_length=3500)
    percent: int = Field(0, ge=0, le=90)
    valid_days: int = Field(3, ge=1, le=30)
    start_at: int = 0


def _campaign_out(db: Session, c) -> dict:
    from .. import marketing
    return {"id": c.id, "name": c.name, "segment": c.segment, "text_ru": c.text_ru,
            "text_en": c.text_en, "percent": c.percent, "valid_days": c.valid_days,
            "start_at": c.start_at, "status": c.status, "total": c.total, "sent": c.sent,
            "failed": c.failed, "created_at": c.created_at, **marketing.campaign_stats(db, c)}


@router.get("/campaigns")
def campaigns(db: Session = Depends(get_db), _=A):
    from ..models import Campaign
    return [_campaign_out(db, c) for c in db.query(Campaign).order_by(Campaign.id.desc()).limit(50).all()]


@router.post("/campaigns")
def create_campaign(body: CampaignBody, db: Session = Depends(get_db), admin=A):
    from .. import marketing
    from ..models import Campaign
    if body.segment not in marketing.SEGMENTS:
        raise HTTPException(400, "unknown segment")
    if not env.bot_token:
        raise HTTPException(400, "BOT_TOKEN is not set")
    c = Campaign(name=body.name.strip(), segment=body.segment, text_ru=body.text_ru.strip(),
                 text_en=body.text_en.strip(), percent=body.percent, valid_days=body.valid_days,
                 start_at=max(0, body.start_at), status="scheduled",
                 total=marketing.segment_query(db, body.segment).count())
    db.add(c)
    db.commit()
    core.audit(db, "Campaign created", admin, f"#{c.id} {c.name}: {c.segment}, {c.percent}%")
    return _campaign_out(db, c)


@router.post("/campaigns/{cid}/cancel")
def cancel_campaign(cid: int, db: Session = Depends(get_db), admin=A):
    from ..models import Campaign
    c = db.get(Campaign, cid)
    if c and c.status in ("scheduled", "sending"):
        c.status = "cancelled"
        db.commit()
        core.audit(db, "Campaign cancelled", admin, f"#{c.id}")
    return {"ok": True}


# ================================================================ referrals
@router.get("/referrals")
def referrals_admin(db: Session = Depends(get_db), _=A):
    from .. import referral
    from ..models import Payout, RefReward
    t = now()

    def total(q) -> float:
        return round(float(q.scalar() or 0), 2)
    return {
        "top": referral.top(db, full=True),
        "mode": referral.mode(db),
        "invited": db.query(User).filter(User.referrer_id.isnot(None)).count(),
        "invited_30d": db.query(User).filter(User.referrer_id.isnot(None),
                                             User.created_at >= t - 30 * 86400).count(),
        "rewards": db.query(RefReward).count(),
        "days_given": int(db.query(func.coalesce(func.sum(RefReward.days), 0)).scalar() or 0),
        "money_given": total(db.query(func.sum(RefReward.money))),
        "revenue": round(float(db.query(func.coalesce(func.sum(RefReward.amount), 0)).scalar() or 0), 2),
        "pending_captcha": db.query(User).filter(User.ref_pending > 0).count(),
        "balances": total(db.query(func.sum(User.ref_balance)).filter(User.ref_balance > 0)),
        "payouts_waiting": db.query(Payout).filter(Payout.status == "pending").count(),
        "payouts_waiting_sum": total(db.query(func.sum(Payout.amount)).filter(Payout.status == "pending")),
        "paid_out": total(db.query(func.sum(Payout.amount)).filter(Payout.status == "paid")),
        "spent_from_balance": total(db.query(func.sum(Transaction.amount))
                                    .filter(Transaction.method == "balance", Transaction.status == "success")),
        "methods": referral.payout_methods(db, only_on=False),
        "kinds": list(referral.PAYOUT_KINDS),
    }


@router.get("/payouts")
def payouts(status: str = "", limit: int = 100, db: Session = Depends(get_db), _=A):
    from .. import referral
    from ..models import Payout
    q = db.query(Payout)
    if status:
        q = q.filter(Payout.status == status)
    # Waiting requests first, then the newest.
    rows = q.order_by((Payout.status != "pending"), Payout.id.desc()).limit(max(1, min(limit, 500))).all()
    return [referral.payout_out(p, full=True) for p in rows]


class PayoutDecision(BaseModel):
    note: str = Field("", max_length=255)


@router.post("/payouts/{payout_id}/paid")
def payout_paid(payout_id: int, db: Session = Depends(get_db), admin=A):
    from .. import referral
    p = referral.decide(db, payout_id, True, admin)
    if not p:
        raise HTTPException(409, "the request is already decided")
    return referral.payout_out(p, full=True)


@router.post("/payouts/{payout_id}/reject")
def payout_reject(payout_id: int, body: PayoutDecision, db: Session = Depends(get_db), admin=A):
    from .. import referral
    p = referral.decide(db, payout_id, False, admin, body.note)
    if not p:
        raise HTTPException(409, "the request is already decided")
    return referral.payout_out(p, full=True)


# ================================================================ templates
@router.get("/templates")
def templates(_=A):
    """Starting points the owner may take: plans with suggested prices,
    the two documents, the trial. Nothing here is applied by itself."""
    from .. import legal, starter
    return {"plans": starter.plan_templates(), "currency": env.currency, "symbol": env.currency_symbol,
            "docs": legal.TEMPLATES, "trial": starter.TRIAL_HINT}


# ================================================================ channel
@router.post("/channel/check")
def channel_check_admin(db: Session = Depends(get_db), _=A):
    from .. import channel
    return channel.check_setup(db)


@router.post("/brand/logo")
async def upload_logo(request: Request, db: Session = Depends(get_db), admin=A):
    from .. import theme
    form = await request.form()
    file = form.get("file")
    if file is None or not hasattr(file, "read"):
        raise HTTPException(400, "no file")
    data = await file.read(theme.LOGO_MAX + 1)
    try:
        name = theme.save_logo(db, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    core.audit(db, "Logo uploaded", admin, name)
    return {"ok": True, "logo": f"/brand/{name}", "logo_info": theme.logo_info(db)}


@router.delete("/brand/logo")
def delete_logo(db: Session = Depends(get_db), admin=A):
    from .. import theme
    theme.remove_logo(db)
    core.audit(db, "Logo removed", admin, "")
    return {"ok": True}


@router.post("/bot/setup")
def bot_setup(db: Session = Depends(get_db), admin=A):
    """Register the webhook and the menu button again (after changing domain)."""
    res = notify.setup(db)
    core.audit(db, "Bot setup", admin, "; ".join(res))
    return {"result": res, "webhook": notify.call("getWebhookInfo").get("result", {})}


# ================================================================ log
@router.get("/logs")
def logs(limit: int = 200, level: str = "", db: Session = Depends(get_db), _=A):
    q = db.query(Log)
    if level:
        q = q.filter(Log.level == level)
    return [{"id": r.id, "level": r.level, "action": r.action, "actor": r.actor,
             "details": r.details, "at": r.created_at}
            for r in q.order_by(Log.id.desc()).limit(min(1000, limit)).all()]
