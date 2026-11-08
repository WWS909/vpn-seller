"""
Running the shop from Telegram.

Admins (ADMIN_TELEGRAM_IDS) get commands in the bot and buttons under its
messages. Everything here calls the same functions as the web panel, so
a change made from the bot is the same change — with the same checks and
the same line in the journal. The whole panel also opens inside Telegram
from /admin without a password (the Telegram signature proves who it is).

Commands
  /admin            menu with buttons and the panel
  /stats            sales and state
  /report           the last 24 hours (the evening message, on demand)
  /user <id|@name>  a customer's card with actions
  /give <id> <days> add (or, with a minus, take away) days
  /servers          servers with a check and on/off
  /orders           paid but not granted, and the latest sales
  /trial [days|off] the free trial
  /promo            codes; /promo CODE 20% [limit] | CODE 100 | CODE 7d; /promo del CODE
  /broadcast <text> a message to customers, with the audience picked by button
  /sale [25 [days] | off]  the sale on every plan
  /backup           the database archive right now, to this chat
"""

from __future__ import annotations

import datetime as dt
import html
import re

from sqlalchemy import or_
from sqlalchemy.orm import Session

from . import access, billing, core, notify
from .config import settings as env
from .models import GB, Broadcast, Promo, Server, Transaction, User, now

E = html.escape

