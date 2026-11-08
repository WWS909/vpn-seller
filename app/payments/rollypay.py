"""RollyPay — SBP, cards and crypto in roubles (the shop's balance is kept
in USDT). https://docs.rollypay.io

Requests carry the API key and a one-time X-Nonce. Notifications are
signed: X-Signature = HMAC-SHA256(signing secret, X-Timestamp + "." + raw
body), hex. Even a signed notification is only a hint — the payment is
taken as paid after the shop asks RollyPay itself (status() below).
The notification address is set on the terminal in RollyPay's dashboard.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid

import httpx
from sqlalchemy.orm import Session

from ..config import settings as env
from ..models import Transaction, User

NAME = "rollypay"
API = "https://rollypay.io/api/v1"
# Retries go on for about two hours; older timestamps are refused.
MAX_AGE = 3 * 3600


def configured() -> bool:
    return bool(env.rollypay_api_key and env.rollypay_signing_secret)


def _headers() -> dict:
    return {"X-API-Key": env.rollypay_api_key, "X-Nonce": str(uuid.uuid4()),
            "Content-Type": "application/json"}


def create(db: Session, tx: Transaction, user: User) -> dict:
    from . import description
    if env.currency != "RUB":
        raise RuntimeError("RollyPay is set up for roubles only")
    body = {
        "amount": f"{float(tx.amount):.2f}",
        "payment_currency": "RUB",
        # unique even if the shop is installed again and numbers start over
        "order_id": f"{tx.id}-{uuid.uuid4().hex[:8]}",
        "description": description(db, tx)[:200],
        "customer_id": str(user.tg_id),
        "redirect_url": f"{env.public_url}/app?paid={tx.id}",
        "success_redirect_url": f"{env.public_url}/app?paid={tx.id}",
        "fail_redirect_url": f"{env.public_url}/app",
    }
    r = httpx.post(f"{API}/payments", json=body, headers=_headers(), timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"RollyPay {r.status_code}: {r.text[:200]}")
    data = r.json()
    url, pid = data.get("pay_url"), str(data.get("payment_id") or "")
    if not url or not pid:
        raise RuntimeError(f"no payment link in the answer: {str(data)[:150]}")
    tx.external_id = pid
    return {"type": "url", "url": url}


def status(tx: Transaction) -> dict:
    r = httpx.get(f"{API}/payments/{tx.external_id}", headers=_headers(), timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"RollyPay {r.status_code}")
    data = r.json()
    if isinstance(data.get("payment"), dict):
        data = data["payment"]
    order = str(data.get("order_id") or "")
    return {"paid": str(data.get("status", "")).lower() == "paid",
            "amount": data.get("amount"),
            "currency": str(data.get("payment_currency") or data.get("currency") or "RUB").upper(),
            "ref": order.split("-", 1)[0] if order else ""}


def verify(raw: bytes, headers: dict) -> bool:
    h = {k.lower(): v for k, v in headers.items()}
    sig, ts = h.get("x-signature", ""), h.get("x-timestamp", "")
    if not configured() or not sig or not ts.isdigit() or abs(time.time() - int(ts)) > MAX_AGE:
        return False
    want = hmac.new(env.rollypay_signing_secret.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig.strip().lower())
