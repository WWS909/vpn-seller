"""
Payment gateways set up in the admin panel.

The keys used to live only in .env (the installer's "payments" menu). Now
the owner can enter them on the "Payments" page too: they are kept in the
database (setting "pay_env") and laid over .env — a value from the panel
wins, an empty one falls back to .env. Applied at start and right after
saving, no restart needed.

Only the owner reaches this (the path is not in any team section), and
secrets never go back to the browser: the panel only learns that one is set.
"""
from __future__ import annotations

import json
import os

from sqlalchemy.orm import Session

from . import core
from .config import settings as env

# (env variable, label ru, label en, secret)
F = tuple
GATEWAYS: list[dict] = [
    {"id": "cryptobot", "name": "Crypto Pay (CryptoBot)", "kind": "crypto",
     "fields": [F(("CRYPTOBOT_TOKEN", "Токен Crypto Pay", "Crypto Pay token", True))],
     "how": {"ru": "@CryptoBot → Crypto Pay → Create App → скопируйте токен. Там же включите Webhooks и вставьте адрес ниже.",
             "en": "@CryptoBot → Crypto Pay → Create App → copy the token. Turn Webhooks on there and paste the address below."}},
    {"id": "heleket", "name": "Heleket", "kind": "crypto",
     "fields": [F(("HELEKET_MERCHANT_ID", "Merchant ID", "Merchant ID", False)),
                F(("HELEKET_API_KEY", "API-ключ (Payment key)", "API key (Payment key)", True))],
     "how": {"ru": "Адрес уведомлений передаётся в каждом счёте сам — в кабинете вводить не нужно.",
             "en": "The callback address goes with every invoice — nothing to set in the dashboard."}},
    {"id": "lavepay", "name": "Lave Pay", "kind": "crypto",
     "fields": [F(("LAVEPAY_API_KEY", "API-ключ (LAVE-…)", "API key (LAVE-…)", True)),
                F(("LAVEPAY_WEBHOOK_SECRET", "Секрет вебхука (whsec_…)", "Webhook secret (whsec_…)", True)),
                F(("LAVEPAY_RUB_PER_USD", "Курс: рублей за 1 доллар", "Rate: roubles per 1 USD", False))],
     "how": {"ru": "Лавешка → Сервисы → Для бизнеса → Lave Pay: ключи и вебхук ниже. Счета в долларах по вашему курсу.",
             "en": "Laveshka → Services → For business → Lave Pay: the keys, and the webhook below. Invoices in USD at your rate."}},
    {"id": "yookassa", "name": "ЮKassa", "kind": "card",
     "fields": [F(("YOOKASSA_SHOP_ID", "shopId", "shopId", False)),
                F(("YOOKASSA_SECRET_KEY", "Секретный ключ", "Secret key", True))],
     "how": {"ru": "Кабинет → Интеграция → HTTP-уведомления: адрес ниже, событие payment.succeeded. Нужны ИП или самозанятость.",
             "en": "Dashboard → Integration → HTTP notifications: the address below, event payment.succeeded."}},
    {"id": "freekassa", "name": "FreeKassa", "kind": "card",
     "fields": [F(("FREEKASSA_MERCHANT_ID", "ID магазина", "Shop ID", False)),
                F(("FREEKASSA_SECRET1", "Секретное слово 1", "Secret word 1", True)),
                F(("FREEKASSA_SECRET2", "Секретное слово 2", "Secret word 2", True)),
                F(("FREEKASSA_API_KEY", "API-ключ (не обязательно)", "API key (optional)", True))],
     "how": {"ru": "Кабинет → Настройки магазина → URL оповещения: адрес ниже.",
             "en": "Dashboard → shop settings → notification URL: the address below."}},
    {"id": "platega", "name": "Platega", "kind": "card",
     "fields": [F(("PLATEGA_MERCHANT_ID", "Merchant ID", "Merchant ID", False)),
                F(("PLATEGA_SECRET", "Секрет", "Secret", True))],
     "how": {"ru": "Кабинет Platega → Callback URL: адрес ниже.", "en": "Platega → callback URL: the address below."}},
    {"id": "paypalych", "name": "PayPalych (Pally)", "kind": "card",
     "fields": [F(("PAYPALYCH_TOKEN", "API-токен", "API token", True)),
                F(("PAYPALYCH_SHOP_ID", "ID магазина", "Shop ID", False))],
     "how": {"ru": "Кабинет Pally → магазин → Result URL: адрес ниже.", "en": "Pally → shop → Result URL: the address below."}},
    {"id": "lava", "name": "Lava", "kind": "card",
     "fields": [F(("LAVA_SHOP_ID", "ID магазина", "Shop ID", False)),
                F(("LAVA_SECRET_KEY", "Секретный ключ", "Secret key", True)),
                F(("LAVA_ADDITIONAL_KEY", "Дополнительный ключ", "Additional key", True))],
     "how": {"ru": "Кабинет Lava → магазин → Webhook URL: адрес ниже. Только рубли.",
             "en": "Lava → shop → webhook URL: the address below. Roubles only."}},
    {"id": "wata", "name": "WATA", "kind": "card",
     "fields": [F(("WATA_TOKEN", "API-токен", "API token", True))],
     "how": {"ru": "Кабинет WATA → адрес уведомлений: адрес ниже.", "en": "WATA → webhook URL: the address below."}},
    {"id": "tribute", "name": "Tribute", "kind": "card",
     "fields": [F(("TRIBUTE_API_KEY", "API-ключ", "API key", True))],
     "how": {"ru": "Tribute → Магазин → API → Вебхуки: адрес ниже.", "en": "Tribute → Shop → API → Webhooks: the address below."}},
    {"id": "cloudpayments", "name": "CloudPayments", "kind": "card",
     "fields": [F(("CLOUDPAYMENTS_PUBLIC_ID", "Public ID", "Public ID", False)),
                F(("CLOUDPAYMENTS_API_SECRET", "Пароль для API", "API secret", True))],
     "how": {"ru": "Кабинет → Настройки → Уведомления → Pay: адрес ниже.",
             "en": "Dashboard → Settings → Notifications → Pay: the address below."}},
    {"id": "mulenpay", "name": "MulenPay", "kind": "card",
     "fields": [F(("MULENPAY_SHOP_ID", "ID магазина", "Shop ID", False)),
                F(("MULENPAY_API_KEY", "API-ключ", "API key", True)),
                F(("MULENPAY_SECRET_KEY", "Секретный ключ", "Secret key", True))],
     "how": {"ru": "Кабинет MulenPay → Callback URL: адрес ниже.", "en": "MulenPay → callback URL: the address below."}},
]
GATEWAYS += [
    {"id": "rollypay", "name": "RollyPay", "kind": "card",
     "fields": [F(("ROLLYPAY_API_KEY", "API-ключ (rpk_live_…)", "API key (rpk_live_…)", True)),
                F(("ROLLYPAY_SIGNING_SECRET", "Signing secret (для вебхуков)", "Signing secret (for webhooks)", True))],
     "how": {"ru": "RollyPay → Кассы → создайте кассу: API-ключ и signing secret. В кассе укажите Callback URL — адрес ниже. СБП, карты и крипта; баланс в USDT.",
             "en": "RollyPay → Terminals → create one: the API key and the signing secret. Set its callback URL to the address below."}},
    {"id": "cispay", "name": "cisPay", "kind": "card",
     "fields": [F(("CISPAY_SHOP_ID", "ID магазина (UUID)", "Shop ID (UUID)", False)),
                F(("CISPAY_API_KEY", "Секретный ключ (cis_sec_…)", "Secret key (cis_sec_…)", True))],
     "how": {"ru": "Кабинет cisPay → магазин: ID и секретный ключ. Адрес вебхука — ниже. Карты и СБП, суммы в рублях.",
             "en": "cisPay dashboard → shop: the ID and the secret key. Webhook address — below. Cards and SBP in roubles."}},
]
NALOG = [F(("NALOG_INN", "ИНН (12 цифр)", "INN (12 digits)", False)),
         F(("NALOG_PASSWORD", "Пароль от «Мой налог»", "«Мой налог» password", True))]