T = {
    "ru": {
        "menu": "<b>{brand}</b> · управление\n\n{summary}",
        "sup_waiting": "Ждут ответа в поддержке: <b>{n}</b>",
        "summary": "Клиентов: {users} · активных: {active}\nВыручка сегодня: {today} · за 30 дней: {month}",
        "b_stats": "Статистика", "b_servers": "Серверы", "b_orders": "Заказы", "b_trial": "Пробный период",
        "b_promo": "Промокоды", "b_cast": "Рассылка", "b_panel": "Открыть панель", "b_back": "Меню",
        "stats": ("<b>Статистика</b>\n\nКлиентов: {users} (+{new_today} сегодня)\nАктивных подписок: {active}\n"
                  "Выручка сегодня: {today}\nЗа 30 дней: {month} · заказов: {orders} · средний чек: {avg}\n"
                  "Трафик: {traffic} ГБ"),
        "stuck": "\nОплачено, но ещё не выдано: {n}",
        "sync_err": "\nКлиентов с ошибкой синхронизации: {n}",
        "problems": "\n\n<b>Требует внимания</b>\n{list}",
        "servers": "<b>Серверы</b>\n\n{list}",
        "srv_ok": "работает", "srv_err": "ошибка: {e}", "srv_off": "выключен", "srv_none": "Серверов нет. Добавьте их в панели.",
        "srv_line": "{flag}<b>{name}</b> — {state}\n{url} · {ver} · клиентов: {n}",
        "b_check": "Проверить {name}", "b_on": "Включить {name}", "b_off": "Выключить {name}",
        "checked_ok": "{name}: связь есть, {ver}, инбаундов: {n}", "checked_bad": "{name}: {e}",
        "orders": "<b>Заказы</b>", "orders_stuck": "\n\nОплачены, но не выданы:\n{list}",
        "orders_last": "\n\nПоследние оплаты:\n{list}", "orders_none": "\n\nОплат пока нет.",
        "b_grant": "Выдать #{id}", "granted": "Заказ #{id} выдан", "grant_fail": "Заказ #{id}: {e}",
        "trial": "<b>Пробный период</b>\n\n{state}\nВзяли: {used} · за неделю: {week}\n\n"
                 "Изменить: /trial 7 — семь дней, /trial off — выключить.",
        "trial_on": "Включён: {days} дн., устройств: {dev}, трафик: {gb}", "trial_off": "Выключен.",
        "b_trial_off": "Выключить", "b_trial_on": "Включить",
        "user": ("<b>{name}</b>{uname}\nID: <code>{tg}</code>\n\nПодписка: {state}\nТариф: {plan}\n"
                 "Устройства: {dev}\nТрафик: {traffic}\nПотрачено: {spent}\nС нами с {since}"),
        "u_active": "до {date}", "u_expired": "закончилась {date}", "u_never": "не было", "u_gone": "Клиент не найден", "u_bypass": "Обход белых списков: {used} / {limit} ГБ", "u_blocked": " · <b>заблокирован</b>",
        "u_nolimit": "без ограничений",
        "b_block": "Заблокировать", "b_unblock": "Разблокировать", "b_reset": "Сбросить трафик",
        "b_sync": "Синхронизировать", "b_days": "+{n} дн.",
        "u_notfound": "Клиент не найден. Пример: /user 123456789 или /user @username",
        "u_usage": "Кого открыть? Пример: /user 123456789 или /user @username",
        "give_usage": "Пример: /give 123456789 30 — добавить 30 дней, /give 123456789 -3 — убрать три.",
        "done": "Готово", "sync_res": "Серверы: {ok} ок, {failed} с ошибкой",
        "promos": "<b>Промокоды</b>\n\n{list}\n\nСоздать: /promo КОД 20% [лимит] · /promo КОД 100 · /promo КОД 7d\nУдалить: /promo del КОД",
        "promo_none": "Промокодов нет.", "promo_line": "<code>{code}</code> — {what} · использован {used}{limit}{off}",
        "promo_made": "Промокод <code>{code}</code> создан: {what}", "promo_bad": "Не понял. Пример: /promo SUMMER 20% 100",
        "promo_exists": "Такой код уже есть.", "promo_deleted": "Промокод {code} удалён.", "promo_missing": "Нет такого кода.",
        "p_percent": "скидка {v}%", "p_amount": "скидка {v}", "p_days": "+{v} дн.",
        "cast_usage": "Напишите текст после команды: /broadcast Текст сообщения.\nИли ответьте командой /broadcast на готовое сообщение.",
        "cast_preview": "<b>Рассылка</b> — кому отправить?\n\n{text}",
        "a_all": "Всем ({n})", "a_active": "С подпиской ({n})", "a_inactive": "Подписка кончилась ({n})",
        "a_never": "Не покупали ({n})", "b_cancel": "Отмена",
        "cast_started": "Рассылка запущена: {n} получателей. Итог придёт в журнал панели.",
        "cast_cancelled": "Рассылка отменена.", "cast_gone": "Эта рассылка уже неактуальна.",
        "help": ("<b>Команды администратора</b>\n\n/admin — меню и панель\n/stats — статистика\n/report — итоги за сутки\n"
                 "/user 123456789 — карточка клиента\n/give 123456789 30 — добавить дни\n/servers — серверы\n"
                 "/orders — заказы\n/trial 3 — пробный период (off — выключить)\n/promo — промокоды\n"
                 "/broadcast текст — рассылка\n/sale 25 3 — распродажа −25% на три дня (off — выключить)\n/backup — резервная копия базы\n"
                 "/diag — диагностика\n/emergency — режим аварии (on текст / off)\n\n"
                 "<b>Поддержка</b>: вопросы клиентов приходят сюда. Ответьте на сообщение (свайп → Ответить) — "
                 "клиент получит ответ от бота."),
        "not_https": "Панель внутри Telegram откроется, когда у сервиса будет адрес https://.",
        "never": "никогда", "unlimited": "∞",
        "b_report": "Итоги за сутки", "b_sale": "Распродажа", "backup_fail": "Не удалось отправить архив — он сохранён на сервере в data/backups.",
        "sale": "<b>Распродажа</b>\n\n{state}\n\nИзменить: /sale 25 — скидка 25% без срока, /sale 25 3 — на три дня, "
                "/sale off — выключить.",
        "sale_on": "Идёт: −{p}%{until}", "sale_until": " до {date}", "sale_off": "Не идёт.",
        "b_po_ok": "Выплачено", "b_po_no": "Отклонить",
        "po_paid": "\n\n<b>Выплачено</b> · {who}", "po_rejected": "\n\n<b>Отклонено</b> · {who} · деньги вернулись на баланс",
        "po_gone": "Эта заявка уже обработана",
        "em": "<b>Режим аварии</b>\n\n{state}\n\nВключить: /emergency on Текст для клиентов (без текста — стандартный)\n"
              "Выключить: /emergency off\nОстальное — в панели, раздел «Авария».",
        "em_on": "<b>Включён</b> с {date}\nСообщение: {text}\nПродажи: {sales} · отключения: {keep}",
        "em_off": "Выключен.", "em_sales_off": "остановлены", "em_sales_on": "идут",
        "em_keep_on": "никого не отключаем", "em_keep_off": "как обычно",
        "diag_wait": "Проверяю… это до 20 секунд.",
        "u_grace": "закончилась {date}, VPN работает до {until}",
    },
    "en": {
        "menu": "<b>{brand}</b> · admin\n\n{summary}",
        "sup_waiting": "Waiting for a support answer: <b>{n}</b>",
        "summary": "Customers: {users} · active: {active}\nRevenue today: {today} · 30 days: {month}",
        "b_stats": "Stats", "b_servers": "Servers", "b_orders": "Orders", "b_trial": "Free trial",
        "b_promo": "Promo codes", "b_cast": "Broadcast", "b_panel": "Open the panel", "b_back": "Menu",
        "stats": ("<b>Stats</b>\n\nCustomers: {users} (+{new_today} today)\nActive subscriptions: {active}\n"
                  "Revenue today: {today}\n30 days: {month} · orders: {orders} · average: {avg}\n"
                  "Traffic: {traffic} GB"),
        "stuck": "\nPaid but not granted yet: {n}",
        "sync_err": "\nCustomers with sync errors: {n}",
        "problems": "\n\n<b>Needs attention</b>\n{list}",
        "servers": "<b>Servers</b>\n\n{list}",
        "srv_ok": "working", "srv_err": "error: {e}", "srv_off": "off", "srv_none": "No servers. Add them in the panel.",
        "srv_line": "{flag}<b>{name}</b> — {state}\n{url} · {ver} · clients: {n}",
        "b_check": "Check {name}", "b_on": "Turn on {name}", "b_off": "Turn off {name}",
        "checked_ok": "{name}: reachable, {ver}, inbounds: {n}", "checked_bad": "{name}: {e}",
        "orders": "<b>Orders</b>", "orders_stuck": "\n\nPaid, not granted:\n{list}",
        "orders_last": "\n\nLatest payments:\n{list}", "orders_none": "\n\nNo payments yet.",
        "b_grant": "Grant #{id}", "granted": "Order #{id} granted", "grant_fail": "Order #{id}: {e}",
        "trial": "<b>Free trial</b>\n\n{state}\nTaken: {used} · this week: {week}\n\n"
                 "Change: /trial 7 — seven days, /trial off — turn off.",
        "trial_on": "On: {days} d, devices: {dev}, traffic: {gb}", "trial_off": "Off.",
        "b_trial_off": "Turn off", "b_trial_on": "Turn on",
        "user": ("<b>{name}</b>{uname}\nID: <code>{tg}</code>\n\nSubscription: {state}\nPlan: {plan}\n"
                 "Devices: {dev}\nTraffic: {traffic}\nSpent: {spent}\nWith us since {since}"),
        "u_active": "until {date}", "u_expired": "ended {date}", "u_never": "never had one", "u_gone": "Customer not found", "u_bypass": "Whitelist bypass: {used} / {limit} GB", "u_blocked": " · <b>blocked</b>",
        "u_nolimit": "no limit",
        "b_block": "Block", "b_unblock": "Unblock", "b_reset": "Reset traffic",
        "b_sync": "Sync", "b_days": "+{n} d",
        "u_notfound": "Customer not found. Example: /user 123456789 or /user @username",
        "u_usage": "Whom to open? Example: /user 123456789 or /user @username",
        "give_usage": "Example: /give 123456789 30 — add 30 days, /give 123456789 -3 — take three away.",
        "done": "Done", "sync_res": "Servers: {ok} ok, {failed} failed",
        "promos": "<b>Promo codes</b>\n\n{list}\n\nCreate: /promo CODE 20% [limit] · /promo CODE 100 · /promo CODE 7d\nDelete: /promo del CODE",
        "promo_none": "No promo codes.", "promo_line": "<code>{code}</code> — {what} · used {used}{limit}{off}",
        "promo_made": "Promo code <code>{code}</code> created: {what}", "promo_bad": "Didn't get it. Example: /promo SUMMER 20% 100",
        "promo_exists": "This code already exists.", "promo_deleted": "Promo code {code} deleted.", "promo_missing": "No such code.",
        "p_percent": "{v}% off", "p_amount": "{v} off", "p_days": "+{v} d",
        "cast_usage": "Put the text after the command: /broadcast Message text.\nOr reply /broadcast to a ready message.",
        "cast_preview": "<b>Broadcast</b> — to whom?\n\n{text}",
        "a_all": "Everyone ({n})", "a_active": "Subscribed ({n})", "a_inactive": "Expired ({n})",
        "a_never": "Never bought ({n})", "b_cancel": "Cancel",
        "cast_started": "Broadcast started: {n} recipients. The result goes to the panel's log.",
        "cast_cancelled": "Broadcast cancelled.", "cast_gone": "This broadcast is no longer pending.",
        "help": ("<b>Admin commands</b>\n\n/admin — menu and panel\n/stats — stats\n/report — the last 24 hours\n"
                 "/user 123456789 — customer card\n/give 123456789 30 — add days\n/servers — servers\n"
                 "/orders — orders\n/trial 3 — free trial (off — turn off)\n/promo — promo codes\n"
                 "/broadcast text — broadcast\n/sale 25 3 — 25% off for three days (off — stop)\n/backup — database backup\n"
                 "/diag — diagnostics\n/emergency — emergency mode (on text / off)\n\n"
                 "<b>Support</b>: customer questions arrive here. Reply to the message (swipe → Reply) — "
                 "the customer gets the answer from the bot."),
        "not_https": "The panel opens inside Telegram once the service has an https:// address.",
        "never": "never", "unlimited": "∞",
        "b_report": "The last 24 hours", "b_sale": "Sale", "backup_fail": "Could not send the archive — it is saved on the server in data/backups.",
        "sale": "<b>Sale</b>\n\n{state}\n\nChange: /sale 25 — 25% off with no end, /sale 25 3 — for three days, "
                "/sale off — stop.",
        "sale_on": "On: −{p}%{until}", "sale_until": " until {date}", "sale_off": "Not running.",
        "b_po_ok": "Paid", "b_po_no": "Decline",
        "po_paid": "\n\n<b>Paid</b> · {who}", "po_rejected": "\n\n<b>Declined</b> · {who} · the money is back on the balance",
        "po_gone": "This request is already decided",
        "em": "<b>Emergency mode</b>\n\n{state}\n\nTurn on: /emergency on Text for customers (none — the standard one)\n"
              "Turn off: /emergency off\nThe rest is in the panel, section “Emergency”.",
        "em_on": "<b>On</b> since {date}\nMessage: {text}\nSales: {sales} · cut-offs: {keep}",
        "em_off": "Off.", "em_sales_off": "stopped", "em_sales_on": "open",
        "em_keep_on": "nobody is cut off", "em_keep_off": "as usual",
        "diag_wait": "Checking… up to 20 seconds.",
        "u_grace": "ended {date}, VPN works until {until}",
    },
}

