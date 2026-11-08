"""
Tribute, CloudPayments, MulenPay, Lave Pay.

Their notifications are never trusted on their own: the order is closed only
on the gateway's own answer about it (asked with our keys), and only when
the amount is paid in full. Tribute, CloudPayments and Lave Pay sign their
notifications too — a wrong signature is refused before anything is asked.
"""

import base64
import hashlib
import hmac
import json
import time

import httpx
import pytest

from conftest import tg

REAL_POST, REAL_GET = httpx.post, httpx.get


@pytest.fixture(autouse=True)
def unreachable_server(env, monkeypatch):
    monkeypatch.setenv("XUI_URL", "http://127.0.0.1:9/panel/")
    monkeypatch.setenv("XUI_USERNAME", "admin")
    monkeypatch.setenv("XUI_PASSWORD", "admin")
    monkeypatch.setenv("XUI_INBOUNDS", "1")
    env.__init__()


class Gate:
    """A stand-in for one gateway's API: answers create and status."""

    def __init__(self, host, create, look):
        self.host, self.create, self.look = host, create, look
        self.asked = 0
        self.sent = []

    def post(self, url, **kw):
        if self.host not in url:
            return REAL_POST(url, **kw)
        self.sent.append((url, kw.get("json")))
        req = httpx.Request("POST", url)
        if "find" in url:
            self.asked += 1
            return httpx.Response(200, json=self.look(), request=req)
        return httpx.Response(200, json=self.create(kw.get("json") or {}), request=req)

    def get(self, url, **kw):
        if self.host not in url:
            return REAL_GET(url, **kw)
        self.asked += 1
        return httpx.Response(200, json=self.look(), request=httpx.Request("GET", url))


def _order(client, method="card", uid=80):
    plan = client.get("/api/app/config").json()["plans"][0]
    r = client.post("/api/app/order", json={"plan_id": plan["id"], "method": method}, headers=tg(uid))
    assert r.status_code == 200, r.text
    return r.json()


def _tx(db, oid):
    from app.models import Transaction
    db.expire_all()
    return db.get(Transaction, oid)


def _install(monkeypatch, gate):
    monkeypatch.setattr(httpx, "post", gate.post)
    monkeypatch.setattr(httpx, "get", gate.get)
    from app.routers import pay
    pay._asked.clear()


# ---------------------------------------------------------------- Tribute
def test_tribute(client, env, monkeypatch, db):
    monkeypatch.setenv("CARD_GATEWAY", "tribute")
    monkeypatch.setenv("TRIBUTE_API_KEY", "tk")
    env.__init__()
    state = {"status": "pending", "amount": 0}
    gate = Gate("tribute.tg", lambda b: {"uuid": "tr-1", "paymentUrl": "https://tribute/pay/tr-1"},
                lambda: {"uuid": "tr-1", "status": state["status"], "amount": state["amount"], "currency": "rub"})
    _install(monkeypatch, gate)
    order = _order(client, uid=81)
    assert order["pay"]["url"] == "https://tribute/pay/tr-1"
    body = gate.sent[0][1]
    tx = _tx(db, order["order_id"])
    assert body["amount"] == int(round(tx.amount * 100)) and body["currency"] == "rub" and body["period"] == "onetime"
    raw = json.dumps({"name": "shop_order", "payload": {"uuid": "tr-1", "amount": 100, "status": "paid"}}).encode()
    sig = hmac.new(b"tk", raw, hashlib.sha256)
    url = "/api/pay/tribute"
    assert client.post(url, content=raw, headers={"trbt-signature": "00" * 32}).status_code == 403
    assert gate.asked == 0                                       # nothing asked on a bad signature
    # Signed, but Tribute itself says it is not paid (the body lied about it).
    assert client.post(url, content=raw, headers={"trbt-signature": sig.hexdigest()}).json() == {"status": "ok"}
    assert _tx(db, order["order_id"]).status == "pending"
    state.update(status="paid", amount=int(round(tx.amount * 100)) - 100)          # one rouble short
    client.post(url, content=raw, headers={"trbt-signature": base64.b64encode(sig.digest()).decode()})
    assert _tx(db, order["order_id"]).status == "pending"
    state["amount"] = int(round(tx.amount * 100))
    r = client.post(url, content=raw, headers={"trbt-signature": sig.hexdigest()})
    assert r.status_code == 503 and _tx(db, order["order_id"]).status == "paid"   # no server: parked


