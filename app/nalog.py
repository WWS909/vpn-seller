"""
Receipts in «Мой налог» (lknpd.nalog.ru) for a self-employed seller.

Every paid order of the chosen gateways gets one receipt. The FNS endpoint
that registers income is not idempotent — sent twice, it makes two
receipts and two taxes — so a receipt is claimed in the database before it
is sent, and an answer that never came ("uncertain") is NEVER retried
automatically: the admin looks in the «Мой налог» cabinet and decides.

States of a receipt row:
  pending    claimed, waiting for the job
  sending    the request is on its way (left like this = the process died)
  sent       FNS registered it, `uuid` is the receipt
  failed     FNS refused it for sure (bad password, bad data) — safe to resend
  uncertain  no answer: maybe registered, maybe not — check by hand
  skipped    the admin decided not to send it

A refund or a cancelled order does not cancel the receipt by itself: that
is done by hand in the «Мой налог» app (it asks for the reason).
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import core, notify
from .config import settings as env
from .models import Receipt, now

API = "https://lknpd.nalog.ru/api/v1"
MSK = timezone(timedelta(hours=3))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class NotReached(Exception):
    """The request never got to FNS — nothing was registered, try later."""


class AuthError(Exception):
    """INN or password refused — nothing was registered."""


class Refused(Exception):
    """FNS answered and refused the receipt — nothing was registered."""


class Uncertain(Exception):
    """No clear answer — the receipt may or may not exist."""


_token: dict = {"value": "", "for": "", "at": 0.0}


def _headers(token: str = "") -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json, text/plain, */*",
         "Accept-Language": "ru-RU,ru;q=0.9", "Referer": "https://lknpd.nalog.ru/auth/login",
         "User-Agent": UA}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def login(inn: str, password: str, *, force: bool = False) -> str:
    """A token for the cabinet, kept for 20 minutes."""
    key = hashlib.sha256(f"{inn}:{password}".encode()).hexdigest()
    if not force and _token["value"] and _token["for"] == key and time.time() - _token["at"] < 1200:
        return _token["value"]
    device = hashlib.sha256(f"vpn-shop-nalog:{inn}".encode()).hexdigest()[:21]
    try:
        r = httpx.post(f"{API}/auth/lkfl", headers=_headers(), timeout=20, json={
            "username": inn, "password": password,
            "deviceInfo": {"sourceDeviceId": device, "sourceType": "WEB", "appVersion": "1.0.0",
                           "metaDetails": {"userAgent": UA}}})
    except httpx.HTTPError as e:
        raise NotReached(f"«Мой налог» не отвечает: {type(e).__name__}") from e
    if r.status_code in (400, 401, 403, 422):
        raise AuthError(f"ФНС не приняла ИНН или пароль «Мой налог» ({r.status_code})")
    if r.status_code != 200:
        raise NotReached(f"вход в «Мой налог»: ответ {r.status_code}")
    try:
        token = (r.json() or {}).get("token") or ""
    except ValueError:
        token = ""
    if not token:
        raise NotReached("«Мой налог» не выдал токен")
    _token.update(value=token, **{"for": key}, at=time.time())
    return token


def _income(token: str, amount: float, name: str, when: datetime) -> str:
    # The date of the income is when the money came; the request is now.
    # FNS reads the clock digits as Moscow time, so both are written in it.
    stamp = when.astimezone(MSK).isoformat(timespec="seconds")
    asked = datetime.now(MSK).isoformat(timespec="seconds")
    try:
        r = httpx.post(f"{API}/income", headers=_headers(token), timeout=30, json={
            "operationTime": stamp, "requestTime": asked,
            "services": [{"name": name[:120], "amount": round(amount, 2), "quantity": 1}],
            "totalAmount": f"{amount:.2f}",
            "client": {"contactPhone": None, "displayName": None, "inn": None,
                       "incomeType": "FROM_INDIVIDUAL"},
            "paymentType": "CASH", "ignoreMaxTotalIncomeRestriction": False})
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
        # Not connected at all: the request was never sent.
        raise NotReached(f"«Мой налог» недоступен: {type(e).__name__}") from e
    except httpx.HTTPError as e:
        # Sent, but the answer was lost (a read timeout, a cut connection):
        # FNS may have registered it.
        raise Uncertain(f"ответ «Мой налог» не получен: {type(e).__name__}") from e
    if r.status_code in (401, 403):
        raise AuthError("сессия «Мой налог» истекла")
    if r.status_code >= 500:
        raise Uncertain(f"«Мой налог» ответил {r.status_code}")
    if r.status_code not in (200, 201):
        raise Refused(f"«Мой налог» отклонил чек: {r.status_code} {r.text[:150]}")
    try:
        data = r.json() or {}
    except ValueError as e:
        raise Uncertain("непонятный ответ «Мой налог»") from e
    uuid = str(data.get("approvedReceiptUuid") or data.get("id") or "")
    if not uuid:
        raise Uncertain("«Мой налог» не вернул номер чека")
    return uuid


def register(inn: str, password: str, amount: float, name: str, when: datetime) -> str:
    """Register one income. Returns the receipt uuid. A 401/403 proves the
    receipt was not made, so one retry with a fresh token is safe."""
    try:
        return _income(login(inn, password), amount, name, when)
    except AuthError:
        return _income(login(inn, password, force=True), amount, name, when)


def receipt_url(inn: str, uuid: str) -> str:
    return f"{API}/receipt/{inn}/{uuid}/print"


# ---------------------------------------------------------------------------
#  The queue
# ---------------------------------------------------------------------------
def configured() -> bool:
    return bool(env.nalog_inn and env.nalog_password)


def wanted(tx) -> bool:
    """Does this paid order need a receipt? Real money in roubles, through
    one of the chosen gateways (Stars, balance, gifts by the admin: no)."""
    return (configured() and float(tx.amount or 0) > 0 and tx.currency == "RUB"
            and (tx.gateway or "") in env.nalog_gateways)


HOLD_NOTE = "выдано вручную, оплата не подтверждена кассой — если деньги пришли, отправьте чек"


def claim(db: Session, tx, *, hold: bool = False) -> Receipt | None:
    """Add the receipt of an order being closed to the session, WITHOUT
    committing: it is stored by the same commit that marks the order done,
    and rolled back with it. hold=True: the admin granted an order no
    gateway confirmed — the receipt waits for the admin ("skipped")."""
    if not wanted(tx):
        return None
    if db.query(Receipt.id).filter(Receipt.tx_id == tx.id).first():
        return None
    item = (core.get(db, "nalog_item") or "").strip() or core.DEFAULTS["nalog_item"]
    r = Receipt(tx_id=tx.id, amount=round(float(tx.amount), 2),
                name=f"{item} — заказ №{tx.id}"[:120],
                paid_at=int(tx.money_at or 0) or now(),
                status="skipped" if hold else "pending", error=HOLD_NOTE if hold else "")
    db.add(r)
    return r


def queue(db: Session, tx) -> bool:
    """Claim and store the receipt of an already closed order. A second
    call for the same order does nothing (the order id is unique)."""
    if claim(db, tx) is None:
        return False
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    return True


_alerted: dict[str, float] = {}


def _alert(kind: str, text: str, every: int = 3600) -> None:
    t = time.time()
    if t - _alerted.get(kind, 0) < every:
        return
    _alerted[kind] = t
    notify.admins(text)


def _set(db: Session, r: Receipt, **values) -> None:
    db.execute(update(Receipt).where(Receipt.id == r.id).values(updated_at=now(), **values))
    db.commit()


def run(db: Session, limit: int = 10) -> int:
    """Send waiting receipts. Returns how many were registered."""
    if not configured() or paused(db):
        return 0
    t = now()
    # A receipt left "sending" means the process stopped in the middle:
    # FNS may have it. Never resent by itself.
    for r in db.query(Receipt).filter(Receipt.status == "sending", Receipt.updated_at < t - 900).all():
        _set(db, r, status="uncertain", error="отправка прервалась — проверьте в «Мой налог»")
        core.audit(db, "Receipt uncertain", f"tx:{r.tx_id}", "sending was interrupted", level="error")
        _alert("uncertain", "Мой налог: не ясно, создан ли чек по заказу "
                            f"#{r.tx_id} ({r.amount:g} ₽). Проверьте в приложении «Мой налог» и отметьте в админке → Заказы → Чеки.", 0)
    done = 0
    rows = (db.query(Receipt).filter(Receipt.status == "pending", Receipt.next_try_at <= t)
            .order_by(Receipt.id).limit(limit).all())
    for r in rows:
        took = db.execute(update(Receipt).where(Receipt.id == r.id, Receipt.status == "pending")
                          .values(status="sending", updated_at=now(), tries=Receipt.tries + 1)).rowcount
        db.commit()
        if not took:
            continue
        db.refresh(r)
        when = datetime.fromtimestamp(r.paid_at or r.created_at, MSK)
        try:
            uuid = register(env.nalog_inn, env.nalog_password, r.amount, r.name, when)
        except NotReached as e:
            # Nothing reached FNS: back in the queue, a bit later each time.
            _set(db, r, status="pending", error=str(e)[:250],
                 next_try_at=now() + min(3600, 60 * 2 ** min(r.tries, 6)))
            if r.tries >= 5:
                _alert("down", f"Мой налог не отвечает: чек по заказу #{r.tx_id} пока не создан, "
                               "магазин будет пробовать дальше.")
            break                                   # the service is down: wait for the next run
        except AuthError as e:
            # Nothing was registered: the receipt goes back to the queue, and
            # the whole queue stops — trying a wrong password every minute
            # could get the account locked. It starts again after a
            # successful "Check sign-in" in the admin panel.
            _set(db, r, status="pending", error=str(e)[:250])
            core.put(db, "nalog_paused", "1")
            core.audit(db, "Receipts paused", f"tx:{r.tx_id}", str(e), level="error")
            _alert("auth", "Мой налог: ФНС не приняла ИНН или пароль. Чеки не создаются, очередь "
                           "остановлена. Проверьте ИНН и пароль (vpn-seller → «Способы оплаты» → «Мой налог»), "
                           "затем в админке → Настройки → «Чеки «Мой налог»» нажмите «Проверить вход».", 0)
            break
        except Refused as e:
            _set(db, r, status="failed", error=str(e)[:250])
            core.audit(db, "Receipt refused", f"tx:{r.tx_id}", str(e), level="error")
            _alert("refused", f"Мой налог отклонил чек по заказу #{r.tx_id}: {str(e)[:150]}")
        except Uncertain as e:
            _set(db, r, status="uncertain", error=str(e)[:250])
            core.audit(db, "Receipt uncertain", f"tx:{r.tx_id}", str(e), level="error")
            _alert("uncertain", "Мой налог: не ясно, создан ли чек по заказу "
                                f"#{r.tx_id} ({r.amount:g} ₽). Проверьте в приложении «Мой налог» и отметьте в админке → Заказы → Чеки.", 0)
        except Exception as e:  # noqa: BLE001 — unknown: treat as possibly sent
            _set(db, r, status="uncertain", error=f"ошибка: {e}"[:250])
            core.audit(db, "Receipt uncertain", f"tx:{r.tx_id}", repr(e), level="error")
        else:
            _set(db, r, status="sent", uuid=uuid[:64], error="")
            core.audit(db, "Receipt sent", f"tx:{r.tx_id}", f"{r.amount:g} RUB, {uuid}")
            done += 1
    return done


def paused(db: Session) -> bool:
    return core.get(db, "nalog_paused") == "1"


def recover_after_restore(db: Session) -> int:
    """After a database restore, receipts that were waiting in the backup
    may have been sent since it was made: none of them is sent by itself,
    a person checks each one in «Мой налог»."""
    n = (db.query(Receipt).filter(Receipt.status.in_(("pending", "sending")))
         .update({"status": "uncertain", "error": "база восстановлена из копии — проверьте в «Мой налог»",
                  "updated_at": now()}, synchronize_session=False))
    db.commit()
    return n


def out(r: Receipt) -> dict:
    return {"id": r.id, "tx_id": r.tx_id, "amount": r.amount, "name": r.name, "status": r.status,
            "uuid": r.uuid, "error": r.error, "tries": r.tries, "paid_at": r.paid_at,
            "created_at": r.created_at, "updated_at": r.updated_at,
            "url": receipt_url(env.nalog_inn, r.uuid) if r.uuid and env.nalog_inn else ""}
