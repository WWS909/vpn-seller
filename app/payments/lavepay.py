"""
Lave Pay (Лавешка) — crypto and SBP through the Laveshka Telegram app.

Invoices are in US dollars: the shop's price is converted at the owner's
rate (LAVEPAY_RUB_PER_USD) when the order is paid for, rounded up to a
cent, and the dollar amount is stored with the order — a later change of
the rate does not touch invoices already made.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import time

import httpx
from sqlalchemy.orm import Session

from ..config import settings as env
from ..models import Transaction, User

NAME = "lavepay"
API = "https://api.laveshka.io/merchant/v1"


def configured() -> bool:
    return bool(env.lavepay_api_key and env.lavepay_webhook_secret and usd_rate() > 0)


def usd_rate() -> float:
    """How many units of the shop's currency one US dollar costs."""
    if env.currency == "USD":
        return 1.0
    try:
        return float(env.lavepay_rate.replace(",", "."))
    except ValueError:
        return 0.0


def to_usd(amount: float) -> float:
    return math.ceil(round(float(amount) / usd_rate() * 100, 6)) / 100


def _headers() -> dict:
    return {"X-Api-Key": env.lavepay_api_key, "Accept": "application/json"}


def create(db: Session, tx: Transaction, user: User) -> dict:
    from . import description
    usd = to_usd(tx.amount)
    if not 0.10 <= usd <= 10000:
        raise RuntimeError(f"Lave Pay takes 0.10–10000 USD, this is {usd:.2f}")
    r = httpx.post(f"{API}/invoices", headers=_headers(), timeout=20, json={
        "amount": f"{usd:.2f}", "currency": "USD", "description": description(db, tx)[:256],
        "order_id": str(tx.id), "payload": str(tx.id), "return_url": f"{env.public_url}/app?paid={tx.id}",
        "ttl": 3600,
    })
    if r.status_code >= 400:
        raise RuntimeError(r.text[:200])
    data = r.json()
    inv = data.get("data") or {}
    if not data.get("ok") or not inv.get("id") or not inv.get("pay_url"):
        raise RuntimeError(f"no invoice in the answer: {str(data)[:150]}")
    tx.external_id = str(inv["id"])
    tx.ext_amount = usd
    return {"type": "url", "url": inv["pay_url"], "telegram": inv["pay_url"].startswith("https://t.me/")}


def invoice(inv_id: str) -> dict:
    r = httpx.get(f"{API}/invoices/{inv_id}", headers=_headers(), timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Lave Pay {r.status_code}")
    data = r.json()
    return (data.get("data") or {}) if data.get("ok") else {}


def status(tx: Transaction) -> dict:
    """Paid in full means the dollar amount of the invoice, not the
    roubles: the check against the order's price is done here, and the
    order's own amount is reported back when the dollars are right."""
    inv = invoice(tx.external_id)
    try:
        usd = float(inv.get("amount") or 0)
    except (TypeError, ValueError):
        usd = 0.0
    full = usd + 0.001 >= float(tx.ext_amount or 0) > 0 and str(inv.get("currency") or "USD").upper() == "USD"
    return {"paid": str(inv.get("status") or "") == "paid" and str(inv.get("order_id") or "") == str(tx.id),
            "amount": tx.amount if full else 0, "currency": None, "ref": str(inv.get("order_id") or "")}


def verify(raw: bytes, headers: dict, max_age: int = 300) -> bool:
    """x-lavepay-signature: "v1=" + hex HMAC-SHA256(webhook secret,
    "<timestamp>." + raw body); the timestamp must be fresh."""
    if not configured():
        return False
    h = {k.lower(): v for k, v in headers.items()}
    stamp, got = str(h.get("x-lavepay-timestamp") or ""), str(h.get("x-lavepay-signature") or "")
    if not stamp.isdigit() or abs(time.time() - int(stamp)) > max_age or not got.startswith("v1="):
        return False
    want = hmac.new(env.lavepay_webhook_secret.encode(), stamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, got[3:].strip().lower())
