"""The referral program in money mode: a share of each payment on a
balance, withdrawal requests the owner decides, paying from the balance."""

from __future__ import annotations

import pytest

from conftest import Fake, tg
from test_remnawave import rw  # noqa: F401 — the fixture

CARD = "4111 1111 1111 1111"           # passes the Luhn check


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


@pytest.fixture()
def money(client, admin):
    r = client.post("/api/admin/settings", headers=admin, json={
        "ref_enabled": "1", "ref_mode": "money", "ref_percent": "20", "ref_friend_days": "0",
        "ref_payout_methods": ('[{"kind":"card","ru":"Банковская карта","en":"Bank card"},'
                               '{"kind":"sbp","ru":"СБП по номеру","en":"SBP"},'
                               '{"kind":"crypto","ru":"USDT (TRC20)","en":"USDT (TRC20)"},'
                               '{"kind":"other","ru":"Выключенный","en":"Off","on":false}]')})
    assert r.status_code == 200, r.text


def _pay_stars(client, db, uid: int, plan_index: int = 0, **extra) -> int:
    """A real Stars payment: an order, then Telegram's successful_payment."""
    from app.models import Transaction
    plan = [p for p in client.get("/api/app/config").json()["plans"] if not p["is_trial"]][plan_index]
    r = client.post("/api/app/order", headers=tg(uid), json={"plan_id": plan["id"], "method": "stars", **extra})
    assert r.status_code == 200, r.text
    tx = db.get(Transaction, r.json()["order_id"])
    db.refresh(tx)
    upd = {"update_id": 700000 + tx.id, "message": {
        "chat": {"id": uid, "type": "private"}, "from": {"id": uid},
        "successful_payment": {"currency": "XTR", "total_amount": tx.stars, "invoice_payload": f"tx:{tx.id}",
                               "telegram_payment_charge_id": f"ch-{tx.id}"}}}
    assert _bot(client, upd).status_code == 200
    db.expire_all()
    assert db.get(Transaction, tx.id).status == "success"
    return tx.id


def _invite(client, db, inviter: int, friend: int):
    from app.models import User
    client.get("/api/app/me", headers=tg(inviter))
    ref = db.query(User).filter(User.tg_id == inviter).one()
    client.get("/api/app/me", headers=tg(friend, start=f"r{ref.id}"))
    db.expire_all()
    assert db.query(User).filter(User.tg_id == friend).one().referrer_id == ref.id
    return ref


def _user(db, tg_id):
    from app.models import User
    db.expire_all()
    return db.query(User).filter(User.tg_id == tg_id).one()


# ---------------------------------------------------------------- earning
def test_inviter_earns_a_share_of_the_first_payment(client, admin, db, server, money):
    _invite(client, db, 6001, 6002)
    _pay_stars(client, db, 6002)                              # 199 ₽
    ref = _user(db, 6001)
    assert ref.ref_balance == pytest.approx(39.8) and ref.ref_earned == pytest.approx(39.8)
    assert ref.expires_at == 0                                # money, not days
    note = [m["text"] for m in Fake.sent("sendMessage") if m["chat_id"] == 6001]
    assert any("39,80" in t or "39.80" in t or "39.8" in t for t in note), note
    _pay_stars(client, db, 6002)                              # only the first payment by default
    assert _user(db, 6001).ref_balance == pytest.approx(39.8)
    client.post("/api/admin/settings", headers=admin, json={"ref_every": "1"})
    _pay_stars(client, db, 6002, plan_index=1)                # 499 ₽ → +99.8
    assert _user(db, 6001).ref_balance == pytest.approx(139.6)
    d = client.get("/api/app/referrals", headers=tg(6001)).json()
    assert d["mode"] == "money" and d["percent"] == 20 and d["paid"] == 1
    assert d["wallet"]["earned"] == pytest.approx(139.6)
    assert [m["id"] for m in d["wallet"]["methods"]] == ["card1", "sbp2", "crypto3"]   # the off one is hidden
    assert d["friends"][0]["paid"] and d["friends"][0]["money"] == pytest.approx(139.6)
    assert d["friends"][0]["name"].endswith("***")
    adm = client.get("/api/admin/referrals", headers=admin).json()
    assert adm["money_given"] == pytest.approx(139.6) and adm["balances"] == pytest.approx(139.6)


