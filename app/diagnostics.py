"""
Diagnostics: everything the shop depends on, checked for real, on demand.

Each check asks the thing itself — the database answers a query, Telegram
answers getMe and getWebhookInfo, the site's certificate is read over TLS,
every panel is logged into. Nothing is guessed from settings alone, and a
check that could not run says so instead of showing "OK".

Result of one check:
    {"id", "status": ok | warn | fail | off, "title", "detail", "fix"}
title/detail/fix are {"ru", "en"}. "off" — not used here (not an error).
"""

from __future__ import annotations

import shutil
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import access, core, emergency, notify, payments
from .config import settings as env
from .models import Server, Transaction, User, now

LAST: dict = {}
_lock = threading.Lock()


def _c(cid: str, status: str, title: tuple[str, str], detail: tuple[str, str] = ("", ""),
       fix: tuple[str, str] = ("", "")) -> dict:
    return {"id": cid, "status": status, "title": {"ru": title[0], "en": title[1]},
            "detail": {"ru": detail[0], "en": detail[1]}, "fix": {"ru": fix[0], "en": fix[1]}}


def _ago(ts: int) -> tuple[str, str]:
    s = max(0, now() - int(ts))
    if s < 90:
        return f"{s} с назад", f"{s} s ago"
    if s < 5400:
        return f"{s // 60} мин назад", f"{s // 60} min ago"
    if s < 172800:
        return f"{s // 3600} ч назад", f"{s // 3600} h ago"
    return f"{s // 86400} дн. назад", f"{s // 86400} d ago"


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


# ---------------------------------------------------------------------------
#  Checks
# ---------------------------------------------------------------------------
def check_emergency(db: Session) -> dict:
    t = ("Режим аварии", "Emergency mode")
    if not emergency.is_on(db):
        return _c("emergency", "ok", t, ("Выключен.", "Off."))
    st = emergency.state(db)
    parts_ru = [f"Включён с {_date(st['since'])}." if st["since"] else "Включён."]
    parts_en = [f"On since {_date(st['since'])}." if st["since"] else "On."]
    if st["stop_sales"]:
        parts_ru.append("Продажи остановлены."); parts_en.append("Sales are stopped.")
    if st["keep_access"]:
        parts_ru.append("Никого не отключаем."); parts_en.append("Nobody is cut off.")
    return _c("emergency", "warn", t, (" ".join(parts_ru), " ".join(parts_en)),
              ("Когда всё починится, выключите режим.", "Turn it off once everything works again."))


def check_db(db: Session) -> dict:
    t = ("База данных", "Database")
    started = time.time()
    try:
        db.execute(text("SELECT 1"))
        users = db.query(User).count()
    except Exception as e:  # noqa: BLE001
        return _c("db", "fail", t, (f"Не отвечает: {e}", f"Does not answer: {e}"),
                  ("Проверьте DATABASE_URL и что база запущена.", "Check DATABASE_URL and that the database runs."))
    ms = int((time.time() - started) * 1000)
    kind = "PostgreSQL" if env.database_url.startswith("postgres") else "SQLite"
    status = "warn" if ms > 1500 else "ok"
    return _c("db", status, t, (f"{kind}, ответ за {ms} мс, клиентов: {users}.",
                                f"{kind}, answered in {ms} ms, customers: {users}."),
              ("Ответ медленный — проверьте диск и нагрузку сервера.", "Slow — check the disk and the server load.")
              if status == "warn" else ("", ""))


def check_bot() -> dict:
    t = ("Бот Telegram", "Telegram bot")
    if not env.bot_token:
        return _c("bot", "fail", t, ("BOT_TOKEN не задан.", "BOT_TOKEN is not set."),
                  ("Возьмите токен у @BotFather и пропишите в .env.", "Get a token from @BotFather and put it in .env."))
    res = notify.call("getMe", timeout=8)
    if not res.get("ok"):
        d = str(res.get("description") or "no answer")
        return _c("bot", "fail", t, (f"Telegram не принял токен: {d}", f"Telegram refused the token: {d}"),
                  ("Проверьте BOT_TOKEN; если сервер в России — доступ к api.telegram.org.",
                   "Check BOT_TOKEN and that the server reaches api.telegram.org."))
    name = (res.get("result") or {}).get("username", "")
    return _c("bot", "ok", t, (f"@{name} отвечает.", f"@{name} answers."))


