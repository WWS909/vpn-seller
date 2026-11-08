"""Lava, WATA and Heleket: a notice counts only if it is genuinely signed
and the amount is paid in full."""

import base64
import hashlib
import hmac
import json

import pytest

from conftest import tg


@pytest.fixture(autouse=True)
def unreachable_server(env, monkeypatch):
    """One server that refuses connections: orders can be made, access
    cannot be granted yet — the case these tests are about."""
    monkeypatch.setenv("XUI_URL", "http://127.0.0.1:9/panel/")
    monkeypatch.setenv("XUI_USERNAME", "admin")
    monkeypatch.setenv("XUI_PASSWORD", "admin")
    monkeypatch.setenv("XUI_INBOUNDS", "1")
    env.__init__()


def _order(client, method="card", uid=70):
    plan = client.get("/api/app/config").json()["plans"][0]
    r = client.post("/api/app/order", json={"plan_id": plan["id"], "method": method}, headers=tg(uid))
    assert r.status_code == 200, r.text
    return r.json()


def _status(db, tx_id):
    from app.models import Transaction
    tx = db.get(Transaction, tx_id)
    db.refresh(tx)
    return tx.status


# ---------------------------------------------------------------- Lava
def _lava_sig(body: dict, key="add") -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hmac.new(key.encode(), text.encode(), hashlib.sha256).hexdigest()


def test_lava_needs_the_additional_key_and_the_full_amount(client, env, monkeypatch, db):
    from app.payments import lava
    monkeypatch.setenv("CARD_GATEWAY", "lava")
    monkeypatch.setenv("LAVA_SHOP_ID", "shop")
    monkeypatch.setenv("LAVA_SECRET_KEY", "sec")
    monkeypatch.setenv("LAVA_ADDITIONAL_KEY", "add")
    env.__init__()
    sent = {}

    def post(path, body):
        sent.update(body, path=path)
        return {"data": {"id": "lv-1", "url": "https://lava/pay/lv-1"}}
    monkeypatch.setattr(lava, "_post", post)
    order = _order(client)
    assert order["pay"]["url"] == "https://lava/pay/lv-1" and sent["orderId"] == str(order["order_id"])
    amount = db.get(__import__("app.models", fromlist=["Transaction"]).Transaction, order["order_id"]).amount
    body = {"invoice_id": "lv-1", "order_id": str(order["order_id"]), "status": "success", "amount": amount}
    url = "/api/pay/lava"
    assert client.post(url, json=body).status_code == 403
    assert client.post(url, json=body, headers={"Authorization": _lava_sig(body, "wrong")}).status_code == 403
    short = {**body, "amount": 1}
    assert client.post(url, json=short, headers={"Authorization": _lava_sig(short)}).status_code == 200
    assert _status(db, order["order_id"]) == "pending"
    other = {**body, "invoice_id": "lv-9"}
    assert client.post(url, json=other, headers={"Authorization": _lava_sig(other)}).status_code == 200
    assert _status(db, order["order_id"]) == "pending"
    r = client.post(url, json=body, headers={"Authorization": _lava_sig(body)})
    assert r.status_code == 503                            # genuine; no server to grant on yet
    assert _status(db, order["order_id"]) == "paid"


def test_lava_is_off_without_keys(client):
    assert client.post("/api/pay/lava", json={}).status_code == 404
    assert client.post("/api/pay/wata", content=b"{}").status_code == 404
    assert client.post("/api/pay/heleket", content=b"{}").status_code == 404


