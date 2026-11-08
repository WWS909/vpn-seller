"""CloudPayments — cards, SBP, Mir Pay; a payment page from /orders/create."""

from __future__ import annotations

import base64
import hashlib
import hmac

import httpx
from sqlalchemy.orm import Session

from ..config import settings as env
from ..models import Transaction, User

NAME = "cloudpayments"
API = "https://api.cloudpayments.ru"


def configured() -> bool:
    return bool(env.cloudpayments_public_id and env.cloudpayments_secret)


def _auth() -> tuple[str, str]:
    return (env.cloudpayments_public_id, env.cloudpayments_secret)


def create(db: Session, tx: Transaction, user: User) -> dict:
    from . import description
    r = httpx.post(f"{API}/orders/create", auth=_auth(), timeout=20, json={
        "Amount": round(float(tx.amount), 2),
        "Currency": env.currency,
        "Description": description(db, tx)[:200],
        "InvoiceId": str(tx.id),
        "AccountId": str(user.tg_id),
        "RequireConfirmation": False,          # one-stage: the money is taken at once
        "SendEmail": False,
        "SuccessRedirectUrl": f"{env.public_url}/app?paid={tx.id}",
        "FailRedirectUrl": f"{env.public_url}/app",
    })
    if r.status_code >= 400:
        raise RuntimeError(r.text[:200])
    data = r.json()
    model = data.get("Model") or {}
    if not data.get("Success") or not model.get("Url"):
        raise RuntimeError(str(data.get("Message") or data)[:200])
    tx.external_id = str(model.get("Id") or tx.id)
    return {"type": "url", "url": model["Url"]}


def find(invoice_id: str) -> dict:
    """The latest operation of an order, asked with our own keys."""
    r = httpx.post(f"{API}/payments/find", auth=_auth(), json={"InvoiceId": invoice_id}, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"CloudPayments {r.status_code}")
    data = r.json()
    return (data.get("Model") or {}) if data.get("Success") else {}


def status(tx: Transaction) -> dict:
    m = find(str(tx.id))
    return {"paid": m.get("Status") == "Completed" and str(m.get("InvoiceId") or "") == str(tx.id),
            "amount": m.get("Amount"), "currency": m.get("Currency") or None,
            "ref": str(m.get("InvoiceId") or "")}


def verify(raw: bytes, headers: dict) -> bool:
    """Content-HMAC: base64 HMAC-SHA256 of the raw body with the API secret."""
    if not configured():
        return False
    h = {k.lower(): v for k, v in headers.items()}
    got = (h.get("content-hmac") or "").strip()
    if not got:
        return False
    want = base64.b64encode(hmac.new(env.cloudpayments_secret.encode(), raw, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(want, got)