def check_webhook() -> dict:
    t = ("Вебхук бота", "Bot webhook")
    if not env.bot_token:
        return _c("webhook", "off", t, ("Нет токена бота.", "No bot token."))
    res = notify.call("getWebhookInfo", timeout=8)
    if not res.get("ok"):
        d = str(res.get("description") or "no answer")
        return _c("webhook", "fail", t, (f"Не удалось спросить Telegram: {d}", f"Could not ask Telegram: {d}"))
    info = res.get("result")
    info = info if isinstance(info, dict) else {}
    want = f"{env.public_url}/api/bot/webhook"
    url = info.get("url") or ""
    if url != want:
        return _c("webhook", "fail", t,
                  (f"Telegram шлёт сообщения на «{url or 'никуда'}», а нужно «{want}».",
                   f"Telegram sends updates to “{url or 'nowhere'}”, it should be “{want}”."),
                  ("Нажмите «Настроить бота заново» в Настройках.", "Press “Set up the bot again” in Settings."))
    pending = int(info.get("pending_update_count") or 0)
    err_at = int(info.get("last_error_date") or 0)
    if err_at and now() - err_at < 3600:
        e = str(info.get("last_error_message") or "")
        return _c("webhook", "fail" if pending > 20 else "warn", t,
                  (f"Последняя ошибка доставки {_ago(err_at)[0]}: {e}. В очереди: {pending}.",
                   f"Last delivery error {_ago(err_at)[1]}: {e}. Queued: {pending}."),
                  ("Telegram не может достучаться до сайта: проверьте HTTPS и что сервис запущен.",
                   "Telegram cannot reach the site: check HTTPS and that the service runs."))
    if pending > 100:
        return _c("webhook", "warn", t, (f"В очереди {pending} сообщений.", f"{pending} updates queued."))
    return _c("webhook", "ok", t, (f"Настроен, в очереди: {pending}.", f"Set, queued: {pending}."))


def _cert_expiry(host: str, port: int = 443) -> int:
    """The certificate's end, read from the site itself (verified as a browser would)."""
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=6) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            cert = tls.getpeercert()
    return int(ssl.cert_time_to_seconds(cert["notAfter"]))


def check_https() -> dict:
    t = ("Адрес и сертификат", "Address and certificate")
    if not env.https:
        return _c("https", "fail", t, (f"PUBLIC_URL = {env.public_url} — не https.",
                                        f"PUBLIC_URL = {env.public_url} — not https."),
                  ("Telegram открывает мини-приложения только по https. Нужен домен или туннель.",
                   "Telegram opens mini apps only over https. Use a domain or a tunnel."))
    u = urlsplit(env.public_url)
    try:
        end = _cert_expiry(u.hostname or "", u.port or 443)
    except ssl.SSLCertVerificationError as e:
        return _c("https", "fail", t, (f"Сертификат не проходит проверку: {e.verify_message}",
                                        f"The certificate fails verification: {e.verify_message}"),
                  ("Перевыпустите сертификат (Caddy делает это сам, если порты 80 и 443 открыты).",
                   "Renew the certificate (Caddy does it itself when ports 80 and 443 are open)."))
    except (OSError, ValueError, KeyError) as e:
        return _c("https", "fail", t, (f"Сайт {u.hostname} не отвечает по https: {e.__class__.__name__}",
                                        f"{u.hostname} does not answer over https: {e.__class__.__name__}"),
                  ("Проверьте, что домен указывает на этот сервер и порт 443 открыт.",
                   "Check that the domain points here and port 443 is open."))
    days = (end - now()) // 86400
    detail = (f"{u.hostname}, сертификат до {_date(end)} (осталось {days} дн.).",
              f"{u.hostname}, certificate until {_date(end)} ({days} days left).")
    if days < 7:
        return _c("https", "fail", t, detail, ("Сертификат вот-вот кончится — проверьте автопродление.",
                                               "The certificate is about to end — check auto-renewal."))
    if days < 20:
        return _c("https", "warn", t, detail, ("Обычно он продлевается сам за 30 дней — проверьте Caddy.",
                                               "It normally renews 30 days ahead — check Caddy."))
    return _c("https", "ok", t, detail)