COMMANDS = ("admin", "panel", "stats", "report", "day", "user", "give", "servers", "orders", "trial", "promo",
            "broadcast", "sale", "backup", "help", "diag", "emergency")


def is_admin(tg_id) -> bool:
    try:
        return int(tg_id) in env.admin_telegram_ids
    except (TypeError, ValueError):
        return False


def _helps(db: Session, tg_id) -> bool:
    """A team member who gets the customers' questions (and may see whose they are)."""
    from . import team
    try:
        return int(tg_id) in team.support_ids(db)
    except (TypeError, ValueError):
        return False


def _lang(sender: dict) -> str:
    return core.lang_of(sender.get("language_code"))


def _t(lang: str, key: str, **kw) -> str:
    text = T.get(lang, T["en"]).get(key) or T["en"].get(key, key)
    return text.format(**kw) if kw else text


def _money(value: float) -> str:
    v = float(value or 0)
    num = f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"
    return num.replace(",", " ") + " " + env.currency_symbol


def _date(ts: int, lang: str) -> str:
    if not ts:
        return "—"
    return dt.datetime.fromtimestamp(int(ts)).strftime("%d.%m.%Y" if lang == "ru" else "%Y-%m-%d")


def _gb(v: int) -> str:
    return f"{(v or 0) / GB:.1f}".rstrip("0").rstrip(".")


def panel_label(kind: str | None, version: str | None) -> str:
    v = str(version or "")
    if kind == "marzban" or v.startswith("m"):
        return "Marzban " + v.lstrip("m")
    if kind == "remnawave" or v.startswith("r"):
        return ("Remnawave " + v.lstrip("r")).strip()
    return "3x-ui " + (v or "?")


def _actor(sender: dict) -> str:
    return f"tg:{sender.get('id')}"


# ---------------------------------------------------------------------------
#  Sending
# ---------------------------------------------------------------------------
def _kb(rows: list[list[tuple[str, str]]]) -> dict:
    """Rows of (text, callback_data); callback_data starting with "url:" or
    "app:" becomes a link or a mini app button."""
    out = []
    for row in rows:
        line = []
        for text, data in row:
            if data.startswith("app:"):
                line.append({"text": text, "web_app": {"url": data[4:]}})
            elif data.startswith("url:"):
                line.append({"text": text, "url": data[4:]})
            else:
                line.append({"text": text, "callback_data": data[:64]})
        out.append(line)
    return {"inline_keyboard": out}


def _send(chat: int, text: str, rows: list | None = None) -> None:
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if rows:
        payload["reply_markup"] = _kb(rows)
    notify.call("sendMessage", payload)


def _edit(chat: int, message_id: int, text: str, rows: list | None = None) -> None:
    payload = {"chat_id": chat, "message_id": message_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True, "reply_markup": _kb(rows or [])}
    res = notify.call("editMessageText", payload)
    if not res.get("ok") and "not modified" not in str(res.get("description", "")):
        _send(chat, text, rows)


def _back(lang: str) -> list:
    return [(_t(lang, "b_back"), "a:menu")]


