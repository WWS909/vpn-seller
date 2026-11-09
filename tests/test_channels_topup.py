"""Several mandatory channels, and traffic top-ups as a feature that is off
until the owner turns it on."""
from __future__ import annotations

import json
import time

from conftest import Fake, tg


def _member(monkeypatch, joined: dict):
    """getChatMember answers from `joined` {chat: bool}."""
    from app import notify
    real = notify.call

    def call(method, body=None, **kw):
        if method == "getChatMember":
            ok = joined.get(body["chat_id"], True)
            return {"ok": True, "result": {"status": "member" if ok else "left"}}
        return real(method, body, **kw) if body is not None else real(method, **kw)
    monkeypatch.setattr(notify, "call", call)


def test_several_channels_all_must_be_joined(client, admin, db, monkeypatch):
    chans = [{"id": "@first_channel", "link": ""}, {"id": "-1001234567890", "link": "https://t.me/+AbCdEf123"}]
    r = client.post("/api/admin/settings", headers=admin,
                    json={"channels_json": json.dumps(chans), "channel_mode": "all"})
    assert r.status_code == 200, r.text
    cfg = client.get("/api/app/config").json()["channel"]
    assert cfg["mode"] == "all" and [c["link"] for c in cfg["channels"]] == \
        ["https://t.me/first_channel", "https://t.me/+AbCdEf123"]
    joined = {"@first_channel": True, "-1001234567890": False}
    _member(monkeypatch, joined)
    me = client.get("/api/app/me", headers=tg(95001)).json()
    assert me["gate"] and [c["link"] for c in me["gate"]["channels"]] == ["https://t.me/+AbCdEf123"]
    r = client.post("/api/app/channel/check", headers=tg(95001)).json()
    assert r["ok"] is False
    joined["-1001234567890"] = True
    r = client.post("/api/app/channel/check", headers=tg(95001)).json()
    assert r["ok"] is True and client.get("/api/app/me", headers=tg(95001)).json()["gate"] is None


def test_channel_list_is_checked(client, admin):
    for bad in ([{"id": "not a channel"}], [{"id": "@good_one", "link": "https://evil.example/x"}],
                [{"id": f"@channel_{i}"} for i in range(6)], {"id": "@x"}, "nope"):
        r = client.post("/api/admin/settings", headers=admin,
                        json={"channels_json": bad if isinstance(bad, str) else json.dumps(bad)})
        assert r.status_code == 400, bad
    # repeats and empty rows are dropped
    r = client.post("/api/admin/settings", headers=admin, json={"channels_json": json.dumps(
        [{"id": "@same_name"}, {"id": "@SAME_NAME"}, {"id": "", "link": ""}])})
    assert r.status_code == 200
    v = client.get("/api/admin/settings", headers=admin).json()["values"]["channels_json"]
    assert [c["id"] for c in json.loads(v)] == ["@same_name"]


def test_the_older_single_channel_still_counts(client, admin, db):
    from app import channel, core
    core.put(db, "channel_id", "@old_channel")
    core.put(db, "channel_mode", "trial")
    assert [c["id"] for c in channel.channels(db)] == ["@old_channel"]
    assert channel.mode(db) == "trial"
    # saving the list (empty) from the new panel replaces it
    client.post("/api/admin/settings", headers=admin, json={"channels_json": "[]", "channel_id": ""})
    assert channel.channels(db) == [] and channel.mode(db) == "off"


def test_check_setup_reports_each_channel(client, admin, db, monkeypatch):
    from app import core, notify
    core.put(db, "channels_json", json.dumps([{"id": "@ok_channel", "link": ""}, {"id": "@no_admin", "link": ""}]))

    def call(method, body=None, **kw):
        if method == "getMe":
            return {"ok": True, "result": {"id": 1}}
        if method == "getChat":
            return {"ok": True, "result": {"title": body["chat_id"][1:].replace("_", " ").title(),
                                           "username": body["chat_id"][1:]}}
        if method == "getChatMember":
            return {"ok": True, "result": {"status": "administrator" if body["chat_id"] == "@ok_channel" else "left"}}
        return {"ok": True}
    monkeypatch.setattr(notify, "call", call)
    r = client.post("/api/admin/channel/check", headers=admin).json()
    assert r["ok"] is False and [x["ok"] for x in r["channels"]] == [True, False]
    assert "not an administrator" in r["error"]
    stored = json.loads(core.get(db, "channels_json"))
    assert stored[0]["title"] == "Ok Channel"


def test_panel_and_app_have_the_channel_pieces(client):
    panel, app = client.get("/admin").text, client.get("/app").text
    for part in ("function chAdd(", "function chBody(", 'id="chRows"', 'chAdd:"Добавить канал"', 'chAdd:"Add a channel"'):
        assert part in panel, part
    for part in ("function chButtons(", 'gateTMany:"Подпишитесь на каналы"', 'gateTMany:"Subscribe to the channels"'):
        assert part in app, part


# ---------------------------------------------------------------- traffic top-ups
def test_traffic_topups_are_off_until_turned_on(client, admin, db):
    from app.models import Plan, User
    assert client.get("/api/app/config").json()["traffic_packs"] == []
    plan = db.query(Plan).filter(Plan.is_trial.is_(False)).first() or Plan(name="M", days=30, price=100, enabled=True)
    plan.traffic_gb = 50
    db.add(plan)
    db.commit()
    client.get("/api/app/me", headers=tg(96001))
    u = db.query(User).filter(User.tg_id == 96001).one()
    u.expires_at, u.traffic_limit, u.plan_id = int(time.time()) + 86400, 50 * 1024 ** 3, plan.id
    db.commit()
    r = client.post("/api/app/order", headers=tg(96001), json={"kind": "traffic", "gb": 10, "method": "stars"})
    assert r.status_code >= 400                                        # nothing is sold while it is off
    assert client.post("/api/admin/settings", headers=admin, json={"traffic_topup": "1"}).status_code == 200
    packs = client.get("/api/app/config").json()["traffic_packs"]
    assert [p["gb"] for p in packs] == [10, 50, 100]
    panel = client.get("/admin").text
    assert 'sw("traffic_topup"' in panel and 'tpOn:"Докупка трафика"' in panel


def test_traffic_out_texts_follow_the_switch(client, db):
    from app import core
    from app.jobs import job_traffic
    from app import access
    from app.models import User
    client.get("/api/app/me", headers=tg(96002))
    u = db.query(User).filter(User.tg_id == 96002).one()
    original = access.collect_traffic
    access.collect_traffic = lambda db: {"over": [96002]}
    try:
        Fake.reset()
        job_traffic(db)
        assert "Продлите подписку" in Fake.sent("sendMessage")[-1]["text"]
        core.put(db, "traffic_topup", "1")
        job_traffic(db)
        assert "Докупите гигабайты" in Fake.sent("sendMessage")[-1]["text"]
    finally:
        access.collect_traffic = original
    assert u.id


def test_the_single_channel_field_still_works_after_the_list_was_saved(client, admin, db):
    from app import channel
    assert client.post("/api/admin/settings", headers=admin, json={"channels_json": "[]"}).status_code == 200
    assert channel.channels(db) == []
    r = client.post("/api/admin/settings", headers=admin, json={"channel_id": "@orbit_news", "channel_mode": "all"})
    assert r.status_code == 200
    db.expire_all()
    import time; time.sleep(3.2)
    assert [c["id"] for c in channel.channels(db)] == ["@orbit_news"]
