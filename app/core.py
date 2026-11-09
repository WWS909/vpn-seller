"""
Small shared helpers: key/value settings with defaults, the admin log,
and texts in two languages for messages the backend itself writes.
"""

from __future__ import annotations

import json
import logging
import threading
import time

from sqlalchemy.orm import Session

from .config import settings as env
from .models import Log, Setting

log = logging.getLogger("vpnshop")


# ---------------------------------------------------------------------------
#  Settings edited in the admin panel. The defaults here are what a fresh
#  install shows; the panel stores only what the operator changed.
# ---------------------------------------------------------------------------
DEFAULTS: dict[str, str] = {
    "brand_name": "VPN",
    "nalog_item": "Доступ к VPN-сервису",   # the line on «Мой налог» receipts
    "nalog_paused": "",                     # "1": FNS refused the password, receipts wait
    "support_url": "",                 # https://t.me/username or mailto:
    "about_ru": "",
    "about_en": "",
    "faq_json": "[]",                  # [{"q": "...", "a": "...", "lang": "ru"}]
    "setup_hidden": "0",               # the "first steps" card on the overview was closed
    "device_price": "50",              # one extra device for the rest of the term
    "max_devices": "10",
    "traffic_packs": "[[10, 99], [50, 299], [100, 499]]",   # [[GB, price], ...]
    "traffic_topup": "0",               # sell those packs (plans with a traffic limit); off until the owner turns it on
    "bypass_packs": "[]",              # [[GB, price], ...] for the whitelist-bypass servers
    "stars_rate": "1.6",               # currency units per 1 Telegram Star
    "require_hwid": "0",               # refuse VPN clients that do not send a device id
    "connect_apps": "happ,v2raytun,incy,onexray,karing,hiddify",  # VPN apps offered, first is suggested
    "panel_ip_limit": "0",             # also push the device limit as limitIp to panels
    "sub_update_hours": "12",
    "admin_link": "all",               # who sees the admin entry in the app: all | admins | none
    "reviews_enabled": "1",
    "review_reward_days": "0",
    "pay_labels": "{}",
    "ai_enabled": "0",                  # the AI helper answers support first (needs AI_API_KEY)
    "ai_name": "",                      # its name in answers
    "ai_prompt": "",                    # the owner's notes for it: style, facts about the service                 # the owner's names of payment methods, see payments.labels()          # bonus days for the first published review; 0 = none
    "gifts_enabled": "1",
    "legal_owner": "",                 # seller name / status for the legal page
    "legal_contact": "",               # e-mail for legal questions
    # Documents on the public page. Empty on a fresh install: the owner
    # writes them or starts from a template (app/legal.py) in the panel.
    "terms_ru": "",
    "terms_en": "",
    "privacy_ru": "",
    "privacy_en": "",
    "welcome_ru": "",
    "welcome_en": "",
    # Look (app/theme.py): a ready theme or "custom" with own accent and surface
    "theme_preset": "amber",
    "theme_accent": "#F2B441",
    "theme_surface": "black",           # black | graphite | midnight | indigo
    "theme_mode": "auto",               # auto (as in Telegram) | dark | light
    "logo": "",                         # set by uploading a file, not by this form
    "logo_info": "",                    # what the logo is like (shape, colour), worked out on upload
    "tagline_ru": "",                   # the line under "Choose your access" on home
    "tagline_en": "",
    "font": "unbounded",                # a font pair from app/theme.py FONTS
    "layout": "classic",                # the style of the surfaces: app/theme.py LAYOUTS
    "home": "classic",                  # what the home screen is built around: app/theme.py HOMES
    "font_custom": "",                  # a Google Fonts family for the headings, when font is "custom"
    "radius": "normal",                 # sharp | normal | round
    "backdrop": "glow",                 # glow | aurora | orbs | night | grid | stars | plain
    "cards": "solid",                   # solid | glass (frosted) | neon (frosted, neon edges)
    "icons": "regular",                 # thin | regular | bold | badge | duo | 3d | neon
    "motion": "lively",                 # lively | calm | off
    "effect": "none",                   # none | snow
    "splash": "1",                      # the opening screen with the logo
    "splash_kind": "auto",              # auto (the style's own) | pulse | orbit | ring | burst | type | minimal
    "splash_mark": "logo",              # the sign on it: logo (the shield when there is none) | letter | shield
    "splash_plate": "auto",             # what the sign sits on: auto | tile | circle | none
    "splash_size": "m",                 # s | m | l
    "splash_color": "style",            # style | logo | #RRGGBB
    "splash_bg": "app",                 # app | glow | flood (filled with the colour)
    "splash_name": "auto",              # auto (hidden when the logo is a wide wordmark) | show | hide
    "splash_speed": "normal",           # fast | normal | slow: how long it stays at least
    "splash_text_ru": "",               # a line under the name on the opening screen
    "splash_text_en": "",
    "bot_about_ru": "",                 # the bot's short description (profile, shares)
    "bot_about_en": "",
    "bot_desc_ru": "",                  # "What can this bot do?" in an empty chat
    "bot_desc_en": "",
    "links_json": "[]",                 # own buttons in the profile: [{"ru", "en", "url"}]
    # Sale: a discount on every plan for everyone, until a date (0 = no end)
    "sale_percent": "0",
    "sale_until": "0",
    "sale_title_ru": "",
    "sale_title_en": "",
    # Referral program — off until the owner turns it on. The numbers are
    # only what the form suggests then.
    "ref_enabled": "0",
    "ref_mode": "days",                 # days (bonus days) | money (a share of payments, withdrawable)
    "ref_days": "7",                    # days mode: days for the inviter per paid friend
    "ref_percent": "20",                # money mode: the inviter's share of the friend's payment, %
    "ref_min_payout": "0",              # money mode: the smallest withdrawal, 0 = any amount
    "ref_balance_pay": "1",             # money mode: the balance can also pay for a subscription
    "ref_payout_methods": "[]",         # how money is paid out: [{"id","kind","ru","en","on"}]
    "ref_friend_days": "3",             # extra days for the friend's first purchase (both modes)
    "ref_every": "0",                   # reward every payment of the friend, not only the first
    "ref_captcha": "0",                 # a new referral must solve a captcha in the bot
    "top_enabled": "0",                 # the top of inviters in the app
    "top_period": "all",                # all | month | week
    "top_type": "days",                 # days (earned) | paid (friends who paid) | invited
    "top_size": "10",
    # Mandatory channel subscription
    "channel_id": "",                   # @username or -100… id
    "channel_link": "",                 # invite link (for a private channel)
    "channel_mode": "off",              # off | trial (only for the free trial) | all
    "channel_title": "",
    "channels_json": "",                # several channels: [{"id", "link", "title"}]; app/channel.py
    # The day's results to the owners in Telegram (app/report.py), at this server hour
    "report_on": "1",
    "report_hour": "18",
    "report_sent": "",
    # New versions from GitHub (app/updates.py): what was found, whom it was told,
    # and whether the shop may update itself at night (at this server hour)
    "update_info": "",
    "update_notified": "",
    "update_announced": "",
    "auto_update": "0",
    "auto_update_hour": "1",
    "auto_update_tried": "",
    # Daily backup to the admins in Telegram
    "backup_enabled": "1",
    "backup_hour": "4",                 # server local time, 0–23
    # Support chat in the bot: customers write, admins answer by replying
    "support_enabled": "1",
    # Locations: show customers how busy each server is (low / medium / high)
    "servers_load": "1",
    # A server whose VPN port stops answering is left out of subscriptions until it is back
    "vpn_autohide": "1",
    # Reminders about the end of a subscription carry a Telegram Stars invoice: renew in one tap
    "renew_invoice": "1",
    # Days the VPN keeps working after the paid time ends (0 — it stops at once)
    "grace_days": "0",
    # Emergency mode (app/emergency.py): a message for everyone, sales stopped,
    # and, if chosen, nobody is cut off while it lasts
    "emergency": "0",
    "emergency_since": "0",
    "emergency_text_ru": "",
    "emergency_text_en": "",
    "emergency_stop_sales": "1",
    "emergency_keep_access": "0",
    "emergency_auto": "0",              # turn on by itself when every server is down
    "emergency_auto_minutes": "10",     # … for this long
    "emergency_auto_on": "0",           # it was turned on by itself (so it may turn off by itself)
    # Shared blacklists (app/blacklist.py)
    "blacklist_urls": "",               # one URL per line: text files of Telegram IDs
    "blacklist_action": "trial",        # off | trial (no free trial) | block (no trial, no purchases)
    "blacklist_publish": "0",           # serve the shop's own blocked customers at /blacklist.txt
    "blacklist_updated_at": "0",
    "blacklist_status_json": "{}",      # per URL: how many IDs, or the error
    "probe_token": "",                  # the token of the checkers inside Russia (app/probe.py)
    "dpi_last_json": "{}",              # the last DPI//CHECKER check per server
    "dpi_uuid": "",                     # the service client whose keys DPI//CHECKER connects to
    "dpi_sub": "",
    "tg_only_hours": "0",               # after the end: hours with Telegram only (0 — off)
}


