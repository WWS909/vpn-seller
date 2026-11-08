"""
Money: cases a review found going wrong, each fixed and kept here.

A one-time code or a personal discount used on several orders, an old
invoice paid after the person had renewed, a plan edited after the order,
"+1 device" on an unlimited plan, the same invoice paid twice, two
payments of one person closing at once.
"""

import threading
import time

import pytest

from conftest import Fake, tg
from test_remnawave import rw  # noqa: F401


def _bot(client, update):
    from app import notify
    return client.post("/api/bot/webhook", json=update,
                       headers={"X-Telegram-Bot-Api-Secret-Token": notify.webhook_secret()})


@pytest.fixture()
def server(client, admin, rw):  # noqa: F811
    r = client.post("/api/admin/servers", headers=admin, json={
        "name": "Finland", "country": "FI", "kind": "remnawave", "url": rw["url"],
        "api_token": rw["token"], "inbound_ids": rw["squads"]})
    assert r.status_code == 200, r.text
    return r.json()


def _plan(client):
    return [p for p in client.get("/api/app/config").json()["plans"] if not p["is_trial"]][0]


def _order(client, uid, **body):
    r = client.post("/api/app/order", headers=tg(uid), json=body)
    assert r.status_code == 200, r.text
    return r.json()


_upd = [700000]


def _pay(client, db, tx_id, uid, charge=""):
    """Telegram's "the stars are taken" for this order."""
    from app.models import Transaction
    db.expire_all()
    tx = db.get(Transaction, tx_id)
    _upd[0] += 1
    upd = {"update_id": _upd[0], "message": {
        "chat": {"id": uid, "type": "private"}, "from": {"id": uid},
        "successful_payment": {"currency": "XTR", "total_amount": tx.stars, "invoice_payload": f"tx:{tx.id}",
                               "telegram_payment_charge_id": charge or f"ch-{_upd[0]}"}}}
    assert _bot(client, upd).status_code == 200
    db.expire_all()
    return db.get(Transaction, tx_id)


def _pre(client, db, tx_id):
    """Telegram's "may I take the stars?" — returns our answer."""
    from app.models import Transaction
    db.expire_all()
    tx = db.get(Transaction, tx_id)
    _upd[0] += 1
    Fake.calls.clear()
    upd = {"update_id": _upd[0], "pre_checkout_query": {
        "id": f"pq{_upd[0]}", "from": {"id": 1, "language_code": "ru"}, "currency": "XTR",
        "total_amount": tx.stars, "invoice_payload": f"tx:{tx_id}"}}
    assert _bot(client, upd).status_code == 200
    return Fake.sent("answerPreCheckoutQuery")[-1]


def _user(db, tg_id):
    from app.models import User
    db.expire_all()
    return db.query(User).filter(User.tg_id == tg_id).one()


def _days_left(user) -> float:
    return round((user.expires_at - time.time()) / 86400, 1)


# ---------------------------------------------------------------- promo codes
def test_one_time_promo_goes_with_one_order_only(client, admin, db, server):
    from app.models import Log, Promo, PromoUse
    db.add(Promo(code="HALF", kind="percent", value=50, limit=1, new_only=True))
    db.commit()
    plan = _plan(client)
    a = _order(client, 7001, plan_id=plan["id"], method="stars", promo="HALF")
    b = _order(client, 7001, plan_id=plan["id"], method="stars", promo="HALF")
    c = _order(client, 7002, plan_id=plan["id"], method="stars", promo="HALF")
    assert a["amount"] == b["amount"] == c["amount"] == 99.0      # all opened before any was paid

    assert _pre(client, db, a["order_id"])["ok"] is True
    assert _pay(client, db, a["order_id"], 7001).status == "success"

    # The other two are refused before Telegram takes the stars.
    for o in (b, c):
        answer = _pre(client, db, o["order_id"])
        assert answer["ok"] is False and "Промокод" in answer["error_message"]

    # Paid anyway (both invoices accepted a moment before): the money is in,
    # the order is granted — but the code is counted once and the admin sees it.
    assert _pay(client, db, b["order_id"], 7001).status == "success"
    db.expire_all()
    p = db.query(Promo).filter(Promo.code == "HALF").one()
    assert p.used == 1 and db.query(PromoUse).count() == 1
    assert db.query(Log).filter(Log.action == "Promo code was already used").count() == 1


