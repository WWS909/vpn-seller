"""
Three owner tools: the reminder about a payment left unfinished, the day's
results in Telegram, and the tables for Excel (orders, customers).
"""
from __future__ import annotations

import datetime as dt
import io
import time

import openpyxl
import pytest

from conftest import Fake, init_data, tg


def _user(client, db, tg_id: int, **kw):
    from app.models import User
    client.get("/api/app/me", headers=tg(tg_id))
    u = db.query(User).filter(User.tg_id == tg_id).one()
    for k, v in kw.items():
        setattr(u, k, v)
    db.commit()
    return u


def _tx(db, u, ago: int, status: str = "pending", amount: float = 299, **kw):
    from app.models import Transaction
    t = int(time.time())
    tx = Transaction(user_id=u.id, kind=kw.pop("kind", "purchase"), amount=amount, currency="RUB",
                     method=kw.pop("method", "card"), gateway=kw.pop("gateway", "yookassa"), status=status,
                     created_at=t - ago, paid_at=(t - ago) if status == "success" else 0, **kw)
    db.add(tx)
    db.commit()
    return tx


def _to(tg_id: int) -> list[dict]:
    return [b for b in Fake.sent("sendMessage") if b.get("chat_id") == tg_id]


# ---------------------------------------------------------------- the unfinished payment
def test_unfinished_payment_gets_one_reminder(client, admin, db):
    from app import marketing
    warm = {t["key"]: t for t in client.get("/api/admin/warm", headers=admin).json()["triggers"]}
    assert warm["unpaid"]["on"] and warm["unpaid"]["percent"] == 0 and warm["unpaid"]["param"] == 60
    Fake.reset()
    a = _user(client, db, 81001)
    _tx(db, a, ago=2 * 3600)                                  # two hours ago, never paid
    b = _user(client, db, 81002)
    _tx(db, b, ago=20 * 60)                                   # twenty minutes: too early
    c = _user(client, db, 81003)
    _tx(db, c, ago=2 * 3600)
    _tx(db, c, ago=3600, status="success")                    # paid another way afterwards
    d = _user(client, db, 81004)
    _tx(db, d, ago=3 * 86400)                                 # too old to catch up
    e = _user(client, db, 81005)
    _tx(db, e, ago=2 * 3600, method="free", amount=0)         # a free one is not a payment
    sent = marketing.run_triggers(db, only="unpaid")
    assert sent == {"unpaid": 1}
    msgs = _to(81001)
    assert len(msgs) == 1 and "299" in msgs[0]["text"] and "не завершили" in msgs[0]["text"]
    btn = msgs[0]["reply_markup"]["inline_keyboard"][0][0]
    assert btn["text"] == "Завершить оплату"
    for other in (81002, 81003, 81004, 81005):
        assert not _to(other), other
    # once: the same order again, and another unfinished one the next hour
    marketing.run_triggers(db, only="unpaid")
    _tx(db, a, ago=90 * 60)
    marketing.run_triggers(db, only="unpaid")
    assert len(_to(81001)) == 1
    # the stats on the warm-up page: reminded, then paid
    _tx(db, a, ago=0, status="success")
    warm = {t["key"]: t for t in client.get("/api/admin/warm", headers=admin).json()["triggers"]}
    assert warm["unpaid"]["sent_7d"] == 1 and warm["unpaid"]["bought_7d"] == 1


def test_unfinished_payment_with_a_discount(client, admin, db):
    from app import marketing
    from app.models import Offer
    r = client.put("/api/admin/warm/unpaid", headers=admin, json={"percent": 10, "valid": 1, "param": 30})
    assert r.status_code == 200 and r.json()["param"] == 30
    Fake.reset()
    u = _user(client, db, 81011)
    _tx(db, u, ago=45 * 60, status="failed")                 # a declined one counts too
    marketing.run_triggers(db, only="unpaid")
    m = _to(81011)[0]
    assert m["reply_markup"]["inline_keyboard"][0][0]["text"] == "Забрать скидку"
    assert db.query(Offer).filter(Offer.source == "trigger:unpaid").count() == 1
    # out of range minutes are clamped
    assert client.put("/api/admin/warm/unpaid", headers=admin, json={"param": 1}).json()["param"] == 15


