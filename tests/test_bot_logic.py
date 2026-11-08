"""The bot's own logic: repeated updates, broken updates, the trial race,
unmatched Stars payments, the referral captcha, a bot that never stays silent."""

from __future__ import annotations

import pytest

from conftest import Fake, tg
from test_remnawave import rw  # noqa: F401 — the fixture


def _bot(client, update):
    from app import notify
    return client.post("/api/bot/webhook", json=update,
                       headers={"X-Telegram-Bot-Api-Secret-Token": notify.webhook_secret()})


def _press(client, update_id, data, admin_id=999, cq_id="cq1"):
    return _bot(client, {"update_id": update_id, "callback_query": {
        "id": cq_id, "from": {"id": admin_id, "language_code": "ru"}, "data": data,
        "message": {"message_id": 10, "chat": {"id": admin_id, "type": "private"}}}})


@pytest.fixture()
def server(client, admin, rw):  # noqa: F811
    r = client.post("/api/admin/servers", headers=admin, json={
        "name": "Finland", "country": "FI", "kind": "remnawave", "url": rw["url"],
        "api_token": rw["token"], "inbound_ids": rw["squads"]})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- repeats
def test_repeated_update_is_done_once(client, db):
    """Telegram sends an update again when our answer was slow: "+30 days"
    pressed once must add 30 days once."""
    from app.models import User
    client.get("/api/app/me", headers=tg(4401))
    _press(client, 5001, "a:ux:4401:30")
    first = db.query(User).filter(User.tg_id == 4401).first().expires_at
    assert first > 0
    _press(client, 5001, "a:ux:4401:30")                 # the same update again
    db.expire_all()
    assert db.query(User).filter(User.tg_id == 4401).first().expires_at == first
    _press(client, 5002, "a:ux:4401:30")                 # a new press does work
    db.expire_all()
    assert db.query(User).filter(User.tg_id == 4401).first().expires_at >= first + 30 * 86400 - 5


def test_broken_update_is_not_repeated_forever(client, monkeypatch):
    """An update that crashes is answered 200 (Telegram would otherwise
    resend it again and again), logged, and the admin's button says why."""
    from app import botadmin

    def boom(*a, **k):
        raise RuntimeError("panel exploded")
    monkeypatch.setattr(botadmin, "handle_callback", boom)
    r = _press(client, 6001, "a:menu", cq_id="cq-boom")
    assert r.status_code == 200
    answer = [b for b in Fake.sent("answerCallbackQuery") if b.get("callback_query_id") == "cq-boom"]
    assert answer and "panel exploded" in answer[-1]["text"]
    logs = client.get("/api/admin/logs?limit=20", headers={"Authorization": "Bearer " + client.post(
        "/api/admin/login", json={"login": "admin", "password": "correct-horse-battery"}).json()["token"]}).json()
    assert any(x["action"] == "Bot update failed" for x in logs)


def test_failed_payment_update_is_left_to_telegram_to_repeat(client, monkeypatch):
    """A payment is the exception: Telegram must repeat it (closing an
    order is safe to repeat, losing money is not)."""
    from app.payments import stars
    calls = []

    def flaky(db, payment):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return True
    monkeypatch.setattr(stars, "paid", flaky)
    upd = {"update_id": 7001, "message": {"chat": {"id": 1, "type": "private"}, "from": {"id": 1},
                                          "successful_payment": {"currency": "XTR", "total_amount": 1,
                                                                 "invoice_payload": "tx:1"}}}
    assert _bot(client, upd).status_code == 500
    assert _bot(client, upd).status_code == 200          # the repeat is processed, not skipped
    assert len(calls) == 2


# ---------------------------------------------------------------- the trial
def test_double_tap_on_the_trial_gives_it_once(client, db, server):
    """Two trial orders opened before either is granted (a double tap):
    only one trial is given, the other order is cancelled."""
    from app import billing
    from app.models import Plan, Transaction, User
    client.get("/api/app/me", headers=tg(4501))
    user = db.query(User).filter(User.tg_id == 4501).first()
    trial = db.query(Plan).filter(Plan.is_trial.is_(True)).first()
    trial.enabled = True
    db.commit()
    tx1 = billing.new_order(db, user, "purchase", "stars", plan=trial)
    tx2 = billing.new_order(db, user, "purchase", "stars", plan=trial)   # passed the check too
    assert billing.finish(db, tx1, confirmed=False) is True
    with pytest.raises(billing.OrderError):
        billing.finish(db, tx2, confirmed=False)
    db.expire_all()
    user = db.query(User).filter(User.tg_id == 4501).first()
    assert user.trial_used
    days = (user.expires_at - db.get(Transaction, tx1.id).paid_at) / 86400
    assert round(days) == trial.days                     # not twice the trial
    assert db.get(Transaction, tx2.id).status == "cancelled"