# ---------------------------------------------------------------------------
#  Screens: (text, keyboard rows)
# ---------------------------------------------------------------------------
def _overview(db: Session) -> dict:
    from .routers import admin_api
    return admin_api.overview(db=db, _=None)


def screen_menu(db: Session, lang: str) -> tuple[str, list]:
    o = _overview(db)
    summary = _t(lang, "summary", users=o["users"], active=o["active"],
                 today=_money(o["revenue_today"]), month=_money(o["revenue_month"]))
    rows = [[(_t(lang, "b_stats"), "a:stats"), (_t(lang, "b_servers"), "a:srv")],
            [(_t(lang, "b_orders"), "a:ord"), (_t(lang, "b_trial"), "a:trial")],
            [(_t(lang, "b_promo"), "a:promo"), (_t(lang, "b_sale"), "a:sale")],
            [(_t(lang, "b_cast"), "a:casthelp")]]
    if env.https:
        rows.append([(_t(lang, "b_panel"), f"app:{env.public_url}/admin")])
    from . import support
    waiting = support.waiting(db)
    if waiting:
        summary += "\n" + _t(lang, "sup_waiting", n=waiting)
    return _t(lang, "menu", brand=E(core.get(db, "brand_name")), summary=summary), rows


def screen_stats(db: Session, lang: str) -> tuple[str, list]:
    o = _overview(db)
    text = _t(lang, "stats", users=o["users"], new_today=o["new_today"], active=o["active"],
              today=_money(o["revenue_today"]), month=_money(o["revenue_month"]),
              orders=o["orders_month"], avg=_money(o["avg_check"]),
              traffic=_gb(o["traffic_bytes"]))
    if o["stuck_orders"]:
        text += _t(lang, "stuck", n=o["stuck_orders"])
    if o["sync_errors"]:
        text += _t(lang, "sync_err", n=o["sync_errors"])
    if o["problems"]:
        text += _t(lang, "problems", list="\n".join("— " + E(p["text"]) for p in o["problems"]))
    return text, [[(_t(lang, "b_report"), "a:rep")],
                  [(_t(lang, "b_servers"), "a:srv"), (_t(lang, "b_orders"), "a:ord")], _back(lang)]


def screen_report(db: Session, lang: str) -> tuple[str, list]:
    """The day's results (the same as the evening message), on demand."""
    from . import report
    return report.text(db, report.collect(db), lang), [[(_t(lang, "b_stats"), "a:stats")], _back(lang)]


def screen_servers(db: Session, lang: str) -> tuple[str, list]:
    rows_db = access.servers(db, include_disabled=True)
    if not rows_db:
        return _t(lang, "srv_none"), [_back(lang)]
    lines, kb = [], []
    for s in rows_db:
        if not s.enabled:
            state = _t(lang, "srv_off")
        elif s.last_error:
            state = _t(lang, "srv_err", e=E(s.last_error[:120]))
        else:
            state = _t(lang, "srv_ok")
        flag = access.flag(s.country)
        lines.append(_t(lang, "srv_line", flag=(flag + " ") if flag else "", name=E(s.name),
                        state=state, url=E(s.url), ver=E(panel_label(s.kind, s.api_version)),
                        n=s.clients_count or 0))
        kb.append([(_t(lang, "b_check", name=s.name[:20]), f"a:sc:{s.id}"),
                   (_t(lang, "b_off" if s.enabled else "b_on", name=s.name[:20]), f"a:st:{s.id}")])
    kb.append(_back(lang))
    return _t(lang, "servers", list="\n\n".join(lines)), kb


def screen_orders(db: Session, lang: str) -> tuple[str, list]:
    stuck = (db.query(Transaction).filter(Transaction.status == "paid")
             .order_by(Transaction.id).limit(8).all())
    last = (db.query(Transaction).filter(Transaction.status == "success", Transaction.amount > 0)
            .order_by(Transaction.paid_at.desc()).limit(8).all())

    def line(tx: Transaction) -> str:
        who = f"@{tx.user.username}" if tx.user and tx.user.username else (tx.user.tg_id if tx.user else "?")
        return (f"#{tx.id} · {_money(tx.amount)} · {E(tx.kind)} · {E(str(who))} · "
                f"{E(tx.gateway or tx.method)} · {_date(tx.paid_at or tx.created_at, lang)}")

    text = _t(lang, "orders")
    kb = []
    if stuck:
        text += _t(lang, "orders_stuck", list="\n".join(
            line(t) + (f"\n   {E(t.error[:100])}" if t.error else "") for t in stuck))
        kb += [[(_t(lang, "b_grant", id=t.id), f"a:og:{t.id}")] for t in stuck]
    text += _t(lang, "orders_last", list="\n".join(line(t) for t in last)) if last \
        else _t(lang, "orders_none")
    kb.append(_back(lang))
    return text, kb


def screen_trial(db: Session, lang: str) -> tuple[str, list]:
    from .routers import admin_api
    info = admin_api.get_trial(db=db, _=None)
    state = _t(lang, "trial_on", days=info["days"], dev=info["devices"] or _t(lang, "unlimited"),
               gb=(f"{info['traffic_gb']} GB" if info["traffic_gb"] else _t(lang, "unlimited"))) \
        if info["enabled"] else _t(lang, "trial_off")
    kb = [[("1", "a:tr:1"), ("3", "a:tr:3"), ("7", "a:tr:7"), ("14", "a:tr:14")],
          [(_t(lang, "b_trial_off" if info["enabled"] else "b_trial_on"),
            "a:tr:off" if info["enabled"] else "a:tr:on")], _back(lang)]
    return _t(lang, "trial", state=state, used=info["used"], week=info["used_week"]), kb


def screen_sale(db: Session, lang: str) -> tuple[str, list]:
    from . import marketing
    s = marketing.sale(db)
    state = _t(lang, "sale_on", p=s["percent"],
               until=_t(lang, "sale_until", date=_date(s["until"], lang)) if s["until"] else "") \
        if s["percent"] else _t(lang, "sale_off")
    kb = [[(f"−{p}%", f"a:sl:{p}") for p in (10, 20, 30, 50)],
          [(_t(lang, "b_trial_off"), "a:sl:0")], _back(lang)]
    return _t(lang, "sale", state=state), kb