def test_promo_code_is_not_spent_when_the_sale_is_cheaper(client, admin, db, server):
    from app import marketing
    from app.models import Promo, PromoUse
    marketing.set_sale(db, 50)
    db.add(Promo(code="TEN", kind="percent", value=10, limit=1))
    db.commit()
    plan = _plan(client)
    q = client.post("/api/app/quote", headers=tg(7011), json={"plan_id": plan["id"], "promo": "TEN"}).json()
    assert q["amount"] == 99.0 and q["promo_applied"] is False      # the sale won
    o = _order(client, 7011, plan_id=plan["id"], method="stars", promo="TEN")
    assert _pay(client, db, o["order_id"], 7011).status == "success"
    db.expire_all()
    assert db.query(PromoUse).count() == 0
    assert db.query(Promo).filter(Promo.code == "TEN").one().used == 0   # still there for a later purchase


# ---------------------------------------------------------------- personal offers
def test_personal_discount_is_spent_by_one_order(client, admin, db, server):
    from app import marketing
    from app.models import Log, Offer
    client.get("/api/app/me", headers=tg(7101))
    marketing.give_offer(db, _user(db, 7101), 40, 3, "test")
    plan = _plan(client)
    a = _order(client, 7101, plan_id=plan["id"], method="stars")
    b = _order(client, 7101, plan_id=plan["id"], method="stars")
    assert a["amount"] == b["amount"] == 119.0
    assert _pay(client, db, a["order_id"], 7101).status == "success"
    answer = _pre(client, db, b["order_id"])
    assert answer["ok"] is False and "Скидка" in answer["error_message"]
    assert _pay(client, db, b["order_id"], 7101).status == "success"      # paid anyway: granted
    db.expire_all()
    offer = db.query(Offer).one()
    assert offer.tx_id == a["order_id"]                                  # spent once, by the first
    assert db.query(Log).filter(Log.action == "Personal discount used twice").count() == 1


def test_expired_discount_cannot_be_paid_later(client, admin, db, server):
    from app import marketing
    from app.models import Offer
    client.get("/api/app/me", headers=tg(7111))
    marketing.give_offer(db, _user(db, 7111), 30, 3, "test")
    o = _order(client, 7111, plan_id=_plan(client)["id"], method="stars")
    db.query(Offer).update({"expires_at": int(time.time()) - 60})
    db.commit()
    answer = _pre(client, db, o["order_id"])
    assert answer["ok"] is False and "Скидка закончилась" in answer["error_message"]


# ---------------------------------------------------------------- old invoices
def test_old_invoice_is_refused_after_another_purchase(client, admin, db, server):
    from app.models import Transaction
    plan = _plan(client)
    old = _order(client, 7201, plan_id=plan["id"], method="stars")       # e.g. a reminder in the chat
    db.query(Transaction).filter(Transaction.id == old["order_id"]).update(
        {"created_at": int(time.time()) - 3600})                         # sent an hour ago
    db.commit()
    # Nothing paid since it was sent: it can be paid.
    assert _pre(client, db, old["order_id"])["ok"] is True
    new = _order(client, 7201, plan_id=plan["id"], method="stars")       # renewed in the app instead
    assert _pay(client, db, new["order_id"], 7201).status == "success"
    answer = _pre(client, db, old["order_id"])
    assert answer["ok"] is False and "устарел" in answer["error_message"]


def test_invoice_older_than_ten_days_is_refused(client, admin, db, server):
    from app.models import Transaction
    o = _order(client, 7211, plan_id=_plan(client)["id"], method="stars")
    assert _pre(client, db, o["order_id"])["ok"] is True
    db.query(Transaction).filter(Transaction.id == o["order_id"]).update(
        {"created_at": int(time.time()) - 11 * 86400})
    db.commit()
    assert _pre(client, db, o["order_id"])["ok"] is False


def test_plan_edited_after_the_order_gives_what_was_shown(client, admin, db, server):
    from app.models import Plan
    plan = _plan(client)                                                 # 30 days for 199
    o = _order(client, 7221, plan_id=plan["id"], method="stars")
    p = db.get(Plan, plan["id"])
    p.days, p.price = 365, 1990
    db.commit()
    assert _pay(client, db, o["order_id"], 7221).status == "success"
    assert 29.5 < _days_left(_user(db, 7221)) < 30.5