# ---------------------------------------------------------------- the day's results
def test_daily_report_numbers_and_text(client, admin, db):
    from app import report
    from app.models import Server
    a = _user(client, db, 82001)
    b = _user(client, db, 82002, expires_at=int(time.time()) + 5 * 3600)
    _tx(db, a, ago=3 * 86400, status="success", amount=100)   # old: a has paid before
    _tx(db, a, ago=3600, status="success", amount=500)        # a renewal today
    _tx(db, b, ago=7200, status="success", amount=290)        # b's first payment
    z = _user(client, db, 82004)
    _tx(db, z, ago=30 * 3600, status="success", amount=200)   # the day before (someone else)
    c = _user(client, db, 82003)
    _tx(db, c, ago=5000)                                      # started, not finished
    db.add(Server(name="NL-1", url="https://x", enabled=True, last_error="timeout"))
    db.commit()
    d = report.collect(db)
    assert d["revenue"] == 790 and d["revenue_prev"] == 200 and d["orders"] == 2 and d["first"] == 1
    assert d["unpaid"] == 1 and d["ending"] == 1 and d["down"] == ["NL-1"] and d["new"] >= 3
    text = report.text(db, d, "ru")
    assert "Итоги за сутки" in text and "790" in text and "▲ 295%" in text and "вчера 200" in text
    assert "не отвечает: NL-1" in text and "Начали оплату и не закончили: 1" in text
    en = report.text(db, d, "en")
    assert "The last 24 hours" in en and "not answering: NL-1" in en


def test_daily_report_sent_once_at_the_hour(client, admin, db, env):
    from app import core, report
    hour = dt.datetime.now().hour
    Fake.reset()
    client.post("/api/admin/settings", headers=admin, json={"report_hour": str((hour + 1) % 24)})
    assert report.run_daily(db) is False
    client.post("/api/admin/settings", headers=admin, json={"report_hour": str(hour)})
    assert report.run_daily(db) is True
    assert report.run_daily(db) is False                      # once a day
    assert len(_to(999)) == 1 and "Итоги за сутки" in _to(999)[0]["text"]
    # off: nothing
    core.put(db, "report_sent", "")
    client.post("/api/admin/settings", headers=admin, json={"report_on": "0"})
    assert report.run_daily(db) is False
    # bad hours are refused
    assert client.post("/api/admin/settings", headers=admin, json={"report_hour": "25"}).status_code == 400
    # "send now" from the panel
    r = client.post("/api/admin/report/send", headers=admin)
    assert r.status_code == 200 and r.json()["sent"] == 1


# ---------------------------------------------------------------- Excel
def _book(content: bytes):
    return openpyxl.load_workbook(io.BytesIO(content)).active