def screen_emergency(db: Session, lang: str) -> tuple[str, list]:
    import html
    from . import emergency
    st = emergency.state(db)
    if st["on"]:
        state = _t(lang, "em_on", date=_date(st["since"], lang) if st["since"] else "—",
                   text=html.escape(emergency.message(db, lang)),
                   sales=_t(lang, "em_sales_off" if st["stop_sales"] else "em_sales_on"),
                   keep=_t(lang, "em_keep_on" if st["keep_access"] else "em_keep_off"))
    else:
        state = _t(lang, "em_off")
    return _t(lang, "em", state=state), [_back(lang)]


def _promo_what(p: Promo, lang: str) -> str:
    v = f"{p.value:g}"
    return _t(lang, {"percent": "p_percent", "amount": "p_amount", "days": "p_days"}[p.kind],
              v=_money(p.value) if p.kind == "amount" else v)


def screen_promos(db: Session, lang: str) -> tuple[str, list]:
    rows = db.query(Promo).order_by(Promo.id.desc()).limit(20).all()
    lines = [_t(lang, "promo_line", code=E(p.code), what=_promo_what(p, lang), used=p.used,
                limit=f"/{p.limit}" if p.limit else "", off="" if p.enabled else " · off")
             for p in rows]
    return _t(lang, "promos", list="\n".join(lines) or _t(lang, "promo_none")), [_back(lang)]


def screen_user(db: Session, lang: str, user: User) -> tuple[str, list]:
    t = now()
    from . import access
    grace = access.grace_until(db, user)
    if user.expires_at and user.expires_at > t:
        state = _t(lang, "u_active", date=_date(user.expires_at, lang))
    elif grace:
        state = _t(lang, "u_grace", date=_date(user.expires_at, lang), until=_date(grace, lang))
    elif user.expires_at:
        state = _t(lang, "u_expired", date=_date(user.expires_at, lang))
    else:
        state = _t(lang, "u_never")
    if user.is_blocked:
        state += _t(lang, "u_blocked")
    plan = user.plan
    devs = len(access.devices(db, user))
    spent = sum(x.amount for x in db.query(Transaction).filter(
        Transaction.user_id == user.id, Transaction.status == "success").all())
    traffic = (f"{_gb(user.traffic_used)} / {_gb(user.traffic_limit)} GB" if user.traffic_limit
               else f"{_gb(user.traffic_used)} GB · {_t(lang, 'u_nolimit')}")
    text = _t(lang, "user", name=E(user.first_name or "—"),
              uname=f" · @{E(user.username)}" if user.username else "", tg=user.tg_id, state=state,
              plan=E((plan.name if plan else "") or "—"),
              dev=f"{devs} / {user.device_limit or _t(lang, 'unlimited')}", traffic=traffic,
              spent=_money(spent), since=_date(user.created_at, lang))
    if user.bypass_limit or user.bypass_used or access.bypass_servers(db):
        text += "\n" + _t(lang, "u_bypass", used=_gb(user.bypass_used or 0), limit=_gb(user.bypass_limit or 0))
    g = user.tg_id
    kb = [[(_t(lang, "b_days", n=n), f"a:ux:{g}:{n}") for n in (3, 7, 30)],
          [(_t(lang, "b_unblock" if user.is_blocked else "b_block"), f"a:ub:{g}"),
           (_t(lang, "b_sync"), f"a:us:{g}")]]
    if user.traffic_limit:
        kb.append([(_t(lang, "b_reset"), f"a:ur:{g}")])
    kb.append(_back(lang))
    return text, kb


def find_user(db: Session, query: str) -> User | None:
    q = (query or "").strip().lstrip("@")
    if not q:
        return None
    if q.isdigit():
        return db.query(User).filter(User.tg_id == int(q)).first()
    return (db.query(User).filter(or_(User.username.ilike(q), User.username.ilike(q.lower())))
            .first())