# ---------------------------------------------------------------- CloudPayments
def test_cloudpayments(client, env, monkeypatch, db):
    monkeypatch.setenv("CARD_GATEWAY", "cloudpayments")
    monkeypatch.setenv("CLOUDPAYMENTS_PUBLIC_ID", "pk_1")
    monkeypatch.setenv("CLOUDPAYMENTS_API_SECRET", "cs")
    env.__init__()
    state = {"Status": "Declined", "Amount": 0}
    gate = Gate("cloudpayments.ru", lambda b: {"Success": True, "Model": {"Id": "cp-1", "Url": "https://orders.cp/d/1"}},
                lambda: {"Success": True, "Model": {"InvoiceId": state.get("InvoiceId"), "Status": state["Status"],
                                                    "Amount": state["Amount"], "Currency": "RUB"}})
    _install(monkeypatch, gate)
    order = _order(client, uid=82)
    tx = _tx(db, order["order_id"])
    state["InvoiceId"] = str(tx.id)
    assert order["pay"]["url"] == "https://orders.cp/d/1" and tx.external_id == "cp-1"
    raw = f"TransactionId=55&Amount={tx.amount}&Currency=RUB&InvoiceId={tx.id}&Status=Completed&OperationType=Payment".encode()
    good = base64.b64encode(hmac.new(b"cs", raw, hashlib.sha256).digest()).decode()
    h = {"Content-Type": "application/x-www-form-urlencoded"}
    assert client.post("/api/pay/cloudpayments", content=raw, headers=h).status_code == 403
    assert client.post("/api/pay/cloudpayments", content=raw, headers={**h, "Content-HMAC": good}).json() == {"code": 0}
    assert _tx(db, tx.id).status == "pending"                     # CloudPayments says Declined
    state.update(Status="Completed", Amount=tx.amount)
    r = client.post("/api/pay/cloudpayments", content=raw, headers={**h, "Content-HMAC": good})
    assert r.status_code == 503 and _tx(db, tx.id).status == "paid"


# ---------------------------------------------------------------- MulenPay
def test_mulenpay_is_checked_with_its_api(client, env, monkeypatch, db):
    monkeypatch.setenv("CARD_GATEWAY", "mulenpay")
    monkeypatch.setenv("MULENPAY_API_KEY", "mk")
    monkeypatch.setenv("MULENPAY_SECRET_KEY", "ms")
    monkeypatch.setenv("MULENPAY_SHOP_ID", "77")
    env.__init__()
    state = {"status": 1, "amount": "0"}
    gate = Gate("mulenpay.ru", lambda b: {"success": True, "id": 555, "paymentUrl": "https://mulen/pay/555"},
                lambda: {"success": True, "payment": {"id": 555, "uuid": state.get("uuid"), "status": state["status"],
                                                      "amount": state["amount"], "currency": "rub"}})
    _install(monkeypatch, gate)
    order = _order(client, uid=83)
    tx = _tx(db, order["order_id"])
    state["uuid"] = str(tx.id)
    sent = gate.sent[0][1]
    assert sent["sign"] == hashlib.sha1(f"rub{tx.amount:.2f}77ms".encode()).hexdigest()
    assert sent["items"][0]["price"] == round(tx.amount, 2) and sent["uuid"] == str(tx.id)
    hint = {"id": 555, "uuid": str(tx.id), "payment_status": "success", "amount": tx.amount}
    client.post("/api/pay/mulenpay", json=hint)                   # MulenPay: still processing
    assert _tx(db, tx.id).status == "pending"
    client.post("/api/pay/mulenpay", json={**hint, "id": 556})    # not our payment id: not even asked
    asked = gate.asked
    state.update(status=3, amount=f"{tx.amount:.2f}")
    r = client.post("/api/pay/mulenpay", json=hint)
    assert gate.asked == asked + 1
    assert r.status_code == 503 and _tx(db, tx.id).status == "paid"