CHOICES = ("CARD_GATEWAY", "CRYPTO_GATEWAY", "STARS_ENABLED")

KEYS = {f[0] for g in GATEWAYS for f in g["fields"]} | {f[0] for f in NALOG} | set(CHOICES)
SECRETS = {f[0] for g in GATEWAYS for f in g["fields"] if f[3]} | {f[0] for f in NALOG if f[3]}
SETTING = "pay_env"

# What a key was (from .env) before the panel laid its value over it:
# taking the panel's value away brings that back. Keys the panel never
# touched are left alone.
_ORIG: dict[str, str | None] = {}


def stored(db: Session) -> dict:
    raw = core.get_json(db, SETTING, {})
    return {k: str(v) for k, v in raw.items() if k in KEYS} if isinstance(raw, dict) else {}


def apply(db: Session) -> None:
    """Lay the panel's values over .env and re-read the settings."""
    mine = {k: v for k, v in stored(db).items() if v != ""}
    touched = False
    for k in KEYS:
        if k in mine:
            if k not in _ORIG:
                _ORIG[k] = os.environ.get(k)
            if os.environ.get(k) != mine[k]:
                os.environ[k] = mine[k]
                touched = True
        elif k in _ORIG:
            was = _ORIG.pop(k)
            if was is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = was
            touched = True
    if touched:
        _reload()


