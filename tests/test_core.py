"""Tests that need no VPN panel: auth, pricing, orders, payments, admin."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from conftest import Fake, init_data, tg


@pytest.fixture(autouse=True)
def unreachable_server(env, monkeypatch):
    """No real panel here: one server that refuses connections. Orders can
    be created, but access can never be granted — exactly the case these
    tests are about."""
    monkeypatch.setenv("XUI_URL", "http://127.0.0.1:9/panel/")
    monkeypatch.setenv("XUI_USERNAME", "admin")
    monkeypatch.setenv("XUI_PASSWORD", "admin")
    monkeypatch.setenv("XUI_INBOUNDS", "1")
    env.__init__()


# ---------------------------------------------------------------- initData
def test_init_data_valid_tampered_and_expired(client):
    assert client.get("/api/app/me", headers=tg(1001)).status_code == 200
    bad = init_data(1001).replace("User1001", "User1002")
    assert client.get("/api/app/me", headers={"X-Init-Data": bad}).status_code == 401
    other_bot = init_data(1001, token="999:another-bot")
    assert client.get("/api/app/me", headers={"X-Init-Data": other_bot}).status_code == 401
    old = init_data(1001, auth_date=int(time.time()) - 3 * 86400)
    assert client.get("/api/app/me", headers={"X-Init-Data": old}).status_code == 401
    assert client.get("/api/app/me").status_code == 401
    odd = init_data(1001).rsplit("hash=", 1)[0] + "hash=%C3%A9"    # "é" as the hash
    assert client.get("/api/app/me", headers={"X-Init-Data": odd}).status_code == 401


def test_new_user_gets_language_from_telegram(client):
    assert client.get("/api/app/me", headers=tg(7, lang="en")).json()["lang"] == "en"
    assert client.get("/api/app/me", headers=tg(8, lang="ru")).json()["lang"] == "ru"
    assert client.post("/api/app/lang", json={"lang": "en"}, headers=tg(8)).json()["lang"] == "en"
    assert client.get("/api/app/me", headers=tg(8)).json()["lang"] == "en"


# ---------------------------------------------------------------- config
def test_config_lists_only_configured_payment_methods(client, env, monkeypatch):
    methods = [m["id"] for m in client.get("/api/app/config").json()["methods"]]
    assert methods == ["stars"]                 # only the bot token is set
    monkeypatch.setenv("CRYPTOBOT_TOKEN", "1:abc")
    monkeypatch.setenv("CARD_GATEWAY", "yookassa")
    monkeypatch.setenv("YOOKASSA_SHOP_ID", "1")
    monkeypatch.setenv("YOOKASSA_SECRET_KEY", "s")
    env.__init__()
    methods = [m["id"] for m in client.get("/api/app/config").json()["methods"]]
    assert methods == ["stars", "card", "crypto"]
    monkeypatch.setenv("YOOKASSA_SECRET_KEY", "")
    env.__init__()
    methods = [m["id"] for m in client.get("/api/app/config").json()["methods"]]
    assert "card" not in methods, "a gateway without keys must not be offered"


# ---------------------------------------------------------------- pricing
def test_quote_with_extra_devices_and_promo(client, admin):
    client.post("/api/admin/promos", json={"code": "half", "kind": "percent", "value": 50},
                headers=admin)
    plan = client.get("/api/app/config").json()["plans"][0]      # 199, 3 devices
    q = client.post("/api/app/quote", json={"plan_id": plan["id"], "devices": 5},
                    headers=tg(1)).json()
    assert q["amount"] == 199 + 2 * 50 and q["extra_devices"] == 2
    q = client.post("/api/app/quote", json={"plan_id": plan["id"], "devices": 5, "promo": "HALF"},
                    headers=tg(1)).json()
    assert q["amount"] == 149                  # (199 + 100) / 2 = 149.5 → whole roubles, never up
    r = client.post("/api/app/quote", json={"plan_id": plan["id"], "promo": "NOPE"}, headers=tg(1))
    assert r.status_code == 400 and r.json()["detail"] == "Промокод не найден"
    r = client.post("/api/app/quote", json={"plan_id": plan["id"], "promo": "NOPE"},
                    headers=tg(2, lang="en"))
    assert r.json()["detail"] == "Promo code not found"


def test_devices_are_capped(client):
    plan = client.get("/api/app/config").json()["plans"][0]
    q = client.post("/api/app/quote", json={"plan_id": plan["id"], "devices": 500},
                    headers=tg(1)).json()
    assert q["devices"] == 10


# ---------------------------------------------------------------- orders without servers
def test_paid_order_without_servers_is_not_marked_success(client, env, monkeypatch, db):
    """Money must never be marked as done without access behind it."""
    monkeypatch.setenv("PAYMENTS_TEST_MODE", "1")
    env.__init__()
    plan = client.get("/api/app/config").json()["plans"][0]
    r = client.post("/api/app/order", json={"plan_id": plan["id"], "method": "stars"},
                    headers=tg(55))
    assert r.status_code == 503
    from app.models import Transaction, User
    tx = db.query(Transaction).one()
    user = db.query(User).filter(User.tg_id == 55).one()
    assert tx.status == "pending" and user.expires_at == 0


def test_unconfigured_method_is_refused(client):
    plan = client.get("/api/app/config").json()["plans"][0]
    r = client.post("/api/app/order", json={"plan_id": plan["id"], "method": "card"},
                    headers=tg(56, lang="en"))
    assert r.status_code == 502
    assert r.json()["detail"] == "This payment method is not available right now"


def test_stars_invoice_is_created_with_our_rate(client, db):
    plan = client.get("/api/app/config").json()["plans"][0]            # 199 ₽, rate 1.6
    r = client.post("/api/app/order", json={"plan_id": plan["id"], "method": "stars"},
                    headers=tg(57))
    assert r.status_code == 200, r.text
    pay = r.json()["pay"]
    assert pay["type"] == "invoice" and pay["stars"] == 125          # ceil(199 / 1.6)
    sent = Fake.sent("createInvoiceLink")[-1]
    assert sent["currency"] == "XTR" and sent["prices"][0]["amount"] == 125


# ---------------------------------------------------------------- Stars webhook
def _bot(client, update, secret=None):
    from app import notify
    return client.post("/api/bot/webhook", json=update,
                       headers={"X-Telegram-Bot-Api-Secret-Token": secret or notify.webhook_secret()})


def test_bot_webhook_rejects_wrong_secret(client):
    assert _bot(client, {"message": {"text": "/start"}}, secret="nope").status_code == 403


def test_bot_start_replies_with_open_button(client):
    r = _bot(client, {"message": {"text": "/start", "chat": {"id": 42, "type": "private"},
                                  "from": {"id": 42, "language_code": "en"}}})
    assert r.status_code == 200
    msg = Fake.sent("sendMessage")[-1]
    assert msg["chat_id"] == 42
    button = msg["reply_markup"]["inline_keyboard"][0][0]
    assert button["text"] == "Open" and button["web_app"]["url"] == "https://shop.example.com/app"


def test_bot_start_with_gift_passes_the_code_to_the_app(client):
    _bot(client, {"message": {"text": "/start giftABCD2345", "chat": {"id": 43, "type": "private"},
                              "from": {"id": 43, "language_code": "ru"}}})
    button = Fake.sent("sendMessage")[-1]["reply_markup"]["inline_keyboard"][0][0]
    assert button["web_app"]["url"].endswith("/app?gift=ABCD2345")


def test_stars_pre_checkout_checks_the_amount(client):
    plan = client.get("/api/app/config").json()["plans"][0]
    order = client.post("/api/app/order", json={"plan_id": plan["id"], "method": "stars"},
                        headers=tg(58)).json()
    q = {"id": "q1", "invoice_payload": f"tx:{order['order_id']}", "currency": "XTR",
         "total_amount": 1}
    _bot(client, {"pre_checkout_query": q})
    ans = Fake.sent("answerPreCheckoutQuery")[-1]
    assert ans["ok"] is False
    q["total_amount"] = 125
    _bot(client, {"pre_checkout_query": q})
    assert Fake.sent("answerPreCheckoutQuery")[-1]["ok"] is True


def test_stars_payment_without_servers_is_parked_as_paid(client, db):
    """Telegram never repeats successful_payment. If no server answers at
    that moment, the order is kept as "paid" for the retry job, and the
    admins are told."""
    plan = client.get("/api/app/config").json()["plans"][0]
    order = client.post("/api/app/order", json={"plan_id": plan["id"], "method": "stars"},
                        headers=tg(59)).json()
    _bot(client, {"message": {"chat": {"id": 59, "type": "private"}, "from": {"id": 59},
                              "successful_payment": {"currency": "XTR", "total_amount": 125,
                                                     "invoice_payload": f"tx:{order['order_id']}",
                                                     "telegram_payment_charge_id": "ch_1"}}})
    from app.models import Transaction
    tx = db.get(Transaction, order["order_id"])
    db.refresh(tx)
    assert tx.status == "paid" and tx.external_id == "ch_1"
    assert any("no server accepted" in m["text"] for m in Fake.sent("sendMessage")
               if m["chat_id"] == 999)


# ---------------------------------------------------------------- gateway webhooks
def _crypto_sig(raw: bytes, token: str) -> str:
    return hmac.new(hashlib.sha256(token.encode()).digest(), raw, hashlib.sha256).hexdigest()


def test_cryptobot_webhook_requires_signature(client, env, monkeypatch):
    monkeypatch.setenv("CRYPTOBOT_TOKEN", "1:crypto")
    env.__init__()
    raw = json.dumps({"update_type": "invoice_paid", "payload": {"status": "paid"}}).encode()
    r = client.post("/api/pay/cryptobot", content=raw,
                    headers={"crypto-pay-api-signature": "00" * 32})
    assert r.status_code == 403
    r = client.post("/api/pay/cryptobot", content=raw,
                    headers={"crypto-pay-api-signature": _crypto_sig(raw, "1:crypto")})
    assert r.status_code == 200


def test_freekassa_signature_and_amount(client, env, monkeypatch, db):
    monkeypatch.setenv("CARD_GATEWAY", "freekassa")
    monkeypatch.setenv("FREEKASSA_MERCHANT_ID", "7012")
    monkeypatch.setenv("FREEKASSA_SECRET1", "s1")
    monkeypatch.setenv("FREEKASSA_SECRET2", "s2")
    monkeypatch.setenv("FREEKASSA_IPS", "")
    env.__init__()
    plan = client.get("/api/app/config").json()["plans"][0]
    order = client.post("/api/app/order", json={"plan_id": plan["id"], "method": "card"},
                        headers=tg(60)).json()
    url = order["pay"]["url"]
    assert url.startswith("https://pay.fk.money/?")
    expected = hashlib.md5(f"7012:199.00:s1:RUB:{order['order_id']}".encode()).hexdigest()
    assert f"s={expected}" in url

    def sign(amount):
        return hashlib.md5(f"7012:{amount}:s2:{order['order_id']}".encode()).hexdigest()

    form = {"MERCHANT_ID": "7012", "AMOUNT": "199.00", "MERCHANT_ORDER_ID": str(order["order_id"]),
            "SIGN": "bad", "intid": "5"}
    assert client.post("/api/pay/freekassa", data=form).status_code == 400
    form["SIGN"] = "подпись"                     # not ASCII: refused, not a crash
    assert client.post("/api/pay/freekassa", data=form).status_code == 400
    form.update(AMOUNT="1.00", SIGN=sign("1.00"))
    assert client.post("/api/pay/freekassa", data=form).status_code == 400    # underpaid
    form.update(AMOUNT="199.00", SIGN=sign("199.00"))
    # Signature is right, but there is no server to give access on: the
    # gateway gets an error and will retry.
    assert client.post("/api/pay/freekassa", data=form).status_code == 503
    from app.models import Transaction
    assert db.get(Transaction, order["order_id"]).status == "paid"


def test_yookassa_decides_on_its_own_api(client, env, monkeypatch, db):
    monkeypatch.setenv("CARD_GATEWAY", "yookassa")
    monkeypatch.setenv("YOOKASSA_SHOP_ID", "1")
    monkeypatch.setenv("YOOKASSA_SECRET_KEY", "k")
    env.__init__()
    plan = client.get("/api/app/config").json()["plans"][0]
    order = client.post("/api/app/order", json={"plan_id": plan["id"], "method": "card"},
                        headers=tg(61)).json()
    pid = order["pay"]["url"].rsplit("/", 1)[1]
    hook = {"event": "payment.succeeded", "object": {"id": pid}}
    # From a foreign address — rejected before anything else.
    assert client.post("/api/pay/yookassa", json=hook).status_code == 403
    ok_ip = {"X-Forwarded-For": "185.71.76.1"}
    # From the right network, but YooKassa itself says "pending" — nothing happens.
    assert client.post("/api/pay/yookassa", json=hook, headers=ok_ip).status_code == 200
    from app.models import Transaction
    assert db.get(Transaction, order["order_id"]).status == "pending"


# ---------------------------------------------------------------- admin
def test_admin_login_locks_after_many_failures(client):
    for _ in range(8):
        assert client.post("/api/admin/login",
                           json={"login": "admin", "password": "x"}).status_code == 401
    r = client.post("/api/admin/login", json={"login": "admin", "password": "correct-horse-battery"})
    assert r.status_code == 429
    from app import auth
    auth._attempts.clear()


def test_admin_api_requires_token(client, admin):
    assert client.get("/api/admin/overview").status_code == 401
    assert client.get("/api/admin/overview",
                      headers={"Authorization": "Bearer forged"}).status_code == 401
    r = client.get("/api/admin/overview", headers=admin)
    assert r.status_code == 200
    assert isinstance(r.json()["problems"], list)


def test_nothing_is_sold_without_servers(client, admin, db):
    from app.models import Server
    db.query(Server).delete()
    db.commit()
    probs = client.get("/api/admin/overview", headers=admin).json()["problems"]
    assert any(p["code"] == "no_servers" for p in probs)
    plan = client.get("/api/app/config").json()["plans"][0]
    r = client.post("/api/app/order", json={"plan_id": plan["id"], "method": "stars"},
                    headers=tg(57))
    assert r.status_code == 400 and "недоступны" in r.json()["detail"]


def test_admin_server_passwords_are_never_returned(client, admin):
    r = client.post("/api/admin/servers", headers=admin, json={
        "name": "X", "url": "https://panel.example.com:2053/secret/panel/inbounds",
        "username": "u", "password": "top-secret", "api_token": "Zq9-token-value", "inbound_ids": "1, 2"})
    assert r.status_code == 200
    body = r.json()
    assert body["url"] == "https://panel.example.com:2053/secret/"
    assert body["inbound_ids"] == "1,2"
    assert "top-secret" not in json.dumps(body) and "Zq9-token-value" not in json.dumps(body)
    assert body["has_password"] and body["has_token"]
    # Editing without a password keeps the stored one.
    r = client.put(f"/api/admin/servers/{body['id']}", headers=admin, json={
        "name": "X", "url": body["url"], "username": "u", "inbound_ids": "1"})
    assert r.json()["has_password"]


def test_settings_reject_unknown_keys_and_bad_json(client, admin):
    r = client.post("/api/admin/settings", headers=admin,
                    json={"brand_name": "Moon VPN", "evil": "1"})
    assert r.json()["changed"] == ["brand_name"]
    assert client.get("/api/app/config").json()["brand"] == "Moon VPN"
    r = client.post("/api/admin/settings", headers=admin, json={"faq_json": "{oops"})
    assert r.status_code == 400


def test_public_page_and_verification_file(client, env):
    assert client.get("/").status_code == 200
    (env.data_dir / "verify").mkdir(parents=True, exist_ok=True)
    (env.data_dir / "verify" / "shop-verification-X1.txt").write_text("shop-verification-X1\n")
    r = client.get("/shop-verification-X1.txt")
    assert r.status_code == 200 and r.text == "shop-verification-X1"
    assert client.get("/..%2Fsecret.txt").status_code == 404
    assert client.get("/api/public").json()["plans"]


def test_old_database_gets_new_columns(tmp_path):
    """An install made with an older version keeps working after an upgrade."""
    import sqlite3
    from sqlalchemy import create_engine, inspect
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("create table gifts (id integer primary key, code varchar(16))")
    con.execute("insert into gifts(code) values ('ABCD-EFGH')")
    con.commit()
    con.close()
    from app import migrate
    engine = create_engine(f"sqlite:///{path}")
    added = migrate.run(engine)
    assert "gifts.tx_id" in added and "gifts.amount" in added
    cols = {c["name"] for c in inspect(engine).get_columns("gifts")}
    assert {"tx_id", "used_at", "amount"} <= cols
    assert migrate.run(engine) == []          # idempotent
    row = sqlite3.connect(path).execute("select code, amount from gifts").fetchone()
    assert row == ("ABCD-EFGH", 0)


def test_platega_callback_needs_merchant_credentials(client, env, monkeypatch, db):
    monkeypatch.setenv("CARD_GATEWAY", "platega")
    monkeypatch.setenv("PLATEGA_MERCHANT_ID", "m-1")
    monkeypatch.setenv("PLATEGA_SECRET", "s-1")
    env.__init__()
    plan = client.get("/api/app/config").json()["plans"][0]
    order = client.post("/api/app/order", json={"plan_id": plan["id"], "method": "card"},
                        headers=tg(62)).json()
    assert order["pay"]["url"] == "https://platega/pay/pl-1"
    body = {"id": "pl-1", "status": "CONFIRMED", "amount": 199}
    assert client.post("/api/pay/platega", json=body).status_code == 403
    assert client.post("/api/pay/platega", json=body,
                       headers={"X-MerchantId": "m-1", "X-Secret": "wrong"}).status_code == 403
    r = client.post("/api/pay/platega", json={**body, "amount": 10},
                    headers={"X-MerchantId": "m-1", "X-Secret": "s-1"})
    from app.models import Transaction
    assert r.status_code == 200 and db.get(Transaction, order["order_id"]).status == "pending"
    r = client.post("/api/pay/platega", json=body, headers={"X-MerchantId": "m-1", "X-Secret": "s-1"})
    assert r.status_code == 503                       # genuine, but no server to grant on
    tx = db.get(Transaction, order["order_id"])
    db.refresh(tx)
    assert tx.status == "paid"


def test_paypalych_trusts_only_its_own_api(client, env, monkeypatch, db, fake_server):
    monkeypatch.setenv("CARD_GATEWAY", "paypalych")
    monkeypatch.setenv("PAYPALYCH_TOKEN", "tok")
    monkeypatch.setenv("PAYPALYCH_SHOP_ID", "shop")
    monkeypatch.setenv("PAYPALYCH_API_BASE", fake_server + "/paypalych")
    env.__init__()
    plan = client.get("/api/app/config").json()["plans"][0]
    order = client.post("/api/app/order", json={"plan_id": plan["id"], "method": "card"},
                        headers=tg(63)).json()
    bill = f"pp-{order['order_id']}"
    # A forged postback: PayPalych itself still says NEW — nothing happens.
    r = client.post("/api/pay/paypalych", data={"InvId": str(order["order_id"]), "Status": "SUCCESS",
                                                "OutSum": "199"})
    from app.models import Transaction
    assert r.status_code == 200 and db.get(Transaction, order["order_id"]).status == "pending"
    Fake.paypalych[bill] = {"status": "SUCCESS", "amount": "199.00"}
    r = client.post("/api/pay/paypalych", data={"InvId": str(order["order_id"])})
    assert r.status_code == 503                        # confirmed by the API, no servers yet
    tx = db.get(Transaction, order["order_id"])
    db.refresh(tx)
    assert tx.status == "paid"


def test_blocked_bot_is_remembered(client, db):
    from app import notify
    from app.models import User
    client.get("/api/app/me", headers=tg(403))
    user = db.query(User).filter(User.tg_id == 403).one()
    assert notify.send_safe(db, user, "hi") is False
    db.refresh(user)
    assert user.bot_blocked
    client.get("/api/app/me", headers=tg(403))      # opening the app again un-blocks
    db.refresh(user)
    assert not user.bot_blocked


# ---------------------------------------------------------------- lost webhooks
def _card(env, monkeypatch, gateway, **keys):
    monkeypatch.setenv("CARD_GATEWAY", gateway)
    for k, v in keys.items():
        monkeypatch.setenv(k, v)
    env.__init__()


def test_lost_yookassa_webhook_is_caught_by_polling(client, env, monkeypatch, db):
    """The webhook never came (wrong URL in the dashboard). While the app
    polls the order, the gateway is asked directly — and the money is not
    lost. Without servers the order is parked as "paid"."""
    _card(env, monkeypatch, "yookassa", YOOKASSA_SHOP_ID="1", YOOKASSA_SECRET_KEY="k")
    from app import payments
    payments._last_poll.clear()
    plan = client.get("/api/app/config").json()["plans"][0]
    order = client.post("/api/app/order", json={"plan_id": plan["id"], "method": "card"},
                        headers=tg(71)).json()
    oid = order["order_id"]
    pid = order["pay"]["url"].rsplit("/", 1)[1]
    assert client.get(f"/api/app/order/{oid}", headers=tg(71)).json()["status"] == "pending"
    Fake.yookassa[pid].update(status="succeeded", paid=True)
    # Checked at most once in 10 s per order.
    assert client.get(f"/api/app/order/{oid}", headers=tg(71)).json()["status"] == "pending"
    payments._last_poll.clear()
    assert client.get(f"/api/app/order/{oid}", headers=tg(71)).json()["status"] == "paid"


def test_polling_refuses_underpaid_and_foreign_orders(client, env, monkeypatch, db):
    _card(env, monkeypatch, "yookassa", YOOKASSA_SHOP_ID="1", YOOKASSA_SECRET_KEY="k")
    from app import payments
    from app.models import Transaction
    payments._last_poll.clear()
    plan = client.get("/api/app/config").json()["plans"][0]
    order = client.post("/api/app/order", json={"plan_id": plan["id"], "method": "card"},
                        headers=tg(72)).json()
    pid = order["pay"]["url"].rsplit("/", 1)[1]
    Fake.yookassa[pid].update(status="succeeded", paid=True,
                              amount={"value": "1.00", "currency": "RUB"})
    client.get(f"/api/app/order/{order['order_id']}", headers=tg(72))
    assert db.get(Transaction, order["order_id"]).status == "pending"
    # Somebody else's order cannot be polled at all.
    assert client.get(f"/api/app/order/{order['order_id']}", headers=tg(73)).status_code == 404


def test_background_check_finds_paid_cryptobot_and_platega(client, env, monkeypatch, db):
    """Orders the person paid and closed the app on are found by the job."""
    monkeypatch.setenv("CRYPTOBOT_TOKEN", "1:crypto")
    _card(env, monkeypatch, "platega", PLATEGA_MERCHANT_ID="m", PLATEGA_SECRET="s")
    from app import jobs, payments
    from app.models import Transaction
    payments._last_poll.clear()
    plan = client.get("/api/app/config").json()["plans"][0]
    a = client.post("/api/app/order", json={"plan_id": plan["id"], "method": "crypto"},
                    headers=tg(74)).json()["order_id"]
    b = client.post("/api/app/order", json={"plan_id": plan["id"], "method": "card"},
                    headers=tg(75)).json()["order_id"]
    for oid in (a, b):                  # the job leaves orders younger than a minute alone
        db.get(Transaction, oid).created_at -= 120
    db.commit()
    Fake.cryptobot["777"] = {"invoice_id": 777, "status": "paid", "currency_type": "fiat",
                             "fiat": "RUB", "amount": str(plan["price"]), "payload": str(a)}
    Fake.platega["pl-1"] = {"id": "pl-1", "status": "CONFIRMED",
                            "paymentDetails": {"amount": plan["price"], "currency": "RUB"}}
    jobs.job_check_pending(db)
    db.expire_all()
    assert db.get(Transaction, a).status == "paid"
    assert db.get(Transaction, b).status == "paid"


def test_interrupted_orders_are_retried_after_restart(client, db):
    from app import billing
    from app.models import Transaction, User
    user = User(tg_id=76)
    db.add(user)
    db.commit()
    tx = Transaction(user_id=user.id, kind="purchase", amount=100, currency="RUB",
                     status="processing")
    db.add(tx)
    db.commit()
    assert billing.recover_interrupted(db) == 1
    db.refresh(tx)
    assert tx.status == "paid"


# ---------------------------------------------------------------- reviews, promos, orders
def test_reviews_only_from_customers_and_after_moderation(client, admin, db):
    body = {"stars": 5, "text": "Всё работает быстро"}
    r = client.post("/api/app/reviews", json=body, headers=tg(81))
    assert r.status_code == 400                           # never connected
    from app.models import User
    user = db.query(User).filter(User.tg_id == 81).one()
    user.expires_at = 1
    db.commit()
    assert client.post("/api/app/reviews", json=body, headers=tg(81)).json()["status"] == "pending"
    assert client.get("/api/app/reviews").json()["count"] == 0     # not before moderation
    rid = client.get("/api/admin/reviews", headers=admin).json()[0]["id"]
    assert client.post(f"/api/admin/reviews/{rid}/published", headers=admin).status_code == 200
    pub = client.get("/api/app/reviews").json()
    assert pub["count"] == 1 and pub["items"][0]["text"] == body["text"]
    for _ in range(2):
        client.post("/api/app/reviews", json=body, headers=tg(81))
    assert client.post("/api/app/reviews", json=body, headers=tg(81)).status_code == 429


def test_promo_code_cannot_be_renamed_into_a_duplicate(client, admin):
    a = client.post("/api/admin/promos", headers=admin, json={"code": "ONE", "value": 10}).json()
    client.post("/api/admin/promos", headers=admin, json={"code": "TWO", "value": 10})
    r = client.put(f"/api/admin/promos/{a['id']}", headers=admin,
                   json={"code": "two", "value": 10})
    assert r.status_code == 400


def test_admin_grant_refuses_an_order_in_progress(client, admin, db):
    from app.models import Transaction, User
    user = User(tg_id=82)
    db.add(user)
    db.commit()
    tx = Transaction(user_id=user.id, kind="purchase", amount=100, currency="RUB",
                     status="processing")
    db.add(tx)
    db.commit()
    assert client.post(f"/api/admin/orders/{tx.id}/grant", headers=admin).status_code == 409


def test_last_forwarded_address_is_the_trusted_one(client):
    from starlette.requests import Request
    from app.auth import client_ip
    req = Request({"type": "http", "headers": [(b"x-forwarded-for", b"185.71.76.1, 10.0.0.9")],
                   "client": ("127.0.0.1", 1)})
    assert client_ip(req) == "10.0.0.9"


def test_forwarded_address_is_believed_only_from_a_proxy():
    """Reached straight from the internet, a made-up X-Forwarded-For must not
    change who is counted by the login rate limit."""
    from starlette.requests import Request
    from app.auth import client_ip

    def ip(peer: str, fwd: str = "") -> str:
        headers = [(b"x-forwarded-for", fwd.encode())] if fwd else []
        return client_ip(Request({"type": "http", "headers": headers, "client": (peer, 1)}))

    assert ip("8.8.8.8", "1.2.3.4") == "8.8.8.8"          # internet peer: header ignored
    assert ip("172.18.0.3", "77.88.8.8") == "77.88.8.8"     # Caddy / cloudflared in Docker
    assert ip("::1", "77.88.8.8") == "77.88.8.8"
    assert ip("8.8.8.8") == "8.8.8.8"
    # uvicorn already took the client from a trusted proxy: what it set stands
    assert ip("77.88.8.8", "6.6.6.6, 77.88.8.8") == "77.88.8.8"


def test_message_with_broken_html_is_sent_as_plain_text(client):
    from app import notify
    assert notify.send(5, "panel said <oops> & failed") is True
    last = Fake.sent("sendMessage")[-1]
    assert "parse_mode" not in last and last["text"].startswith("panel said")


# ---------------------------------------------------------------- look
def _png(w=300, h=200, frames=1) -> bytes:
    import io

    from PIL import Image
    out = io.BytesIO()
    ims = [Image.new("RGBA", (w, h), (20 * i, 120, 200, 255)) for i in range(frames)]
    ims[0].save(out, "PNG", save_all=frames > 1, append_images=ims[1:])
    return out.getvalue()


PNG = _png()


def test_every_theme_keeps_text_readable():
    from app import theme
    for accent in ("#F2B441", "#FFFFFF", "#FFFF00", "#101010", "#3DD68C", "#0000FF"):
        for surface in theme.SURFACES:
            th = theme.resolve(accent, surface)
            assert theme.contrast(th["text_dark"], th["s_card"]) >= 4.5
            assert theme.contrast(th["text_light"], "#FFFFFF") >= 5.5
            assert theme.contrast(th["on_fill"], th["fill"]) >= 4.5
            assert theme.contrast(th["on_fill_light"], th["fill_light"]) >= 4.5
            assert theme.contrast(th["muted"], th["s_card2"]) >= 4.5


def test_theme_settings_reach_the_pages(client, admin):
    r = client.post("/api/admin/settings", headers=admin,
                    json={"theme_preset": "custom", "theme_accent": "#12AB34", "theme_surface": "midnight",
                          "theme_mode": "light", "brand_name": "Orbit"})
    assert r.status_code == 200
    page = client.get("/app").text
    assert "--sig:#12AB34" in page and "--ground:#060913" in page
    assert '"mode": "light"' in page and "<title>Orbit</title>" in page
    assert "--accent:#12AB34" in client.get("/admin").text
    assert "#12AB34" in client.get("/favicon.ico").text
    for bad in ({"theme_accent": "red"}, {"theme_surface": "pink"}, {"theme_preset": "nope"},
                {"theme_mode": "sepia"}):
        assert client.post("/api/admin/settings", headers=admin, json=bad).status_code == 400
    # A preset overrides the custom colours.
    client.post("/api/admin/settings", headers=admin, json={"theme_preset": "ocean"})
    assert "--sig:#4DA3FF" in client.get("/app").text


def test_logo_upload_checks_the_real_type(client, admin):
    def up(data, name="logo.png"):
        return client.post("/api/admin/brand/logo", headers=admin, files={"file": (name, data, "image/png")})
    assert up(b"GIF89a not really a png").status_code == 400
    assert up(b"\x89PNG\r\n\x1a\n broken").status_code == 400
    assert up(b"x" * (2 * 1024 * 1024 + 10)).status_code == 400
    assert up(_png(2100, 100)).status_code == 400                    # too big a picture
    assert "animated" in up(_png(frames=3)).json()["detail"]
    r = up(PNG)
    assert r.status_code == 200
    url = r.json()["logo"]
    assert url.startswith("/brand/logo-") and url.endswith(".png")
    f = client.get(url)
    assert f.status_code == 200 and "immutable" in f.headers["cache-control"]
    import io

    from PIL import Image
    assert Image.open(io.BytesIO(f.content)).size == (256, 256)     # fitted into a square
    assert client.get("/api/app/config").json()["logo"] == url
    assert url in client.get("/app").text and url in client.get("/").text
    assert client.get("/favicon.ico", follow_redirects=False).headers["location"] == url
    # SVG is served so that it cannot run scripts when opened directly.
    svg = up(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "x.svg").json()["logo"]
    assert "sandbox" in client.get(svg).headers["content-security-policy"]
    assert client.get(url).status_code == 404                      # the old file is gone
    assert client.delete("/api/admin/brand/logo", headers=admin).status_code == 200
    assert client.get("/api/app/config").json()["logo"] == ""
    assert client.get("/brand/../../etc/passwd").status_code == 404
    assert client.post("/api/admin/brand/logo", files={"file": ("a.png", PNG)}).status_code == 401


def test_admin_panel_may_be_framed_only_by_telegram(client):
    csp = client.get("/admin").headers["content-security-policy"]
    assert "frame-ancestors" in csp and "web.telegram.org" in csp


# ---------------------------------------------------------------- the shop from Telegram
def _admin_msg(client, text, uid=999, **extra):
    msg = {"message_id": 1, "chat": {"id": uid, "type": "private"},
           "from": {"id": uid, "language_code": "ru"}, "text": text, **extra}
    return _bot(client, {"message": msg})


def _press(client, data, uid=999):
    return _bot(client, {"callback_query": {"id": "cb1", "data": data, "from": {"id": uid, "language_code": "ru"},
                                            "message": {"message_id": 5, "chat": {"id": uid}}}})


def _last_text():
    sent = [b for m, b in Fake.calls if m in ("sendMessage", "editMessageText")]
    return sent[-1]["text"] if sent else ""


def test_admin_menu_only_for_admins(client):
    _admin_msg(client, "/admin")
    body = Fake.sent("sendMessage")[-1]
    assert "управление" in body["text"]
    buttons = [b["callback_data"] for row in body["reply_markup"]["inline_keyboard"] for b in row
               if "callback_data" in b]
    assert "a:stats" in buttons and "a:srv" in buttons
    panel = [b for row in body["reply_markup"]["inline_keyboard"] for b in row if "web_app" in b]
    assert panel and panel[0]["web_app"]["url"] == "https://shop.example.com/admin"
    Fake.reset()
    _admin_msg(client, "/admin", uid=12345)                 # not an admin: treated as nobody's command
    assert not any("управление" in m["text"] for m in Fake.sent("sendMessage"))
    _press(client, "a:tr:off", uid=12345)                   # buttons from a stranger do nothing
    from app import billing
    from app.db import SessionLocal
    s = SessionLocal()
    assert billing.trial_info(s)["enabled"] is True
    s.close()


def test_bot_trial_promo_and_give(client, db):
    from app import billing
    from app.models import Promo, User
    _admin_msg(client, "/trial 7")
    assert billing.trial_info(db)["days"] == 7 and "7 дн." in _last_text()
    _press(client, "a:tr:off")
    db.expire_all()
    assert billing.trial_info(db)["enabled"] is False
    _admin_msg(client, "/promo SUMMER 20% 100")
    p = db.query(Promo).filter(Promo.code == "SUMMER").one()
    assert (p.kind, p.value, p.limit) == ("percent", 20, 100)
    _admin_msg(client, "/promo WEEK 7d")
    assert db.query(Promo).filter(Promo.code == "WEEK").one().kind == "days"
    _admin_msg(client, "/promo SUMMER 10%")
    assert "уже есть" in _last_text()
    _admin_msg(client, "/promo del WEEK")
    assert db.query(Promo).filter(Promo.code == "WEEK").first() is None
    # A customer: the card, then days added (the server is unreachable here,
    # so the note says so — the change itself is saved).
    client.get("/api/app/me", headers=tg(4242, username="ann"))
    _admin_msg(client, "/user @ann")
    assert "4242" in _last_text()
    _admin_msg(client, "/give 4242 30")
    db.expire_all()
    u = db.query(User).filter(User.tg_id == 4242).one()
    assert 29 * 86400 < u.expires_at - time.time() <= 30 * 86400
    assert "с ошибкой" in _last_text()
    _press(client, "a:ub:4242")
    db.expire_all()
    assert db.query(User).filter(User.tg_id == 4242).one().is_blocked is True


def test_bot_broadcast_needs_an_audience_pick(client, db):
    from app.models import Broadcast
    _admin_msg(client, "/broadcast Скидка 20% до пятницы")
    cast = db.query(Broadcast).one()
    assert cast.status == "draft"
    buttons = [b["callback_data"] for row in Fake.sent("sendMessage")[-1]["reply_markup"]["inline_keyboard"]
               for b in row]
    assert f"a:bc:{cast.id}:active" in buttons
    _press(client, f"a:bc:{cast.id}:active")
    db.refresh(cast)
    assert cast.status == "sending" and cast.audience == "active"
    _press(client, f"a:bc:{cast.id}:all")                    # a second press does not restart it
    db.refresh(cast)
    assert cast.audience == "active"


def test_bot_stats_servers_orders_screens(client):
    for data, word in (("a:stats", "Статистика"), ("a:srv", "Серверы"), ("a:ord", "Заказы"),
                       ("a:trial", "Пробный"), ("a:promo", "Промокоды"), ("a:menu", "управление")):
        _press(client, data)
        assert word in Fake.sent("editMessageText")[-1]["text"], data
    assert Fake.sent("answerCallbackQuery")


def test_panel_opens_from_telegram_for_admins_only(client):
    r = client.post("/api/admin/tg-login", headers={"X-Init-Data": init_data(999, username="boss")})
    assert r.status_code == 200 and r.json()["name"] == "@boss"
    token = r.json()["token"]
    assert client.get("/api/admin/overview", headers={"Authorization": "Bearer " + token}).status_code == 200
    assert client.post("/api/admin/tg-login", headers={"X-Init-Data": init_data(5)}).status_code == 403
    old = init_data(999, auth_date=int(time.time()) - 7200)
    assert client.post("/api/admin/tg-login", headers={"X-Init-Data": old}).status_code == 401
    assert client.get("/api/app/me", headers=tg(999)).json()["is_admin"] is True


# ---------------------------------------------------------------- sale, offers, warm-up
def test_sale_and_offer_do_not_stack_and_promo_competes(client, admin, db):
    from app import marketing
    from app.models import User
    plan = client.get("/api/app/config").json()["plans"][0]                 # 199
    def q(**kw):
        return client.post("/api/app/quote", json={"plan_id": plan["id"], **kw}, headers=tg(91)).json()
    assert q()["amount"] == 199
    r = client.put("/api/admin/sale", headers=admin, json={"percent": 20, "days": 2, "title_ru": "Осень"})
    assert r.json()["percent"] == 20 and r.json()["until"] > time.time()
    cfg = client.get("/api/app/config").json()
    assert cfg["sale"]["percent"] == 20 and cfg["sale"]["title"]["ru"] == "Осень"
    assert q()["amount"] == 159 and q()["discount"] == 20            # 159.2 → whole roubles
    user = db.query(User).filter(User.tg_id == 91).one()
    marketing.give_offer(db, user, 30, 3, "test")
    assert q()["amount"] == 139 and q()["discount_source"] == "offer"        # the bigger one, not both
    assert client.get("/api/app/me", headers=tg(91)).json()["offer"]["percent"] == 30
    client.post("/api/admin/promos", headers=admin, json={"code": "HALF", "kind": "percent", "value": 50})
    r = q(promo="HALF")
    assert r["amount"] == 99 and r["discount"] == 0 and r["promo"] == "HALF"     # the lower price wins
    client.post("/api/admin/promos", headers=admin, json={"code": "TEN", "kind": "percent", "value": 10})
    r = q(promo="TEN")
    assert r["amount"] == 139 and r["discount"] == 30                           # the offer is better
    # The sale ends by itself.
    client.put("/api/admin/sale", headers=admin, json={"percent": 0})
    assert client.get("/api/app/config").json()["sale"]["percent"] == 0
    assert client.put("/api/admin/sale", headers=admin, json={"percent": 95}).status_code == 422


def test_warm_up_notice_goes_once_and_gives_an_offer(client, admin, db):
    from app import jobs
    from app.models import Offer, User
    client.get("/api/app/me", headers=tg(92))
    u = db.query(User).filter(User.tg_id == 92).one()
    u.expires_at = int(time.time()) - 86400 - 600            # ended a day ago
    db.commit()
    r = client.put("/api/admin/warm/expired", headers=admin,
                   json={"on": True, "param": 1, "percent": 25, "valid": 2,
                         "ru": "{name}, возвращайтесь: −{percent}% до {until}"})
    assert r.json()["percent"] == 25
    Fake.calls.clear()
    jobs.job_warm(db)
    jobs.job_warm(db)
    msgs = [m for m in Fake.sent("sendMessage") if m["chat_id"] == 92]
    assert len(msgs) == 1 and msgs[0]["text"].startswith("User92, возвращайтесь: −25% до ")
    assert msgs[0]["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"].endswith("/app?go=sub")
    offer = db.query(Offer).filter(Offer.user_id == u.id).one()
    assert offer.percent == 25 and offer.source == "trigger:expired"
    # Old events are not caught up when a notice is switched on.
    client.get("/api/app/me", headers=tg(93))
    old = db.query(User).filter(User.tg_id == 93).one()
    old.expires_at = int(time.time()) - 60 * 86400
    db.commit()
    jobs.job_warm(db)
    assert not [m for m in Fake.sent("sendMessage") if m["chat_id"] == 93]
    w = client.get("/api/admin/warm", headers=admin).json()
    exp = next(t for t in w["triggers"] if t["key"] == "expired")
    assert exp["sent_7d"] == 1
    assert client.post("/api/admin/warm/expired/reset", headers=admin).json()["percent"] == 15
    assert client.put("/api/admin/warm/nope", headers=admin, json={}).status_code == 404
    assert client.post("/api/admin/warm/expiring/test", headers=admin).json()["chat"] == 999


def test_campaign_to_a_segment_with_a_discount(client, admin, db):
    from app import jobs
    from app.models import Campaign, Offer, User
    for i in (94, 95, 96):
        client.get("/api/app/me", headers=tg(i))
    u = db.query(User).filter(User.tg_id == 96).one()
    u.expires_at = int(time.time()) + 20 * 86400           # active: not in "never"
    db.commit()
    counts = client.get("/api/admin/warm", headers=admin).json()["segments"]
    assert counts["never"] == 2 and counts["active"] == 1
    r = client.post("/api/admin/campaigns", headers=admin, json={
        "name": "Первая покупка", "segment": "never", "text_ru": "Скидка {percent}% до {until}",
        "percent": 30, "valid_days": 2})
    assert r.status_code == 200 and r.json()["total"] == 2
    for _ in range(3):
        jobs.job_campaigns(db)
    c = db.query(Campaign).one()
    db.refresh(c)
    assert c.status == "done" and c.sent == 2
    got = {m["chat_id"] for m in Fake.sent("sendMessage")}
    assert {94, 95} <= got and 96 not in got
    assert db.query(Offer).filter(Offer.source == f"campaign:{c.id}").count() == 2
    assert client.post("/api/admin/campaigns", headers=admin, json={
        "name": "x", "segment": "martians", "text_ru": "x"}).status_code == 400
    # A scheduled one waits for its time and can be cancelled.
    later = client.post("/api/admin/campaigns", headers=admin, json={
        "name": "Позже", "segment": "all", "text_ru": "x", "start_at": int(time.time()) + 3600}).json()
    jobs.job_campaigns(db)
    assert client.get("/api/admin/campaigns", headers=admin).json()[0]["status"] == "scheduled"
    client.post(f"/api/admin/campaigns/{later['id']}/cancel", headers=admin)
    assert client.get("/api/admin/campaigns", headers=admin).json()[0]["status"] == "cancelled"


# ---------------------------------------------------------------- referrals
def test_referral_link_and_captcha(client, admin, db):
    from app.models import User
    client.get("/api/app/me", headers=tg(101))
    inviter = db.query(User).filter(User.tg_id == 101).one()
    link = client.get("/api/app/referrals", headers=tg(101)).json()["link"]
    assert link == f"https://t.me/shop_test_bot?start=r{inviter.id}"
    # Without the captcha: linked at once, through the bot's /start.
    _bot(client, {"message": {"chat": {"id": 102, "type": "private"}, "from": {"id": 102},
                              "text": f"/start r{inviter.id}"}})
    assert db.query(User).filter(User.tg_id == 102).one().referrer_id == inviter.id
    # Nobody can re-link an existing person or invite themselves.
    _bot(client, {"message": {"chat": {"id": 101, "type": "private"}, "from": {"id": 101},
                              "text": f"/start r{inviter.id}"}})
    db.expire_all()
    assert db.query(User).filter(User.tg_id == 101).one().referrer_id is None
    # With the captcha: pending until the right number is pressed.
    client.post("/api/admin/settings", headers=admin, json={"ref_captcha": "1"})
    Fake.calls.clear()
    _bot(client, {"message": {"chat": {"id": 103, "type": "private"}, "from": {"id": 103},
                              "text": f"/start r{inviter.id}"}})
    ask = Fake.sent("sendMessage")[-1]
    assert "нажмите на число" in ask["text"]
    u = db.query(User).filter(User.tg_id == 103).one()
    assert u.referrer_id is None and u.ref_pending == inviter.id
    wrong = next(b["callback_data"] for b in ask["reply_markup"]["inline_keyboard"][0]
                 if b["callback_data"] != f"cap:{u.captcha}")
    _bot(client, {"callback_query": {"id": "c", "data": wrong, "from": {"id": 103},
                                     "message": {"message_id": 1, "chat": {"id": 103}}}})
    db.expire_all()
    u = db.query(User).filter(User.tg_id == 103).one()
    assert u.referrer_id is None
    _bot(client, {"callback_query": {"id": "c", "data": f"cap:{u.captcha}", "from": {"id": 103},
                                     "message": {"message_id": 1, "chat": {"id": 103}}}})
    db.expire_all()
    assert db.query(User).filter(User.tg_id == 103).one().referrer_id == inviter.id
    assert client.get("/api/app/referrals", headers=tg(101)).json()["invited"] == 2
    # The mini app's own start parameter works too.
    client.get("/api/app/me", headers=tg(104, start=f"r{inviter.id}"))
    assert db.query(User).filter(User.tg_id == 104).one().ref_pending == inviter.id


def test_referral_top_masks_names_for_customers(client, admin, db):
    from app.models import RefReward, User
    for i, name in ((111, "alexander"), (112, "bo"), (113, "")):
        client.get("/api/app/me", headers=tg(i, username=name))
    users = {u.tg_id: u for u in db.query(User).all()}
    for n, (ref, days) in enumerate(((111, 14), (112, 7), (111, 7))):
        db.add(RefReward(referrer_id=users[ref].id, referee_id=users[113].id, tx_id=900 + n, days=days))
    db.commit()
    client.post("/api/admin/settings", headers=admin, json={"top_enabled": "1", "top_type": "days"})
    top = client.get("/api/app/referrals", headers=tg(113)).json()["top"]
    assert [(t["name"], t["value"]) for t in top] == [("ale***", 21), ("bo***", 7)]
    full = client.get("/api/admin/referrals", headers=admin).json()["top"]
    assert full[0]["username"] == "alexander"
    client.post("/api/admin/settings", headers=admin, json={"top_enabled": "0"})
    assert client.get("/api/app/referrals", headers=tg(113)).json()["top"] is None


# ---------------------------------------------------------------- channel
def test_channel_gate_for_everything_or_only_the_trial(client, admin, db):
    assert client.post("/api/admin/settings", headers=admin,
                       json={"channel_id": "bad name"}).status_code == 400
    client.post("/api/admin/settings", headers=admin, json={"channel_id": "@testchannel", "channel_mode": "all"})
    chk = client.post("/api/admin/channel/check", headers=admin).json()
    assert chk["ok"] and chk["title"] == "Test channel" and chk["link"] == "https://t.me/testchannel"
    me = client.get("/api/app/me", headers=tg(121)).json()
    assert me["gate"]["link"] == "https://t.me/testchannel"
    plan = client.get("/api/app/config").json()["plans"][0]
    r = client.post("/api/app/order", json={"plan_id": plan["id"]}, headers=tg(121))
    assert r.status_code == 403
    Fake.members.add(121)
    assert client.post("/api/app/channel/check", headers=tg(121)).json()["ok"] is True
    assert client.get("/api/app/me", headers=tg(121)).json()["gate"] is None
    # Only the trial is gated.
    client.post("/api/admin/settings", headers=admin, json={"channel_mode": "trial"})
    me = client.get("/api/app/me", headers=tg(122)).json()
    assert me["gate"] is None and me["trial_gate"]["title"] == "Test channel"
    trial = next(p for p in client.get("/api/app/config").json()["plans"] if p["is_trial"])
    assert client.post("/api/app/order", json={"plan_id": trial["id"]}, headers=tg(122)).status_code == 403
    Fake.bot_is_admin = False
    assert "not an administrator" in client.post("/api/admin/channel/check", headers=admin).json()["error"]


# ---------------------------------------------------------------- look extras
def test_fonts_radius_links_and_brand_reset(client, admin):
    r = client.post("/api/admin/settings", headers=admin, json={
        "font": "manrope", "radius": "round", "brand_name": "Orbit",
        "links_json": '[{"ru": "Наш канал", "en": "Channel", "url": "https://t.me/orbit"},'
                      ' {"ru": "Плохая", "url": "javascript:alert(1)"}]'})
    assert r.status_code == 200
    page = client.get("/app").text
    assert "family=Manrope" in page and '--font-d:"Manrope"' in page and "--r:24px" in page
    assert client.get("/api/app/config").json()["links"] == [
        {"ru": "Наш канал", "en": "Channel", "url": "https://t.me/orbit"}]
    assert client.post("/api/admin/settings", headers=admin, json={"font": "wingdings"}).status_code == 400
    client.post("/api/admin/brand/logo", headers=admin, files={"file": ("l.png", PNG, "image/png")})
    assert client.post("/api/admin/brand/reset", headers=admin).json()["brand"] == "VPN"
    cfg = client.get("/api/app/config").json()
    assert cfg["brand"] == "VPN" and cfg["logo"] == ""


def test_bot_sale_command(client, db):
    from app import marketing
    _admin_msg(client, "/sale 25 3")
    s = marketing.sale(db)
    assert s["percent"] == 25 and 2 * 86400 < s["until"] - time.time() <= 3 * 86400
    assert "−25%" in _last_text()
    _press(client, "a:sl:0")
    assert marketing.sale(db)["percent"] == 0


# ---------------------------------------------------------------- backups
def test_backup_is_made_sent_listed_and_restorable(client, admin, db, tmp_path, monkeypatch):
    import zipfile

    from app import backup
    client.get("/api/app/me", headers=tg(131, username="keep_me"))
    client.post("/api/admin/brand/logo", headers=admin, files={"file": ("l.png", PNG, "image/png")})
    Fake.calls.clear()
    r = client.post("/api/admin/backups", headers=admin).json()
    assert r["sent"] == 1
    doc = Fake.sent("sendDocument")[-1]
    assert doc["chat_id"] == 999 and doc["zip"]
    items = client.get("/api/admin/backups", headers=admin).json()["items"]
    assert items[0]["name"] == r["name"]
    got = client.get(f"/api/admin/backups/{r['name']}", headers=admin)
    assert got.status_code == 200 and got.content[:2] == b"PK"
    assert client.get(f"/api/admin/backups/{r['name']}").status_code == 401
    assert client.get("/api/admin/backups/..%2Fvpn.db", headers=admin).status_code == 404
    archive = tmp_path / r["name"]
    archive.write_bytes(got.content)
    with zipfile.ZipFile(archive) as z:
        names = set(z.namelist())
    assert {"db.json", "meta.json"} <= names and any(n.startswith("brand/logo-") for n in names)

    # Into an empty database of another install.
    from app import db as dbmod
    from app.config import settings
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{other}/fresh.db")
    monkeypatch.setattr(settings, "data_dir", other)
    try:
        counts = backup.restore(str(archive))
        assert counts["users"] >= 1
        from app.models import User
        s = dbmod.SessionLocal()
        assert s.query(User).filter(User.tg_id == 131).one().username == "keep_me"
        s.close()
        assert any((other / "brand").iterdir())
        with pytest.raises(SystemExit):
            backup.restore(str(archive))                 # a database with customers is not overwritten
        assert backup.restore(str(archive), force=True)["users"] == counts["users"]
    finally:
        monkeypatch.undo()
        dbmod.init_engine()


def test_daily_backup_once_at_its_hour(client, admin, db):
    import datetime as dt

    from app import backup
    hour = dt.datetime.now().hour
    client.post("/api/admin/settings", headers=admin, json={"backup_hour": str((hour + 1) % 24)})
    assert backup.run_daily(db) is False                    # not its hour
    client.post("/api/admin/settings", headers=admin, json={"backup_hour": str(hour)})
    assert backup.run_daily(db) is True
    assert backup.run_daily(db) is False                    # already done today
    assert client.post("/api/admin/settings", headers=admin, json={"backup_hour": "25"}).status_code == 400


def test_bot_backup_command_sends_to_that_admin(client):
    Fake.calls.clear()
    _admin_msg(client, "/backup")
    assert Fake.sent("sendDocument")[-1]["chat_id"] == 999


def test_shop_keeps_its_own_secret_when_env_has_none(monkeypatch, tmp_path):
    """Started by hand without SECRET_KEY: a random key is made once in data/
    and kept, not an empty (guessable) one."""
    import os
    from app import config
    monkeypatch.setenv("SECRET_KEY", "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    try:
        first = config.Settings().secret_key
        assert len(first) == 64 and config.Settings().secret_key == first
        assert oct(os.stat(tmp_path / "secret.key").st_mode & 0o777) == "0o600"
        assert not [p for p in config.Settings().problems() if p["code"] == "secret"]
        # a key in .env wins
        monkeypatch.setenv("SECRET_KEY", "k" * 40)
        assert config.Settings().secret_key == "k" * 40
    finally:
        monkeypatch.undo()
        config.reload()