# What a Telegram Star costs a buyer, roughly, in the shop's currency.
# Only the starting value — the admin sets the real rate in settings.
STAR_RATES = {"RUB": 1.6, "USD": 0.02, "EUR": 0.018, "GBP": 0.016, "UAH": 0.8,
              "KZT": 9.5, "BYN": 0.06, "UZS": 250, "TRY": 0.7, "INR": 1.7}


def default_for(key: str) -> str:
    if key == "stars_rate":
        return str(STAR_RATES.get(env.currency, 0.02))
    return DEFAULTS.get(key, "")


# Settings are read on almost every request (the app page alone needs a few
# dozen), so they are kept in memory: all of them in one query, dropped the
# moment this process writes one, and read again after a few seconds anyway —
# a change made by another process (a backup run from the server menu) shows
# up soon. The same dict is kept while nothing changed, so what is built from
# it (the app page) can be kept too.
_TTL = 3.0
_cache: tuple | None = None         # (engine, generation, loaded at, {key: value})
_gen = 0
_lock = threading.Lock()


def snapshot(db: Session) -> dict[str, str | None]:
    """All stored settings (without defaults), fresh to a few seconds."""
    global _cache
    eng, c, t = db.get_bind(), _cache, time.monotonic()
    if c is not None and c[0] is eng and c[1] == _gen and t - c[2] < _TTL:
        return c[3]
    gen = _gen
    rows = dict(db.query(Setting.key, Setting.value).all())
    with _lock:
        if gen != _gen:              # a write came in while reading: use, do not keep
            return rows
        same = c is not None and c[0] is eng and c[3] == rows
        _cache = (eng, gen, t, c[3] if same else rows)
        return _cache[3]


