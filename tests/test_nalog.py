"""
Receipts in «Мой налог»: one receipt per paid order, never two.

The income endpoint of FNS is not idempotent, so the dangerous case is an
answer that never came: such a receipt must wait for a person, not be
sent again by the shop.
"""

import time

import httpx
import pytest

from conftest import Fake, tg
from test_remnawave import rw  # noqa: F401


REAL_POST = httpx.post


class FNS:
    """A stand-in for lknpd.nalog.ru; every other address goes through."""

    def __init__(self):
        self.incomes = []          # what was registered
        self.logins = 0
        self.mode = "ok"           # ok | timeout | down | auth | refuse | 500

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        if "lknpd.nalog.ru" not in url:
            return REAL_POST(url, json=json, headers=headers, timeout=timeout, **kw)
        req = httpx.Request("POST", url)
        if url.endswith("/auth/lkfl"):
            self.logins += 1
            if self.mode == "down":
                raise httpx.ConnectError("refused", request=req)
            if self.mode == "auth":
                return httpx.Response(401, json={"message": "bad"}, request=req)
            return httpx.Response(200, json={"token": f"tok{self.logins}"}, request=req)
        assert url.endswith("/income")
        assert headers["Authorization"].startswith("Bearer tok")
        if self.mode == "timeout":
            self.incomes.append(json)          # FNS got it, the answer is lost
            raise httpx.ReadTimeout("slow", request=req)
        if self.mode == "500":
            return httpx.Response(500, text="oops", request=req)
        if self.mode == "refuse":
            return httpx.Response(400, json={"message": "bad amount"}, request=req)
        self.incomes.append(json)
        return httpx.Response(200, json={"approvedReceiptUuid": f"r{len(self.incomes)}"}, request=req)


@pytest.fixture()
def fns(env, monkeypatch, client, admin, rw):  # noqa: F811
    from app import nalog
    monkeypatch.setenv("NALOG_INN", "123456789012")
    monkeypatch.setenv("NALOG_PASSWORD", "pw-pw-pw")
    env.__init__()
    fake = FNS()
    monkeypatch.setattr(nalog.httpx, "post", fake.post)
    nalog._token.update(value="", at=0)
    nalog._alerted.clear()
    r = client.post("/api/admin/servers", headers=admin, json={
        "name": "Finland", "country": "FI", "kind": "remnawave", "url": rw["url"],
        "api_token": rw["token"], "inbound_ids": rw["squads"]})
    assert r.status_code == 200, r.text
    return fake


def _paid(client, db, uid, gateway="lava"):
    from app import billing
    from app.models import Plan, Transaction, User
    client.get("/api/app/me", headers=tg(uid))
    user = db.query(User).filter(User.tg_id == uid).one()
    plan = db.query(Plan).filter(Plan.is_trial.is_(False)).first()
    tx = billing.new_order(db, user, "purchase", "card", plan=plan)
    tx.gateway = gateway
    db.commit()
    assert billing.finish(db, tx, external_id=f"ext-{uid}")
    return db.get(Transaction, tx.id)


def _receipt(db, tx_id):
    from app.models import Receipt
    db.expire_all()
    return db.query(Receipt).filter(Receipt.tx_id == tx_id).one_or_none()


def test_a_paid_order_gets_one_receipt(client, db, fns):
    from app import billing, nalog
    tx = _paid(client, db, 8001)
    r = _receipt(db, tx.id)
    assert r.status == "pending" and r.amount == tx.amount and f"№{tx.id}" in r.name
    assert nalog.run(db) == 1
    r = _receipt(db, tx.id)
    assert r.status == "sent" and r.uuid == "r1"
    assert fns.incomes[0]["totalAmount"] == f"{tx.amount:.2f}"
    assert fns.incomes[0]["client"]["incomeType"] == "FROM_INDIVIDUAL"
    # The same order again (a repeated notification, the admin's button):
    assert not billing.finish(db, tx)
    assert not nalog.queue(db, tx)
    assert nalog.run(db) == 0 and len(fns.incomes) == 1


def test_no_receipt_without_real_money_through_a_chosen_gateway(client, db, fns, env, monkeypatch):
    for uid, gw in ((8011, "stars"), (8012, "yookassa"), (8013, "cryptobot")):
        assert _receipt(db, _paid(client, db, uid, gw).id) is None
    monkeypatch.setenv("NALOG_GATEWAYS", "stars")
    env.__init__()
    assert _receipt(db, _paid(client, db, 8014, "stars").id) is not None
    monkeypatch.delenv("NALOG_PASSWORD")
    env.__init__()
    assert _receipt(db, _paid(client, db, 8015, "stars").id) is None


