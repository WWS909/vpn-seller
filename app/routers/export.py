"""
Tables for Excel: orders for a period (for taxes and bookkeeping) and the
list of customers. GET gives the file; POST …/send puts it into the
admin's Telegram chat — inside Telegram a file cannot be saved from the
panel page itself.

Orders belong to the "sales" section of the team, customers to "people"
(app/team.py: the first part of the address decides).
"""

from __future__ import annotations

import datetime as dt

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import auth, notify, xlsx
from ..config import settings as env
from ..db import get_db
from ..models import Plan, Promo, Transaction, User, now
from ..payments import NAMES

router = APIRouter(prefix="/api/admin", tags=["admin"])
A = Depends(auth.admin_required)
MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_ROWS = 100_000

L = {
    "ru": {
        "orders": "Заказы", "users": "Клиенты", "total": "Итого",
        "o_cols": ["Заказ", "Дата", "Статус", "Клиент (Telegram ID)", "Username", "Имя", "Что куплено", "Тариф",
                   "Дней", "Сумма", "Валюта", "Звёзды", "Способ оплаты", "Промокод", "Скидка, %"],
        "u_cols": ["Telegram ID", "Username", "Имя", "Язык", "Пришёл", "Источник", "Тариф", "Подписка до",
                   "Устройств", "Трафик, ГБ", "Покупок", "Оплачено всего", "Последняя оплата", "Пробный",
                   "Заблокировал бота", "Заблокирован"],
        "status": {"success": "Оплачен", "pending": "Ожидает оплаты", "processing": "Оплачен, выдаётся",
                   "paid": "Оплачен, выдаётся", "failed": "Не прошёл", "cancelled": "Отменён"},
        "kind": {"purchase": "Подписка", "devices": "Устройства", "traffic": "Трафик", "bypass": "Обход",
                 "gift": "Подарок"},
        "method": {"balance": "Баланс", "free": "Бесплатно", "admin": "Выдано вручную", "card": "Карта",
                   "crypto": "Крипта"},
        "yes": "да", "no": "",
    },
    "en": {
        "orders": "Orders", "users": "Customers", "total": "Total",
        "o_cols": ["Order", "Date", "Status", "Customer (Telegram ID)", "Username", "Name", "What", "Plan",
                   "Days", "Amount", "Currency", "Stars", "Payment method", "Promo code", "Discount, %"],
        "u_cols": ["Telegram ID", "Username", "Name", "Language", "Came", "Source", "Plan", "Subscription until",
                   "Devices", "Traffic, GB", "Purchases", "Paid in total", "Last payment", "Trial",
                   "Blocked the bot", "Blocked"],
        "status": {"success": "Paid", "pending": "Waiting", "processing": "Paid, being given",
                   "paid": "Paid, being given", "failed": "Failed", "cancelled": "Cancelled"},
        "kind": {"purchase": "Subscription", "devices": "Devices", "traffic": "Traffic", "bypass": "Bypass",
                 "gift": "Gift"},
        "method": {"balance": "Balance", "free": "Free", "admin": "Given by hand", "card": "Card",
                   "crypto": "Crypto"},
        "yes": "yes", "no": "",
    },
}


def _lang(lang: str) -> str:
    return "en" if lang == "en" else "ru"


def _when(ts: int | None) -> dt.datetime | None:
    return dt.datetime.fromtimestamp(int(ts)) if ts else None


def _day(value: str, end: bool) -> int:
    """YYYY-MM-DD (server time) → the first second of that day, or of the next one."""
    try:
        d = dt.datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "dates: YYYY-MM-DD")
    return int((d + dt.timedelta(days=1 if end else 0)).timestamp())


def _name(t: int, what: str, start: str = "", end: str = "") -> str:
    stamp = f"{start}_{end}" if start or end else dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d")
    return f"{what}_{stamp}.xlsx"


# ---------------------------------------------------------------- orders
def orders_file(db: Session, start: str, end: str, status: str, lang: str) -> tuple[str, bytes]:
    lg = _lang(lang)
    T = L[lg]
    t = now()
    a = _day(start, False) if start else 0
    b = _day(end, True) if end else t + 1
    if a >= b:
        raise HTTPException(400, "the period is empty")
    q = db.query(Transaction, User).join(User, User.id == Transaction.user_id)
    if status == "paid":
        q = q.filter(Transaction.status == "success", Transaction.amount > 0,
                     Transaction.paid_at >= a, Transaction.paid_at < b).order_by(Transaction.paid_at, Transaction.id)
    elif status == "all":
        q = q.filter(Transaction.created_at >= a, Transaction.created_at < b).order_by(Transaction.id)
    else:
        raise HTTPException(400, "status: paid | all")
    plans = {p.id: p.name if lg == "ru" else (p.name_en or p.name) for p in db.query(Plan).all()}
    promos = {p.id: p.code for p in db.query(Promo.id, Promo.code).all()}
    rows, total = [], 0.0
    for tx, u in q.limit(MAX_ROWS).all():
        paid = tx.status == "success"
        if paid:
            total += float(tx.amount or 0)
        method = NAMES.get(tx.gateway or "", "") or NAMES.get(tx.method or "", "") or \
            T["method"].get(tx.method or "", tx.method or "")
        rows.append([tx.id, _when(tx.paid_at if paid and tx.paid_at else tx.created_at),
                     T["status"].get(tx.status, tx.status), u.tg_id, ("@" + u.username) if u.username else "",
                     u.first_name or "", T["kind"].get(tx.kind or "purchase", tx.kind),
                     plans.get(tx.plan_id, "") if tx.plan_id else "", tx.days or None,
                     round(float(tx.amount or 0), 2), tx.currency or env.currency, tx.stars or None, method,
                     promos.get(tx.promo_id, "") if tx.promo_id else "", tx.discount or None])
    cols = list(zip(T["o_cols"], ["int", "date", "text", "int", "text", "text", "text", "text", "int", "money",
                                  "text", "int", "text", "text", "int"],
                    [9, 17, 18, 14, 16, 16, 14, 16, 7, 12, 8, 8, 16, 12, 9]))
    data = xlsx.build(T["orders"], cols, rows, totals={T["o_cols"][9]: total}, total_label=T["total"])
    return _name(t, "orders", start, end), data