def get(db: Session, key: str, default: str | None = None) -> str:
    value = snapshot(db).get(key)
    if value is not None:
        return value
    return default_for(key) if default is None else default


def stars_rate(db: Session) -> float:
    try:
        rate = float(get(db, "stars_rate") or 0)
    except ValueError:
        rate = 0.0
    return rate if rate > 0 else float(default_for("stars_rate"))


def get_int(db: Session, key: str, default: int = 0) -> int:
    """The setting as a number; the default of DEFAULTS wins over the
    argument, which only covers keys that have none."""
    raw = get(db, key) if key in DEFAULTS else get(db, key, str(default))
    try:
        return int(float(raw or default))
    except ValueError:
        return default


def get_float(db: Session, key: str, default: float = 0.0) -> float:
    raw = get(db, key) if key in DEFAULTS else get(db, key, str(default))
    try:
        return float(raw or default)
    except ValueError:
        return default


def get_json(db: Session, key: str, default):
    try:
        value = json.loads(get(db, key) or "null")
        return default if value is None else value
    except ValueError:
        return default


def put(db: Session, key: str, value: str) -> None:
    global _gen
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
    try:
        db.commit()
    finally:
        with _lock:
            _gen += 1                # every reader of this process sees it from now on


# ---------------------------------------------------------------------------
def audit(db: Session, action: str, actor: str = "system", details: str = "",
          level: str = "info") -> None:
    """A line in the admin panel's log (and in the process log)."""
    db.add(Log(action=action[:96], actor=str(actor)[:64], details=str(details)[:2000],
               level=level))
    db.commit()
    getattr(log, "warning" if level != "info" else "info")("%s | %s | %s", action, actor, details)