def test_lost_answer_is_never_sent_again_by_itself(client, db, fns, admin):
    from app import nalog
    tx = _paid(client, db, 8021)
    fns.mode = "timeout"
    Fake.calls.clear()
    nalog.run(db)
    r = _receipt(db, tx.id)
    assert r.status == "uncertain" and len(fns.incomes) == 1
    assert any("не ясно" in m["text"] for m in Fake.sent("sendMessage"))
    fns.mode = "ok"
    for _ in range(3):
        nalog.run(db)
    assert len(fns.incomes) == 1                                   # still one
    # "Send again" is only for a sure refusal.
    assert client.post(f"/api/admin/receipts/{r.id}/retry", headers=admin).status_code == 409
    # The admin found it in «Мой налог»: marks it with the number.
    res = client.post(f"/api/admin/receipts/{r.id}/resolve", headers=admin,
                      json={"status": "sent", "uuid": "from-app"})
    assert res.status_code == 200
    assert _receipt(db, tx.id).uuid == "from-app"
    assert client.post(f"/api/admin/receipts/{r.id}/resolve", headers=admin,
                       json={"status": "pending"}).status_code == 409


def test_uncertain_receipt_not_found_in_the_app_can_be_made(client, db, fns, admin):
    from app import nalog
    tx = _paid(client, db, 8022)
    fns.mode = "500"
    nalog.run(db)
    r = _receipt(db, tx.id)
    assert r.status == "uncertain"
    fns.mode = "ok"
    assert client.post(f"/api/admin/receipts/{r.id}/resolve", headers=admin,
                       json={"status": "pending"}).status_code == 200
    nalog.run(db)
    assert _receipt(db, tx.id).status == "sent" and len(fns.incomes) == 1


def test_fns_down_waits_and_tries_later(client, db, fns):
    from app import nalog
    from app.models import Receipt
    tx = _paid(client, db, 8031)
    fns.mode = "down"
    nalog.run(db)
    r = _receipt(db, tx.id)
    assert r.status == "pending" and r.next_try_at > time.time() and r.tries == 1
    fns.mode = "ok"
    nalog.run(db)
    assert _receipt(db, tx.id).status == "pending"                # not due yet
    db.query(Receipt).update({"next_try_at": 0})
    db.commit()
    nalog.run(db)
    assert _receipt(db, tx.id).status == "sent" and len(fns.incomes) == 1


def test_wrong_password_pauses_the_queue_until_sign_in_works(client, db, fns, admin):
    from app import nalog
    a, b = _paid(client, db, 8041), _paid(client, db, 8042)
    fns.mode = "auth"
    Fake.calls.clear()
    nalog.run(db)
    assert _receipt(db, a.id).status == "pending" and _receipt(db, b.id).status == "pending"
    assert fns.logins == 2                                          # stopped, did not hammer FNS
    assert any("ИНН или пароль" in m["text"] for m in Fake.sent("sendMessage"))
    for _ in range(3):
        nalog.run(db)
    assert fns.logins == 2                                          # paused: no more tries
    assert client.get("/api/admin/receipts", headers=admin).json()["paused"]
    assert client.post("/api/admin/receipts/check", headers=admin).status_code == 400
    fns.mode = "ok"
    assert client.post("/api/admin/receipts/check", headers=admin).status_code == 200
    db.expire_all()
    from app.models import Receipt
    db.query(Receipt).update({"next_try_at": 0})
    db.commit()
    nalog.run(db)
    assert {_receipt(db, a.id).status, _receipt(db, b.id).status} == {"sent"}
    assert len(fns.incomes) == 2


def test_receipt_is_stored_with_the_order_not_after_it(client, db, fns, monkeypatch):
    """Whatever happens after the order is closed (the referral reward, the
    message, a restart), its receipt already exists."""
    from app import referral

    def boom(*a, **kw):
        raise SystemExit("process killed")
    monkeypatch.setattr(referral, "reward", boom)
    with pytest.raises(SystemExit):
        _paid(client, db, 8091)
    from app.models import Receipt, Transaction
    db.expire_all()
    tx = db.query(Transaction).order_by(Transaction.id.desc()).first()
    assert tx.status == "success"
    assert db.query(Receipt).filter(Receipt.tx_id == tx.id).one().status == "pending"


def test_failed_grant_leaves_no_receipt_and_the_retry_makes_one(client, db, fns, monkeypatch):
    from app import access, billing
    from app.models import Plan, Receipt, Transaction, User
    client.get("/api/app/me", headers=tg(8092))
    user = db.query(User).filter(User.tg_id == 8092).one()
    tx = billing.new_order(db, user, "purchase", "card", plan=db.query(Plan).filter(Plan.is_trial.is_(False)).first())
    tx.gateway = "lava"
    db.commit()
    real = access._parallel
    monkeypatch.setattr(access, "_parallel", lambda fn, items, **kw: [access.PanelError("down")] * len(items))
    with pytest.raises(access.AccessError):
        billing.finish(db, tx, external_id="e")
    db.expire_all()
    assert db.get(Transaction, tx.id).status == "paid" and db.query(Receipt).count() == 0
    monkeypatch.setattr(access, "_parallel", real)
    assert billing.finish(db, db.get(Transaction, tx.id))
    assert db.query(Receipt).filter(Receipt.tx_id == tx.id).count() == 1