def test_orders_to_excel(client, admin, db):
    a = _user(client, db, 83001, username="ivan")
    _tx(db, a, ago=3600, status="success", amount=1490.5, gateway="rollypay")
    _tx(db, a, ago=7200, status="success", amount=290, method="stars", gateway="stars", stars=150)
    _tx(db, a, ago=600)                                       # not paid
    _tx(db, a, ago=40 * 86400, status="success", amount=99)  # outside "this month" for sure? use dates below
    today = dt.date.today()
    r = client.get("/api/admin/orders/export", headers=admin,
                   params={"start": (today - dt.timedelta(days=1)).isoformat(), "end": today.isoformat()})
    assert r.status_code == 200 and r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert 'filename="orders_' in r.headers["content-disposition"]
    ws = _book(r.content)
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0][:3] == ("Заказ", "Дата", "Статус") and ws.freeze_panes == "A2"
    data = [x for x in rows[1:] if isinstance(x[0], int)]
    assert len(data) == 2 and {x[12] for x in data} == {"RollyPay", "Telegram Stars"}
    assert all(isinstance(x[1], dt.datetime) and x[3] == 83001 and x[4] == "@ivan" for x in data)
    assert rows[-1][0] == "Итого" and rows[-1][9] == pytest.approx(1780.5)
    # with the unpaid ones, in English
    r = client.get("/api/admin/orders/export", headers=admin,
                   params={"start": (today - dt.timedelta(days=1)).isoformat(), "end": today.isoformat(),
                           "status": "all", "lang": "en"})
    rows = list(_book(r.content).iter_rows(values_only=True))
    assert rows[0][0] == "Order" and "Waiting" in [x[2] for x in rows[1:]]
    assert rows[-1][9] == pytest.approx(1780.5)               # the total is still paid ones only
    # bad periods
    for p in ({"start": "2026-13-01"}, {"start": "2026-11-09", "end": "2026-11-01"}, {"status": "weird"}):
        assert client.get("/api/admin/orders/export", headers=admin, params=p).status_code == 400, p


def test_customers_to_excel_and_to_telegram(client, admin, db, monkeypatch):
    from app.routers import export
    a = _user(client, db, 84001, username="olga", expires_at=int(time.time()) + 10 * 86400, trial_used=True)
    _tx(db, a, ago=3600, status="success", amount=500)
    _user(client, db, 84002)
    r = client.get("/api/admin/users/export", headers=admin)
    assert r.status_code == 200
    rows = list(_book(r.content).iter_rows(values_only=True))
    assert rows[0][0] == "Telegram ID"
    olga = next(x for x in rows if x[0] == 84001)
    assert olga[1] == "@olga" and isinstance(olga[7], dt.datetime) and olga[10] == 1 and olga[11] == 500
    assert olga[13] == "да"
    # inside Telegram the file comes to the chat
    got = []
    monkeypatch.setattr(export, "send_file", lambda chat, name, data, caption: got.append((chat, name, len(data))) or True)
    r = client.post("/api/admin/users/export/send", headers=admin)
    assert r.status_code == 200 and got and got[0][0] == 999 and got[0][1].startswith("customers_")
    r = client.post("/api/admin/orders/export/send", headers=admin, params={"start": "", "end": ""})
    assert r.status_code == 200 and got[-1][1].startswith("orders_")


def test_export_follows_team_sections(client, admin):
    r = client.post("/api/admin/team", headers=admin, json={"tg_id": 7600011, "name": "Маша", "sections": ["sales"]})
    assert r.status_code == 200
    tok = client.post("/api/admin/tg-login", headers={"X-Init-Data": init_data(7600011)}).json()["token"]
    h = {"Authorization": "Bearer " + tok}
    assert client.get("/api/admin/orders/export", headers=h).status_code == 200
    assert client.get("/api/admin/users/export", headers=h).status_code == 403
    assert client.post("/api/admin/report/send", headers=h).status_code == 403   # the results: "stats" only


def test_panel_has_the_pieces(client):
    panel = client.get("/admin").text
    for part in ("function exportModal(", "function exportUsers(", 'onclick="exportModal()"', 'id="i-download"',
                 'sw("report_on"', 'f("report_hour"', "function sendReport(", 'trUnpaid:"Брошенная оплата"', 'trUnpaid:"Abandoned payment"',
                 'unpaid:["trUnpaid", "trUnpaidW", "unitMinutes"]', 'exExcel:"Excel"'):
        assert part in panel, part


def test_bot_gives_the_results_on_demand(client, db):
    from test_bot_logic import _press
    Fake.reset()
    _press(client, 9601, "a:stats")
    _press(client, 9602, "a:rep")
    edits = Fake.sent("editMessageText")
    assert edits and "Итоги за сутки" in edits[-1]["text"]
