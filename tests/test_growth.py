"""
The support chat and the sales funnel.

Access is granted on the Remnawave stand-in (tests/fake_remnawave.py), so
these run without any real panel.
"""

from __future__ import annotations


import pytest

from conftest import Fake, tg
from test_remnawave import rw  # noqa: F401 — the fixture


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


def _pay(client, uid, order_id, amount, charge, **extra):
    return _bot(client, {"message": {"chat": {"id": uid, "type": "private"}, "from": {"id": uid},
                                     "successful_payment": {"currency": "XTR", "total_amount": amount,
                                                            "invoice_payload": f"tx:{order_id}",
                                                            "telegram_payment_charge_id": charge,
                                                            **extra}}})


# ---------------------------------------------------------------- support
def test_support_question_and_answer_through_the_bot(client, admin):
    uid = 7201
    _bot(client, {"message": {"message_id": 5, "text": "Не подключается <b>", "chat": {"id": uid, "type": "private"},
                              "from": {"id": uid, "first_name": "Ann", "username": "ann"}}})
    to_admin = [m for m in Fake.sent("sendMessage") if m["chat_id"] == 999][-1]
    assert "Не подключается &lt;b&gt;" in to_admin["text"] and "@ann" in to_admin["text"]
    assert to_admin["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"a:uc:{uid}"
    assert any("передано в поддержку" in m["text"] for m in Fake.sent("sendMessage") if m["chat_id"] == uid)
    admin_msg_id = Fake.message_id - 1          # the copy the admin got (the ack came after)

    threads = client.get("/api/admin/support", headers=admin).json()
    assert threads["waiting"] == 1 and threads["threads"][0]["tg_id"] == uid

    # The admin replies to the forwarded message in the bot.
    _bot(client, {"message": {"message_id": 77, "text": "Обновите подписку в приложении",
                              "chat": {"id": 999, "type": "private"}, "from": {"id": 999},
                              "reply_to_message": {"message_id": admin_msg_id}}})
    answer = [m for m in Fake.sent("sendMessage") if m["chat_id"] == uid][-1]
    assert "Обновите подписку" in answer["text"]
    assert [m for m in Fake.sent("sendMessage") if m["chat_id"] == 999][-1]["text"] == "Отправлено клиенту"
    assert client.get("/api/admin/support", headers=admin).json()["waiting"] == 0

    # The same thread from the app and from the panel.
    client.post("/api/app/support", json={"text": "Спасибо, заработало"}, headers=tg(uid))
    r = client.post(f"/api/admin/support/{uid}", headers=admin, json={"text": "Рады помочь"})
    msgs = r.json()["messages"]
    assert [m["from_admin"] for m in msgs] == [False, True, False, True]
    assert [m["source"] for m in msgs] == ["bot", "bot", "app", "panel"]
    mine = client.get("/api/app/support", headers=tg(uid)).json()
    assert mine["enabled"] and mine["messages"][-1]["text"] == "Рады помочь"


def test_support_off_and_flood_limit(client, admin):
    uid = 7202
    for i in range(22):
        _bot(client, {"message": {"message_id": i, "text": f"q{i}", "chat": {"id": uid, "type": "private"},
                                  "from": {"id": uid}}})
    got = [m for m in Fake.sent("sendMessage") if m["chat_id"] == 999 and "q" in m["text"]]
    assert len(got) == 20
    # Over the limit the customer hears "too many" — and nothing else on top.
    to_customer = [m["text"] for m in Fake.sent("sendMessage") if m["chat_id"] == uid]
    assert not any("Подписка, серверы" in x for x in to_customer)
    assert client.post("/api/app/support", json={"text": "x"}, headers=tg(uid)).status_code == 429
    client.post("/api/admin/settings", headers=admin, json={"support_enabled": "0"})
    before = len(Fake.sent("sendMessage"))
    _bot(client, {"message": {"message_id": 99, "text": "hello", "chat": {"id": 7203, "type": "private"},
                              "from": {"id": 7203}}})
    sent = Fake.sent("sendMessage")[before:]
    # Support is off: nothing reaches the admins, but the bot is not silent —
    # the customer gets the way into the app.
    assert [m["chat_id"] for m in sent] == [7203]
    assert sent[0]["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"].endswith("/app")
    assert client.get("/api/app/support", headers=tg(7203)).json()["enabled"] is False


def test_admin_notes_to_the_bot_are_not_questions(client):
    before = len(Fake.sent("sendMessage"))
    _bot(client, {"message": {"message_id": 1, "text": "just a note", "chat": {"id": 999, "type": "private"},
                              "from": {"id": 999}}})
    assert len(Fake.sent("sendMessage")) == before


# ---------------------------------------------------------------- funnel
def test_funnel_and_revenue_split(client, server, admin):
    cfg = client.get("/api/app/config").json()
    month = next(p for p in cfg["plans"] if p["days"] == 30)
    trial = next(p for p in cfg["plans"] if p["is_trial"])
    for uid in (7301, 7302, 7303, 7304):
        client.get("/api/app/me", headers=tg(uid))                 # opened the app
    client.post("/api/app/order", json={"plan_id": trial["id"]}, headers=tg(7301))
    me = client.get("/api/app/me", headers=tg(7301)).json()
    client.get(me["sub_url"].replace("https://shop.example.com", ""), headers={"User-Agent": "Happ/3"})
    for uid, charge in ((7301, "f1"), (7302, "f2"), (7302, "f3")):
        order = client.post("/api/app/order", json={"plan_id": month["id"], "method": "stars"},
                            headers=tg(uid)).json()
        _pay(client, uid, order["order_id"], 125, charge)
    a = client.get("/api/admin/analytics?days=30", headers=admin).json()
    f = {row["key"]: row["n"] for row in a["funnel"]}
    assert f == {"started": 4, "opened": 4, "trial": 1, "connected": 1, "paid": 2, "renewed": 1}
    assert a["conversion"]["to_paid"] == 50.0 and a["conversion"]["trial_to_paid"] == 100.0
    assert a["revenue"] == 597 and a["payers"] == 2 and a["new_payers"] == 2
    assert a["by_gateway"] == [{"name": "stars", "amount": 597, "count": 3}]
    assert a["by_plan"][0]["name"] == month["name"] and a["by_plan"][0]["count"] == 3


# ---------------------------------------------------------------- pre-launch review fixes
def _stars_order(client, uid, plan_id, **extra):
    r = client.post("/api/app/order", json={"plan_id": plan_id, "method": "stars", **extra}, headers=tg(uid))
    assert r.status_code == 200, r.text
    return r.json()


def test_trial_ignores_extra_devices(client, server, admin):
    client.put("/api/admin/trial", headers=admin, json={"enabled": True, "days": 3, "devices": 1})
    trial = next(p for p in client.get("/api/app/config").json()["plans"] if p["is_trial"])
    client.post("/api/app/order", json={"plan_id": trial["id"], "devices": 10}, headers=tg(7401))
    me = client.get("/api/app/me", headers=tg(7401)).json()
    assert me["active"] and me["device_limit"] == 1


def test_promo_bonus_days_once_even_with_several_open_orders(client, server, admin):
    client.post("/api/admin/promos", headers=admin, json={"code": "WEEK", "kind": "days", "value": 7})
    month = next(p for p in client.get("/api/app/config").json()["plans"] if p["days"] == 30)
    a = _stars_order(client, 7402, month["id"], promo="WEEK")
    b = _stars_order(client, 7402, month["id"], promo="WEEK")           # opened before paying a
    _pay(client, 7402, a["order_id"], 125, "pa")
    first = client.get("/api/app/me", headers=tg(7402)).json()["expires_at"]
    _pay(client, 7402, b["order_id"], 125, "pb")
    second = client.get("/api/app/me", headers=tg(7402)).json()["expires_at"]
    assert second - first == 30 * 86400                                # no second +7 days


def test_payment_on_a_cancelled_order_is_granted(client, server, admin):
    month = next(p for p in client.get("/api/app/config").json()["plans"] if p["days"] == 30)
    o = _stars_order(client, 7403, month["id"])
    assert client.post(f"/api/admin/orders/{o['order_id']}/cancel", headers=admin).status_code == 200
    _pay(client, 7403, o["order_id"], 125, "late")
    assert client.get("/api/app/me", headers=tg(7403)).json()["active"]


def test_webhook_refused_without_a_real_secret_key(client, env, monkeypatch):
    from app import notify
    monkeypatch.setenv("SECRET_KEY", "short")
    env.__init__()
    r = client.post("/api/bot/webhook", json={"message": {}},
                    headers={"X-Telegram-Bot-Api-Secret-Token": notify.webhook_secret()})
    assert r.status_code == 403


def test_background_sync_sends_the_latest_state(client, server, rw, db):  # noqa: F811
    """A purchase made while the slow sync loop runs must not be undone."""
    import httpx
    from app import access
    from app.db import SessionLocal
    from app.models import User
    month = next(p for p in client.get("/api/app/config").json()["plans"] if p["days"] == 30)
    _pay(client, 7404, _stars_order(client, 7404, month["id"])["order_id"], 125, "s1")
    stale = SessionLocal()
    old = stale.query(User).filter(User.tg_id == 7404).one()           # loaded before the renewal
    old.sync_error = "x"
    stale.commit()
    _pay(client, 7404, _stars_order(client, 7404, month["id"])["order_id"], 125, "s2")
    fresh = client.get("/api/app/me", headers=tg(7404)).json()["expires_at"]
    access.sync_users(stale, only_failed=False)
    stale.close()
    u = httpx.get(rw["url"] + "api/users/by-username/tg7404", trust_env=False,
                  headers={"Authorization": "Bearer " + rw["token"], "X-Forwarded-Proto": "https",
                           "X-Forwarded-For": "1.1.1.1"}).json()["response"]
    import datetime as dt
    exp = dt.datetime.fromisoformat(u["expireAt"].replace("Z", "+00:00")).timestamp()
    assert abs(exp - fresh) <= 2