def check_panel(server: Server, snap) -> dict:
    from .providers import panel_for
    t = (f"Панель: {server.name}", f"Panel: {server.name}")
    try:
        res = panel_for(snap, timeout=10).test()
    except Exception as e:  # noqa: BLE001 — reported as the check's result
        return _c(f"panel:{server.id}", "fail", t, (f"{server.url} — {e}", f"{server.url} — {e}"),
                  ("Откройте панель в браузере, проверьте логин, пароль или токен в «Серверах».",
                   "Open the panel in a browser; check the login, password or token in Servers."))
    clients = res.get("clients") if "clients" in res else sum(i.get("clients", 0) for i in res.get("inbounds", []))
    return _c(f"panel:{server.id}", "ok", t, (f"Отвечает, версия {res.get('version') or '?'}, клиентов: {clients}.",
                                               f"Answers, version {res.get('version') or '?'}, clients: {clients}."))


def check_vpn(db: Session) -> dict:
    t = ("VPN на серверах", "VPN on the servers")
    rows = access.servers(db)
    if not rows:
        return _c("vpn", "off", t, ("Серверов нет.", "No servers."))
    down = [s for s in rows if s.vpn_down_at]
    checked = [s for s in rows if s.endpoints or s.vpn_ok_at]
    if down:
        names = ", ".join(s.name for s in down)
        return _c("vpn", "fail", t, (f"Не отвечает VPN: {names}.", f"VPN does not answer: {names}."),
                  ("Панель может работать, а ядро VPN — нет. Перезапустите Xray на этих серверах.",
                   "The panel may work while the VPN core does not. Restart Xray on those servers."))
    if not checked:
        return _c("vpn", "warn", t, ("Ещё не проверялся: адреса узнаются, когда клиент обновит подписку.",
                                      "Not checked yet: addresses are learned when a customer refreshes."))
    return _c("vpn", "ok", t, (f"Отвечают все {len(checked)} из {len(rows)} проверяемых.",
                               f"All {len(checked)} of {len(rows)} checked answer."))


def check_russia(db: Session) -> dict:
    from . import probe
    t = ("Доступность из России", "Reachable from Russia")
    st = probe.status(db)
    if not st["probes"]:
        return _c("russia", "off", t, ("Проверяльщик в России не подключён.", "No checker inside Russia."),
                  ("Поставьте его на любой дешёвый российский сервер — раздел «Авария».",
                   "Put one on any cheap Russian VPS — see the Emergency section."))
    offline = [p["name"] for p in st["probes"] if not p["online"]]
    blocked = [f"{s['name']} ({c['probe']})" for s in st["servers"] for c in s["cells"] if c["state"] == "blocked"]
    partial = [f"{s['name']} ({c['probe']})" for s in st["servers"] for c in s["cells"] if c["state"] == "partial"]
    if blocked:
        return _c("russia", "fail", t, ("Недоступен: " + ", ".join(blocked[:6]), "Unreachable: " + ", ".join(blocked[:6])),
                  ("Смените IP или порт сервера, включите каскад через российский сервер.",
                   "Change the server's IP or port, or route it through a Russian relay."))
    if partial or offline:
        d_ru = ("Частично: " + ", ".join(partial[:6]) + ". " if partial else "") + \
               ("Молчит проверяльщик: " + ", ".join(offline) if offline else "")
        d_en = ("Partly: " + ", ".join(partial[:6]) + ". " if partial else "") + \
               ("Silent checker: " + ", ".join(offline) if offline else "")
        return _c("russia", "warn", t, (d_ru.strip(), d_en.strip()))
    names = ", ".join(p["name"] for p in st["probes"])
    return _c("russia", "ok", t, (f"Все серверы доступны ({names}).", f"Every server is reachable ({names})."))


def check_payments(db: Session) -> dict:
    t = ("Оплата", "Payments")
    ms = payments.methods()
    if not ms:
        return _c("payments", "fail", t, ("Не настроен ни один способ оплаты.", "No payment method is set up."),
                  ("Telegram Stars работают без настройки — проверьте, что они не выключены.",
                   "Telegram Stars work without setup — check they are not turned off."))
    names = ", ".join(sorted({m.get("gateway") or m.get("id") for m in ms}))
    stuck = db.query(Transaction).filter(Transaction.status == "paid").count()
    last = (db.query(Transaction.paid_at).filter(Transaction.status == "success", Transaction.amount > 0)
            .order_by(Transaction.paid_at.desc()).first())
    when = _ago(last[0]) if last and last[0] else ("не было", "none yet")
    detail = (f"Способы: {names}. Последняя оплата: {when[0]}.", f"Methods: {names}. Last payment: {when[1]}.")
    if stuck:
        return _c("payments", "fail", t,
                  (detail[0] + f" Оплачено, но не выдано: {stuck}.", detail[1] + f" Paid, not granted: {stuck}."),
                  ("Деньги пришли, а серверы не приняли клиента. Магазин повторяет сам; "
                   "если серверы в порядке — выдайте вручную в «Заказах».",
                   "Money arrived but no server took the customer. The shop retries; "
                   "if servers are fine, grant by hand in Orders."))
    if emergency.sales_stopped(db):
        return _c("payments", "warn", t, (detail[0] + " Продажи остановлены режимом аварии.",
                                          detail[1] + " Sales are stopped by the emergency mode."))
    return _c("payments", "ok", t, detail)