def test_trial_not_granted_can_be_taken_again(client, db, admin):
    """No server accepted the customer: the trial is not burned."""
    from app import billing
    from app.models import Plan, User
    client.post("/api/admin/servers", headers=admin, json={
        "name": "Down", "country": "DE", "kind": "3xui", "url": "http://127.0.0.1:9/p/",
        "username": "a", "password": "b", "inbound_ids": "1"})
    client.get("/api/app/me", headers=tg(4502))
    user = db.query(User).filter(User.tg_id == 4502).first()
    trial = db.query(Plan).filter(Plan.is_trial.is_(True)).first()
    trial.enabled = True
    db.commit()
    tx = billing.new_order(db, user, "purchase", "stars", plan=trial)
    with pytest.raises(Exception):
        billing.finish(db, tx, confirmed=False)
    db.expire_all()
    user = db.query(User).filter(User.tg_id == 4502).first()
    assert not user.trial_used and not user.expires_at
    assert billing.trial_available(db, user)


# ---------------------------------------------------------------- Stars
def test_stars_payment_without_an_order_alerts_the_admins(client):
    r = _bot(client, {"update_id": 8001, "message": {
        "chat": {"id": 5, "type": "private"}, "from": {"id": 5},
        "successful_payment": {"currency": "XTR", "total_amount": 150, "invoice_payload": "tx:999999",
                               "telegram_payment_charge_id": "ch-lost"}}})
    assert r.status_code == 200
    note = [m["text"] for m in Fake.sent("sendMessage") if m["chat_id"] == 999]
    assert any("ch-lost" in t and "не сопоставлена" in t for t in note)


# ---------------------------------------------------------------- captcha
def test_captcha_is_asked_again_on_a_second_start(client, admin, db):
    from app.models import User
    client.post("/api/admin/settings", headers=admin, json={"ref_enabled": "1", "ref_captcha": "1"})
    client.get("/api/app/me", headers=tg(4601))
    inviter = db.query(User).filter(User.tg_id == 4601).first()
    start = {"chat": {"id": 4602, "type": "private"}, "from": {"id": 4602, "first_name": "New"}}
    _bot(client, {"update_id": 9001, "message": {**start, "text": f"/start r{inviter.id}"}})
    first = [m for m in Fake.sent("sendMessage") if m["chat_id"] == 4602]
    assert len(first) == 1 and "cap:" in str(first[0]["reply_markup"])
    # The message got lost; the person presses /start again.
    _bot(client, {"update_id": 9002, "message": {**start, "text": "/start"}})
    again = [m for m in Fake.sent("sendMessage") if m["chat_id"] == 4602]
    assert len(again) == 2 and "cap:" in str(again[1]["reply_markup"])
    db.expire_all()
    newbie = db.query(User).filter(User.tg_id == 4602).first()
    # Solve it, then press the old button once more: no second welcome.
    _bot(client, {"update_id": 9003, "callback_query": {
        "id": "c1", "from": {"id": 4602}, "data": f"cap:{newbie.captcha}",
        "message": {"message_id": 3, "chat": {"id": 4602}}}})
    db.expire_all()
    assert db.query(User).filter(User.tg_id == 4602).first().referrer_id == inviter.id
    greetings = len([m for m in Fake.sent("sendMessage") if m["chat_id"] == 4602])
    _bot(client, {"update_id": 9004, "callback_query": {
        "id": "c2", "from": {"id": 4602}, "data": "cap:11",
        "message": {"message_id": 3, "chat": {"id": 4602}}}})
    assert len([m for m in Fake.sent("sendMessage") if m["chat_id"] == 4602]) == greetings


# ---------------------------------------------------------------- never silent
def test_unknown_command_from_a_customer_gets_the_app_button(client):
    before = len(Fake.sent("sendMessage"))
    _bot(client, {"update_id": 9101, "message": {"message_id": 1, "text": "/help",
                                                 "chat": {"id": 4701, "type": "private"}, "from": {"id": 4701}}})
    sent = Fake.sent("sendMessage")[before:]
    assert [m["chat_id"] for m in sent] == [4701]
    assert "web_app" in str(sent[0]["reply_markup"])


def test_group_messages_are_ignored(client):
    before = len(Fake.sent("sendMessage"))
    _bot(client, {"update_id": 9201, "message": {"message_id": 1, "text": "hi all",
                                                 "chat": {"id": -100500, "type": "group"}, "from": {"id": 4801}}})
    assert len(Fake.sent("sendMessage")) == before


def test_trial_command_with_nonsense_changes_nothing(client, admin):
    before = client.get("/api/admin/logs?limit=50", headers=admin).json()
    _bot(client, {"update_id": 9301, "message": {"message_id": 1, "text": "/trial 200",
                                                 "chat": {"id": 999, "type": "private"}, "from": {"id": 999}}})
    after = client.get("/api/admin/logs?limit=50", headers=admin).json()
    assert [x for x in after if x["action"] == "Trial changed"] == [x for x in before if x["action"] == "Trial changed"]
