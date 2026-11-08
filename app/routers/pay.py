"""
Payment notifications from gateways.

Every handler first proves the notification is genuine (signature, the
gateway's own API, or its documented auth), then checks the amount, and
only then closes the order. Access is granted inside billing.finish().

The handlers are async only to read the request; the work itself (gateway
API calls, granting access on the panels) runs in a worker thread, so a
slow panel never stalls the whole server.
"""

from __future__ import annotations

import hmac
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .. import access, billing, core
from ..auth import client_ip
from ..config import settings as env
from ..db import get_db
from ..models import Transaction
from ..payments import (cispay, cloudpayments, cryptobot, freekassa, heleket, lava, lavepay, mulenpay, paypalych,
                        platega, rollypay,
                        tribute, wata, yookassa)

router = APIRouter(prefix="/api/pay", tags=["payments"])


_noted: dict[tuple, float] = {}


def _note_rejected(db: Session, action: str, ip: str, details: str = "") -> None:
    """A refused notification goes to the log once per address and kind in
    10 minutes: these addresses are public, and anybody could otherwise
    fill the log (and hide real errors) with thousands of fake requests."""
    t = time.time()
    if len(_noted) > 2000:
        for k in [k for k, v in _noted.items() if t - v > 600]:
            _noted.pop(k, None)
    key = (action, ip)
    if t - _noted.get(key, 0) < 600 or len(_noted) > 5000:
        return
    _noted[key] = t
    core.audit(db, action, ip, details, level="warn")


def _amount_ok(db: Session, tx: Transaction, paid, where: str) -> bool:
    try:
        value = float(paid)
    except (TypeError, ValueError):
        value = 0.0
    if value + 0.01 < float(tx.amount):
        core.audit(db, "Underpaid", where, f"#{tx.id}: got {value}, expected {tx.amount}",
                   level="error")
        return False
    return True


def _close(db: Session, tx: Transaction, external_id: str = "") -> None:
    """finish() with the right HTTP answer when access cannot be granted yet:
    an error makes the gateway retry later instead of forgetting the payment."""
    try:
        billing.finish(db, tx, external_id=external_id)
    except access.AccessError:
        raise HTTPException(503, "access not granted yet, retry later")


def _form_or_json(raw: bytes, content_type: str) -> dict:
    if "json" in content_type:
        try:
            data = json.loads(raw or b"{}")
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}
    from urllib.parse import parse_qsl
    return dict(parse_qsl(raw.decode("utf-8", "replace")))


async def _json(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, "bad json")
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- YooKassa
@router.post("/yookassa")
async def yookassa_hook(request: Request, db: Session = Depends(get_db)):
    ip = client_ip(request)
    if not yookassa.ip_allowed(ip):
        _note_rejected(db, "Rejected YooKassa notification", ip, "foreign address")
        raise HTTPException(403, "forbidden")
    body = await _json(request)
    return await run_in_threadpool(_yookassa, db, body)


def _yookassa(db: Session, body: dict):
    obj = body.get("object") or {}
    if body.get("event") != "payment.succeeded" or not obj.get("id"):
        return {"ok": True}
    # Decide on YooKassa's own answer, not on the notification body.
    payment = yookassa.fetch(obj["id"])
    if payment.get("status") != "succeeded" or not payment.get("paid"):
        return {"ok": True}
    tx_id = str((payment.get("metadata") or {}).get("tx_id", ""))
    tx = db.get(Transaction, int(tx_id)) if tx_id.isdigit() else None
    if not tx or tx.external_id != payment.get("id"):
        core.audit(db, "YooKassa: order not found", payment.get("id", ""), level="warn")
        return {"ok": True}
    if (payment.get("amount") or {}).get("currency") != tx.currency or \
            not _amount_ok(db, tx, (payment.get("amount") or {}).get("value"), "yookassa"):
        return {"ok": True}
    _close(db, tx, payment["id"])
    return {"ok": True}


# ---------------------------------------------------------------- FreeKassa
@router.post("/freekassa")
async def freekassa_hook(request: Request, db: Session = Depends(get_db)):
    """FreeKassa posts a form and expects exactly "YES" back."""
    ip = client_ip(request)
    if env.freekassa_ips and ip not in env.freekassa_ips:
        _note_rejected(db, "Rejected FreeKassa notification", ip, "foreign address")
        raise HTTPException(403, "forbidden")
    form = dict(await request.form())
    return await run_in_threadpool(_freekassa, db, form, ip)


