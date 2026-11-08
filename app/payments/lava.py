"""Lava (lava.ru business) — cards and SBP in roubles, for individuals and ИП."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
from sqlalchemy.orm import Session

from ..config import settings as env
from ..models import Transaction, User

NAME = "lava"
API = "https://api.lava.ru"


def configured() -> bool:
    return bool(env.lava_shop_id and env.lava_secret and env.lava_additional)


def _sign(key: str, raw: bytes) -> str:
    return hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()


def _post(path: str, body: dict) -> dict:
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    r = httpx.post(API + path, content=raw, timeout=20, headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "Signature": _sign(env.lava_secret, raw)})
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400 or not isinstance(data, dict):
        raise RuntimeError(str(data.get("error") or r.text)[:200] if isinstance(data, dict) else r.text[:200])
    return data


def create(db: Session, tx: Transaction, user: User) -> dict:
    from . import description
    if env.currency != "RUB":
        raise RuntimeError("Lava takes roubles only")
    data = _post("/business/invoice/create", {
        "sum": round(float(tx.amount), 2),
        "orderId": str(tx.id),
        "shopId": env.lava_shop_id,
        "hookUrl": f"{env.public_url}/api/pay/lava",
        "successUrl": f"{env.public_url}/app?paid={tx.id}",
        "failUrl": f"{env.public_url}/app",
        "expire": 60,
        "comment": description(db, tx)[:200],
    })
    inv = data.get("data") or {}
    if not inv.get("id") or not inv.get("url"):
        raise RuntimeError(f"no payment link in the answer: {str(data)[:150]}")
    tx.external_id = str(inv["id"])
    return {"type": "url", "url": inv["url"]}


def status(tx: Transaction) -> dict:
    data = _post("/business/invoice/status", {
        "shopId": env.lava_shop_id, "orderId": str(tx.id), "invoiceId": tx.external_id})
    inv = data.get("data") or {}
    return {"paid": str(inv.get("status", "")).lower() in ("success", "paid"),
            "amount": inv.get("amount", inv.get("sum")), "currency": "RUB",
            "ref": str(inv.get("order_id") or inv.get("orderId") or tx.id)}


def verify(raw: bytes, headers: dict) -> dict | None:
    """The notice body if its signature is right, else None. Lava signs the
    body with its keys sorted, with the "additional" key — and writers of
    that sorting differ in how they escape unicode and "/", so each
    spelling of the same data is accepted."""
    if not configured():
        return None
    h = {k.lower(): v for k, v in headers.items()}
    got = str(h.get("authorization", "")).replace("Bearer ", "").strip().lower()
    try:
        body = json.loads(raw.decode() or "{}")
    except ValueError:
        return None
    if not got or not isinstance(body, dict):
        return None
    for ascii_only in (False, True):
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=ascii_only)
        for variant in (text, text.replace("/", "\\/")):
            if hmac.compare_digest(_sign(env.lava_additional, variant.encode()).encode(), got.encode()):
                return body
    return None
