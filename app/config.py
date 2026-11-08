"""
Configuration from environment variables (.env).

Everything a deploy needs is here. Things an operator changes while the
shop is running (plans, servers, texts, prices) live in the database and
are edited in the admin panel instead.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _flag(key: str, default: bool = False) -> bool:
    return _env(key, "1" if default else "0").lower() in ("1", "true", "yes", "on")


def _ids(key: str) -> set[int]:
    raw = _env(key).replace(" ", "")
    return {int(x) for x in raw.split(",") if x.lstrip("-").isdigit()}


# Currency symbols shown in the app. The code (RUB, USD...) is what payment
# gateways receive; the symbol is only for people.
CURRENCY_SYMBOLS = {
    "RUB": "₽", "USD": "$", "EUR": "€", "UAH": "₴", "KZT": "₸",
    "BYN": "Br", "GBP": "£", "TRY": "₺", "UZS": "сўм",
}


class Settings:
    def __init__(self) -> None:
        # --- core ---
        self.bot_token = _env("BOT_TOKEN")
        # PUBLIC_URL wins; otherwise it is derived from DOMAIN (the one value
        # docker-compose needs for the certificate anyway).
        domain = _env("DOMAIN")
        self.public_url = (_env("PUBLIC_URL") or (f"https://{domain}" if domain else "")
                           or "http://localhost:8000").rstrip("/")
        self.secret_key = _env("SECRET_KEY", "")
        self.database_url = _env("DATABASE_URL", "sqlite:///./data/vpn.db")
        self.data_dir = Path(_env("DATA_DIR", "./data"))
        self.default_lang = _env("DEFAULT_LANG", "ru").lower()[:2] or "ru"
        self.currency = _env("CURRENCY", "RUB").upper()

        # --- admin ---
        self.admin_login = _env("ADMIN_LOGIN", "admin")
        self.admin_password = _env("ADMIN_PASSWORD", "")
        # Telegram IDs that get a message about every payment and error.
        self.admin_telegram_ids = _ids("ADMIN_TELEGRAM_IDS")

        # --- bot ---
        # On start the service registers its own webhook, menu button and
        # /start command. Turn off if the same bot is used by something else.
        self.bot_setup = _flag("BOT_SETUP", True)

        # Tunnel mode (no domain): the metrics address of the cloudflared
        # container. The shop asks it for the current *.trycloudflare.com
        # address and re-registers the bot whenever that address changes.
        self.tunnel_metrics_url = _env("TUNNEL_METRICS_URL").rstrip("/")
        # DPI//CHECKER (dpichecker.st): paid checks of a server from many Russian regions
        self.dpichecker_api_key = _env("DPICHECKER_API_KEY")
        self.dpichecker_url = _env("DPICHECKER_URL", "https://dpichecker.st/api/v1").rstrip("/")

        # --- 3x-ui ---
        # Client emails on the panels look like "<prefix><telegram id>".
        # Change it if the panel already has clients named this way.
        self.xui_email_prefix = _env("XUI_EMAIL_PREFIX", "tg")
        # Optional first server — handy for a one-server quick start.
        # More servers are added in the admin panel.
        self.xui_url = _env("XUI_URL")
        self.xui_username = _env("XUI_USERNAME")
        self.xui_password = _env("XUI_PASSWORD")
        self.xui_token = _env("XUI_TOKEN")
        self.xui_inbounds = _env("XUI_INBOUNDS")
        self.xui_sub_url = _env("XUI_SUB_URL")
        self.xui_verify_tls = _flag("XUI_VERIFY_TLS", True)

        # --- payments ---
        # true = every order is treated as paid without money. For local
        # development only; the admin panel shows a red banner while it is on.
        self.payments_test_mode = _flag("PAYMENTS_TEST_MODE", False)

        # Which gateway takes card / SBP payments. Empty = no card payments.
        self.card_gateway = _env("CARD_GATEWAY").lower()

        self.yookassa_shop_id = _env("YOOKASSA_SHOP_ID")
        self.yookassa_secret = _env("YOOKASSA_SECRET_KEY")

        self.freekassa_merchant_id = _env("FREEKASSA_MERCHANT_ID")
        self.freekassa_secret1 = _env("FREEKASSA_SECRET1")
        self.freekassa_secret2 = _env("FREEKASSA_SECRET2")
        # The API key (cabinet → settings → API): only to ask the status of a
        # payment whose notification did not arrive. Payments work without it.
        self.freekassa_api_key = _env("FREEKASSA_API_KEY")
        self.freekassa_api_base = _env("FREEKASSA_API_BASE", "https://api.fk.life/v1").rstrip("/")
        self.freekassa_ips = tuple(x for x in _env(
            "FREEKASSA_IPS",
            "168.119.157.136,168.119.60.227,178.154.197.79,51.250.54.238",
        ).replace(" ", "").split(",") if x)

        self.platega_merchant_id = _env("PLATEGA_MERCHANT_ID")
        self.platega_secret = _env("PLATEGA_SECRET")
        self.platega_method = _env("PLATEGA_METHOD")

        self.paypalych_token = _env("PAYPALYCH_TOKEN")
        self.paypalych_shop_id = _env("PAYPALYCH_SHOP_ID")
        self.paypalych_api_base = _env("PAYPALYCH_API_BASE",
                                       "https://paypalych.com/api/v1").rstrip("/")

        self.lava_shop_id = _env("LAVA_SHOP_ID")
        self.lava_secret = _env("LAVA_SECRET_KEY")
        self.lava_additional = _env("LAVA_ADDITIONAL_KEY")

        self.wata_token = _env("WATA_TOKEN")
        self.wata_api_base = _env("WATA_API_BASE", "https://api.wata.pro/api/h2h").rstrip("/")

        self.tribute_api_key = _env("TRIBUTE_API_KEY")
        self.tribute_shop_id = _env("TRIBUTE_SHOP_ID")

        self.cloudpayments_public_id = _env("CLOUDPAYMENTS_PUBLIC_ID")
        self.cloudpayments_secret = _env("CLOUDPAYMENTS_API_SECRET")

        self.mulenpay_api_key = _env("MULENPAY_API_KEY")
        self.mulenpay_secret = _env("MULENPAY_SECRET_KEY")
        self.mulenpay_shop_id = _env("MULENPAY_SHOP_ID")
        self.mulenpay_api_base = (_env("MULENPAY_API_BASE") or "https://mulenpay.ru/api").rstrip("/")

        # RollyPay: roubles in, USDT on the balance. The signing secret checks notifications.
        self.rollypay_api_key = _env("ROLLYPAY_API_KEY")
        self.rollypay_signing_secret = _env("ROLLYPAY_SIGNING_SECRET")

        # cisPay: cards and SBP; the shop id and the secret key (cis_sec_…).
        self.cispay_shop_id = _env("CISPAY_SHOP_ID")
        self.cispay_api_key = _env("CISPAY_API_KEY")

        # Lave Pay (Laveshka): invoices in USD at the owner's rate.
        self.lavepay_api_key = _env("LAVEPAY_API_KEY")
        self.lavepay_webhook_secret = _env("LAVEPAY_WEBHOOK_SECRET")
        self.lavepay_rate = _env("LAVEPAY_RUB_PER_USD")

        # Crypto: CryptoBot, Heleket or Lave Pay. Empty = the first with keys.
        self.crypto_gateway = _env("CRYPTO_GATEWAY").lower()
        self.heleket_merchant = _env("HELEKET_MERCHANT_ID")
        self.heleket_api_key = _env("HELEKET_API_KEY")

        self.cryptobot_token = _env("CRYPTOBOT_TOKEN")
        self.cryptobot_testnet = _flag("CRYPTOBOT_TESTNET", False)

        # «Мой налог» (self-employed): a receipt for every paid order of the
        # gateways in NALOG_GATEWAYS. Empty list = the gateways that do not
        # make receipts themselves (YooKassa for the self-employed does).
        self.nalog_inn = _env("NALOG_INN")
        self.nalog_password = _env("NALOG_PASSWORD")
        self.nalog_gateways = tuple(x for x in (
            _env("NALOG_GATEWAYS") or "freekassa,platega,paypalych,lava,wata,rollypay,cispay").replace(" ", "").lower().split(",") if x)

        # The AI helper in support: any OpenAI-compatible API.
        self.ai_api_key = _env("AI_API_KEY")
        self.ai_base_url = (_env("AI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.ai_model = _env("AI_MODEL") or "gpt-4o-mini"

        # Telegram Stars need nothing but the bot token.
        self.stars_enabled = _flag("STARS_ENABLED", True)

    # ------------------------------------------------------------------
    @property
    def whole_prices(self) -> bool:
        """Discounted prices are rounded to whole units, except in currencies
        where cents are normal (USD, EUR, GBP)."""
        return self.currency not in ("USD", "EUR", "GBP")

    @property
    def currency_symbol(self) -> str:
        return CURRENCY_SYMBOLS.get(self.currency, self.currency)

    @property
    def https(self) -> bool:
        return self.public_url.startswith("https://")

    def problems(self) -> list[dict]:
        """Dangerous or broken settings, reported at start and in the panel.
        The code lets the panel show the text in its own language."""
        out = []

        def add(code: str, text: str) -> None:
            out.append({"code": code, "text": text})

        if len(self.secret_key) < 32:
            add("secret", "SECRET_KEY is empty or shorter than 32 characters")
        if len(self.admin_password) < 8:
            add("admin_password", "ADMIN_PASSWORD is empty or shorter than 8 characters")
        if not self.bot_token:
            add("bot_token", "BOT_TOKEN is not set — the mini app cannot sign users in")
        if not self.https:
            add("https", "PUBLIC_URL is not https:// — Telegram opens mini apps only over HTTPS")
        if self.payments_test_mode:
            add("test_mode", "PAYMENTS_TEST_MODE is on — every order is free")
        return out


settings = Settings()


def reload() -> Settings:
    """Re-read the environment in place, so every importer sees the change
    (used by tests)."""
    settings.__init__()
    return settings
