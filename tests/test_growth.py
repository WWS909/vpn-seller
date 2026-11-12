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
def _msg(uid, text, mid=1, **extra):
    return {"message": {"message_id": mid, "text": text, "chat": {"id": uid, "type": "private"},
                        "from": {"id": uid, "first_name": "Ann", "username": "ann"}, **extra}}


def _to(uid):
    return [m for m in Fake.sent("sendMessage") if m["chat_id"] == uid]


def _press(client, uid, data, mid=500):
    return _bot(client, {"callback_query": {"id": f"cb{mid}", "data": data, "from": {"id": uid},
                                            "message": {"message_id": mid, "chat": {"id": uid}}}})


def test_plain_message_to_the_bot_is_not_a_request(client, admin):
    uid = 7211
    for i in range(3):
        _bot(client, _msg(uid, f"привет {i}", mid=i))
    # nothing reached the admins and nothing was stored
    assert not [m for m in Fake.sent("sendMessage") if m["chat_id"] == 999 and "привет" in m["text"]]
    assert client.get("/api/admin/support", headers=admin).json()["waiting"] == 0
    assert client.get("/api/app/support", headers=tg(uid)).json()["messages"] == []
    # the person got one answer (not one per message), with the way to the app and to support
    got = _to(uid)
    assert len(got) == 1 and "не читаются" in got[0]["text"]
    rows = got[0]["reply_markup"]["inline_keyboard"]
    assert rows[0][0]["web_app"]["url"].endswith("/app")
    assert rows[1][0] == {"text": "Написать в поддержку", "callback_data": "sup:open"}


def test_support_question_and_answer_through_the_bot(client, admin):
    uid = 7201
    # the button opens the chat; the next message is the question
    _press(client, uid, "sup:open")
    assert "Напишите вопрос" in _to(uid)[-1]["text"]
    assert _to(uid)[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "sup:cancel"
    _bot(client, _msg(uid, "Не подключается <b>", mid=5))
    to_admin = [m for m in Fake.sent("sendMessage") if m["chat_id"] == 999][-1]
    assert "Не подключается &lt;b&gt;" in to_admin["text"] and "@ann" in to_admin["text"]
    assert to_admin["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"a:uc:{uid}"
    assert any("передано в поддержку" in m["text"] for m in _to(uid))
    admin_msg_id = Fake.message_id - 1          # the copy the admin got (the ack came after)

    threads = client.get("/api/admin/support", headers=admin).json()
    assert threads["waiting"] == 1 and threads["threads"][0]["tg_id"] == uid

    # The admin replies to the forwarded message in the bot.
    _bot(client, {"message": {"message_id": 77, "text": "Обновите подписку в приложении",
                              "chat": {"id": 999, "type": "private"}, "from": {"id": 999},
                              "reply_to_message": {"message_id": admin_msg_id}}})
    answer = _to(uid)[-1]
    assert "Обновите подписку" in answer["text"]
    assert [m for m in Fake.sent("sendMessage") if m["chat_id"] == 999][-1]["text"] == "Отправлено клиенту"
    assert client.get("/api/admin/support", headers=admin).json()["waiting"] == 0

    # After an answer the customer simply replies in the bot — no button needed.
    _bot(client, _msg(uid, "А на телефоне?", mid=6))
    assert [m for m in Fake.sent("sendMessage") if m["chat_id"] == 999 and "А на телефоне?" in m["text"]]

    # The same thread from the app and from the panel.
    client.post("/api/app/support", json={"text": "Спасибо, заработало"}, headers=tg(uid))
    r = client.post(f"/api/admin/support/{uid}", headers=admin, json={"text": "Рады помочь"})
    msgs = r.json()["messages"]
    assert [m["from_admin"] for m in msgs] == [False, True, False, False, True]
    assert [m["source"] for m in msgs] == ["bot", "bot", "bot", "app", "panel"]
    mine = client.get("/api/app/support", headers=tg(uid)).json()
    assert mine["enabled"] and mine["messages"][-1]["text"] == "Рады помочь"


def test_support_chat_closes_by_itself_and_on_cancel(client, admin, db):
    from app import support
    from app.models import User
    uid = 7212
    _bot(client, _msg(uid, "/support"))
    assert "Напишите вопрос" in _to(uid)[-1]["text"]
    # changed their mind
    _press(client, uid, "sup:cancel", mid=501)
    edits = Fake.sent("editMessageText")
    assert edits and "не создано" in edits[-1]["text"]
    _bot(client, _msg(uid, "ой", mid=2))
    assert not [m for m in Fake.sent("sendMessage") if m["chat_id"] == 999 and "ой" in m["text"]]
    # opened, but the time ran out: a later message is plain again
    _press(client, uid, "sup:open", mid=502)
    u = db.query(User).filter(User.tg_id == uid).one()
    u.support_open_until = int(__import__("time").time()) - 1
    db.commit()
    _bot(client, _msg(uid, "через час", mid=3))
    assert not [m for m in Fake.sent("sendMessage") if m["chat_id"] == 999 and "через час" in m["text"]]
    # closed from the panel: the chat closes too
    _press(client, uid, "sup:open", mid=503)
    _bot(client, _msg(uid, "вопрос", mid=4))
    db.expire_all()
    assert support.is_open(db.query(User).filter(User.tg_id == uid).one())
    client.post(f"/api/admin/support/{uid}/close", headers=admin)
    db.expire_all()
    assert not support.is_open(db.query(User).filter(User.tg_id == uid).one())


def test_support_off_and_flood_limit(client, admin):
    uid = 7202
    _press(client, uid, "sup:open")
    for i in range(22):
        _bot(client, _msg(uid, f"q{i}", mid=i))
    got = [m for m in Fake.sent("sendMessage") if m["chat_id"] == 999 and "q" in m["text"]]
    assert len(got) == 20
    # Over the limit the customer hears "too many" — and nothing else on top.
    to_customer = [m["text"] for m in _to(uid)]
    assert not any("не читаются" in x for x in to_customer)
    assert client.post("/api/app/support", json={"text": "x"}, headers=tg(uid)).status_code == 429
    client.post("/api/admin/settings", headers=admin, json={"support_enabled": "0"})
    before = len(Fake.sent("sendMessage"))
    _bot(client, _msg(7203, "hello", mid=99))
    sent = Fake.sent("sendMessage")[before:]
    # Support is off: nothing reaches the admins, but the bot is not silent —
    # the customer gets the way into the app, and no support button.
    assert [m["chat_id"] for m in sent] == [7203]
    rows = sent[0]["reply_markup"]["inline_keyboard"]
    assert rows[0][0]["web_app"]["url"].endswith("/app") and len(rows) == 1
    # /support while it is off: the same way into the app
    _bot(client, _msg(7204, "/support"))
    assert "не читаются" in _to(7204)[-1]["text"]
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
    # a short SECRET_KEY is not used: the shop keeps its own random one in data/
    monkeypatch.setenv("SECRET_KEY", "short")
    env.__init__()
    assert len(env.secret_key) == 64
    env.secret_key = "short"
    weak = notify.webhook_secret()
    env.__init__()
    hook = lambda secret: client.post("/api/bot/webhook", json={"message": {}},  # noqa: E731
                                      headers={"X-Telegram-Bot-Api-Secret-Token": secret})
    assert hook(weak).status_code == 403                 # what the short key would give is refused
    assert hook(notify.webhook_secret()).status_code == 200
    # and with no usable key at all (data/ not writable), the webhook refuses everything
    env.secret_key = "short"
    assert hook(notify.webhook_secret()).status_code == 403


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
