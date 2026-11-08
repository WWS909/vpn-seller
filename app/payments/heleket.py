"""Heleket — crypto invoices priced in fiat."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re

import httpx
from sqlalchemy.orm import Session

from ..config import settings as env
from ..models import Transaction, User

NAME = "heleket"
API = "https://api.heleket.com"
_SIGN_TAIL = re.compile(r',\s*"sign"\s*:\s*"[^"]*"\s*}\s*$')


def configured() -> bool:
    return bool(env.heleket_merchant and env.heleket_api_key)


def _sign(raw: bytes) -> str:
    return hashlib.md5(base64.b64encode(raw) + env.heleket_api_key.encode()).hexdigest()


def _post(path: str, body: dict) -> dict:
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    r = httpx.post(API + path, content=raw, timeout=20, headers={
        "Content-Type": "application/json", "merchant": env.heleket_merchant, "sign": _sign(raw)})
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400 or not isinstance(data, dict) or data.get("state") != 0:
        raise RuntimeError(str((data or {}).get("message") or r.text)[:200])
    return data.get("result") or {}


def create(db: Session, tx: Transaction, user: User) -> dict:
    res = _post("/v1/payment", {
        "amount": f"{tx.amount:.2f}",
        "currency": env.currency,
        "order_id": str(tx.id),
        "url_callback": f"{env.public_url}/api/pay/heleket",
        "url_return": f"{env.public_url}/app?paid={tx.id}",
        "url_success": f"{env.public_url}/app?paid={tx.id}",
        "lifetime": 3600,
    })
    if not res.get("uuid") or not res.get("url"):
        raise RuntimeError(f"no payment link in the answer: {str(res)[:150]}")
    tx.external_id = str(res["uuid"])
    return {"type": "url", "url": res["url"]}


def status(tx: Transaction) -> dict:
    res = _post("/v1/payment/info", {"uuid": tx.external_id})
    return {"paid": str(res.get("payment_status", "")).lower() in ("paid", "paid_over"),
            "amount": res.get("amount"), "currency": res.get("currency"),
            "ref": str(res.get("order_id", ""))}


def verify(raw: bytes) -> dict | None:
    """The notice body if it is signed with our key, else None. The
    signature covers the body without its "sign" field, exactly as sent."""
    if not configured():
        return None
    text = raw.decode("utf-8", "replace")
    try:
        body = json.loads(text)
    except ValueError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("sign"), str):
        return None
    unsigned = _SIGN_TAIL.sub("}", text).encode()
    if hmac.compare_digest(_sign(unsigned).encode(), body["sign"].lower().encode()):
        return body
    rest = {k: v for k, v in body.items() if k != "sign"}
    for variant in (json.dumps(rest, separators=(",", ":"), ensure_ascii=False),
                    json.dumps(rest, separators=(",", ":")).replace("/", "\\/")):
        if hmac.compare_digest(_sign(variant.encode()).encode(), body["sign"].lower().encode()):
            return body
    return None