# ---------------------------------------------------------------- devices and traffic
def test_no_extra_devices_for_an_unlimited_plan(client, admin, db, server):
    o = _order(client, 7301, plan_id=_plan(client)["id"], method="stars")
    _pay(client, db, o["order_id"], 7301)
    extra = _order(client, 7301, kind="devices", qty=1, method="stars")  # bought while limited (3)
    u = _user(db, 7301)
    u.device_limit = 0                                                   # moved to an unlimited plan
    db.commit()
    r = client.post("/api/app/order", headers=tg(7301), json={"kind": "devices", "qty": 1, "method": "stars"})
    assert r.status_code == 400 and "без ограничения" in r.json()["detail"]
    # The old order paid now must not turn "unlimited" into "1 device".
    assert _pay(client, db, extra["order_id"], 7301).status == "success"
    assert _user(db, 7301).device_limit == 0


# ---------------------------------------------------------------- one invoice, two charges
def test_second_charge_for_the_same_invoice_is_given_back(client, admin, db, server):
    o = _order(client, 7401, plan_id=_plan(client)["id"], method="stars")
    assert _pay(client, db, o["order_id"], 7401, charge="first").status == "success"
    days = _days_left(_user(db, 7401))
    Fake.calls.clear()
    _pay(client, db, o["order_id"], 7401, charge="second")               # opened on two phones
    refunds = Fake.sent("refundStarPayment")
    assert refunds and refunds[0]["telegram_payment_charge_id"] == "second"
    assert _days_left(_user(db, 7401)) == days                           # granted once
    Fake.calls.clear()
    _pay(client, db, o["order_id"], 7401, charge="first")                # Telegram repeating itself
    assert not Fake.sent("refundStarPayment")


# ---------------------------------------------------------------- at the same moment
def _slow_panels(monkeypatch, seconds=1.0):
    from app import access
    orig = access._parallel

    def slow(fn, items, timeout=40.0, **kw):
        time.sleep(seconds)
        return orig(fn, items, timeout, **kw)
    monkeypatch.setattr(access, "_parallel", slow)


def _finish_in_thread(tx_id):
    from app import billing
    from app.db import SessionLocal
    from app.models import Transaction

    def run():
        s = SessionLocal()
        try:
            billing.finish(s, s.get(Transaction, tx_id))
        finally:
            s.close()
    return threading.Thread(target=run)


def test_two_payments_closing_at_once_both_count(client, admin, db, server, monkeypatch):
    plan = _plan(client)
    a = _order(client, 7501, plan_id=plan["id"], method="stars")
    b = _order(client, 7501, plan_id=plan["id"], method="stars")
    _slow_panels(monkeypatch)
    t1, t2 = _finish_in_thread(a["order_id"]), _finish_in_thread(b["order_id"])
    t1.start()
    time.sleep(0.2)
    t2.start()
    t1.join()
    t2.join()
    assert 59.5 < _days_left(_user(db, 7501)) < 60.5                   # not 30


def test_referral_days_and_own_purchase_at_once(client, admin, db, server, monkeypatch):
    from app import core
    core.put(db, "ref_days", "7")
    plan = _plan(client)
    client.get("/api/app/me", headers=tg(7601))
    ref = _user(db, 7601)
    client.get("/api/app/me", headers=tg(7602, start=f"r{ref.id}"))
    own = _order(client, 7601, plan_id=plan["id"], method="stars")       # the inviter buys
    friend = _order(client, 7602, plan_id=plan["id"], method="stars")    # the friend buys
    _slow_panels(monkeypatch)
    t1, t2 = _finish_in_thread(own["order_id"]), _finish_in_thread(friend["order_id"])
    t2.start()
    time.sleep(0.9)
    t1.start()
    t1.join()
    t2.join()
    assert 36.5 < _days_left(_user(db, 7601)) < 37.5                   # 30 bought + 7 reward