def _freekassa(db: Session, form: dict, ip: str):
    order_id = str(form.get("MERCHANT_ORDER_ID", ""))
    amount = str(form.get("AMOUNT", ""))
    sign = str(form.get("SIGN", "")).lower()
    if not freekassa.configured() or not order_id.isdigit() or \
            not hmac.compare_digest(sign.encode(), freekassa.notice_sign(amount, order_id).encode()):
        _note_rejected(db, "Rejected FreeKassa notification", ip, f"bad signature #{order_id}"[:80])
        raise HTTPException(400, "bad signature")
    tx = db.get(Transaction, int(order_id))
    if not tx:
        raise HTTPException(404, "order not found")
    if not _amount_ok(db, tx, amount, "freekassa"):
        raise HTTPException(400, "amount")
    _close(db, tx, str(form.get("intid", "")))
    return PlainTextResponse("YES")


# ---------------------------------------------------------------- Platega
@router.post("/platega")
async def platega_hook(request: Request, db: Session = Depends(get_db)):
    if not platega.verify(dict(request.headers)):
        _note_rejected(db, "Rejected Platega notification", client_ip(request), "bad credentials")
        raise HTTPException(403, "forbidden")
    body = await _json(request)
    return await run_in_threadpool(_platega, db, body)


def _platega(db: Session, body: dict):
    if str(body.get("status", "")).upper() != "CONFIRMED":
        return {"ok": True}
    ext = str(body.get("id") or body.get("transactionId") or "")
    tx = db.query(Transaction).filter(Transaction.external_id == ext,
                                      Transaction.gateway == "platega").first() if ext else None
    if not tx:
        core.audit(db, "Platega: order not found", ext, level="warn")
        return {"ok": True}
    paid = body.get("amount") or (body.get("paymentDetails") or {}).get("amount")
    if not _amount_ok(db, tx, paid, "platega"):
        return {"ok": True}
    _close(db, tx, ext)
    return {"ok": True}


# ---------------------------------------------------------------- PayPalych
_pp_checked: list[float] = []


@router.post("/paypalych")
async def paypalych_hook(request: Request, db: Session = Depends(get_db)):
    if not paypalych.configured():
        raise HTTPException(404, "not found")
    body = _form_or_json(await request.body(), request.headers.get("content-type", ""))
    return await run_in_threadpool(_paypalych, db, body)


def _pp_order(db: Session, body: dict) -> Transaction | None:
    order_id = str(body.get("InvId") or body.get("order_id") or body.get("custom") or "")
    tx = db.get(Transaction, int(order_id)) if order_id.isdigit() and len(order_id) < 12 else None
    return tx if tx and tx.gateway == "paypalych" and tx.external_id else None


def _paypalych(db: Session, body: dict):
    tx = _pp_order(db, body)
    if not tx or tx.status not in ("pending", "failed", "cancelled"):
        return {"ok": True}
    # The notification is not signed: the bill is checked with PayPalych
    # itself. At most 30 checks a minute in all, so fake notifications
    # cannot turn the shop into a machine that hammers PayPalych; over that,
    # "try later" — PayPalych repeats, and the poll job asks on its own.
    t = time.time()
    _pp_checked[:] = [x for x in _pp_checked if t - x < 60]
    if len(_pp_checked) >= 30:
        raise HTTPException(503, "busy, retry later")
    _pp_checked.append(t)
    bill = paypalych.bill_status(tx.external_id)
    if str(bill.get("status", "")).upper() not in ("SUCCESS", "OVERPAID"):
        return {"ok": True}
    if not _amount_ok(db, tx, bill.get("amount"), "paypalych"):
        return {"ok": True}
    _close(db, tx, tx.external_id)
    return {"ok": True}


def _pp_money_back(db: Session, body: dict, what: str, level: str) -> dict:
    """A refund or chargeback notice. It is not signed either, so only one
    about a real PayPalych order of this shop is written down, once — and
    the admins are told, because the access was paid with that money."""
    from .. import notify
    tx = _pp_order(db, body)
    if tx is None:
        return {"ok": True}
    key = (what, tx.id)
    if key in _noted:
        return {"ok": True}
    _noted[key] = time.time()
    core.audit(db, f"PayPalych {what}", f"tx:{tx.id}",
               f"#{tx.id} {tx.amount:g} {tx.currency}, status {tx.status}: {str(body)[:300]}", level=level)
    if tx.status == "success":
        notify.admins(f"PayPalych: {what} по заказу #{tx.id} ({tx.amount:g} {tx.currency}). "
                      "Проверьте в кабинете PayPalych и при необходимости заберите доступ в панели.")
    return {"ok": True}