# ---------------------------------------------------------------------------
#  Texts the backend writes itself: errors shown in the app, bot messages,
#  reminders. The mini app carries its own dictionary for the interface.
# ---------------------------------------------------------------------------
TEXTS: dict[str, dict[str, str]] = {
    "plan_not_found": {"ru": "Тариф не найден", "en": "Plan not found"},
    "trial_used": {"ru": "Пробный период уже использован", "en": "The free trial has already been used"},
    "no_subscription": {"ru": "Нет активной подписки", "en": "No active subscription"},
    "unlimited_traffic": {"ru": "У вас безлимитный трафик", "en": "Your traffic is unlimited"},
    "unlimited_devices": {"ru": "У вас и так без ограничения устройств",
                          "en": "Your plan already has no device limit"},
    # Telegram shows these when it refuses a Stars payment (before taking money).
    "order_closed": {"ru": "Этот счёт уже оплачен или закрыт", "en": "This invoice is already paid or closed"},
    "order_price": {"ru": "Цена изменилась — откройте заказ заново",
                    "en": "The price has changed, open the order again"},
    "order_old": {"ru": "Счёт устарел — откройте приложение, чтобы оплатить по актуальной цене",
                  "en": "This invoice is out of date — open the app to pay the current price"},
    "order_promo_gone": {"ru": "Промокод уже использован — откройте приложение и оформите заказ заново",
                         "en": "The promo code has been used — open the app and order again"},
    "order_offer_gone": {"ru": "Скидка закончилась — откройте приложение, чтобы оплатить по актуальной цене",
                         "en": "The discount has ended — open the app to pay the current price"},
    "promo_not_found": {"ru": "Промокод не найден", "en": "Promo code not found"},
    "code_not_found": {"ru": "Такого кода нет: проверьте подарочный код или промокод",
                       "en": "No such code: check the gift or promo code"},
    "promo_used_up": {"ru": "Промокод закончился", "en": "This promo code has run out"},
    "promo_expired": {"ru": "Срок промокода истёк", "en": "This promo code has expired"},
    "promo_new_only": {"ru": "Промокод только для новых пользователей",
                       "en": "This promo code is for new customers only"},
    "promo_already": {"ru": "Вы уже использовали этот промокод",
                      "en": "You have already used this promo code"},
    "method_unavailable": {"ru": "Этот способ оплаты сейчас недоступен",
                           "en": "This payment method is not available right now"},
    "gateway_failed": {"ru": "Касса не приняла платёж, попробуйте другой способ",
                       "en": "The payment service declined, try another method"},
    "no_servers": {"ru": "Серверы временно недоступны, попробуйте позже",
                   "en": "Servers are temporarily unavailable, try again later"},
    "gift_invalid": {"ru": "Код не найден или уже использован",
                     "en": "The code is not valid or was already used"},
    "gifts_off": {"ru": "Подарки отключены", "en": "Gifts are turned off"},
    "reviews_off": {"ru": "Отзывы отключены", "en": "Reviews are turned off"},
    "review_customers": {"ru": "Отзыв могут оставить только клиенты сервиса",
                         "en": "Only customers can leave a review"},
    "review_short": {"ru": "Напишите хотя бы пару слов", "en": "Write at least a few words"},
    "device_not_found": {"ru": "Устройство не найдено", "en": "Device not found"},
    "devices_full": {"ru": "Все места заняты — сначала отвяжите другое устройство или докупите место",
                     "en": "All slots are taken — remove another device or buy one more slot"},
    "reissue_failed": {"ru": "Серверы сейчас не отвечают — ссылка осталась прежней. Попробуйте через пару минут",
                       "en": "The servers do not answer right now — the link stays the same. Try again in a few minutes"},
    "order_not_found": {"ru": "Заказ не найден", "en": "Order not found"},
    "too_many": {"ru": "Слишком много попыток, подождите", "en": "Too many attempts, please wait"},
    "blocked": {"ru": "Доступ заблокирован администратором", "en": "Access blocked by the administrator"},

    # --- bot ---
    "open_app": {"ru": "Открыть", "en": "Open"},
    "welcome": {"ru": "<b>{brand}</b>\n\nПодписка, серверы и настройка — в приложении. "
                      "Нажмите «Открыть».",
                "en": "<b>{brand}</b>\n\nYour subscription, servers and setup are in the app. "
                      "Tap “Open”."},
    "gift_hint": {"ru": "Вам подарили подписку. Откройте приложение — код подставится сам.",
                  "en": "You have received a gift. Open the app — the code is filled in for you."},
    "paid": {"ru": "Оплата прошла. {what}", "en": "Payment received. {what}"},
    "paid_plan": {"ru": "Подписка «{plan}» активна до {date}.",
                  "en": "“{plan}” is active until {date}."},
    "paid_carry": {"ru": "Неистраченные {gb} ГБ из докупленного трафика перенесены.",
                   "en": "{gb} GB of unused bought traffic carried over."},
    "paid_devices": {"ru": "Лимит устройств: {n}.", "en": "Device limit: {n}."},
    "paid_traffic": {"ru": "Добавлено {gb} ГБ.", "en": "{gb} GB added."},
    "bypass_out": {"ru": "Гигабайты для обхода белых списков закончились — серверы обхода отключены. "
                         "Докупить можно в приложении, остальные серверы работают как обычно.",
                   "en": "Your whitelist-bypass gigabytes are used up — the bypass servers are off. "
                         "Top up in the app; the other servers work as usual."},
    "paid_bypass": {"ru": "Добавлено {gb} ГБ для обхода белых списков.",
                    "en": "{gb} GB added for getting through whitelists."},
    "paid_gift": {"ru": "Подарочный код: <code>{code}</code>\nСсылка для получателя: {link}",
                  "en": "Gift code: <code>{code}</code>\nLink for the recipient: {link}"},
    "remind": {"ru": "Подписка заканчивается через {days} дн. Продлите, чтобы доступ не прервался.",
               "en": "Your subscription ends in {days} day(s). Renew to keep your access."},
    "renew": {"ru": "Продлить", "en": "Renew"},
    "renew_title": {"ru": "Продление: {plan}", "en": "Renewal: {plan}"},
    "renew_pay": {"ru": "Оплатить {n} ⭐", "en": "Pay {n} ⭐"},
    "renew_other": {"ru": "Другие способы оплаты", "en": "Other ways to pay"},
    "take_offer": {"ru": "Забрать скидку", "en": "Get the discount"},
    "finish_pay": {"ru": "Завершить оплату", "en": "Finish the payment"},
    "backup_caption": {"ru": "Резервная копия {brand} · {date}\nКлиентов: {users}, с подпиской: {active}",
                       "en": "Backup of {brand} · {date}\nCustomers: {users}, subscribed: {active}"},
    "support_off": {"ru": "Чат поддержки выключен", "en": "Support chat is off"},
    "support_empty": {"ru": "Напишите сообщение", "en": "Write a message"},
    "sup_header": {"ru": "<b>Поддержка</b> · {name}{uname}\nID: <code>{tg}</code> · подписка {state}\n"
                         "Ответьте на это сообщение — ответ уйдёт клиенту от имени бота.",
                   "en": "<b>Support</b> · {name}{uname}\nID: <code>{tg}</code> · subscription {state}\n"
                         "Reply to this message — the answer goes to the customer from the bot."},
    "sup_active": {"ru": "до {date}", "en": "until {date}"},
    "sup_expired": {"ru": "закончилась {date}", "en": "ended {date}"},
    "sup_never": {"ru": "не было", "en": "none"},
    "sup_card": {"ru": "Карточка клиента", "en": "Customer card"},
    "sup_panel": {"ru": "Открыть в панели", "en": "Open in the panel"},
    "upd_new": {"ru": "🆕 <b>Вышла новая версия VPN Seller — {latest}</b>\nУ вас сейчас {cur}.",
                "en": "🆕 <b>A new VPN Seller version is out — {latest}</b>\nYou have {cur}."},
    "upd_whats_new": {"ru": "<b>Что нового:</b>", "en": "<b>What's new:</b>"},
    "upd_safe": {"ru": "Перед обновлением делается копия базы. Если новая версия не запустится, "
                       "магазин сам вернётся на прежнюю.",
                 "en": "A database backup is made first. If the new version does not start, "
                       "the shop goes back to the previous one by itself."},
    "upd_by_hand": {"ru": "Обновить: на сервере команда <code>vpn-seller update</code>.",
                    "en": "To update, run <code>vpn-seller update</code> on the server."},
    "upd_button": {"ru": "⬆️ Обновить сейчас", "en": "⬆️ Update now"},
    "upd_panel": {"ru": "Открыть в панели", "en": "Open in the panel"},
    "upd_started": {"ru": "Обновление запущено. Пришлю сообщение, когда закончится (обычно 2–5 минут).",
                    "en": "The update has started. I will write when it is done (usually 2–5 minutes)."},
    "upd_running": {"ru": "Обновление уже идёт", "en": "An update is already running"},
    "upd_no_updater": {"ru": "Кнопка не настроена на сервере. Обновите командой vpn-seller update — "
                             "после этого кнопка заработает.",
                       "en": "The button is not set up on the server. Update with vpn-seller update once — "
                             "the button works after that."},
    "upd_owner_only": {"ru": "Обновлять может только владелец", "en": "Only the owner can update"},
    "upd_done": {"ru": "✅ <b>Магазин обновлён до {to}</b>", "en": "✅ <b>The shop is updated to {to}</b>"},
    "upd_failed": {"ru": "⚠️ <b>Обновление до {to} не удалось</b>\nМагазин вернулся на версию {back} и работает.\n"
                         "Причина: {reason}\nПодробности: vpn-seller → «Журнал».",
                   "en": "⚠️ <b>The update to {to} failed</b>\nThe shop went back to {back} and works.\n"
                         "Reason: {reason}\nDetails: vpn-seller → Logs."},
    "sup_sent": {"ru": "Сообщение передано в поддержку. Ответ придёт сюда, в этот чат.",
                 "en": "Your message went to support. The answer will come here, in this chat."},
    "sup_slow": {"ru": "Слишком много сообщений подряд. Подождите немного — мы уже читаем.",
                 "en": "Too many messages in a row. Please wait a little — we are reading."},
    "sup_reply": {"ru": "<b>Поддержка:</b>\n{text}", "en": "<b>Support:</b>\n{text}"},
    "ai_reply": {"ru": "<b>{name} · ИИ-помощник:</b>\n{text}\n\n<i>Нужен человек — напишите «оператор».</i>",
                 "en": "<b>{name} · AI helper:</b>\n{text}\n\n<i>Need a person? Write “operator”.</i>"},
    "ai_default_name": {"ru": "Помощник", "en": "Helper"},
    "ai_handed": {"ru": "ИИ-помощник передал обращение людям", "en": "The AI helper handed this over to people"},
    "ai_handoff_default": {"ru": "Передал ваш вопрос администратору — он ответит здесь.",
                           "en": "I have passed your question to an admin — they will answer here."},
    "sup_delivered": {"ru": "Отправлено клиенту", "en": "Sent to the customer"},
    "sup_undelivered": {"ru": "Не доставлено: клиент заблокировал бота",
                        "en": "Not delivered: the customer blocked the bot"},
    "backup_too_big": {"ru": "Резервная копия {name} весит {mb} МБ — больше, чем Telegram разрешает боту "
                             "отправить. Она лежит на сервере в data/backups.",
                       "en": "Backup {name} is {mb} MB — more than Telegram lets a bot send. "
                             "It is on the server in data/backups."},
    "captcha": {"ru": "Подтвердите, что вы не бот: нажмите на число {n}.",
                "en": "Confirm you are not a bot: tap the number {n}."},
    "captcha_ok": {"ru": "Готово, спасибо!", "en": "Done, thank you!"},
    "captcha_bad": {"ru": "Не то число, попробуйте ещё раз.", "en": "Wrong number, try again."},
    "review_reward": {"ru": "Спасибо за отзыв! Мы его опубликовали и добавили вам {days} дн. подписки.",
                      "en": "Thank you for the review! It is published, and {days} days were added to your subscription."},
    "ref_reward": {"ru": "Ваш друг оплатил подписку — вам +{days} дн. Спасибо, что рекомендуете нас!",
                   "en": "Your friend has paid — you get +{days} days. Thanks for recommending us!"},
    "ref_reward_money": {"ru": "Ваш друг оплатил заказ — вам начислено {amount}. Баланс: {balance}.",
                         "en": "Your friend has paid — you earned {amount}. Balance: {balance}."},
    "ref_off": {"ru": "Реферальная программа выключена", "en": "The referral program is off"},
    "balance_low": {"ru": "На балансе недостаточно средств", "en": "Not enough money on the balance"},
    "balance_off": {"ru": "Оплата с баланса выключена", "en": "Paying from the balance is turned off"},
    "payout_off": {"ru": "Вывод средств сейчас недоступен", "en": "Withdrawals are not available right now"},
    "payout_method": {"ru": "Выберите способ вывода", "en": "Choose how to withdraw"},
    "payout_min": {"ru": "Минимальная сумма вывода — {min}", "en": "The smallest withdrawal is {min}"},
    "payout_pending": {"ru": "У вас уже есть заявка в обработке", "en": "You already have a request in progress"},
    "payout_card": {"ru": "Проверьте номер карты", "en": "Check the card number"},
    "payout_phone": {"ru": "Укажите номер телефона и банк", "en": "Enter the phone number and the bank"},
    "payout_wallet": {"ru": "Проверьте адрес кошелька", "en": "Check the wallet address"},
    "payout_details": {"ru": "Укажите реквизиты для вывода", "en": "Enter where to send the money"},
    "payout_sent": {"ru": "Заявка на вывод {amount} принята. Мы сообщим, когда деньги будут отправлены.",
                    "en": "Your request to withdraw {amount} is received. We'll let you know when it's sent."},
    "payout_paid": {"ru": "Выплата {amount} отправлена: {method}. Спасибо, что рекомендуете нас!",
                    "en": "Payout of {amount} is sent: {method}. Thanks for recommending us!"},
    "payout_rejected": {"ru": "Заявка на вывод {amount} отклонена{reason}. Деньги вернулись на баланс.",
                        "en": "Your withdrawal of {amount} was declined{reason}. The money is back on your balance."},
    "payout_admin": {"ru": "<b>Заявка на вывод</b> #{id}\n{name} · ID <code>{tg}</code>\n\n"
                           "Сумма: <b>{amount}</b>\nСпособ: {method}\nРеквизиты: <code>{details}</code>\n\n"
                           "Переведите деньги сами и нажмите «Выплачено».",
                     "en": "<b>Withdrawal request</b> #{id}\n{name} · ID <code>{tg}</code>\n\n"
                           "Amount: <b>{amount}</b>\nMethod: {method}\nDetails: <code>{details}</code>\n\n"
                           "Send the money yourself, then press “Paid”."},
    "channel_required": {"ru": "Чтобы пользоваться сервисом, подпишитесь на канал",
                         "en": "Subscribe to the channel to use the service"},
    "channel_trial": {"ru": "Пробный период — для подписчиков канала",
                      "en": "The free trial is for channel subscribers"},
    "subscribe": {"ru": "Подписаться", "en": "Subscribe"},
    "traffic_out": {"ru": "Трафик по подписке закончился. Докупите гигабайты в приложении.",
                    "en": "You have used all your traffic. Top it up in the app."},
    "traffic_out_renew": {"ru": "Трафик по подписке закончился. Продлите подписку или выберите тариф побольше в приложении.",
                          "en": "You have used all your traffic. Renew or pick a bigger plan in the app."},

    # --- notices inside the subscription (shown by the VPN client as a server name) ---
    "sub_expired": {"ru": "Подписка закончилась — продлите в боте", "en": "Subscription ended — renew in the bot"},
    "sub_blocked": {"ru": "Доступ заблокирован", "en": "Access blocked"},
    "sub_traffic": {"ru": "Трафик закончился — докупите в боте", "en": "Traffic used up — top up in the bot"},
    "sub_traffic_renew": {"ru": "Трафик закончился — продлите в боте", "en": "Traffic used up — renew in the bot"},
    "sub_devices": {"ru": "Лимит устройств — отвяжите лишнее в боте",
                    "en": "Device limit reached — remove one in the bot"},
    "sub_device_removed": {"ru": "Это устройство отвязано — верните его в боте",
                           "en": "This device was removed — bring it back in the bot"},
    "sub_hwid": {"ru": "Приложение не передаёт ID устройства — используйте Happ, v2RayTun или Incy",
                 "en": "This app does not send a device ID — use Happ, v2RayTun or Incy"},
    "sub_empty": {"ru": "Серверы обновляются, обновите подписку позже",
                  "en": "Servers are updating, refresh later"},
    "sub_tg_only": {"ru": "Подписка закончилась — до {date} работает только Telegram, продлите в боте",
                    "en": "Subscription ended — only Telegram works until {date}, renew in the bot"},
    "sub_grace": {"ru": "Подписка закончилась — VPN работает до {date}, продлите в боте",
                  "en": "Subscription ended — VPN works until {date}, renew in the bot"},

    # --- the days after the end and the emergency mode ---
    "grace_note": {"ru": "\n\nVPN продолжит работать до {date} — успейте продлить.",
                   "en": "\n\nThe VPN keeps working until {date} — renew before then."},
    "em_sales": {"ru": "Оплата временно недоступна — идут технические работы. Ваш доступ не пострадает.",
                 "en": "Payment is paused for maintenance. Your access is not affected."},
    "bl_blocked": {"ru": "Покупки для этого аккаунта недоступны. Если это ошибка — напишите в поддержку.",
                   "en": "Purchases are not available for this account. If this is a mistake, contact support."},
    "em_auto": {"ru": "Серверы сейчас не отвечают — мы уже чиним. Оплата временно на паузе.",
                "en": "Our servers are not answering right now — we are on it. Payment is paused for now."},
    "em_default": {"ru": "Идут технические работы. Мы уже чиним, скоро всё заработает.",
                   "en": "Maintenance is under way. We are fixing it, everything will be back soon."},
}


def money(value: float, lang: str = "") -> str:
    """An amount in the shop's currency for messages: "1 490 ₽", "49,75 ₽",
    "$3.50" — with the decimal mark of the reader's language."""
    v = round(float(value or 0), 2)
    num = f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"
    num = num.replace(",", " ")
    if (lang or env.default_lang) == "ru":
        num = num.replace(".", ",")
    sym = env.currency_symbol
    return f"{sym}{num}" if sym in ("$", "€", "£") else f"{num} {sym}"


def lang_of(value: str | None) -> str:
    code = (value or "").lower()[:2]
    return code if code in ("ru", "en") else ("ru" if code in ("uk", "be", "kk", "uz") else
                                              (env.default_lang if not code else "en"))


def t(lang: str, key: str, **kw) -> str:
    row = TEXTS.get(key) or {}
    text = row.get(lang) or row.get("en") or key
    return text.format(**kw) if kw else text