def test_order_paid_from_the_balance_brings_no_days(client, admin, db, server):
    from app import core
    core.put(db, "ref_days", "7")
    core.put(db, "ref_friend_days", "3")
    core.put(db, "ref_balance_pay", "1")
    client.get("/api/app/me", headers=tg(7701))
    ref = _user(db, 7701)
    client.get("/api/app/me", headers=tg(7702, start=f"r{ref.id}"))
    friend = _user(db, 7702)
    friend.ref_balance = 500                     # money left from the money mode
    db.commit()
    r = client.post("/api/app/order", headers=tg(7702), json={"plan_id": _plan(client)["id"], "method": "balance"})
    assert r.status_code == 200 and r.json()["pay"]["type"] == "done", r.text
    assert 29.5 < _days_left(_user(db, 7702)) < 30.5                   # no friend bonus on it
    assert int(_user(db, 7701).ref_days or 0) == 0                       # and no inviter days


# ---------------------------------------------------------------- the retry job
def test_stubborn_paid_order_is_retried_slower_and_reported(client, admin, db):
    from app import billing
    from app.models import Transaction
    client.get("/api/app/me", headers=tg(7801))
    tx = Transaction(user_id=_user(db, 7801).id, kind="purchase", plan_id=_plan(client)["id"],
                     amount=199, status="paid", method="card", gateway="yookassa")
    db.add(tx)
    db.commit()
    billing.retry_paid(db)                                               # no servers at all
    db.refresh(tx)
    assert tx.status == "paid" and tx.tries == 1 and tx.next_try_at > time.time()
    billing.retry_paid(db)                                               # not due yet: not tried
    db.refresh(tx)
    assert tx.tries == 1
    tx.tries, tx.next_try_at = 4, 0
    db.commit()
    Fake.calls.clear()
    billing.retry_paid(db)
    db.refresh(tx)
    assert tx.tries == 5
    assert any(f"#{tx.id}" in m.get("text", "") for m in Fake.sent("sendMessage"))


def test_second_charge_while_the_order_waits_for_servers_is_given_back(client, admin, db, server, monkeypatch):
    from app import access, billing
    o = _order(client, 7901, plan_id=_plan(client)["id"], method="stars")
    orig = access._parallel
    monkeypatch.setattr(access, "_parallel", lambda fn, items, timeout=40.0, **kw:
                        [access.PanelError("panel down") for _ in items])
    assert _pay(client, db, o["order_id"], 7901, charge="first").status == "paid"     # parked
    monkeypatch.setattr(access, "_parallel", orig)                                    # servers are back
    Fake.calls.clear()
    _pay(client, db, o["order_id"], 7901, charge="second")                            # the other phone
    assert [r["telegram_payment_charge_id"] for r in Fake.sent("refundStarPayment")] == ["second"]
    billing.retry_paid(db)
    assert 29.5 < _days_left(_user(db, 7901)) < 30.5                                 # granted once


def test_traffic_job_does_not_undo_a_pack_bought_meanwhile(client, admin, db, server, monkeypatch):
    """The traffic job blocks somebody over the limit; at that moment they
    buy more. The job must push what is true now, not its old copy."""
    from app import access, billing
    from app.config import settings as env
    from app.db import SessionLocal
    from app.models import GB, Plan, Transaction
    pushed = []

    class Panel:
        def __init__(self, snap, timeout=15.0):
            pass

        def ensure(self, spec):
            pushed.append((spec.enable, spec.total_bytes))
            return []

        def reset_traffic(self, spec):
            pass

        def traffic_by_email(self):
            return {f"{env.xui_email_prefix}7911": 11 * GB}
    monkeypatch.setattr(access, "panel_for", Panel)
    db.add(Plan(name="10 GB", days=30, price=100, devices=1, traffic_gb=10))
    db.commit()
    plan = db.query(Plan).filter(Plan.name == "10 GB").one()
    from app import core
    core.put(db, "traffic_packs", "[[10, 50]]")
    assert _pay(client, db, _order(client, 7911, plan_id=plan.id, method="stars")["order_id"], 7911).status == "success"
    pack = _order(client, 7911, kind="traffic", gb=10, method="stars")
    orig_push, first = access.push, [True]

    def push(db_, user, **kw):
        if first[0] and not kw.get("required"):
            first[0] = False
            with SessionLocal() as s:                     # the pack is paid right now
                billing.finish(s, s.get(Transaction, pack["order_id"]))
        return orig_push(db_, user, **kw)
    monkeypatch.setattr(access, "push", push)
    pushed.clear()
    access.collect_traffic(db)
    assert pushed[-1] == (True, 20 * GB)                  # unblocked with the new limit