# ---------------------------------------------------------------------------
#  Commands
# ---------------------------------------------------------------------------
def handle_command(db: Session, msg: dict) -> bool:
    """A message from an admin. Returns True if it was an admin command."""
    sender = msg.get("from") or {}
    chat = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    m = re.match(r"^/([a-z_]+)(?:@\w+)?(?:\s+(.*))?$", text, re.S | re.I)
    if not m or not is_admin(sender.get("id")) or (msg.get("chat") or {}).get("type") != "private":
        return False
    cmd, arg = m.group(1).lower(), (m.group(2) or "").strip()
    if cmd not in COMMANDS:
        return False
    lang = _lang(sender)

    if cmd in ("admin", "panel"):
        _send(chat, *screen_menu(db, lang))
    elif cmd == "help":
        _send(chat, _t(lang, "help"), [_back(lang)])
    elif cmd == "stats":
        _send(chat, *screen_stats(db, lang))
    elif cmd in ("report", "day"):
        _send(chat, *screen_report(db, lang))
    elif cmd == "servers":
        _send(chat, *screen_servers(db, lang))
    elif cmd == "orders":
        _send(chat, *screen_orders(db, lang))
    elif cmd == "user":
        if not arg:
            _send(chat, _t(lang, "u_usage"))
        else:
            user = find_user(db, arg)
            _send(chat, *screen_user(db, lang, user)) if user else _send(chat, _t(lang, "u_notfound"))
    elif cmd == "give":
        parts = arg.split()
        if len(parts) != 2 or not re.fullmatch(r"-?\d{1,4}", parts[1]) or int(parts[1]) == 0:
            _send(chat, _t(lang, "give_usage"))
        else:
            user = find_user(db, parts[0])
            if not user:
                _send(chat, _t(lang, "u_notfound"))
            else:
                res = _user_action(db, sender, user, "extend", days=int(parts[1]))
                db.refresh(user)
                text, kb = screen_user(db, lang, user)
                _send(chat, _sync_note(lang, res) + "\n\n" + text, kb)
    elif cmd == "trial":
        if arg:
            a = arg.lower()
            info = billing.trial_info(db)
            if a in ("off", "0", "выкл", "нет"):
                billing.set_trial(db, enabled=False, days=info["days"], devices=info["devices"],
                                  traffic_gb=info["traffic_gb"])
            elif a in ("on", "вкл", "да"):
                billing.set_trial(db, enabled=True, days=info["days"], devices=info["devices"],
                                  traffic_gb=info["traffic_gb"])
            elif a.isdigit() and 1 <= int(a) <= 90:
                billing.set_trial(db, enabled=True, days=int(a), devices=info["devices"],
                                  traffic_gb=info["traffic_gb"])
            else:
                a = ""              # not understood: nothing changed, the screen shows how
            if a:
                core.audit(db, "Trial changed", _actor(sender), f"bot: {arg[:20]}")
        _send(chat, *screen_trial(db, lang))
    elif cmd == "promo":
        _promo_command(db, chat, sender, lang, arg)
    elif cmd == "backup":
        from . import backup
        path = backup.make(db)
        if not backup.send(db, path, [chat]):
            _send(chat, _t(lang, "backup_fail"))
        core.audit(db, "Backup on demand", _actor(sender), path.name)
    elif cmd == "diag":
        from . import diagnostics
        _send(chat, _t(lang, "diag_wait"))
        _send(chat, diagnostics.text_report(diagnostics.run(db), lang), [_back(lang)])
    elif cmd == "emergency":
        from . import emergency
        parts = arg.split(None, 1)
        word = parts[0].lower() if parts else ""
        if word in ("on", "вкл", "да"):
            text = parts[1].strip() if len(parts) > 1 else None
            emergency.update(db, _actor(sender), on=True,
                             **({"text_ru": text, "text_en": text} if text else {}))
        elif word in ("off", "выкл", "нет", "0"):
            emergency.update(db, _actor(sender), on=False)
        _send(chat, *screen_emergency(db, lang))
    elif cmd == "sale":
        from . import marketing
        parts = arg.lower().split()
        if parts and parts[0] in ("off", "0", "выкл", "стоп"):
            marketing.set_sale(db, 0)
            core.audit(db, "Sale changed", _actor(sender), "bot: off")
        elif parts and parts[0].rstrip("%").isdigit():
            days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            marketing.set_sale(db, int(parts[0].rstrip("%")), days)
            core.audit(db, "Sale changed", _actor(sender), f"bot: {arg[:20]}")
        _send(chat, *screen_sale(db, lang))
    elif cmd == "broadcast":
        # Bold, links and the rest come as Telegram "entities" next to plain
        # text; the broadcast goes out as HTML, so they are turned into tags.
        body = arg
        if arg and _formatted(msg.get("entities")):
            full = entities_html(msg.get("text") or "", msg.get("entities"))
            body = re.sub(r"^\s*/broadcast(?:@\w+)?\s*", "", full, flags=re.I)
        reply = msg.get("reply_to_message") or {}
        if not body and reply:
            raw = reply.get("text") or reply.get("caption") or ""
            ents = reply.get("entities") or reply.get("caption_entities")
            body = (entities_html(raw, ents) if _formatted(ents) else raw).strip()
        _broadcast_draft(db, chat, sender, lang, body)
    return True


_TAGS = {"bold": "b", "italic": "i", "underline": "u", "strikethrough": "s", "spoiler": "tg-spoiler",
         "code": "code", "pre": "pre", "blockquote": "blockquote", "expandable_blockquote": "blockquote"}


def _formatted(entities) -> bool:
    return any(isinstance(e, dict) and (e.get("type") in _TAGS or e.get("type") in ("text_link", "text_mention"))
               for e in entities or [])


def entities_html(text: str, entities) -> str:
    """Telegram's text + entities (offsets in UTF-16 units) as HTML."""
    import html as _html
    units = (text or "").encode("utf-16-le")
    size = len(units) // 2
    opens: dict[int, list[tuple[int, str, str]]] = {}
    closes: dict[int, list[str]] = {}
    for e in entities or []:
        if not isinstance(e, dict):
            continue
        kind, start, length = e.get("type"), int(e.get("offset") or 0), int(e.get("length") or 0)
        if length <= 0 or start < 0 or start + length > size:
            continue
        if kind in _TAGS:
            tag = _TAGS[kind]
            head = f"<{tag} expandable>" if kind == "expandable_blockquote" else f"<{tag}>"
        elif kind == "text_link" and str(e.get("url", "")).startswith(("http://", "https://", "tg://")):
            tag, head = "a", f'<a href="{_html.escape(e["url"], quote=True)}">'
        elif kind == "text_mention" and (e.get("user") or {}).get("id"):
            tag, head = "a", f'<a href="tg://user?id={int(e["user"]["id"])}">'
        else:
            continue
        opens.setdefault(start, []).append((length, head, tag))
        closes.setdefault(start + length, [])
    # Entities nest: at each point the inner ones close first, the outer open first.
    stack: list[tuple[int, str]] = []
    out = []
    points = sorted({0, size, *opens, *closes})
    for i, p in enumerate(points):
        while stack and stack[-1][0] <= p:
            out.append(f"</{stack.pop()[1]}>")
        for length, head, tag in sorted(opens.get(p, []), key=lambda x: -x[0]):
            out.append(head)
            stack.append((p + length, tag))
        if i + 1 < len(points):
            out.append(_html.escape(units[p * 2:points[i + 1] * 2].decode("utf-16-le", "replace"), quote=False))
    while stack:
        out.append(f"</{stack.pop()[1]}>")
    return "".join(out)


def _sync_note(lang: str, res: dict) -> str:
    sync = res.get("sync") or {}
    return _t(lang, "done") + (" · " + _t(lang, "sync_res", ok=sync.get("ok", 0),
                                          failed=sync.get("failed", 0)) if sync else "")


def _user_action(db: Session, sender: dict, user: User, action: str, **kw) -> dict:
    from .routers import admin_api
    return admin_api.user_action(user.tg_id, admin_api.UserAction(action=action, **kw),
                                 db=db, admin=_actor(sender))


PROMO_RE = re.compile(r"^([A-Za-z0-9_-]{2,32})\s+(\d+(?:[.,]\d+)?)\s*(%|d|д|дн)?(?:\s+(\d+))?$", re.I)