def check_sync(db: Session) -> dict:
    t = ("Клиенты на серверах", "Customers on the servers")
    n = db.query(User).filter(User.sync_error != "", User.expires_at > access.sync_floor(db)).count()
    if n:
        return _c("sync", "warn", t, (f"У {n} клиентов не все серверы приняли данные.",
                                      f"{n} customers were not accepted by every server."),
                  ("Магазин повторяет каждые 10 минут. Если число не уменьшается — смотрите панели.",
                   "The shop retries every 10 minutes. If the number stays, look at the panels."))
    return _c("sync", "ok", t, ("Все клиенты на месте.", "Every customer is in place."))


def check_jobs() -> dict:
    from . import jobs
    t = ("Фоновые задачи", "Background jobs")
    if not jobs.INTERVALS:
        return _c("jobs", "fail", t, ("Не запущены (SHOP_NO_JOBS).", "Not running (SHOP_NO_JOBS)."),
                  ("Без них не продлеваются оплаты, не идут бэкапы и рассылки. Уберите SHOP_NO_JOBS.",
                   "Without them delayed payments, backups and broadcasts stop. Remove SHOP_NO_JOBS."))
    t_now = now()
    late, failing = [], []
    for name, every in jobs.INTERVALS.items():
        st = jobs.STATE.get(name)
        if st and st["error"]:
            failing.append(f"{name}: {st['error'][:80]}")
        elif (not st and t_now - _STARTED > every * 2 + 120) or (st and t_now - st["at"] > every * 2 + 120):
            late.append(name)
    if failing:
        return _c("jobs", "warn", t, ("С ошибкой: " + "; ".join(failing[:4]), "Failing: " + "; ".join(failing[:4])),
                  ("Подробности — в логах: docker compose logs bot.", "Details in the logs: docker compose logs bot."))
    if late:
        return _c("jobs", "warn", t, ("Давно не запускались: " + ", ".join(late), "Not run for long: " + ", ".join(late)))
    return _c("jobs", "ok", t, (f"Работают все {len(jobs.INTERVALS)}.", f"All {len(jobs.INTERVALS)} run."))


def check_backup(db: Session) -> dict:
    t = ("Резервные копии", "Backups")
    if core.get(db, "backup_enabled") != "1":
        return _c("backup", "warn", t, ("Ежедневный бэкап выключен.", "The daily backup is off."),
                  ("Включите его в Настройках: при поломке сервера магазин вернётся из последнего архива.",
                   "Turn it on in Settings: after a server dies, the shop comes back from the last archive."))
    if not env.admin_telegram_ids:
        return _c("backup", "warn", t, ("Некому отправлять: ADMIN_TELEGRAM_IDS пуст.", "Nobody to send to: ADMIN_TELEGRAM_IDS is empty."))
    last = core.get_int(db, "backup_last", 0)
    if not last:
        return _c("backup", "warn", t, ("Ещё не было ни одного.", "None made yet."),
                  ("Сделайте первый кнопкой в Настройках.", "Make the first one with the button in Settings."))
    if now() - last > 50 * 3600:
        return _c("backup", "warn", t, (f"Последний — {_ago(last)[0]}.", f"The last one was {_ago(last)[1]}."))
    return _c("backup", "ok", t, (f"Последний — {_ago(last)[0]}.", f"The last one was {_ago(last)[1]}."))


def check_disk() -> dict:
    t = ("Место на диске", "Disk space")
    try:
        u = shutil.disk_usage(env.data_dir)
    except OSError as e:
        return _c("disk", "warn", t, (f"Не удалось узнать: {e}", f"Could not tell: {e}"))
    free_gb = u.free / 1024 ** 3
    detail = (f"Свободно {free_gb:.1f} ГБ из {u.total / 1024 ** 3:.0f} ГБ.",
              f"{free_gb:.1f} GB free of {u.total / 1024 ** 3:.0f} GB.")
    if free_gb < 0.3:
        return _c("disk", "fail", t, detail, ("Почти нет места: база и бэкапы перестанут записываться.",
                                              "Almost full: the database and backups will stop writing."))
    if free_gb < 1.5:
        return _c("disk", "warn", t, detail, ("Освободите место: docker system prune, старые логи.",
                                              "Free some space: docker system prune, old logs."))
    return _c("disk", "ok", t, detail)


