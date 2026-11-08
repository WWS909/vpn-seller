"""Tribute (tribute.tg) shop orders — cards, SBP and more, one-time payments."""

from __future__ import annotations

import base64
import hashlib
import hmac

import httpx
from sqlalchemy.orm import Session

from ..config import settings as env
from ..models import Transaction, User

NAME = "tribute"
API = "https://tribute.tg/api/v1"


def configured() -> bool:
    return bool(env.tribute_api_key)


def _headers() -> dict:
    return {"Api-Key": env.tribute_api_key, "Accept": "application/json"}


def create(db: Session, tx: Transaction, user: User) -> dict:
    from . import description
    body = {
        "amount": int(round(float(tx.amount) * 100)),          # kopecks / cents
        "currency": env.currency.lower(),
        "title": description(db, tx)[:100],
        "description": description(db, tx)[:300],
        "comment": str(tx.id),
        "customerId": str(user.tg_id),
        "period": "onetime",
        "successUrl": f"{env.public_url}/app?paid={tx.id}",
        "failUrl": f"{env.public_url}/app",
    }
    if env.tribute_shop_id.isdigit():
        body["shopId"] = int(env.tribute_shop_id)
    r = httpx.post(f"{API}/shop/orders", json=body, headers=_headers(), timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(r.text[:200])
    data = r.json()
    tx.external_id = str(data.get("uuid") or "")
    url = data.get("paymentUrl")
    if not tx.external_id or not url:
        raise RuntimeError(f"no payment link in the answer: {str(data)[:150]}")
    return {"type": "url", "url": url}


def order(uuid: str) -> dict:
    """The order as Tribute sees it, asked with our own key."""
    r = httpx.get(f"{API}/shop/orders/{uuid}", headers=_headers(), timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Tribute {r.status_code}")
    data = r.json()
    return data.get("order", data) if isinstance(data, dict) else {}


def status(tx: Transaction) -> dict:
    o = order(tx.external_id)
    return {"paid": str(o.get("status", "")).lower() == "paid",
            "amount": int(o.get("amount") or 0) / 100,
            "currency": str(o.get("currency") or "").upper() or None}


def verify(raw: bytes, signature: str) -> bool:
    """trbt-signature: HMAC-SHA256 of the body with the API key. Their docs
    do not say hex or base64, so either spelling of the same HMAC is taken."""
    if not configured() or not signature:
        return False
    digest = hmac.new(env.tribute_api_key.encode(), raw, hashlib.sha256).digest()
    got = signature.strip()
    return hmac.compare_digest(digest.hex(), got.lower()) or \
        hmac.compare_digest(base64.b64encode(digest).decode(), got)
