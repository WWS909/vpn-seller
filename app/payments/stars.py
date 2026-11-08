"""Telegram Stars — works with nothing but the bot token. One-time payments only."""

from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import Session

from .. import billing, core, notify
from ..config import settings as env
from ..models import Transaction, User

NAME = "stars"


def configured() -> bool:
    return bool(env.stars_enabled and env.bot_token)


def create(db: Session, tx: Transaction, user: User) -> dict:
    from . import description
    tx.stars = billing.stars_for(db, tx.amount)
    db.commit()
    title = description(db, tx)[:32]
    res = notify.call("createInvoiceLink", {
        "title": title,
        "description": f"{tx.amount:g} {env.currency} · #{tx.id}",
        "payload": f"tx:{tx.id}",
        "currency": "XTR",
        "prices": [{"label": title, "amount": tx.stars}],
    })
    if not res.get("ok"):
        raise RuntimeError(str(res.get("description"))[:200])
    return {"type": "invoice", "link": res["result"], "stars": tx.stars}


def order_from_payload(db: Session, payload: str) -> Transaction | None:
    if not str(payload).startswith("tx:"):
        return None
    tx_id = str(payload)[3:]
    return db.get(Transaction, int(tx_id)) if tx_id.isdigit() else None


def _lang(query: dict, tx: Transaction | None) -> str:
    user = tx.user if tx is not None else None
    lang = (user.lang if user else "") or str((query.get("from") or {}).get("language_code") or "")
    return "ru" if lang.startswith("ru") else "en" if lang else env.default_lang


def pre_checkout(db: Session, query: dict) -> tuple[bool, str]:
    """
    Telegram asks whether to accept the payment and waits at most 10 s.
    Accept only an order that is still open, in Stars, for exactly the
    price we asked — and still worth paying: not out of date, not already
    covered by another purchase, its promo code and discount still valid.
    The text of a refusal is shown to the person by Telegram.
    """
    tx = order_from_payload(db, query.get("invoice_payload", ""))
    lang = _lang(query, tx)
    if not tx or tx.status not in ("pending", "failed"):
        return False, core.t(lang, "order_closed")
    if query.get("currency") != "XTR" or int(query.get("total_amount") or 0) != int(tx.stars or 0):
        return False, core.t(lang, "order_price")
    reason = billing.stale_reason(db, tx)
    if reason:
        core.audit(db, "Stars payment refused", f"tx:{tx.id}", reason)
        return False, core.t(lang, reason)
    from .. import emergency
    if emergency.sales_stopped(db):
        # Refused before any money moves: Telegram shows the reason, nothing is charged.
        return False, core.t(lang, "em_sales")
    return True, ""


def _refund(db: Session, tx: Transaction, payment: dict) -> bool:
    """Give back a second charge for an order that was already paid."""
    charge = str(payment.get("telegram_payment_charge_id") or "")
    user = tx.user
    res = notify.call("refundStarPayment", {"user_id": int(user.tg_id), "telegram_payment_charge_id": charge})
    ok = bool(res.get("ok"))
    core.audit(db, "Second Stars payment for one order", f"tx:{tx.id}",
               f"charge {charge}, {payment.get('total_amount')} XTR: "
               f"{'refunded' if ok else 'refund failed: ' + str(res.get('description', ''))[:120]}",
               level="warn" if ok else "error")
    if not ok:
        notify.admins("Двойная оплата Stars за один заказ / Double Stars payment: "
                      f"#{tx.id}, {payment.get('total_amount')} ⭐, tg {user.tg_id}, charge {charge}. "
                      "Вернуть автоматически не получилось — верните звёзды вручную (refundStarPayment).")
    return ok


def paid(db: Session, payment: dict) -> bool:
    tx = order_from_payload(db, payment.get("invoice_payload", ""))
    if not tx or payment.get("currency") != "XTR" \
            or int(payment.get("total_amount") or 0) < int(tx.stars or 0):
        # The stars are already taken from the customer, but there is no
        # order to close: an admin has to look (and refund if needed).
        what = (f"#{tx.id}, ждали {tx.stars} ⭐" if tx else "заказ не найден / order not found")
        core.audit(db, "Stars payment does not match an order", f"tx:{tx.id}" if tx else "stars",
                   str(payment)[:300], level="error")
        notify.admins("Оплата Stars не сопоставлена с заказом / Stars payment not matched: "
                      f"{payment.get('total_amount')} {payment.get('currency')} · {what} · "
                      f"charge {payment.get('telegram_payment_charge_id', '?')}. "
                      "Проверьте и при необходимости верните звёзды (refundStarPayment).")
        return False
    charge = str(payment.get("telegram_payment_charge_id") or "")[:128]
    if charge:
        # The first charge that arrives owns the order (one conditional
        # UPDATE). The same invoice paid twice — opened on two phones, both
        # accepted while the order was open — is granted once, and every
        # other charge goes back, whatever state the order is in by then.
        db.execute(update(Transaction).where(Transaction.id == tx.id, Transaction.external_id == "")
                   .values(external_id=charge))
        db.commit()
        db.refresh(tx)
        if tx.external_id != charge:
            _refund(db, tx, payment)
            return False
    return billing.finish(db, tx, external_id=charge)
