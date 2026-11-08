"""
A thank-you for reviews, the owner's names for payment methods, and the AI
helper in support.
"""

from __future__ import annotations

import json
import time

import pytest

from conftest import Fake, tg
from test_growth import _bot, server  # noqa: F401
from test_remnawave import rw  # noqa: F401


def _paid_customer(client, db, uid, method="card", amount=None):
    from app import billing
    from app.models import Plan, User
    client.get("/api/app/me", headers=tg(uid))
    user = db.query(User).filter(User.tg_id == uid).one()
    tx = billing.new_order(db, user, "purchase", method, plan=db.query(Plan).filter(Plan.is_trial.is_(False)).first())
    tx.gateway = "lava"
    if amount is not None:
        tx.amount = amount
    db.commit()
    assert billing.finish(db, tx, external_id=f"e{uid}")
    db.expire_all()
    return db.query(User).filter(User.tg_id == uid).one()


# ---------------------------------------------------------------- review reward
def test_review_reward_once_on_publishing_for_paying_customers(client, admin, db, server):  # noqa: F811
    from app.models import User
    client.post("/api/admin/settings", headers=admin, json={"review_reward_days": "5"})
    user = _paid_customer(client, db, 9501)
    assert client.get("/api/app/me", headers=tg(9501)).json()["review_bonus"] == 5
    before = user.expires_at
    for text in ("Отличный сервис", "Ещё раз спасибо"):
        assert client.post("/api/app/reviews", headers=tg(9501), json={"stars": 5, "text": text}).status_code == 200
    db.expire_all()
    assert db.query(User).filter(User.tg_id == 9501).one().expires_at == before     # nothing yet
    ids = [r["id"] for r in client.get("/api/admin/reviews?status=pending", headers=admin).json()]
    r = client.post(f"/api/admin/reviews/{ids[0]}/published", headers=admin).json()
    assert r["reward_days"] == 5
    assert any("добавили вам 5" in m["text"] for m in Fake.sent("sendMessage") if m["chat_id"] == 9501)
    r = client.post(f"/api/admin/reviews/{ids[1]}/published", headers=admin).json()
    assert r["reward_days"] == 0                                                    # once
    r = client.post(f"/api/admin/reviews/{ids[0]}/published", headers=admin).json()
    assert r["reward_days"] == 0                                                    # publishing again
    db.expire_all()
    assert db.query(User).filter(User.tg_id == 9501).one().expires_at == before + 5 * 86400
    assert client.get("/api/app/me", headers=tg(9501)).json()["review_bonus"] == 0


def test_no_review_reward_for_a_trial_only_account(client, admin, db, server):  # noqa: F811
    client.post("/api/admin/settings", headers=admin, json={"review_reward_days": "5"})
    client.get("/api/app/me", headers=tg(9502))
    from app import billing
    from app.models import User
    user = db.query(User).filter(User.tg_id == 9502).one()
    trial = billing.trial_plan(db) if hasattr(billing, "trial_plan") else None
    if trial is not None and trial.enabled:
        tx = billing.new_order(db, user, "purchase", "free", plan=trial)
        billing.finish(db, tx, confirmed=False)
    assert client.get("/api/app/me", headers=tg(9502)).json()["review_bonus"] == 0
    assert client.post("/api/admin/settings", headers=admin, json={"review_reward_days": "-1"}).status_code == 400
    assert client.post("/api/admin/settings", headers=admin, json={"review_reward_days": "1000"}).status_code == 400


# ---------------------------------------------------------------- method names
def test_owner_names_the_payment_methods(client, admin):
    labels = {"card": {"title_ru": "Карта РФ", "note_ru": "без комиссии", "title_en": "", "evil": "x"},
              "nonsense": {"title_ru": "x"}, "stars": {"title_ru": "S" * 99}}
    r = client.post("/api/admin/settings", headers=admin, json={"pay_labels": json.dumps(labels)})
    assert r.status_code == 200
    got = client.get("/api/app/config").json()["pay_labels"]
    assert got["card"]["title_ru"] == "Карта РФ" and got["card"]["note_ru"] == "без комиссии"
    assert "evil" not in got["card"] and "nonsense" not in got
    assert len(got["stars"]["title_ru"]) == 40
    assert client.post("/api/admin/settings", headers=admin, json={"pay_labels": "[1"}).status_code == 400


# ---------------------------------------------------------------- AI helper
class Model:
    def __init__(self):
        self.asked = []
        self.answer = {"reply": "Откройте «Подключить» и нажмите «Добавить подписку».", "handoff": False}
        self.fail = False

    def __call__(self, messages):
        from app import support_ai
        self.asked.append(messages)
        if self.fail:
            raise support_ai.AIError("down")
        return json.dumps(self.answer, ensure_ascii=False)


