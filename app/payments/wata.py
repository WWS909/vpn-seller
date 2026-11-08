"""WATA — cards and SBP. Notices are signed with WATA's RSA key."""

from __future__ import annotations

import base64
import time

import httpx
from sqlalchemy.orm import Session

from ..config import settings as env
from ..models import Transaction, User

NAME = "wata"
_key: dict = {"pem": None, "at": 0.0, "tried": 0.0}


def configured() -> bool:
    return bool(env.wata_token)


def _headers() -> dict:
    return {"Authorization": f"Bearer {env.wata_token}", "Content-Type": "application/json"}


def create(db: Session, tx: Transaction, user: User) -> dict:
    from . import description
    r = httpx.post(f"{env.wata_api_base}/links", headers=_headers(), timeout=20, json={
        "amount": round(float(tx.amount), 2),
        "currency": env.currency,
        "description": description(db, tx)[:200],
        "orderId": str(tx.id),
        "successRedirectUrl": f"{env.public_url}/app?paid={tx.id}",
        "failRedirectUrl": f"{env.public_url}/app",
    })
    if r.status_code >= 400:
        raise RuntimeError(r.text[:200])
    data = r.json()
    if not data.get("id") or not data.get("url"):
        raise RuntimeError(f"no payment link in the answer: {str(data)[:150]}")
    tx.external_id = str(data["id"])
    return {"type": "url", "url": data["url"]}


def _public_key(refresh: bool = False):
    """WATA's public key, kept for an hour. A forced reload (a key that
    stopped fitting) is allowed once in five minutes, so fake notices
    cannot make the shop ask WATA over and over."""
    from cryptography.hazmat.primitives import serialization
    t = time.time()
    if _key["pem"] is None or t - _key["at"] > 3600 or (refresh and t - _key["tried"] > 300):
        _key["tried"] = t
        r = httpx.get(f"{env.wata_api_base}/public-key", headers=_headers(), timeout=15)
        if r.status_code >= 400:
            raise RuntimeError(f"WATA {r.status_code}")
        _key["pem"], _key["at"] = r.json()["value"], t
    return serialization.load_pem_public_key(_key["pem"].encode())


def verify(raw: bytes, signature: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    if not configured() or not signature:
        return False
    try:
        sig = base64.b64decode(signature.strip())
    except ValueError:
        return False
    for refresh in (False, True):
        try:
            _public_key(refresh).verify(sig, raw, padding.PKCS1v15(), hashes.SHA512())
            return True
        except InvalidSignature:
            continue
        except Exception:  # noqa: BLE001 — key not reachable or not RSA: not verified
            return False
    return False


def can_poll() -> bool:
    return configured()


def status(tx: Transaction) -> dict:
    """Payments of this order, found by our order id (a link can be paid
    after a failed try, so any one of them that is Paid counts)."""
    r = httpx.get(f"{env.wata_api_base}/v2/transactions/", headers=_headers(), timeout=20,
                  params={"orderId": str(tx.id), "maxResultCount": 20})
    if r.status_code >= 400:
        raise RuntimeError(f"WATA {r.status_code}")
    items = [t for t in (r.json().get("items") or []) if str(t.get("orderId")) == str(tx.id)]
    paid = [t for t in items if str(t.get("status")) == "Paid"]
    t = (paid or items or [{}])[0]
    return {"paid": bool(paid), "ref": str(t.get("orderId") or "") or None,
            "amount": t.get("amount"), "currency": t.get("currency")}
