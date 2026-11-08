"""CryptoBot (@CryptoBot, Crypto Pay API) — crypto invoices priced in fiat."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
from sqlalchemy.orm import Session

from ..config import settings as env
from ..models import Transaction, User

NAME = "cryptobot"
FIATS = {"USD", "EUR", "RUB", "BYN", "UAH", "GBP", "CNY", "KZT", "UZS", "GEL", "TRY",
         "AMD", "THB", "INR", "BRL", "IDR", "AZN", "AED", "PLN", "ILS"}


def configured() -> bool:
    return bool(env.cryptobot_token)


def _api(method: str) -> str:
    host = "testnet-pay.crypt.bot" if env.cryptobot_testnet else "pay.crypt.bot"
    return f"https://{host}/api/{method}"


def create(db: Session, tx: Transaction, user: User) -> dict:
    from . import description
    if env.currency not in FIATS:
        raise RuntimeError(f"CryptoBot does not price invoices in {env.currency}")
    r = httpx.post(_api("createInvoice"), timeout=20, headers={
        "Crypto-Pay-API-Token": env.cryptobot_token}, json={
        "currency_type": "fiat",
        "fiat": env.currency,
        "amount": f"{tx.amount:.2f}",
        "description": description(db, tx),
        "payload": str(tx.id),
        "expires_in": 3600,
    })
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not data.get("ok"):
        raise RuntimeError(str(data.get("error") or r.text)[:200])
    inv = data["result"]
    tx.external_id = str(inv.get("invoice_id", ""))
    url = inv.get("mini_app_invoice_url") or inv.get("bot_invoice_url") or inv.get("pay_url")
    return {"type": "url", "url": url, "telegram": True}


def status(tx: Transaction) -> dict:
    r = httpx.get(_api("getInvoices"), timeout=20, params={"invoice_ids": tx.external_id},
                  headers={"Crypto-Pay-API-Token": env.cryptobot_token})
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not data.get("ok"):
        raise RuntimeError(str(data.get("error") or r.status_code)[:200])
    items = (data.get("result") or {}).get("items") or []
    inv = next((i for i in items if str(i.get("invoice_id")) == tx.external_id), None)
    if not inv:
        return {"paid": False}
    return {"paid": inv.get("status") == "paid", "amount": inv.get("amount"),
            "currency": inv.get("fiat") if inv.get("currency_type") == "fiat" else None,
            "ref": str(inv.get("payload", ""))}


def verify(raw: bytes, signature: str) -> bool:
    """HMAC-SHA256 of the body; the key is SHA256 of the app token."""
    if not env.cryptobot_token or not signature:
        return False
    key = hashlib.sha256(env.cryptobot_token.encode()).digest()
    expected = hmac.new(key, raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.strip().lower().encode())


def parse(raw: bytes) -> dict | None:
    """The paid invoice from a webhook body, or None if it is not a payment."""
    body = json.loads(raw.decode() or "{}")
    if body.get("update_type") != "invoice_paid":
        return None
    inv = body.get("payload") or {}
    return inv if inv.get("status") == "paid" else None
