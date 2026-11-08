"""cisPay — cards and SBP in roubles. https://cispay.app/developers

Requests carry the shop id (X-Shop-ID) and the secret key (X-Api-Key,
cis_sec_…). Sums go in kopecks. Notifications are signed:
X-Signature = HMAC-SHA256(secret key, raw body), hex. Even a signed one is
only a hint — the shop then asks cisPay for the payment's status.
The notification address is set in cisPay's dashboard.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid

import httpx
from sqlalchemy.orm import Session

from ..config import settings as env
from ..models import Transaction, User

NAME = "cispay"
API = "https://api.cispay.app"


def configured() -> bool:
    return bool(env.cispay_shop_id and env.cispay_api_key)


def _headers() -> dict:
    return {"X-Shop-ID": env.cispay_shop_id, "X-Api-Key": env.cispay_api_key,
            "Content-Type": "application/json"}


def create(db: Session, tx: Transaction, user: User) -> dict:
    from . import description
    if env.currency != "RUB":
        raise RuntimeError("cisPay takes roubles only")
    body = {
        "amount": int(round(float(tx.amount) * 100)),          # kopecks
        "currency": "RUB",
        "order_id": f"{tx.id}-{uuid.uuid4().hex[:8]}",
        "customer_id": str(user.tg_id),                         # SBP asks for it
        "description": description(db, tx)[:500],
        "redirect_success_url": f"{env.public_url}/app?paid={tx.id}",
        "redirect_fail_url": f"{env.public_url}/app",
    }
    r = httpx.post(f"{API}/payments", json=body, headers=_headers(), timeout=20)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"cisPay {r.status_code}: {r.text[:200]}")
    data = r.json()
    url, pid = data.get("payment_url"), str(data.get("id") or "")
    if not url or not pid:
        raise RuntimeError(f"no payment link in the answer: {str(data)[:150]}")
    tx.external_id = pid
    return {"type": "url", "url": url}


def status(tx: Transaction) -> dict:
    r = httpx.get(f"{API}/payments/status", params={"id": tx.external_id}, headers=_headers(), timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"cisPay {r.status_code}")
    data = r.json()
    order = str(data.get("order_id") or "")
    # a test payment or one the gateway holds back is not money
    real = not data.get("is_sandbox") and not data.get("block_reason")
    try:
        rub = int(data.get("source_amount") if data.get("source_amount") is not None else data.get("amount")) / 100
    except (TypeError, ValueError):
        rub = 0.0
    return {"paid": real and str(data.get("status", "")).upper() == "PAID",
            "amount": rub,
            "currency": str(data.get("source_currency") or data.get("currency") or "RUB").upper(),
            "ref": order.split("-", 1)[0] if order else ""}


def verify(raw: bytes, signature: str) -> bool:
    if not configured() or not signature:
        return False
    want = hmac.new(env.cispay_api_key.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, signature.strip().lower())