def _promo_command(db: Session, chat: int, sender: dict, lang: str, arg: str) -> None:
    from fastapi import HTTPException

    from .routers import admin_api
    if not arg:
        _send(chat, *screen_promos(db, lang))
        return
    parts = arg.split()
    if parts[0].lower() in ("del", "delete", "удалить") and len(parts) == 2:
        p = db.query(Promo).filter(Promo.code == parts[1].upper()).first()
        if not p:
            _send(chat, _t(lang, "promo_missing"))
            return
        admin_api.delete_promo(p.id, db=db, admin=_actor(sender))
        _send(chat, _t(lang, "promo_deleted", code=E(p.code)))
        return
    m = PROMO_RE.match(arg)
    if not m:
        _send(chat, _t(lang, "promo_bad"))
        return
    code, value, unit, limit = m.group(1).upper(), float(m.group(2).replace(",", ".")), \
        (m.group(3) or "").lower(), int(m.group(4) or 0)
    kind = "percent" if unit == "%" else "days" if unit in ("d", "д", "дн") else "amount"
    if kind == "percent" and not 0 < value <= 100:
        _send(chat, _t(lang, "promo_bad"))
        return
    try:
        out = admin_api.create_promo(admin_api.PromoBody(code=code, kind=kind, value=value, limit=limit),
                                     db=db, admin=_actor(sender))
    except HTTPException:
        _send(chat, _t(lang, "promo_exists"))
        return
    p = db.get(Promo, out["id"])
    _send(chat, _t(lang, "promo_made", code=E(code), what=_promo_what(p, lang)
                   + (f" · {limit}" if limit else "")), [_back(lang)])


def _broadcast_draft(db: Session, chat: int, sender: dict, lang: str, body: str) -> None:
    from .routers import admin_api
    if not body:
        _send(chat, _t(lang, "cast_usage"))
        return
    cast = Broadcast(text=body[:4000], audience="all", button_text="1", status="draft")
    db.add(cast)
    db.commit()
    counts = {a: admin_api.audience_query(db, a).count() for a in ("all", "active", "inactive", "never")}
    kb = [[(_t(lang, "a_all", n=counts["all"]), f"a:bc:{cast.id}:all")],
          [(_t(lang, "a_active", n=counts["active"]), f"a:bc:{cast.id}:active")],
          [(_t(lang, "a_inactive", n=counts["inactive"]), f"a:bc:{cast.id}:inactive")],
          [(_t(lang, "a_never", n=counts["never"]), f"a:bc:{cast.id}:never")],
          [(_t(lang, "b_cancel"), f"a:bx:{cast.id}")]]
    _send(chat, _t(lang, "cast_preview", text=body[:3000]), kb)


# ---------------------------------------------------------------------------
#  Buttons
# ---------------------------------------------------------------------------
def handle_callback(db: Session, cq: dict) -> None:
    sender = cq.get("from") or {}
    data = str(cq.get("data") or "")
    msg = cq.get("message") or {}
    chat, mid = (msg.get("chat") or {}).get("id"), msg.get("message_id")
    allowed = is_admin(sender.get("id")) or (data.startswith("a:uc:") and _helps(db, sender.get("id")))
    if not allowed or not data.startswith("a:"):
        notify.call("answerCallbackQuery", {"callback_query_id": cq.get("id")})
        return
    lang = _lang(sender)
    parts = data.split(":")
    act = parts[1] if len(parts) > 1 else ""
    note = ""
    screen = None
    try:
        if act == "menu":
            screen = screen_menu(db, lang)
        elif act == "stats":
            screen = screen_stats(db, lang)
        elif act == "rep":
            screen = screen_report(db, lang)
        elif act == "srv":
            screen = screen_servers(db, lang)
        elif act == "ord":
            screen = screen_orders(db, lang)
        elif act == "trial":
            screen = screen_trial(db, lang)
        elif act == "promo":
            screen = screen_promos(db, lang)
        elif act == "sale":
            screen = screen_sale(db, lang)
        elif act == "sl":
            from . import marketing
            marketing.set_sale(db, int(parts[2]), 0)
            core.audit(db, "Sale changed", _actor(sender), f"bot: {parts[2]}%")
            note, screen = _t(lang, "done"), screen_sale(db, lang)
        elif act == "casthelp":
            screen = (_t(lang, "cast_usage"), [_back(lang)])
        elif act == "sc":                                   # check a server
            note, screen = _check_server(db, lang, int(parts[2])), screen_servers(db, lang)
        elif act == "st":                                   # server on/off
            s = db.get(Server, int(parts[2]))
            if s:
                s.enabled = not s.enabled
                db.commit()
                core.audit(db, "Server " + ("enabled" if s.enabled else "disabled"), _actor(sender), s.name)
                note = _t(lang, "done")
            screen = screen_servers(db, lang)
        elif act == "og":                                   # grant a paid order
            note = _grant(db, lang, sender, int(parts[2]))
            screen = screen_orders(db, lang)
        elif act == "tr":                                   # trial
            info = billing.trial_info(db)
            v = parts[2]
            enabled = v != "off" if v in ("on", "off") else True
            days = int(v) if v.isdigit() else info["days"]
            billing.set_trial(db, enabled=enabled, days=days, devices=info["devices"],
                              traffic_gb=info["traffic_gb"])
            core.audit(db, "Trial changed", _actor(sender), f"bot: {v}")
            note, screen = _t(lang, "done"), screen_trial(db, lang)
        elif act in ("ux", "ub", "us", "ur"):               # customer actions
            user = db.query(User).filter(User.tg_id == int(parts[2])).first()
            if user:
                if act == "ux":
                    res = _user_action(db, sender, user, "extend", days=int(parts[3]))
                elif act == "ub":
                    res = _user_action(db, sender, user, "unblock" if user.is_blocked else "block")
                elif act == "ur":
                    res = _user_action(db, sender, user, "reset_traffic")
                else:
                    res = _user_action(db, sender, user, "sync")
                db.refresh(user)
                note, screen = _sync_note(lang, res), screen_user(db, lang, user)
        elif act == "uc":                                   # customer card from a support message
            user = db.query(User).filter(User.tg_id == int(parts[2])).first()
            if not user:
                note = _t(lang, "u_gone")
            elif chat:
                text, rows = screen_user(db, lang, user)
                res = notify.call("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                                  "disable_web_page_preview": True, "reply_markup": _kb(rows)})
                if not res.get("ok"):
                    note = ("Ошибка / Error: " + str(res.get("description") or "Telegram"))[:190]
        elif act == "upd":                                  # update to the new version (owners)
            from . import team, updates
            if not team.is_owner_tg(sender.get("id")):
                note = core.t(lang, "upd_owner_only")
            else:
                try:
                    updates.request(db, _actor(sender))
                    note = core.t(lang, "upd_started")
                    if chat:
                        _send(chat, core.t(lang, "upd_started"))
                except ValueError as e:
                    note = core.t(lang, "upd_running" if str(e) == "running" else "upd_no_updater")
        elif act == "bc":                                   # start a drafted broadcast
            note, screen = _start_broadcast(db, lang, sender, int(parts[2]), parts[3])
        elif act == "po":                                   # a withdrawal request: paid / declined
            note = _payout_decision(db, lang, sender, int(parts[2]), parts[3] == "ok", chat, mid, msg)
        elif act == "bx":
            cast = db.get(Broadcast, int(parts[2]))
            if cast and cast.status == "draft":
                cast.status = "cancelled"
                db.commit()
            screen = (_t(lang, "cast_cancelled"), [_back(lang)])
    except (ValueError, IndexError):
        pass
    notify.call("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": note[:190]})
    if screen and chat and mid:
        _edit(chat, mid, *screen)