@router.post("/paypalych/refund")
async def paypalych_refund(request: Request, db: Session = Depends(get_db)):
    if not paypalych.configured():
        raise HTTPException(404, "not found")
    body = _form_or_json(await request.body(), request.headers.get("content-type", ""))
    return await run_in_threadpool(_pp_money_back, db, body, "refund", "warn")


@router.post("/paypalych/chargeback")
async def paypalych_chargeback(request: Request, db: Session = Depends(get_db)):
    if not paypalych.configured():
        raise HTTPException(404, "not found")
    body = _form_or_json(await request.body(), request.headers.get("content-type", ""))
    return await run_in_threadpool(_pp_money_back, db, body, "chargeback", "error")


# ---------------------------------------------------------------- CryptoBot
@router.post("/cryptobot")
async def cryptobot_hook(request: Request, db: Session = Depends(get_db)):
    raw = await request.body()
    if not cryptobot.verify(raw, request.headers.get("crypto-pay-api-signature", "")):
        _note_rejected(db, "Rejected CryptoBot notification", client_ip(request), "bad signature")
        raise HTTPException(403, "forbidden")
    return await run_in_threadpool(_cryptobot, db, raw)


def _cryptobot(db: Session, raw: bytes):
    inv = cryptobot.parse(raw)
    if not inv:
        return {"ok": True}
    order_id = str(inv.get("payload", ""))
    tx = db.get(Transaction, int(order_id)) if order_id.isdigit() else None
    if not tx or str(inv.get("invoice_id")) != tx.external_id:
        core.audit(db, "CryptoBot: order not found", order_id, level="warn")
        return {"ok": True}
    if inv.get("currency_type") == "fiat" and inv.get("fiat") != tx.currency:
        core.audit(db, "CryptoBot: currency mismatch", order_id, str(inv)[:200], level="error")
        return {"ok": True}
    if not _amount_ok(db, tx, inv.get("amount"), "cryptobot"):
        return {"ok": True}
    _close(db, tx, str(inv.get("invoice_id")))
    return {"ok": True}


# ---------------------------------------------------------------- Lava
@router.post("/lava")
async def lava_hook(request: Request, db: Session = Depends(get_db)):
    if not lava.configured():
        raise HTTPException(404, "not found")
    raw = await request.body()
    body = lava.verify(raw, dict(request.headers))
    if body is None:
        _note_rejected(db, "Rejected Lava notification", client_ip(request), "bad signature")
        raise HTTPException(403, "forbidden")
    return await run_in_threadpool(_lava, db, body)


def _lava(db: Session, body: dict):
    if str(body.get("status", "")).lower() not in ("success", "paid"):
        return {"ok": True}
    order_id = str(body.get("order_id", ""))
    tx = db.get(Transaction, int(order_id)) if order_id.isdigit() and len(order_id) < 12 else None
    if not tx or tx.gateway != "lava" or str(body.get("invoice_id", "")) != tx.external_id:
        core.audit(db, "Lava: order not found", order_id, level="warn")
        return {"ok": True}
    if not _amount_ok(db, tx, body.get("amount"), "lava"):
        return {"ok": True}
    _close(db, tx, tx.external_id)
    return {"ok": True}


# ---------------------------------------------------------------- WATA
@router.post("/wata")
async def wata_hook(request: Request, db: Session = Depends(get_db)):
    if not wata.configured():
        raise HTTPException(404, "not found")
    raw = await request.body()
    ok = await run_in_threadpool(wata.verify, raw, request.headers.get("x-signature", ""))
    if not ok:
        _note_rejected(db, "Rejected WATA notification", client_ip(request), "bad signature")
        raise HTTPException(403, "forbidden")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "bad json")
    return await run_in_threadpool(_wata, db, body if isinstance(body, dict) else {})


def _wata(db: Session, body: dict):
    if str(body.get("transactionStatus", "")).lower() != "paid":
        return {"ok": True}
    order_id = str(body.get("orderId", ""))
    tx = db.get(Transaction, int(order_id)) if order_id.isdigit() and len(order_id) < 12 else None
    if not tx or tx.gateway != "wata":
        core.audit(db, "WATA: order not found", order_id, level="warn")
        return {"ok": True}
    if body.get("currency") and body["currency"] != tx.currency:
        core.audit(db, "WATA: currency mismatch", order_id, str(body)[:200], level="error")
        return {"ok": True}
    if not _amount_ok(db, tx, body.get("amount"), "wata"):
        return {"ok": True}
    _close(db, tx, tx.external_id or str(body.get("id", "")))
    return {"ok": True}


