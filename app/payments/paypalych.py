"""PayPalych (pally.info / pal24) — cards and SBP for individuals."""

from __future__ import annotations

import httpx
from sqlalchemy.orm import Session

from ..config import settings as env
from ..models import Transaction, User

NAME = "paypalych"


def configured() -> bool:
    return bool(env.paypalych_token and env.paypalych_shop_id)


def _headers() -> dict:
    return {"Authorization": f"Bearer {env.paypalych_token}"}


def create(db: Session, tx: Transaction, user: User) -> dict:
    from . import description
    r = httpx.post(f"{env.paypalych_api_base}/bill/create", headers=_headers(), timeout=20,
                   data={
                       "shop_id": env.paypalych_shop_id,
                       "amount": f"{tx.amount:.2f}",
                       "order_id": str(tx.id),
                       "description": description(db, tx),
                       "type": "normal",
                       "currency_in": env.currency,
                       "success_url": f"{env.public_url}/app?paid={tx.id}",
                       "fail_url": f"{env.public_url}/app",
                   })
    if r.status_code >= 400:
        raise RuntimeError(r.text[:200])
    data = r.json()
    url = data.get("link_page_url") or data.get("link_url")
    tx.external_id = str(data.get("bill_id") or "")
    if not url or not tx.external_id:
        raise RuntimeError(f"no bill in the answer: {str(data)[:150]}")
    return {"type": "url", "url": url}


def bill_status(bill_id: str) -> dict:
    """
    The bill as PayPalych sees it. Their postback signature algorithm is
    not published step by step, so instead of guessing it the postback is
    treated as a hint and the bill is checked here with our own token —
    an answer nobody outside can forge.
    """
    r = httpx.get(f"{env.paypalych_api_base}/bill/status", params={"id": bill_id},
                  headers=_headers(), timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"PayPalych {r.status_code}")
    return r.json()


def status(tx: Transaction) -> dict:
    bill = bill_status(tx.external_id)
    return {"paid": str(bill.get("status", "")).upper() in ("SUCCESS", "OVERPAID"),
            "amount": bill.get("amount"), "currency": None}
