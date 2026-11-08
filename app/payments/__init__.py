"""
Payment methods.

A person picks HOW to pay in the app: Telegram Stars, crypto (CryptoBot)
or card/SBP. Card payments go through one gateway chosen by the operator
(CARD_GATEWAY): YooKassa, FreeKassa, Platega, PayPalych, Lava, WATA, Tribute,
CloudPayments, MulenPay, RollyPay or cisPay. Crypto goes through CryptoBot, Heleket or Lave Pay
(CRYPTO_GATEWAY).

start() prepares a payment for an order and says what the app should
open. Confirmation always comes from the gateway (a verified webhook or
the gateway's own API), never from the browser.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from .. import core
from ..config import settings as env
from ..models import Transaction, User
from . import (cispay, cloudpayments, cryptobot, freekassa, heleket, lava, lavepay, mulenpay, paypalych,
               platega, rollypay, stars, tribute, wata, yookassa)

CARD_GATEWAYS = {
    "yookassa": yookassa,
    "freekassa": freekassa,
    "platega": platega,
    "paypalych": paypalych,
    "lava": lava,
    "wata": wata,
    "tribute": tribute,
    "cloudpayments": cloudpayments,
    "mulenpay": mulenpay,
    "rollypay": rollypay,
    "cispay": cispay,
}
CRYPTO_GATEWAYS = {"cryptobot": cryptobot, "heleket": heleket, "lavepay": lavepay}


class PaymentError(Exception):
    pass


# How the gateways are called in the app and the panel.
NAMES = {"stars": "Telegram Stars", "yookassa": "ЮKassa", "freekassa": "FreeKassa", "platega": "Platega",
         "paypalych": "Pally", "lava": "Lava", "wata": "WATA", "tribute": "Tribute",
         "cloudpayments": "CloudPayments", "mulenpay": "MulenPay", "rollypay": "RollyPay", "cispay": "cisPay",
         "cryptobot": "Crypto Pay", "heleket": "Heleket", "lavepay": "Lave Pay"}


def _listed(value: str) -> list[str]:
    return [x for x in (value or "").replace(" ", "").lower().split(",") if x]


def card_gateways() -> list:
    """CARD_GATEWAY — one gateway or several ("yookassa,rollypay"), in the
    order the customer sees them; only those with keys."""
    out = []
    for name in _listed(env.card_gateway):
        mod = CARD_GATEWAYS.get(name)
        if mod and mod.configured() and mod not in out:
            out.append(mod)
    return out


def crypto_gateways() -> list:
    """CRYPTO_GATEWAY — one or several; empty = the first of CryptoBot,
    Heleket, Lave Pay that has its keys; "off" = none."""
    names = _listed(env.crypto_gateway)
    if not names:
        return [next(m for m in CRYPTO_GATEWAYS.values() if m.configured())] \
            if any(m.configured() for m in CRYPTO_GATEWAYS.values()) else []
    out = []
    for name in names:
        mod = CRYPTO_GATEWAYS.get(name)
        if mod and mod.configured() and mod not in out:
            out.append(mod)
    return out


def card_gateway():
    cards = card_gateways()
    return cards[0] if cards else None


def crypto_gateway():
    cryptos = crypto_gateways()
    return cryptos[0] if cryptos else None


def methods() -> list[dict]:
    """Payment methods that are actually configured, in display order. The
    first card and the first crypto gateway are "card" and "crypto" (the
    owner's own names apply to them); more gateways go by their own name."""
    out = []
    if stars.configured():
        out.append({"id": "stars", "gateway": "stars", "kind": "stars", "name": NAMES["stars"]})
    for i, gw in enumerate(card_gateways()):
        out.append({"id": "card" if i == 0 else gw.NAME, "gateway": gw.NAME, "kind": "card",
                    "name": NAMES.get(gw.NAME, gw.NAME)})
    for i, gw in enumerate(crypto_gateways()):
        out.append({"id": "crypto" if i == 0 else gw.NAME, "gateway": gw.NAME, "kind": "crypto",
                    "name": NAMES.get(gw.NAME, gw.NAME)})
    return out


LABEL_METHODS = ("stars", "card", "crypto", "balance")
LABEL_PARTS = ("title_ru", "title_en", "note_ru", "note_en")


def labels(db: Session) -> dict:
    """The owner's own names for the payment methods (setting pay_labels):
    {"card": {"title_ru": "Карта РФ", "note_ru": "без комиссии", ...}}."""
    import json
    try:
        raw = json.loads(core.get(db, "pay_labels") or "{}")
    except ValueError:
        return {}
    out = {}
    for m in LABEL_METHODS:
        row = raw.get(m) if isinstance(raw, dict) else None
        if isinstance(row, dict):
            clean = {k: str(row.get(k) or "").strip()[:40 if k.startswith("title") else 60]
                     for k in LABEL_PARTS}
            if any(clean.values()):
                out[m] = clean
    return out


def gateway_for(method: str):
    if method == "stars" and stars.configured():
        return stars
    if method == "crypto":
        return crypto_gateway()
    if method == "card":
        return card_gateway()
    for gw in card_gateways() + crypto_gateways():      # a second gateway, by its name
        if gw.NAME == method:
            return gw
    return None


def description(db: Session, tx: Transaction) -> str:
    brand = core.get(db, "brand_name")
    what = {"purchase": "subscription", "gift": "gift", "devices": "devices",
            "traffic": "traffic", "bypass": "bypass traffic"}.get(tx.kind, tx.kind)
    return f"{brand} — {what} #{tx.id}"


def start(db: Session, tx: Transaction, user: User) -> dict:
    """
    Prepare the payment. Returns one of
      {"type": "invoice", "link": ...}  — Telegram Stars, open with tg.openInvoice
      {"type": "url", "url": ...}       — a payment page, open with tg.openLink
      {"type": "done"}                  — nothing to pay (free, or test mode)
      {"type": "paid"}                  — paid from the balance, access comes shortly
    """
    from .. import billing

    if tx.amount <= 0 or env.payments_test_mode:
        if env.payments_test_mode and tx.amount > 0:
            tx.gateway = "test"
            db.commit()
            core.audit(db, "TEST MODE: order given without payment", f"tg:{user.tg_id}",
                       f"#{tx.id}", level="warn")
        billing.finish(db, tx, confirmed=False)
        return {"type": "done"}

    if tx.method == "balance":
        # The referral balance: no gateway, the money is already in the shop.
        from .. import referral
        return referral.pay_with_balance(db, tx, user)

    gw = gateway_for(tx.method)
    if gw is None:
        raise PaymentError("method_unavailable")
    tx.gateway = gw.NAME
    db.commit()
    try:
        result = gw.create(db, tx, user)
    except Exception as e:  # noqa: BLE001
        tx.error = str(e)[:250]
        db.commit()
        core.audit(db, "Gateway refused to create a payment", gw.NAME,
                   f"#{tx.id}: {e}", level="error")
        raise PaymentError("gateway_failed") from e
    db.commit()
    return result


# ---------------------------------------------------------------------------
#  Asking the gateway directly
# ---------------------------------------------------------------------------
# Gateways whose API can tell the status of one payment. A webhook that
# never arrives (wrong URL in the gateway's dashboard, a firewall, an
# outage) must not leave a paying customer without access, so open orders
# are also checked here: when the app polls the order, and by a background
# job for orders the person left.
POLLABLE = {"yookassa": yookassa, "paypalych": paypalych, "platega": platega,
            "cryptobot": cryptobot, "lava": lava, "heleket": heleket, "tribute": tribute,
            "cloudpayments": cloudpayments, "mulenpay": mulenpay, "lavepay": lavepay,
            "freekassa": freekassa, "wata": wata, "rollypay": rollypay, "cispay": cispay}
_last_poll: dict[int, float] = {}


def poll(db: Session, tx: Transaction, min_interval: float = 10.0) -> bool:
    """Ask the gateway about an open order; close it if it is paid.
    Returns True if this call completed the order."""
    import time

    from .. import billing

    gw = POLLABLE.get(tx.gateway or "")
    # FreeKassa answers status questions only with its separate API key.
    able = getattr(gw, "can_poll", None) if gw is not None else None
    if gw is None or tx.status != "pending" or not tx.external_id or not (able() if able else gw.configured()):
        return False
    stamp = time.monotonic()
    if stamp - _last_poll.get(tx.id, -1e9) < min_interval:
        return False
    _last_poll[tx.id] = stamp
    if len(_last_poll) > 5000:
        _last_poll.clear()
    try:
        st = gw.status(tx)
    except Exception as e:  # noqa: BLE001 — the next poll will try again
        core.log.warning("status check %s #%s failed: %s", gw.NAME, tx.id, e)
        return False
    if not st.get("paid"):
        return False
    if st.get("ref") and st["ref"] != str(tx.id):
        core.audit(db, "Payment belongs to another order", gw.NAME, f"#{tx.id}", level="error")
        return False
    if st.get("currency") and st["currency"] != tx.currency:
        core.audit(db, "Currency mismatch", gw.NAME, f"#{tx.id}: {st['currency']}", level="error")
        return False
    try:
        paid = float(st.get("amount") or 0)
    except (TypeError, ValueError):
        paid = 0.0
    if paid + 0.01 < float(tx.amount):
        core.audit(db, "Underpaid", gw.NAME, f"#{tx.id}: got {paid}, expected {tx.amount}",
                   level="error")
        return False
    core.audit(db, "Payment found by status check", gw.NAME, f"#{tx.id}")
    return billing.finish(db, tx, external_id=tx.external_id)