# Only the payment settings are re-read: the rest of the settings may have
# been changed while running (in tunnel mode the shop's address follows the
# tunnel) and must stay as they are.
_PREFIXES = ("card_gateway", "crypto_gateway", "stars_", "cryptobot_", "heleket_", "lavepay_", "yookassa_",
             "freekassa_", "platega_", "paypalych_", "lava_", "wata_", "tribute_", "cloudpayments_",
             "mulenpay_", "rollypay_", "cispay_", "nalog_")


def _reload() -> None:
    fresh = type(env)()
    for name, value in vars(fresh).items():
        if name.startswith(_PREFIXES):
            setattr(env, name, value)


def value(k: str) -> str:
    return os.environ.get(k, "").strip()


def save(db: Session, changes: dict) -> list[str]:
    """changes: {VAR: value}. A secret sent as "" keeps the stored one;
    None clears it (back to .env). Returns the changed keys."""
    cur = stored(db)
    changed = []
    for k, v in changes.items():
        if k not in KEYS:
            raise ValueError(f"unknown setting {k}")
        if v is None:
            if k in cur:
                cur.pop(k)
                changed.append(k)
            continue
        v = str(v).strip()
        if len(v) > 1000 or any(c in v for c in "\r\n"):
            raise ValueError(f"{k}: one line, at most 1000 characters")
        if k in SECRETS and v == "":
            continue                     # left empty: keep what is there
        # One gateway or several ("yookassa,rollypay"); "off" turns the method
        # off even when .env names a gateway.
        for var, kind in (("CARD_GATEWAY", "card"), ("CRYPTO_GATEWAY", "crypto")):
            if k == var and v and v != "off":
                names = [x for x in v.replace(" ", "").lower().split(",") if x]
                known = {g["id"] for g in GATEWAYS if g["kind"] == kind}
                if not names or any(x not in known for x in names):
                    raise ValueError(f"{var}: unknown gateway")
                v = ",".join(dict.fromkeys(names))
        if k == "STARS_ENABLED" and v not in ("true", "false"):
            raise ValueError("STARS_ENABLED: true or false")
        if k == "NALOG_INN" and v and not (v.isdigit() and len(v) == 12):
            raise ValueError("NALOG_INN: exactly 12 digits")
        if cur.get(k) != v:
            cur[k] = v
            changed.append(k)
    core.put(db, SETTING, json.dumps(cur, ensure_ascii=False))
    apply(db)
    return changed


def view(db: Session) -> dict:
    """What the page shows: every gateway with its fields (secrets as set /
    not set), whether it works, where its value comes from."""
    from . import payments
    mine = stored(db)
    base = env.public_url

    def field(f):
        k, ru, en, secret = f
        v = value(k)
        return {"key": k, "ru": ru, "en": en, "secret": secret, "set": bool(v),
                "value": "" if secret else v, "from": "panel" if mine.get(k) else ("env" if v else "")}
    mods = {**payments.CARD_GATEWAYS, **payments.CRYPTO_GATEWAYS}
    working = set(payments.card_gateways()) | set(payments.crypto_gateways())
    out = []
    for g in GATEWAYS:
        mod = mods.get(g["id"])
        ok = bool(mod and mod.configured())
        active = mod in working
        out.append({"id": g["id"], "name": g["name"], "kind": g["kind"], "how": g["how"],
                    "fields": [field(f) for f in g["fields"]], "ready": ok, "active": bool(active and ok),
                    "hook": f"{base}/api/pay/{g['id']}"})
    return {"gateways": out, "card": env.card_gateway, "crypto": env.crypto_gateway,
            "stars": bool(env.stars_enabled), "stars_ok": bool(env.bot_token),
            "nalog": [field(f) for f in NALOG], "methods": payments.methods(),
            "test_mode": env.payments_test_mode,
            # a quick tunnel's address changes on every restart: the gateways'
            # notification addresses would go stale
            "tunnel": ".trycloudflare.com" in base}