def payout_notice(db: Session, p) -> None:
    """A new withdrawal request to every admin, with the two buttons."""
    lang = env.default_lang
    u = p.user or db.get(User, p.user_id)
    name = E(u.first_name or "") + (f" @{E(u.username)}" if u and u.username else "")
    text = core.t(lang, "payout_admin", id=p.id, name=name or "—", tg=u.tg_id if u else "?",
                  amount=E(core.money(p.amount, lang)), method=E(p.method_title), details=E(p.details))
    rows = [[(_t(lang, "b_po_ok"), f"a:po:{p.id}:ok"), (_t(lang, "b_po_no"), f"a:po:{p.id}:no")]]
    for tg_id in env.admin_telegram_ids:
        _send(tg_id, text, rows)


def _payout_decision(db: Session, lang: str, sender: dict, payout_id: int, paid: bool,
                     chat, mid, msg: dict) -> str:
    from . import referral
    p = referral.decide(db, payout_id, paid, _actor(sender))
    if p is None:
        return _t(lang, "po_gone")
    who = E("@" + sender["username"] if sender.get("username") else str(sender.get("id")))
    if chat and mid:
        # The same message, without the buttons, with the decision under it.
        base = E(msg.get("text") or "")
        _edit(chat, mid, base + _t(lang, "po_paid" if paid else "po_rejected", who=who))
    return _t(lang, "done")


def _check_server(db: Session, lang: str, server_id: int) -> str:
    from .routers import admin_api
    s = db.get(Server, server_id)
    if not s:
        return ""
    res = admin_api.check_server(server_id, db=db, _=None)
    if res.get("ok"):
        return _t(lang, "checked_ok", name=s.name, ver=panel_label(s.kind, res.get("version", "")),
                  n=len(res.get("inbounds", [])))
    return _t(lang, "checked_bad", name=s.name, e=res.get("error", "")[:150])


def _grant(db: Session, lang: str, sender: dict, order_id: int) -> str:
    from fastapi import HTTPException

    from .routers import admin_api
    try:
        admin_api.grant_order(order_id, db=db, admin=_actor(sender))
    except HTTPException as e:
        return _t(lang, "grant_fail", id=order_id, e=str(e.detail)[:120])
    return _t(lang, "granted", id=order_id)


def _start_broadcast(db: Session, lang: str, sender: dict, cast_id: int, audience: str):
    from .routers import admin_api
    cast = db.get(Broadcast, cast_id)
    if not cast or cast.status != "draft" or audience not in admin_api.AUDIENCES:
        return "", (_t(lang, "cast_gone"), [_back(lang)])
    cast.audience = audience
    cast.total = admin_api.audience_query(db, audience).count()
    cast.status = "sending"
    db.commit()
    core.audit(db, "Broadcast started", _actor(sender), f"bot, {audience}: {cast.total}")
    return _t(lang, "done"), (_t(lang, "cast_started", n=cast.total), [_back(lang)])


# ---------------------------------------------------------------------------
def setup_commands(lang_default: str) -> None:
    """Admin commands in the bot's menu — only in the admins' own chats."""
    for tg_id in env.admin_telegram_ids:
        for lang in ("ru", "en"):
            cmds = [{"command": c, "description": d} for c, d in (
                ("admin", "Меню управления" if lang == "ru" else "Admin menu"),
                ("stats", "Статистика" if lang == "ru" else "Stats"),
                ("report", "Итоги за сутки" if lang == "ru" else "The last 24 hours"),
                ("user", "Клиент по ID или @username" if lang == "ru" else "Customer by ID or @username"),
                ("give", "Добавить дни клиенту" if lang == "ru" else "Add days to a customer"),
                ("servers", "Серверы" if lang == "ru" else "Servers"),
                ("orders", "Заказы" if lang == "ru" else "Orders"),
                ("trial", "Пробный период" if lang == "ru" else "Free trial"),
                ("promo", "Промокоды" if lang == "ru" else "Promo codes"),
                ("broadcast", "Рассылка" if lang == "ru" else "Broadcast"),
                ("sale", "Распродажа" if lang == "ru" else "Sale"),
                ("backup", "Резервная копия базы" if lang == "ru" else "Database backup"),
                ("diag", "Диагностика" if lang == "ru" else "Diagnostics"),
                ("emergency", "Режим аварии" if lang == "ru" else "Emergency mode"),
                ("start", "Открыть приложение" if lang == "ru" else "Open the app"),
            )]
            payload = {"commands": cmds, "scope": {"type": "chat", "chat_id": tg_id}}
            if lang != lang_default:
                payload["language_code"] = lang
            notify.call("setMyCommands", payload)