# ---------------------------------------------------------------- WATA
def test_wata_checks_the_rsa_signature(client, env, monkeypatch, db):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from app.payments import wata
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(wata, "_public_key", lambda refresh=False: private.public_key())
    monkeypatch.setenv("CARD_GATEWAY", "wata")
    monkeypatch.setenv("WATA_TOKEN", "tok")
    env.__init__()

    class R:
        status_code = 200

        def json(self):
            return {"id": "w-1", "url": "https://wata/pay/w-1"}
    posted = {}
    monkeypatch.setattr(wata.httpx, "post", lambda url, **kw: posted.update(kw, url=url) or R())
    order = _order(client, uid=71)
    assert order["pay"]["url"] == "https://wata/pay/w-1" and posted["json"]["orderId"] == str(order["order_id"])
    from app.models import Transaction
    amount = db.get(Transaction, order["order_id"]).amount
    raw = json.dumps({"id": "t-1", "transactionStatus": "Paid", "orderId": str(order["order_id"]),
                      "amount": amount, "currency": "RUB"}).encode()

    def sign(key, data):
        return base64.b64encode(key.sign(data, padding.PKCS1v15(), hashes.SHA512())).decode()
    h = {"Content-Type": "application/json"}
    assert client.post("/api/pay/wata", content=raw, headers=h).status_code == 403
    assert client.post("/api/pay/wata", content=raw,
                       headers={**h, "X-Signature": sign(other, raw)}).status_code == 403
    assert client.post("/api/pay/wata", content=raw + b" ",
                       headers={**h, "X-Signature": sign(private, raw)}).status_code == 403
    assert _status(db, order["order_id"]) == "pending"
    r = client.post("/api/pay/wata", content=raw, headers={**h, "X-Signature": sign(private, raw)})
    assert r.status_code == 503 and _status(db, order["order_id"]) == "paid"


# ---------------------------------------------------------------- Heleket
def _heleket_body(fields: dict, key="hk") -> bytes:
    unsigned = json.dumps(fields, separators=(",", ":"))
    sign = hashlib.md5(base64.b64encode(unsigned.encode()) + key.encode()).hexdigest()
    return (unsigned[:-1] + f',"sign":"{sign}"}}').encode()


def test_heleket_is_a_crypto_method_and_checks_its_signature(client, env, monkeypatch, db):
    from app.models import Transaction
    from app.payments import heleket
    monkeypatch.setenv("HELEKET_MERCHANT_ID", "m")
    monkeypatch.setenv("HELEKET_API_KEY", "hk")
    env.__init__()
    monkeypatch.setattr(heleket, "_post", lambda path, body: {"uuid": "hl-1", "url": "https://heleket/pay/hl-1"})
    from app import payments
    assert ("crypto", "heleket") in [(m["id"], m["gateway"]) for m in payments.methods()]
    order = _order(client, "crypto", uid=72)
    assert order["pay"]["url"] == "https://heleket/pay/hl-1"
    amount = db.get(Transaction, order["order_id"]).amount
    base = {"type": "payment", "uuid": "hl-1", "order_id": str(order["order_id"]),
            "amount": f"{amount:.2f}", "currency": "RUB", "status": "paid", "url": "https://x/y"}
    assert client.post("/api/pay/heleket", content=_heleket_body(base, "wrong")).status_code == 403
    assert client.post("/api/pay/heleket", content=_heleket_body({**base, "amount": "1.00"})).status_code == 200
    assert _status(db, order["order_id"]) == "pending"
    wait = {**base, "status": "check"}
    assert client.post("/api/pay/heleket", content=_heleket_body(wait)).status_code == 200
    assert _status(db, order["order_id"]) == "pending"
    tampered = _heleket_body(base).replace(b'"paid"', b'"paid" ')
    assert client.post("/api/pay/heleket", content=tampered).status_code in (403, 503)
    r = client.post("/api/pay/heleket", content=_heleket_body(base))
    assert r.status_code == 503 and _status(db, order["order_id"]) == "paid"


def test_crypto_gateway_choice(client, env, monkeypatch):
    from app import payments
    monkeypatch.setenv("CRYPTOBOT_TOKEN", "t")
    monkeypatch.setenv("HELEKET_MERCHANT_ID", "m")
    monkeypatch.setenv("HELEKET_API_KEY", "k")
    env.__init__()
    assert payments.crypto_gateway().NAME == "cryptobot"
    monkeypatch.setenv("CRYPTO_GATEWAY", "heleket")
    env.__init__()
    assert payments.crypto_gateway().NAME == "heleket"
    monkeypatch.setenv("CRYPTO_GATEWAY", "nonsense")
    env.__init__()
    assert payments.crypto_gateway() is None
