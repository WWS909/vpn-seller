"""YooKassa — cards and SBP for shops with an ИП or self-employed status."""

from __future__ import annotations

import ipaddress
import uuid

import httpx
from sqlalchemy.orm import Session

from ..config import settings as env
from ..models import Transaction, User

NAME = "yookassa"
API = "https://api.yookassa.ru/v3"

# Networks YooKassa sends notifications from (their documentation).
NETS = ("185.71.76.0/27", "185.71.77.0/27", "77.75.153.0/25", "77.75.156.11/32",
        "77.75.156.35/32", "77.75.154.128/25", "2a02:5180::/32")


def configured() -> bool:
    return bool(env.yookassa_shop_id and env.yookassa_secret)


def _auth() -> tuple[str, str]:
    return (env.yookassa_shop_id, env.yookassa_secret)


def create(db: Session, tx: Transaction, user: User) -> dict:
    from . import description
    r = httpx.post(f"{API}/payments", auth=_auth(), timeout=20,
                   headers={"Idempotence-Key": f"shop-{tx.id}-{uuid.uuid4().hex[:8]}"},
                   json={
                       "amount": {"value": f"{tx.amount:.2f}", "currency": env.currency},
                       "capture": True,
                       "confirmation": {"type": "redirect",
                                        "return_url": f"{env.public_url}/app?paid={tx.id}"},
                       "description": description(db, tx)[:128],
                       "metadata": {"tx_id": str(tx.id)},
                   })
    if r.status_code >= 400:
        raise RuntimeError(r.text[:200])
    data = r.json()
    tx.external_id = data.get("id", "")
    return {"type": "url", "url": data["confirmation"]["confirmation_url"]}


def ip_allowed(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in ipaddress.ip_network(n) for n in NETS)


def fetch(payment_id: str) -> dict:
    """The payment as YooKassa itself sees it. The notification body is only a
    hint; the decision is made on this answer."""
    r = httpx.get(f"{API}/payments/{payment_id}", auth=_auth(), timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"YooKassa {r.status_code}")
    return r.json()


def status(tx: Transaction) -> dict:
    p = fetch(tx.external_id)
    amount = p.get("amount") or {}
    return {"paid": p.get("status") == "succeeded" and bool(p.get("paid")),
            "amount": amount.get("value"), "currency": amount.get("currency"),
            "ref": str((p.get("metadata") or {}).get("tx_id", ""))}