def test_fake_notifications_cannot_hammer_the_gateway(client, env, monkeypatch, db):
    monkeypatch.setenv("CARD_GATEWAY", "mulenpay")
    monkeypatch.setenv("MULENPAY_API_KEY", "mk")
    monkeypatch.setenv("MULENPAY_SECRET_KEY", "ms")
    monkeypatch.setenv("MULENPAY_SHOP_ID", "77")
    env.__init__()
    gate = Gate("mulenpay.ru", lambda b: {"success": True, "id": 9, "paymentUrl": "https://m/9"},
                lambda: {"success": True, "payment": {"id": 9, "status": 1}})
    _install(monkeypatch, gate)
    order = _order(client, uid=84)
    codes = [client.post("/api/pay/mulenpay", json={"id": 9, "uuid": str(order["order_id"])}).status_code
             for _ in range(35)]
    assert gate.asked == 30 and codes.count(503) == 5


# ---------------------------------------------------------------- Lave Pay
def _lave_sig(raw: bytes, secret=b"whsec_x", stamp=None):
    stamp = str(int(time.time()) if stamp is None else stamp)
    return {"x-lavepay-timestamp": stamp,
            "x-lavepay-signature": "v1=" + hmac.new(secret, stamp.encode() + b"." + raw, hashlib.sha256).hexdigest()}


