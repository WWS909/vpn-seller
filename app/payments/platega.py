"""Platega — cards, SBP and crypto for individuals, one contract."""

from __future__ import annotations

import hmac

import httpx
from sqlalchemy.orm import Session

from ..config import settings as env
from ..models import Transaction, User

NAME = "platega"
API = "https://app.platega.io"


def configured() -> bool:
    return bool(env.platega_merchant_id and env.platega_secret)


def _headers() -> dict:
    return {"X-MerchantId": env.platega_merchant_id, "X-Secret": env.platega_secret}


def create(db: Session, tx: Transaction, user: User) -> dict:
    from . import description
    body = {
        "paymentDetails": {"amount": round(tx.amount, 2), "currency": env.currency},
        "description": description(db, tx),
        "return": f"{env.public_url}/app?paid={tx.id}",
        "failedUrl": f"{env.public_url}/app",
        "payload": str(tx.id),
        # Platega's antifraud may switch a shop off when payments arrive
        # without a stable customer id.
        "metadata": {"userId": str(user.tg_id)},
    }
    if env.platega_method.isdigit():
        body["paymentMethod"] = int(env.platega_method)
    r = httpx.post(f"{API}/transaction/process", json=body, headers=_headers(), timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(r.text[:200])
    data = r.json()
    url = data.get("redirect") or data.get("url")
    tx.external_id = str(data.get("transactionId") or data.get("id") or "")
    if not url or not tx.external_id:
        raise RuntimeError(f"no payment link in the answer: {str(data)[:150]}")
    return {"type": "url", "url": url}


def status(tx: Transaction) -> dict:
    r = httpx.get(f"{API}/transaction/{tx.external_id}", headers=_headers(), timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Platega {r.status_code}")
    data = r.json()
    details = data.get("paymentDetails") or {}
    return {"paid": str(data.get("status", "")).upper() == "CONFIRMED",
            "amount": details.get("amount", data.get("amount")),
            "currency": details.get("currency") or data.get("currency")}


def verify(headers: dict) -> bool:
    """Platega authenticates its callbacks with the same merchant id and
    secret the shop uses for requests — that is their documented scheme."""
    h = {k.lower(): v for k, v in headers.items()}
    return bool(configured()
                and h.get("x-merchantid", "") == env.platega_merchant_id
                and hmac.compare_digest(h.get("x-secret", "").encode(),
                                        env.platega_secret.encode()))