def test_same_payment_is_never_rewarded_twice(client, db, server, money):
    from app import referral
    from app.models import Transaction
    _invite(client, db, 6011, 6012)
    tx_id = _pay_stars(client, db, 6012)
    tx = db.get(Transaction, tx_id)
    friend = _user(db, 6012)
    assert referral.reward(db, tx, friend) == 0               # finish() already did it
    assert _user(db, 6011).ref_balance == pytest.approx(39.8)


# ---------------------------------------------------------------- withdrawing
def _give(db, tg_id: int, amount: float):
    u = _user(db, tg_id)
    u.ref_balance = amount
    u.ref_earned = amount
    db.commit()


def test_payout_request_and_the_owner_decides(client, admin, db, money):
    client.get("/api/app/me", headers=tg(6021))
    _give(db, 6021, 150)
    ask = lambda **b: client.post("/api/app/referrals/payout", headers=tg(6021), json=b)  # noqa: E731
    assert ask(method="nope", account=CARD).status_code == 400
    bad = ask(method="card1", account="4111 1111 1111 1112")
    assert bad.status_code == 400 and "карты" in bad.json()["detail"]
    assert ask(method="sbp2", account="+7 900 123-45-67").status_code == 400      # no bank
    assert ask(method="crypto3", account="short").status_code == 400
    r = ask(method="card1", account=CARD)
    assert r.status_code == 200, r.text
    pid = r.json()["payout"]["id"]
    assert r.json()["payout"]["details"] == "4111…1111"      # the customer sees it half hidden
    assert _user(db, 6021).ref_balance == pytest.approx(0)   # taken right away
    assert ask(method="card1", account=CARD).status_code == 400                   # one at a time
    # The admin got it in Telegram with the two buttons.
    notice = [m for m in Fake.sent("sendMessage") if m["chat_id"] == 999 and "a:po:" in str(m.get("reply_markup"))]
    assert notice and "4111 1111 1111 1111" in notice[-1]["text"]
    listed = client.get("/api/admin/payouts", headers=admin).json()
    assert listed[0]["id"] == pid and listed[0]["details"] == "4111 1111 1111 1111"
    # Rejected: the money comes back and the person is told why.
    r = client.post(f"/api/admin/payouts/{pid}/reject", headers=admin, json={"note": "карта не найдена"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert _user(db, 6021).ref_balance == pytest.approx(150)
    assert any("карта не найдена" in m["text"] for m in Fake.sent("sendMessage") if m["chat_id"] == 6021)
    assert client.post(f"/api/admin/payouts/{pid}/paid", headers=admin).status_code == 409
    # A second request, paid this time — for part of the balance.
    r = ask(method="sbp2", account="+7 900 123-45-67", bank="Т-Банк", amount=100)
    assert r.status_code == 200, r.text
    pid2 = r.json()["payout"]["id"]
    assert _user(db, 6021).ref_balance == pytest.approx(50)
    assert client.post(f"/api/admin/payouts/{pid2}/paid", headers=admin).status_code == 200
    assert client.post(f"/api/admin/payouts/{pid2}/reject", headers=admin, json={}).status_code == 409
    assert _user(db, 6021).ref_balance == pytest.approx(50)  # a paid request never comes back
    w = client.get("/api/app/referrals", headers=tg(6021)).json()["wallet"]
    assert w["withdrawn"] == 100 and w["pending"] == 0 and w["balance"] == 50
    assert [h["status"] for h in w["history"]] == ["paid", "rejected"]


def test_cancel_returns_the_money_once(client, db, money):
    client.get("/api/app/me", headers=tg(6031))
    _give(db, 6031, 80)
    r = client.post("/api/app/referrals/payout", headers=tg(6031),
                    json={"method": "crypto3", "account": "TXyz1234567890abcdefGHIJKLmnop"})
    pid = r.json()["payout"]["id"]
    assert client.post(f"/api/app/referrals/payout/{pid}/cancel", headers=tg(6031)).status_code == 200
    assert client.post(f"/api/app/referrals/payout/{pid}/cancel", headers=tg(6031)).status_code == 404
    assert _user(db, 6031).ref_balance == pytest.approx(80)
    # Nobody else can cancel it.
    r = client.post("/api/app/referrals/payout", headers=tg(6031),
                    json={"method": "crypto3", "account": "TXyz1234567890abcdefGHIJKLmnop"})
    client.get("/api/app/me", headers=tg(6032))
    assert client.post(f"/api/app/referrals/payout/{r.json()['payout']['id']}/cancel",
                       headers=tg(6032)).status_code == 404


def test_minimum_and_empty_balance(client, admin, db, money):
    client.get("/api/app/me", headers=tg(6041))
    r = client.post("/api/app/referrals/payout", headers=tg(6041), json={"method": "card1", "account": CARD})
    assert r.status_code == 400 and "недостаточно" in r.json()["detail"]
    _give(db, 6041, 40)
    client.post("/api/admin/settings", headers=admin, json={"ref_min_payout": "100"})
    r = client.post("/api/app/referrals/payout", headers=tg(6041), json={"method": "card1", "account": CARD})
    assert r.status_code == 400 and "100 ₽" in r.json()["detail"]
    r = client.post("/api/app/referrals/payout", headers=tg(6041),
                    json={"method": "card1", "account": CARD, "amount": 500})
    assert r.status_code == 400                                 # more than there is
    assert _user(db, 6041).ref_balance == pytest.approx(40)


def test_two_requests_at_once_cannot_overdraw(client, db, money):
    """The balance is taken with one conditional UPDATE: of two requests
    racing for the same money only one gets it."""
    import threading

    from app import referral
    from app.billing import OrderError
    from app.db import SessionLocal
    from app.models import Payout, User
    client.get("/api/app/me", headers=tg(6051))
    _give(db, 6051, 100)
    results = []

    def go():
        s = SessionLocal()
        try:
            user = s.query(User).filter(User.tg_id == 6051).one()
            referral.request_payout(s, user, "card1", CARD, amount=100)
            results.append("ok")
        except OrderError as e:
            results.append(e.args[0])
        except Exception as e:  # noqa: BLE001 — "database is locked" is a refusal too
            results.append(type(e).__name__)
        finally:
            s.close()
    threads = [threading.Thread(target=go) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert _user(db, 6051).ref_balance >= -0.001
    assert results.count("ok") == 1, results
    paid_out = sum(p.amount for p in db.query(Payout).all())
    assert paid_out <= 100 + 0.001


def test_withdrawals_closed_without_methods_or_program(client, admin, db, money):
    client.get("/api/app/me", headers=tg(6061))
    _give(db, 6061, 300)
    client.post("/api/admin/settings", headers=admin, json={"ref_payout_methods": "[]"})
    r = client.post("/api/app/referrals/payout", headers=tg(6061), json={"method": "card1", "account": CARD})
    assert r.status_code == 400 and "недоступен" in r.json()["detail"]
    # The program is turned off: the money already earned is still shown.
    client.post("/api/admin/settings", headers=admin, json={"ref_enabled": "0"})
    d = client.get("/api/app/referrals", headers=tg(6061)).json()
    assert d["enabled"] is False and d["wallet"]["balance"] == 300


def test_payout_buttons_in_the_bot(client, admin, db, money):
    client.get("/api/app/me", headers=tg(6071))
    _give(db, 6071, 60)
    pid = client.post("/api/app/referrals/payout", headers=tg(6071),
                      json={"method": "card1", "account": CARD}).json()["payout"]["id"]

    def press(uid, data):
        return _bot(client, {"update_id": 800000 + uid + len(Fake.calls), "callback_query": {
            "id": f"cq{uid}", "from": {"id": 999, "language_code": "ru"}, "data": data,
            "message": {"message_id": 55, "text": "Заявка", "chat": {"id": 999, "type": "private"}}}})
    press(1, f"a:po:{pid}:ok")
    assert client.get("/api/admin/payouts", headers=admin).json()[0]["status"] == "paid"
    edited = Fake.sent("editMessageText")
    assert edited and "Выплачено" in edited[-1]["text"]
    press(2, f"a:po:{pid}:no")                                   # too late: nothing changes
    answers = [a for a in Fake.sent("answerCallbackQuery") if a.get("callback_query_id") == "cq2"]
    assert answers and "уже обработана" in answers[-1]["text"]
    assert _user(db, 6071).ref_balance == pytest.approx(0)


# ---------------------------------------------------------------- paying from the balance
def test_subscription_paid_from_the_balance(client, admin, db, server, money):
    from app.models import Transaction
    _invite(client, db, 6081, 6082)
    _give(db, 6082, 250)                     # the friend has their own balance too
    plan = [p for p in client.get("/api/app/config").json()["plans"] if not p["is_trial"]][0]   # 199 ₽
    assert plan["price"] == 199 and not plan["is_trial"]
    r = client.post("/api/app/order", headers=tg(6082), json={"plan_id": plan["id"], "method": "balance"})
    assert r.status_code == 200, r.text
    assert r.json()["pay"]["type"] == "done" and r.json()["status"] == "success"
    u = _user(db, 6082)
    assert u.ref_balance == pytest.approx(51) and u.expires_at > 0
    tx = db.get(Transaction, r.json()["order_id"])
    assert tx.method == "balance" and tx.gateway == "balance"
    # No share for the inviter from money that was a share already.
    assert _user(db, 6081).ref_balance == 0
    # Not enough left: refused, the order is cancelled, nothing is taken.
    r = client.post("/api/app/order", headers=tg(6082), json={"plan_id": plan["id"], "method": "balance"})
    assert r.status_code == 400 and "недостаточно" in r.json()["detail"]
    assert _user(db, 6082).ref_balance == pytest.approx(51)
    assert db.query(Transaction).filter(Transaction.user_id == u.id).order_by(Transaction.id.desc()) \
        .first().status == "cancelled"
    w = client.get("/api/app/referrals", headers=tg(6082)).json()["wallet"]
    assert w["spent"] == 199
    client.post("/api/admin/settings", headers=admin, json={"ref_balance_pay": "0"})
    _give(db, 6082, 1000)
    r = client.post("/api/app/order", headers=tg(6082), json={"plan_id": plan["id"], "method": "balance"})
    assert r.status_code == 400 and "выключена" in r.json()["detail"]
    assert _user(db, 6082).ref_balance == pytest.approx(1000)


# ---------------------------------------------------------------- settings
def test_referral_settings_are_checked(client, admin):
    for bad in ({"ref_mode": "bitcoin"}, {"ref_percent": "95"}, {"ref_percent": "-1"},
                {"ref_min_payout": "much"}, {"ref_days": "1000"}, {"ref_payout_methods": "{nope"}):
        assert client.post("/api/admin/settings", headers=admin, json=bad).status_code == 400, bad
    r = client.post("/api/admin/settings", headers=admin, json={"ref_payout_methods": (
        '[{"kind":"card","ru":"  Карта  "},{"kind":"magic","ru":"x"},{"kind":"other","ru":""},'
        '{"kind":"other","ru":"PayPal","hint_ru":"e-mail","id":"pp"}]')})
    assert r.status_code == 200
    methods = client.get("/api/admin/referrals", headers=admin).json()["methods"]
    assert [(m["id"], m["kind"], m["ru"], m["en"]) for m in methods] == [
        ("card1", "card", "Карта", "Карта"), ("pp", "other", "PayPal", "PayPal")]
    assert client.post("/api/admin/settings", headers=admin, json={"ref_percent": ""}).status_code == 200
