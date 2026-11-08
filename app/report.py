"""
The day's results for the owners in Telegram.

Once a day, at the hour chosen in the panel (server time), the owners get a
short message: revenue and purchases of the last 24 hours against the 24
before, new people and trials, active subscriptions, what ends tomorrow,
unfinished payments, and anything that needs a look (servers, support,
stuck orders). Numbers only — nothing to read twice.
"""

from __future__ import annotations

import datetime as dt
import html

from sqlalchemy import and_, exists, func
from sqlalchemy.orm import Session, aliased

from . import billing, core, notify
from .config import settings as env
from .models import Server, Transaction, User, now

E = html.escape
DAY = 86400

TEXT = {
    "ru": {
        "title": "📊 <b>Итоги за сутки</b> · {date}",
        "revenue": "Выручка: <b>{v}</b>{cmp}",
        "orders": "Покупок: {n} · впервые заплатили: {first}",
        "people": "Новых людей: {n}{cmp} · взяли пробный: {trials}",
        "active": "Активных подписок: {n}",
        "ending": "Закончится в ближайшие сутки: {n}",
        "unpaid": "Начали оплату и не закончили: {n}",
        "prev": " (вчера {v})",
        "attention": "<b>Нужно посмотреть:</b>",
        "srv_down": "• не отвечает: {names}",
        "support": "• вопросов в поддержке без ответа: {n}",
        "stuck": "• оплачено, но доступ не выдан: {n}",
        "all_ok": "Серверы и заказы в порядке ✅",
        "button": "Открыть панель",
        "quiet": "За сутки покупок не было.",
    },
    "en": {
        "title": "📊 <b>The last 24 hours</b> · {date}",
        "revenue": "Revenue: <b>{v}</b>{cmp}",
        "orders": "Purchases: {n} · first-time payers: {first}",
        "people": "New people: {n}{cmp} · took the trial: {trials}",
        "active": "Active subscriptions: {n}",
        "ending": "Ending within a day: {n}",
        "unpaid": "Started a payment and didn't finish: {n}",
        "prev": " (the day before {v})",
        "attention": "<b>Needs a look:</b>",
        "srv_down": "• not answering: {names}",
        "support": "• support questions without an answer: {n}",
        "stuck": "• paid, but access not given: {n}",
        "all_ok": "Servers and orders are fine ✅",
        "button": "Open the panel",
        "quiet": "No purchases in the last 24 hours.",
    },
}


def _sum(q) -> float:
    return float(q.with_entities(func.coalesce(func.sum(Transaction.amount), 0)).scalar() or 0)


def collect(db: Session, t: int | None = None) -> dict:
    """The numbers of the 24 hours before t and of the 24 before those."""
    t = t or now()
    a, b = t - DAY, t - 2 * DAY
    paid = db.query(Transaction).filter(Transaction.status == "success", Transaction.amount > 0)
    day = paid.filter(Transaction.paid_at >= a, Transaction.paid_at <= t)
    prev = paid.filter(Transaction.paid_at >= b, Transaction.paid_at < a)
    earlier = aliased(Transaction)
    first = (day.filter(~exists().where(and_(earlier.user_id == Transaction.user_id, earlier.status == "success",
                                             earlier.amount > 0, earlier.paid_at < a)))
             .with_entities(func.count(func.distinct(Transaction.user_id))).scalar() or 0)
    trial = billing.trial_plan(db)
    trials = (db.query(Transaction).filter(Transaction.status == "success", Transaction.plan_id == trial.id,
                                           Transaction.paid_at >= a, Transaction.paid_at <= t).count()
              if trial else 0)
    later = aliased(Transaction)
    unpaid = (db.query(func.count(func.distinct(Transaction.user_id)))
              .filter(Transaction.status.in_(("pending", "failed")), Transaction.amount > 0,
                      Transaction.method.notin_(("free", "admin", "balance")),
                      Transaction.created_at >= a, Transaction.created_at <= t,
                      ~exists().where(and_(later.user_id == Transaction.user_id, later.status == "success",
                                           later.id > Transaction.id)))
              .scalar() or 0)
    down = [s.name for s in db.query(Server).filter(Server.enabled.is_(True)).order_by(Server.sort, Server.id).all()
            if s.last_error or s.vpn_down_at]
    return {
        "at": t,
        "revenue": round(_sum(day), 2), "revenue_prev": round(_sum(prev), 2),
        "orders": day.count(), "orders_prev": prev.count(), "first": int(first),
        "new": db.query(User).filter(User.created_at >= a, User.created_at <= t).count(),
        "new_prev": db.query(User).filter(User.created_at >= b, User.created_at < a).count(),
        "trials": trials,
        "active": db.query(User).filter(User.expires_at > t, User.is_blocked.is_(False)).count(),
        "ending": db.query(User).filter(User.expires_at > t, User.expires_at <= t + DAY,
                                        User.is_blocked.is_(False)).count(),
        "unpaid": int(unpaid),
        "down": down,
        "support": db.query(User).filter(User.support_at > User.support_answered_at).count(),
        "stuck": db.query(Transaction).filter(Transaction.status == "paid").count(),
    }