@pytest.fixture()
def ai(client, admin, env, monkeypatch):
    from app import support_ai
    monkeypatch.setenv("AI_API_KEY", "sk-test")
    env.__init__()
    model = Model()
    monkeypatch.setattr(support_ai, "_call", model)
    monkeypatch.setattr(support_ai, "run_inline", True)
    support_ai._calls.clear()
    r = client.post("/api/admin/support/ai", headers=admin, json={"on": True, "name": "Ника", "prompt": "Пиши кратко"})
    assert r.status_code == 200
    return model


def _ask(client, uid, text, mid=1):
    return _bot(client, {"message": {"message_id": mid, "text": text, "chat": {"id": uid, "type": "private"},
                                     "from": {"id": uid, "first_name": "Ann"}}})


def test_ai_answers_simple_questions_signed_as_ai(client, admin, ai, db, server):  # noqa: F811
    _paid_customer(client, db, 9601)
    Fake.calls.clear()
    _ask(client, 9601, "Как подключить телефон?")
    to_customer = [m["text"] for m in Fake.sent("sendMessage") if m["chat_id"] == 9601]
    assert any("Ника · ИИ-помощник" in x and "Добавить подписку" in x for x in to_customer)
    assert not [m for m in Fake.sent("sendMessage") if m["chat_id"] == 999]         # admins not bothered
    system = ai.asked[0][0]["content"]
    assert "Пиши кратко" in system and "Подписка активна до" in system and "Тарифы:" in system
    assert "/sub/" not in system                                                      # no subscription link
    d = client.get("/api/admin/support", headers=admin).json()
    assert d["waiting"] == 0
    msgs = client.get("/api/app/support", headers=tg(9601)).json()["messages"]
    assert [m["source"] for m in msgs] == ["bot", "ai"]


def test_ai_hands_over_and_stays_out(client, admin, ai):
    ai.answer = {"reply": "Позвал администратора.", "handoff": True}
    Fake.calls.clear()
    _ask(client, 9602, "Списали деньги дважды")
    assert any("ИИ-помощник передал" in m["text"] for m in Fake.sent("sendMessage") if m["chat_id"] == 999)
    assert client.get("/api/admin/support", headers=admin).json()["waiting"] == 1   # still waits for a person
    n = len(ai.asked)
    Fake.calls.clear()
    _ask(client, 9602, "Ну что там?", 2)
    assert len(ai.asked) == n                                                         # people have it now
    assert any("Ну что там" in m["text"] for m in Fake.sent("sendMessage") if m["chat_id"] == 999)


def test_asking_for_a_person_skips_the_ai(client, ai):
    Fake.calls.clear()
    _ask(client, 9603, "Позовите оператора пожалуйста")
    assert not ai.asked
    assert any("оператора" in m["text"] for m in Fake.sent("sendMessage") if m["chat_id"] == 999)


def test_ai_down_or_nonsense_goes_to_people(client, ai):
    ai.fail = True
    Fake.calls.clear()
    _ask(client, 9604, "Не работает VPN")
    assert any("Не работает VPN" in m["text"] for m in Fake.sent("sendMessage") if m["chat_id"] == 999)
    ai.fail = False
    ai.answer = "not json"
    from app import support_ai
    support_ai._calls.clear()
    Fake.calls.clear()
    _ask(client, 9605, "Какие есть тарифы?")
    assert any("Какие есть тарифы" in m["text"] for m in Fake.sent("sendMessage") if m["chat_id"] == 999)


def test_attachments_and_off_switch_go_to_people(client, admin, ai):
    Fake.calls.clear()
    _bot(client, {"message": {"message_id": 3, "photo": [{"file_id": "x"}], "caption": "скрин ошибки",
                              "chat": {"id": 9606, "type": "private"}, "from": {"id": 9606}}})
    assert not ai.asked
    client.post("/api/admin/support/ai", headers=admin, json={"on": False})
    _ask(client, 9607, "Как подключить?")
    assert not ai.asked


def test_ai_budget_per_customer(client, ai):
    from app import support_ai
    for i in range(support_ai.PER_USER_HOUR + 3):
        _ask(client, 9608, f"вопрос {i}", i + 1)
    assert len(ai.asked) == support_ai.PER_USER_HOUR


def test_ai_cannot_be_switched_on_without_a_key(client, admin):
    assert client.post("/api/admin/support/ai", headers=admin, json={"on": True}).status_code == 400
    assert client.post("/api/admin/support/ai/check", headers=admin).status_code == 400
    assert client.get("/api/admin/support", headers=admin).json()["ai"]["configured"] is False


def test_model_answer_parsing():
    from app import support_ai
    assert support_ai._parse('```json\n{"reply": "ok", "handoff": false}\n```') == ("ok", False)
    assert support_ai._parse('{"reply": "", "handoff": false}') == ("", True)       # empty: a person
    with pytest.raises(support_ai.AIError):
        support_ai._parse("Sure! Here is my answer")
    assert time.time()
