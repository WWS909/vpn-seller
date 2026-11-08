"""
RollyPay and cisPay. As with the others: a notification must be signed, and
even then the order closes only on the gateway's own answer about it, in
full and in roubles. Both can run next to another card gateway.
"""
import hashlib
import hmac
import json
import time

from test_gateways_more import Gate, _install, _order, _tx, unreachable_server  # noqa: F401


def _rolly_sig(raw, secret=b"whs", stamp=None):
    ts = str(int(time.time()) if stamp is None else stamp)
    return {"X-Signature": hmac.new(secret, ts.encode() + b"." + raw, hashlib.sha256).hexdigest(),
            "X-Timestamp": ts, "Content-Type": "application/json"}


def test_rollypay(client, env, monkeypatch, db):
    monkeypatch.setenv("CARD_GATEWAY", "rollypay")
    monkeypatch.setenv("ROLLYPAY_API_KEY", "rpk_live_k")
    monkeypatch.setenv("ROLLYPAY_SIGNING_SECRET", "whs")
    env.__init__()
    state = {"status": "created", "amount": "0"}
    gate = Gate("rollypay.io", lambda b: {"payment_id": "pay_1", "pay_url": "https://rollypay.io/pay/pay_1"},
                lambda: {"payment_id": "pay_1", "order_id": state.get("order"), "status": state["status"],
                         "amount": state["amount"], "payment_currency": "RUB"})
    _install(monkeypatch, gate)
    order = _order(client, "card", uid=91)
    assert order["pay"] == {"type": "url", "url": "https://rollypay.io/pay/pay_1"}
    tx = _tx(db, order["order_id"])
    sent = gate.sent[0][1]
    assert sent["amount"] == f"{tx.amount:.2f}" and sent["payment_currency"] == "RUB"
    assert sent["order_id"].startswith(f"{tx.id}-")
    state["order"] = sent["order_id"]
    raw = json.dumps({"event_type": "payment.paid", "payment_id": "pay_1", "status": "paid",
                      "order_id": sent["order_id"], "amount": sent["amount"]}).encode()
    url = "/api/pay/rollypay"
    assert client.post(url, content=raw, headers=_rolly_sig(raw, b"bad")).status_code == 403
    assert client.post(url, content=raw, headers=_rolly_sig(raw, stamp=int(time.time()) - 5 * 3600)).status_code == 403
    client.post(url, content=raw, headers=_rolly_sig(raw))         # RollyPay itself still says "created"
    assert _tx(db, tx.id).status == "pending"
    state.update(status="paid", amount=f"{tx.amount - 1:.2f}")     # a rouble short
    client.post(url, content=raw, headers=_rolly_sig(raw))
    assert _tx(db, tx.id).status == "pending"
    state["amount"] = f"{tx.amount:.2f}"
    r = client.post(url, content=raw, headers=_rolly_sig(raw))
    assert r.status_code == 503 and _tx(db, tx.id).status == "paid"   # paid; access waits for the server


def test_cispay(client, env, monkeypatch, db):
    monkeypatch.setenv("CARD_GATEWAY", "yookassa,cispay")
    monkeypatch.setenv("YOOKASSA_SHOP_ID", "1")
    monkeypatch.setenv("YOOKASSA_SECRET_KEY", "s")
    monkeypatch.setenv("CISPAY_SHOP_ID", "shop-uuid")
    monkeypatch.setenv("CISPAY_API_KEY", "cis_sec_k")
    env.__init__()
    ms = client.get("/api/app/config").json()["methods"]
    assert [m["id"] for m in ms if m["kind"] == "card"] == ["card", "cispay"]
    state = {"status": "PENDING", "amount": 0}
    gate = Gate("api.cispay.app", lambda b: {"id": "c_1", "payment_url": "https://pay.cispay.app/c_1", "status": "PENDING"},
                lambda: {"id": "c_1", "order_id": state.get("order"), "status": state["status"],
                         "amount": state["amount"], "currency": "RUB", **state.get("extra", {})})
    _install(monkeypatch, gate)
    order = _order(client, "cispay", uid=92)                       # the second card gateway, chosen by name
    tx = _tx(db, order["order_id"])
    assert tx.gateway == "cispay" and order["pay"]["url"] == "https://pay.cispay.app/c_1"
    sent = gate.sent[0][1]
    assert sent["amount"] == round(tx.amount * 100) and sent["customer_id"] == "92"
    state["order"] = sent["order_id"]
    raw = json.dumps({"id": "c_1", "order_id": sent["order_id"], "status": "PAID", "amount": sent["amount"]}).encode()
    sig = hmac.new(b"cis_sec_k", raw, hashlib.sha256).hexdigest()
    url = "/api/pay/cispay"
    assert client.post(url, content=raw, headers={"X-Signature": "0" * 64}).status_code == 403
    state.update(status="PAID", amount=sent["amount"], extra={"is_sandbox": True})   # a test payment is not money
    client.post(url, content=raw, headers={"X-Signature": sig})
    assert _tx(db, tx.id).status == "pending"
    state["extra"] = {}
    r = client.post(url, content=raw, headers={"X-Signature": sig})
    assert r.status_code == 503 and _tx(db, tx.id).status == "paid"


def test_off_without_keys(client):
    for g in ("rollypay", "cispay"):
        assert client.post(f"/api/pay/{g}", content=b"{}").status_code == 404