def _change(cur: float, prev: float) -> str:
    if not prev:
        return " ▲" if cur else ""
    pct = round((cur - prev) / prev * 100)
    if pct == 0:
        return " ="
    return f" ▲ {pct}%" if pct > 0 else f" ▼ {-pct}%"


def text(db: Session, d: dict, lang: str) -> str:
    L = TEXT["en" if lang == "en" else "ru"]
    date = dt.datetime.fromtimestamp(d["at"]).strftime("%d.%m" if lang != "en" else "%b %d")
    money = lambda v: core.money(v, lang)  # noqa: E731
    lines = [L["title"].format(date=date), ""]
    lines.append(L["revenue"].format(v=money(d["revenue"]),
                                     cmp=_change(d["revenue"], d["revenue_prev"]) +
                                     L["prev"].format(v=money(d["revenue_prev"]))))
    lines.append(L["orders"].format(n=d["orders"], first=d["first"]) if d["orders"] else L["quiet"])
    lines.append(L["people"].format(n=d["new"], cmp=_change(d["new"], d["new_prev"]), trials=d["trials"]))
    lines.append(L["active"].format(n=d["active"]))
    if d["ending"]:
        lines.append(L["ending"].format(n=d["ending"]))
    if d["unpaid"]:
        lines.append(L["unpaid"].format(n=d["unpaid"]))
    issues = []
    if d["down"]:
        issues.append(L["srv_down"].format(names=E(", ".join(d["down"][:6]))))
    if d["support"]:
        issues.append(L["support"].format(n=d["support"]))
    if d["stuck"]:
        issues.append(L["stuck"].format(n=d["stuck"]))
    lines.append("")
    lines += [L["attention"], *issues] if issues else [L["all_ok"]]
    return "\n".join(lines)


def send(db: Session, chats: list[int] | None = None) -> int:
    """Send the report now; returns to how many chats it went."""
    lang = env.default_lang
    msg = text(db, collect(db), lang)
    payload: dict = {"text": msg, "parse_mode": "HTML", "disable_web_page_preview": True}
    if env.public_url.startswith("https://"):
        payload["reply_markup"] = {"inline_keyboard": [[{"text": TEXT["en" if lang == "en" else "ru"]["button"],
                                                         "web_app": {"url": f"{env.public_url}/admin"}}]]}
    n = 0
    for chat in chats if chats is not None else env.admin_telegram_ids:
        try:
            if notify.call("sendMessage", {"chat_id": chat, **payload}).get("ok"):
                n += 1
        except Exception:  # noqa: BLE001 — one chat must not stop the others
            pass
    return n


def run_daily(db: Session) -> bool:
    """Called every few minutes: sends once a day at the chosen hour."""
    if core.get(db, "report_on") != "1":
        return False
    now_local = dt.datetime.now()
    if now_local.hour != core.get_int(db, "report_hour", 18) % 24:
        return False
    today = now_local.strftime("%Y-%m-%d")
    if core.get(db, "report_sent") == today:
        return False
    core.put(db, "report_sent", today)
    send(db)
    return True