# ---------------------------------------------------------------- Heleket
@router.post("/heleket")
async def heleket_hook(request: Request, db: Session = Depends(get_db)):
    if not heleket.configured():
        raise HTTPException(404, "not found")
    body = heleket.verify(await request.body())
    if body is None:
        _note_rejected(db, "Rejected Heleket notification", client_ip(request), "bad signature")
        raise HTTPException(403, "forbidden")
    return await run_in_threadpool(_heleket, db, body)


def _heleket(db: Session, body: dict):
    if str(body.get("status", "")).lower() not in ("paid", "paid_over"):
        return {"ok": True}
    order_id = str(body.get("order_id", ""))
    tx = db.get(Transaction, int(order_id)) if order_id.isdigit() and len(order_id) < 12 else None
    if not tx or tx.gateway != "heleket" or str(body.get("uuid", "")) != tx.external_id:
        core.audit(db, "Heleket: order not found", order_id, level="warn")
        return {"ok": True}
    if body.get("currency") and body["currency"] != tx.currency:
        core.audit(db, "Heleket: currency mismatch", order_id, str(body)[:200], level="error")
        return {"ok": True}
    if not _amount_ok(db, tx, body.get("amount"), "heleket"):
        return {"ok": True}
    _close(db, tx, tx.external_id)
    return {"ok": True}


# ================================================================ checked with the gateway
# For the gateways below the notification only says "look at order X": the
# order is closed on the gateway's own answer, asked with our own keys —
# whether or not their notification signature could be checked as well.
_asked: dict[str, list[float]] = {}


def _confirm(db: Session, tx: Transaction, gw) -> None:
    if tx.status not in ("pending", "failed", "cancelled") or not tx.external_id:
        return
    # At most 30 lookups a minute per gateway: fake notifications cannot
    # turn the shop into a machine that hammers the gateway. Over that,
    # "later" — the gateway repeats, and the poll job asks on its own.
    t = time.time()
    seen = _asked.setdefault(gw.NAME, [])
    seen[:] = [x for x in seen if t - x < 60]
    if len(seen) >= 30:
        raise HTTPException(503, "busy, retry later")
    seen.append(t)
    try:
        st = gw.status(tx)
    except Exception as e:  # noqa: BLE001 — their API is down: they will notify again
        core.log.warning("status check %s #%s failed: %s", gw.NAME, tx.id, e)
        raise HTTPException(503, "could not check the payment, retry later")
    if not st.get("paid"):
        return
    if st.get("ref") and st["ref"] != str(tx.id):
        core.audit(db, "Payment belongs to another order", gw.NAME, f"#{tx.id}", level="error")
        return
    if st.get("currency") and st["currency"] != tx.currency:
        core.audit(db, "Currency mismatch", gw.NAME, f"#{tx.id}: {st['currency']}", level="error")
        return
    if not _amount_ok(db, tx, st.get("amount"), gw.NAME):
        return
    _close(db, tx, tx.external_id)


def _by_external(db: Session, gateway: str, ext: str) -> Transaction | None:
    if not ext or len(ext) > 128:
        return None
    return db.query(Transaction).filter(Transaction.external_id == ext, Transaction.gateway == gateway).first()


# ---------------------------------------------------------------- Tribute
@router.post("/tribute")
async def tribute_hook(request: Request, db: Session = Depends(get_db)):
    if not tribute.configured():
        raise HTTPException(404, "not found")
    raw = await request.body()
    if not tribute.verify(raw, request.headers.get("trbt-signature", "")):
        _note_rejected(db, "Rejected Tribute notification", client_ip(request), "bad signature")
        raise HTTPException(403, "forbidden")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "bad json")
    return await run_in_threadpool(_tribute, db, body if isinstance(body, dict) else {})


def _tribute(db: Session, body: dict):
    # Only the final event: "shop_order_payment_received" comes before it.
    if body.get("name") != "shop_order":
        return {"status": "ok"}
    tx = _by_external(db, "tribute", str((body.get("payload") or {}).get("uuid") or ""))
    if tx:
        _confirm(db, tx, tribute)
    return {"status": "ok"}