# ---------------------------------------------------------------- customers
def users_file(db: Session, lang: str) -> tuple[str, bytes]:
    lg = _lang(lang)
    T = L[lg]
    t = now()
    plans = {p.id: p.name if lg == "ru" else (p.name_en or p.name) for p in db.query(Plan).all()}
    paid = dict((uid, (n, s, last)) for uid, n, s, last in (
        db.query(Transaction.user_id, func.count(Transaction.id), func.coalesce(func.sum(Transaction.amount), 0),
                 func.max(Transaction.paid_at))
        .filter(Transaction.status == "success", Transaction.amount > 0)
        .group_by(Transaction.user_id).all()))
    rows = []
    for u in db.query(User).order_by(User.id).limit(MAX_ROWS).all():
        n, s, last = paid.get(u.id, (0, 0, 0))
        active = u.expires_at and u.expires_at > t
        rows.append([u.tg_id, ("@" + u.username) if u.username else "", u.first_name or "", u.lang or "",
                     _when(u.created_at), u.source or "", plans.get(u.plan_id, "") if u.plan_id and active else "",
                     _when(u.expires_at), u.device_limit or None,
                     round((u.traffic_used or 0) / 1024 ** 3, 2) if u.traffic_used else None,
                     n or None, round(float(s), 2) if s else None, _when(last),
                     T["yes"] if u.trial_used else T["no"], T["yes"] if u.bot_blocked else T["no"],
                     T["yes"] if u.is_blocked else T["no"]])
    cols = list(zip(T["u_cols"], ["int", "text", "text", "text", "date", "text", "text", "date", "int", "money",
                                  "int", "money", "date", "text", "text", "text"],
                    [14, 16, 16, 6, 17, 12, 16, 17, 9, 10, 9, 13, 17, 8, 10, 10]))
    return _name(t, "customers"), xlsx.build(T["users"], cols, rows)


# ---------------------------------------------------------------- routes
def _file(name: str, data: bytes) -> Response:
    return Response(data, media_type=MIME, headers={"Content-Disposition": f'attachment; filename="{name}"',
                                                    "Cache-Control": "no-store"})


def send_file(chat: int, name: str, data: bytes, caption: str) -> bool:
    """The file into a Telegram chat, as a document."""
    try:
        r = httpx.post(f"{notify.API_BASE}/bot{env.bot_token}/sendDocument", timeout=120,
                       data={"chat_id": str(chat), "caption": caption[:1000]},
                       files={"document": (name, data, MIME)})
        return bool(r.json().get("ok"))
    except (httpx.HTTPError, ValueError):
        return False


def _chat(admin: str) -> int:
    from .admin_api import _admin_chat
    chat = _admin_chat(admin)
    if not chat:
        raise HTTPException(400, "no Telegram chat: sign in to the panel from Telegram or set ADMIN_TELEGRAM_IDS")
    return chat


@router.get("/orders/export")
def export_orders(start: str = Query("", max_length=10), end: str = Query("", max_length=10),
                  status: str = "paid", lang: str = "ru", db: Session = Depends(get_db), _=A):
    return _file(*orders_file(db, start, end, status, lang))


@router.post("/orders/export/send")
def send_orders(start: str = Query("", max_length=10), end: str = Query("", max_length=10),
                status: str = "paid", lang: str = "ru", db: Session = Depends(get_db), admin=A):
    name, data = orders_file(db, start, end, status, lang)
    if not send_file(_chat(admin), name, data, name):
        raise HTTPException(502, "Telegram did not take the file")
    return {"ok": True, "name": name}


@router.get("/users/export")
def export_users(lang: str = "ru", db: Session = Depends(get_db), _=A):
    return _file(*users_file(db, lang))


@router.post("/users/export/send")
def send_users(lang: str = "ru", db: Session = Depends(get_db), admin=A):
    name, data = users_file(db, lang)
    if not send_file(_chat(admin), name, data, name):
        raise HTTPException(502, "Telegram did not take the file")
    return {"ok": True, "name": name}