def test_receipt_date_is_when_the_money_came(client, db, fns, monkeypatch):
    """Paid on the 31st, servers down until the 1st: the income is the 31st."""
    from app import access, billing, nalog
    from app.models import Plan, Transaction, User
    client.get("/api/app/me", headers=tg(8093))
    user = db.query(User).filter(User.tg_id == 8093).one()
    tx = billing.new_order(db, user, "purchase", "card", plan=db.query(Plan).filter(Plan.is_trial.is_(False)).first())
    tx.gateway = "lava"
    db.commit()
    real = access._parallel
    monkeypatch.setattr(access, "_parallel", lambda fn, items, **kw: [access.PanelError("down")] * len(items))
    with pytest.raises(access.AccessError):
        billing.finish(db, tx, external_id="e")
    db.query(Transaction).filter(Transaction.id == tx.id).update({"money_at": 1761868800})  # 31.10 00:00 UTC
    db.commit()
    monkeypatch.setattr(access, "_parallel", real)
    db.expire_all()
    assert billing.finish(db, db.get(Transaction, tx.id))
    nalog.run(db)
    sent = fns.incomes[-1]
    assert sent["operationTime"].startswith("2025-10-31T03:00:00+03:00")
    assert sent["requestTime"][:10] != "2025-10-31"                   # asked now


def test_admin_grant_without_a_gateway_holds_the_receipt(client, db, fns, admin):
    from app import billing, nalog
    from app.models import Plan, User
    client.get("/api/app/me", headers=tg(8094))
    user = db.query(User).filter(User.tg_id == 8094).one()
    tx = billing.new_order(db, user, "purchase", "card", plan=db.query(Plan).filter(Plan.is_trial.is_(False)).first())
    tx.gateway = "lava"                              # the payment page was opened, nobody paid
    db.commit()
    assert client.post(f"/api/admin/orders/{tx.id}/grant", headers=admin).status_code == 200
    r = _receipt(db, tx.id)
    assert r.status == "skipped" and "вручную" in r.error
    nalog.run(db)
    assert not fns.incomes
    # The admin says the money did come:
    assert client.post(f"/api/admin/receipts/{r.id}/resolve", headers=admin,
                       json={"status": "pending"}).status_code == 200
    nalog.run(db)
    assert _receipt(db, tx.id).status == "sent"


def test_restored_backup_never_resends_by_itself(client, db, fns, tmp_path):
    from app import backup, nalog
    _paid(client, db, 8095)                          # waiting in the queue when the copy is made
    path = backup.make(db)
    nalog.run(db)
    assert len(fns.incomes) == 1
    backup.restore(str(path), force=True)
    from app.models import Receipt
    db.expire_all()
    assert db.query(Receipt).one().status == "uncertain"
    nalog.run(db)
    assert len(fns.incomes) == 1


def test_refused_receipt_is_failed_not_uncertain(client, db, fns):
    from app import nalog
    tx = _paid(client, db, 8051)
    fns.mode = "refuse"
    nalog.run(db)
    r = _receipt(db, tx.id)
    assert r.status == "failed" and "отклонил" in r.error


def test_sending_interrupted_becomes_uncertain(client, db, fns):
    from app import nalog
    from app.models import Receipt
    tx = _paid(client, db, 8061)
    db.query(Receipt).update({"status": "sending", "updated_at": int(time.time()) - 3600})
    db.commit()
    nalog.run(db)
    assert _receipt(db, tx.id).status == "uncertain" and not fns.incomes


def test_two_runs_at_once_send_once(client, db, fns):
    """The claim is a conditional UPDATE: a second worker skips the row."""
    import threading

    from app import nalog
    _paid(client, db, 8071)
    gate = threading.Barrier(2)
    real = fns.post

    def slow(url, **kw):
        if url.endswith("/income"):
            time.sleep(0.2)
        return real(url, **kw)
    nalog.httpx.post = slow
    try:
        _two_workers(nalog, gate)
    finally:
        nalog.httpx.post = real
    assert len(fns.incomes) == 1


def _two_workers(nalog, gate):
    import threading

    from app.db import SessionLocal

    def worker():
        s = SessionLocal()
        try:
            gate.wait()
            nalog.run(s)
        finally:
            s.close()
    ts = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]


def test_receipts_list_for_the_admin(client, db, fns, admin):
    from app import nalog
    _paid(client, db, 8081)
    nalog.run(db)
    d = client.get("/api/admin/receipts", headers=admin).json()
    assert d["on"] and d["counts"] == {"sent": 1}
    item = d["items"][0]
    assert item["uuid"] == "r1" and item["url"].endswith("/receipt/123456789012/r1/print")
    assert client.get("/api/admin/receipts").status_code == 401
