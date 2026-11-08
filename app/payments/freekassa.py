"""FreeKassa — an aggregator that works with individuals without an ИП."""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

import httpx

from sqlalchemy.orm import Session

from ..config import settings as env
from ..models import Transaction, User

NAME = "freekassa"
FORM_URL = "https://pay.fk.money/"


def configured() -> bool:
    return bool(env.freekassa_merchant_id and env.freekassa_secret1 and env.freekassa_secret2)


def form_sign(amount: str, order_id: str) -> str:
    """md5(merchant_id:amount:secret1:currency:order_id)"""
    raw = ":".join([env.freekassa_merchant_id, amount, env.freekassa_secret1,
                    env.currency, order_id])
    return hashlib.md5(raw.encode()).hexdigest()


def notice_sign(amount: str, order_id: str) -> str:
    """md5(merchant_id:amount:secret2:order_id) — the amount exactly as received."""
    raw = ":".join([env.freekassa_merchant_id, amount, env.freekassa_secret2, order_id])
    return hashlib.md5(raw.encode()).hexdigest()


def create(db: Session, tx: Transaction, user: User) -> dict:
    amount = f"{tx.amount:.2f}"
    params = {"m": env.freekassa_merchant_id, "oa": amount, "o": str(tx.id),
              "currency": env.currency, "s": form_sign(amount, str(tx.id)),
              "lang": "ru" if (user.lang or env.default_lang) == "ru" else "en"}
    tx.external_id = str(tx.id)
    return {"type": "url", "url": FORM_URL + "?" + urlencode(params)}


# ---------------------------------------------------------------------------
#  Asking FreeKassa about a payment (API v1, needs FREEKASSA_API_KEY)
# ---------------------------------------------------------------------------
PAID = 1                        # FreeKassa order status: 0 new, 1 paid, 6 refund, 8 error, 9 cancelled


def can_poll() -> bool:
    return configured() and bool(env.freekassa_api_key)


def api_sign(data: dict) -> str:
    """HMAC-SHA256 with the API key over the values sorted by key, joined by "|"."""
    text = "|".join(str(v) for _, v in sorted(data.items()))
    return hmac.new(env.freekassa_api_key.encode(), text.encode(), hashlib.sha256).hexdigest()


def status(tx: Transaction) -> dict:
    data = {"shopId": int(env.freekassa_merchant_id), "nonce": time.time_ns(), "paymentId": str(tx.id)}
    data["signature"] = api_sign(data)
    r = httpx.post(f"{env.freekassa_api_base}/orders", json=data, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"FreeKassa {r.status_code}: {r.text[:120]}")
    body = r.json()
    if body.get("type") != "success":
        raise RuntimeError(f"FreeKassa: {str(body)[:120]}")
    mine = [o for o in body.get("orders") or [] if str(o.get("merchant_order_id")) == str(tx.id)]
    paid = [o for o in mine if int(o.get("status") or 0) == PAID]
    o = (paid or mine or [{}])[0]
    return {"paid": bool(paid), "ref": str(o.get("merchant_order_id") or "") or None,
            "amount": o.get("amount"), "currency": o.get("currency")}