# ---------------------------------------------------------------- CloudPayments
@router.post("/cloudpayments")
async def cloudpayments_hook(request: Request, db: Session = Depends(get_db)):
    if not cloudpayments.configured():
        raise HTTPException(404, "not found")
    raw = await request.body()
    if not cloudpayments.verify(raw, dict(request.headers)):
        _note_rejected(db, "Rejected CloudPayments notification", client_ip(request), "bad signature")
        raise HTTPException(403, "forbidden")
    body = _form_or_json(raw, request.headers.get("content-type", ""))
    return await run_in_threadpool(_cloudpayments, db, body)


def _cloudpayments(db: Session, body: dict):
    order_id = str(body.get("InvoiceId") or "")
    if str(body.get("OperationType") or "Payment") != "Payment" or not order_id.isdigit() or len(order_id) > 11:
        return {"code": 0}
    tx = db.get(Transaction, int(order_id))
    if tx and tx.gateway == "cloudpayments":
        _confirm(db, tx, cloudpayments)
    return {"code": 0}


# ---------------------------------------------------------------- MulenPay
@router.post("/mulenpay")
async def mulenpay_hook(request: Request, db: Session = Depends(get_db)):
    if not mulenpay.configured():
        raise HTTPException(404, "not found")
    body = _form_or_json(await request.body(), request.headers.get("content-type", ""))
    return await run_in_threadpool(_mulenpay, db, body)


def _mulenpay(db: Session, body: dict):
    # Not signed: only a hint to look the payment up with our own key.
    order_id, pid = str(body.get("uuid") or ""), str(body.get("id") or "")
    if not order_id.isdigit() or len(order_id) > 11:
        return {"ok": True}
    tx = db.get(Transaction, int(order_id))
    if tx and tx.gateway == "mulenpay" and pid and tx.external_id == pid:
        _confirm(db, tx, mulenpay)
    return {"ok": True}


# ---------------------------------------------------------------- Lave Pay
@router.post("/lavepay")
async def lavepay_hook(request: Request, db: Session = Depends(get_db)):
    if not lavepay.configured():
        raise HTTPException(404, "not found")
    raw = await request.body()
    if not lavepay.verify(raw, dict(request.headers)):
        _note_rejected(db, "Rejected Lave Pay notification", client_ip(request), "bad signature")
        raise HTTPException(403, "forbidden")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "bad json")
    return await run_in_threadpool(_lavepay, db, body if isinstance(body, dict) else {})


def _lavepay(db: Session, body: dict):
    if body.get("type") != "invoice.paid":
        return {"ok": True}
    tx = _by_external(db, "lavepay", str((body.get("data") or {}).get("id") or ""))
    if tx:
        _confirm(db, tx, lavepay)
    return {"ok": True}


# ---------------------------------------------------------------- RollyPay
@router.post("/rollypay")
async def rollypay_hook(request: Request, db: Session = Depends(get_db)):
    if not rollypay.configured():
        raise HTTPException(404, "not found")
    raw = await request.body()
    if not rollypay.verify(raw, dict(request.headers)):
        _note_rejected(db, "Rejected RollyPay notification", client_ip(request), "bad signature")
        raise HTTPException(403, "forbidden")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "bad json")
    return await run_in_threadpool(_rollypay, db, body if isinstance(body, dict) else {})


def _rollypay(db: Session, body: dict):
    if str(body.get("status", "")).lower() != "paid" or body.get("test"):
        return {"ok": True}
    tx = _by_external(db, "rollypay", str(body.get("payment_id") or ""))
    if tx:
        _confirm(db, tx, rollypay)          # asks RollyPay itself
    return {"ok": True}


# ---------------------------------------------------------------- cisPay
@router.post("/cispay")
async def cispay_hook(request: Request, db: Session = Depends(get_db)):
    if not cispay.configured():
        raise HTTPException(404, "not found")
    raw = await request.body()
    if not cispay.verify(raw, request.headers.get("x-signature", "")):
        _note_rejected(db, "Rejected cisPay notification", client_ip(request), "bad signature")
        raise HTTPException(403, "forbidden")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "bad json")
    return await run_in_threadpool(_cispay, db, body if isinstance(body, dict) else {})


def _cispay(db: Session, body: dict):
    if str(body.get("status", "")).upper() != "PAID" or body.get("event"):
        return {"ok": True}
    tx = _by_external(db, "cispay", str(body.get("id") or body.get("payment_id") or ""))
    if tx:
        _confirm(db, tx, cispay)            # asks cisPay itself
    return {"ok": True}