def check_config() -> list[dict]:
    """Settings in .env that are dangerous (a weak password, test payments)."""
    t = ("Настройки сервера", "Server settings")
    probs = [p for p in env.problems() if p["code"] not in ("bot_token", "https")]   # those have own checks
    if not probs:
        return [_c("config", "ok", t, ("Опасных настроек нет.", "No dangerous settings."))]
    return [_c("config", "fail" if p["code"] != "test_mode" else "warn", t, (p["text"], p["text"])) for p in probs]


_STARTED = now()


# ---------------------------------------------------------------------------
def run(db: Session) -> dict:
    """All checks. Network ones (Telegram, the certificate, every panel) run
    side by side, each with its own timeout — the whole run stays under ~15 s."""
    with _lock:
        rows = access.servers(db)
        snaps = [(s, access._snap(s)) for s in rows]
        local = [check_emergency(db), check_db(db), check_payments(db), check_vpn(db), check_russia(db),
                 check_sync(db), check_backup(db)]
        access.release(db)
        tasks = [("bot", ("Бот Telegram", "Telegram bot"), check_bot),
                 ("webhook", ("Вебхук бота", "Bot webhook"), check_webhook),
                 ("https", ("Адрес и сертификат", "Address and certificate"), check_https)] + \
                [(f"panel:{s.id}", (f"Панель: {s.name}", f"Panel: {s.name}"),
                  (lambda s=s, sn=sn: check_panel(s, sn))) for s, sn in snaps]
        pool = ThreadPoolExecutor(max_workers=min(8, len(tasks)))
        try:
            futures = [pool.submit(fn) for _, _, fn in tasks]
            remote = []
            deadline = time.monotonic() + 20
            for f, (cid, title, _) in zip(futures, tasks):
                try:
                    remote.append(f.result(timeout=max(0.1, deadline - time.monotonic())))
                except Exception as e:  # noqa: BLE001 — a check that could not finish says so
                    why = str(e) or e.__class__.__name__
                    remote.append(_c(cid, "fail", title, (f"Проверка не завершилась: {why}",
                                                          f"The check did not finish: {why}")))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        if not rows:
            remote.append(_c("panels", "fail", ("Серверы", "Servers"),
                             ("Нет ни одного включённого сервера — продавать нечего.",
                              "No enabled server — nothing to sell."),
                             ("Добавьте панель в «Серверах».", "Add a panel in Servers.")))
        checks = [local[0]] + remote[:3] + [local[1]] + remote[3:] + local[2:] + \
            [check_jobs(), check_disk()] + check_config()
        order = {"fail": 0, "warn": 1, "ok": 2, "off": 3}
        summary = {k: sum(1 for c in checks if c["status"] == k) for k in order}
        result = {"at": now(), "summary": summary, "checks": checks,
                  "worst": min((c["status"] for c in checks), key=order.get, default="ok")}
        LAST.clear()
        LAST.update(result)
        return result


def text_report(result: dict, lang: str) -> str:
    """For the bot: one line per check, problems first, as HTML."""
    import html
    mark = {"fail": "✖", "warn": "▲", "ok": "✓", "off": "–"}
    word = {"ru": {"fail": "ошибка", "warn": "внимание", "ok": "норма", "off": "не используется"},
            "en": {"fail": "error", "warn": "attention", "ok": "ok", "off": "not used"}}[lang]
    order = {"fail": 0, "warn": 1, "ok": 2, "off": 3}
    lines = []
    for c in sorted(result["checks"], key=lambda c: order[c["status"]]):
        line = f"{mark[c['status']]} <b>{html.escape(c['title'][lang])}</b> — {word[c['status']]}"
        if c["status"] in ("fail", "warn") and c["detail"][lang]:
            line += f"\n{html.escape(c['detail'][lang])}"
        lines.append(line)
    s = result["summary"]
    head = (f"<b>Диагностика</b> · ошибок: {s['fail']}, внимание: {s['warn']}, норма: {s['ok']}"
            if lang == "ru" else
            f"<b>Diagnostics</b> · errors: {s['fail']}, attention: {s['warn']}, ok: {s['ok']}")
    return head + "\n\n" + "\n".join(lines)
