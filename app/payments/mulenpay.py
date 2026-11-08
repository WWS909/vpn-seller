"""MulenPay — cards and SBP. Its callback is not signed: the payment is
always checked with MulenPay's own API before an order is closed."""

from __future__ import annotations

import hashlib

import httpx
from sqlalchemy.orm import Session

from ..config import settings as env
from ..models import Transaction, User

NAME = "mulenpay"


def configured() -> bool:
    return bool(env.mulenpay_api_key and env.mulenpay_secret and env.mulenpay_shop_id.isdigit())


def _headers() -> dict:
    return {"Authorization": f"Bearer {env.mulenpay_api_key}", "Accept": "application/json"}


def sign(currency: str, amount: str) -> str:
    return hashlib.sha1((currency + amount + env.mulenpay_shop_id + env.mulenpay_secret).encode()).hexdigest()


def create(db: Session, tx: Transaction, user: User) -> dict:
    from . import description
    currency, amount = env.currency.lower(), f"{float(tx.amount):.2f}"
    what = description(db, tx)[:128]
    r = httpx.post(f"{env.mulenpay_api_base}/v2/payments", headers=_headers(), timeout=20, json={
        "currency": currency, "amount": amount, "uuid": str(tx.id), "shopId": int(env.mulenpay_shop_id),
        "description": what, "website_url": env.public_url,
        # The receipt line (54-ФЗ): a service, full payment, no VAT.
        "items": [{"description": what, "quantity": 1, "price": float(amount),
                   "vat_code": 0, "payment_subject": 4, "payment_mode": 4}],
        "sign": sign(currency, amount),
    })
    if r.status_code >= 400:
        raise RuntimeError(r.text[:200])
    data = r.json()
    if not data.get("success") or not data.get("paymentUrl") or not data.get("id"):
        raise RuntimeError(f"no payment link in the answer: {str(data)[:150]}")
    tx.external_id = str(data["id"])
    return {"type": "url", "url": data["paymentUrl"]}


def payment(pid: str) -> dict:
    r = httpx.get(f"{env.mulenpay_api_base}/v2/payments/{pid}", headers=_headers(), timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"MulenPay {r.status_code}")
    data = r.json()
    return (data.get("payment") or {}) if data.get("success") else {}


def status(tx: Transaction) -> dict:
    p = payment(tx.external_id)
    return {"paid": int(p.get("status") or -1) == 3 and str(p.get("uuid") or "") == str(tx.id),
            "amount": p.get("amount"), "currency": str(p.get("currency") or "").upper() or None,
            "ref": str(p.get("uuid") or "")}