def test_lavepay_in_dollars(client, env, monkeypatch, db):
    monkeypatch.setenv("CRYPTO_GATEWAY", "lavepay")
    monkeypatch.setenv("LAVEPAY_API_KEY", "LAVE-1-s")
    monkeypatch.setenv("LAVEPAY_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("LAVEPAY_RUB_PER_USD", "90")
    env.__init__()
    state = {"status": "active", "amount": "0"}
    gate = Gate("laveshka.io", lambda b: {"ok": True, "data": {"id": "inv_1", "pay_url": "https://t.me/laveshka?startapp=lpay_1"}},
                lambda: {"ok": True, "data": {"id": "inv_1", "order_id": state.get("order_id"), "status": state["status"],
                                              "amount": state["amount"], "currency": "USD"}})
    _install(monkeypatch, gate)
    order = _order(client, "crypto", uid=85)
    tx = _tx(db, order["order_id"])
    state["order_id"] = str(tx.id)
    usd = gate.sent[0][1]["amount"]
    import math
    assert float(usd) == math.ceil(tx.amount / 90 * 100) / 100 and tx.ext_amount == float(usd)
    raw = json.dumps({"id": "evt_1", "type": "invoice.paid", "data": {"id": "inv_1"}}).encode()
    url = "/api/pay/lavepay"
    assert client.post(url, content=raw, headers=_lave_sig(raw, b"wrong")).status_code == 403
    assert client.post(url, content=raw, headers=_lave_sig(raw, stamp=int(time.time()) - 3600)).status_code == 403
    state.update(status="paid", amount=f"{float(usd) - 0.05:.2f}")                 # five cents short
    client.post(url, content=raw, headers=_lave_sig(raw))
    assert _tx(db, tx.id).status == "pending"
    state["amount"] = usd
    r = client.post(url, content=raw, headers=_lave_sig(raw))
    assert r.status_code == 503 and _tx(db, tx.id).status == "paid"


def test_new_gateways_off_without_keys(client):
    for g in ("tribute", "cloudpayments", "mulenpay", "lavepay"):
        assert client.post(f"/api/pay/{g}", content=b"{}").status_code == 404


def test_lavepay_needs_a_rate(env, monkeypatch):
    from app.payments import lavepay
    monkeypatch.setenv("LAVEPAY_API_KEY", "k")
    monkeypatch.setenv("LAVEPAY_WEBHOOK_SECRET", "s")
    env.__init__()
    assert not lavepay.configured()                               # roubles, but no rate
    monkeypatch.setenv("LAVEPAY_RUB_PER_USD", "92,5")
    env.__init__()
    assert lavepay.configured() and lavepay.to_usd(185) == 2.0 and lavepay.to_usd(186) == 2.02


# ------------------------------------------------------------------ status checks for FreeKassa and WATA
class _Resp:
    def __init__(self, data, code=200):
        self._d, self.status_code, self.text = data, code, str(data)

    def json(self):
        return self._d


def test_freekassa_status_is_asked_with_the_api_key(monkeypatch):
    import hashlib
    import hmac
    from types import SimpleNamespace

    from app.config import settings as env
    from app.payments import freekassa
    monkeypatch.setattr(env, "freekassa_merchant_id", "123")
    monkeypatch.setattr(env, "freekassa_secret1", "s1")
    monkeypatch.setattr(env, "freekassa_secret2", "s2")
    monkeypatch.setattr(env, "freekassa_api_key", "")
    assert not freekassa.can_poll()                     # without the API key it is not asked at all
    monkeypatch.setattr(env, "freekassa_api_key", "apikey")
    assert freekassa.can_poll()
    sent = {}

    def post(url, json=None, timeout=0):
        sent.update(json, url=url)
        return _Resp({"type": "success", "orders": [
            {"merchant_order_id": "77", "fk_order_id": 1, "amount": 199.0, "currency": "RUB", "status": 1}]})
    monkeypatch.setattr(freekassa.httpx, "post", post)
    st = freekassa.status(SimpleNamespace(id=77))
    assert st == {"paid": True, "ref": "77", "amount": 199.0, "currency": "RUB"}
    assert sent["url"].endswith("/orders") and sent["paymentId"] == "77" and sent["shopId"] == 123
    body = {k: v for k, v in sent.items() if k not in ("signature", "url")}
    want = hmac.new(b"apikey", "|".join(str(v) for _, v in sorted(body.items())).encode(), hashlib.sha256).hexdigest()
    assert sent["signature"] == want
    monkeypatch.setattr(freekassa.httpx, "post", lambda url, json=None, timeout=0: _Resp(
        {"type": "success", "orders": [{"merchant_order_id": "77", "status": 0, "amount": 199}]}))
    assert freekassa.status(SimpleNamespace(id=77))["paid"] is False


def test_wata_status_finds_the_paid_transaction(monkeypatch):
    from types import SimpleNamespace

    from app.config import settings as env
    from app.payments import wata
    monkeypatch.setattr(env, "wata_token", "t")
    asked = {}

    def get(url, headers=None, timeout=0, params=None):
        asked.update(params, url=url)
        return _Resp({"items": [{"orderId": "88", "status": "Declined", "amount": 299, "currency": "RUB"},
                                {"orderId": "88", "status": "Paid", "amount": 299, "currency": "RUB"}]})
    monkeypatch.setattr(wata.httpx, "get", get)
    st = wata.status(SimpleNamespace(id=88))
    assert st["paid"] is True and st["amount"] == 299 and st["ref"] == "88"
    assert asked["orderId"] == "88" and asked["url"].endswith("/v2/transactions/")


def test_freekassa_and_wata_are_polled():
    from app import payments
    assert "freekassa" in payments.POLLABLE and "wata" in payments.POLLABLE


def test_a_revoked_token_does_not_come_back_when_the_database_blinks(monkeypatch):
    from app import auth
    from app import db as dbmod
    monkeypatch.setattr(auth, "_revoked", None)

    class Broken:
        def __enter__(self):
            raise RuntimeError("database is down")

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(dbmod, "SessionLocal", lambda: Broken())
    assert auth._load_revoked() == {}
    assert auth._revoked is None            # not cached: read again next time
