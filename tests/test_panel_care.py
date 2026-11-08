"""
Things a customer notices when they go wrong: bought gigabytes that burn
on renewal, a removed phone that comes back, a "new" link with the old
keys, a panel that blinked, a panel user that belongs to somebody else.
"""

import time
import uuid

import pytest

from conftest import tg
from test_remnawave import _panel, _user, rw  # noqa: F401

GB = 1024 ** 3


@pytest.fixture()
def server(client, admin, rw):  # noqa: F811
    r = client.post("/api/admin/servers", headers=admin, json={
        "name": "Finland", "country": "FI", "kind": "remnawave", "url": rw["url"],
        "api_token": rw["token"], "inbound_ids": rw["squads"]})
    assert r.status_code == 200, r.text
    return r.json()


def _order(db, uid, kind="purchase", plan=None, **kw):
    from app import billing
    from app.models import Plan, User
    user = db.query(User).filter(User.tg_id == uid).one()
    if kind == "purchase" and plan is None:
        plan = db.query(Plan).filter(Plan.is_trial.is_(False)).first()
    tx = billing.new_order(db, user, kind, "admin", plan=plan, **kw)
    assert billing.finish(db, tx)
    db.expire_all()
    return db.query(User).filter(User.tg_id == uid).one()


def _traffic_plan(db, gb=50):
    from app.models import Plan
    plan = db.query(Plan).filter(Plan.is_trial.is_(False)).first()
    plan.traffic_gb = gb
    db.commit()
    return plan


# ---------------------------------------------------------------- traffic packs
def test_unused_bought_traffic_moves_to_the_next_period(client, db, server):
    client.get("/api/app/me", headers=tg(9101))
    plan = _traffic_plan(db)
    u = _order(db, 9101, plan=plan)
    assert u.traffic_limit == 50 * GB and not u.traffic_extra
    from app import billing
    pack = billing.traffic_packs(db)[0]["gb"]
    u = _order(db, 9101, "traffic", gb=pack)
    assert u.traffic_limit == (50 + pack) * GB and u.traffic_extra == pack * GB
    u.traffic_used = (50 + pack - 3) * GB       # the plan's 50 and all but 3 GB of the pack
    db.commit()
    u = _order(db, 9101, plan=plan)
    assert u.traffic_used == 0 and u.traffic_limit == 53 * GB and u.traffic_extra == 3 * GB
    # Nothing of the pack left: nothing to carry.
    u.traffic_used = 53 * GB
    db.commit()
    u = _order(db, 9101, plan=plan)
    assert u.traffic_limit == 50 * GB and not u.traffic_extra


def test_plan_traffic_itself_does_not_carry(client, db, server):
    client.get("/api/app/me", headers=tg(9102))
    plan = _traffic_plan(db)
    u = _order(db, 9102, plan=plan)
    u.traffic_used = 5 * GB
    db.commit()
    u = _order(db, 9102, plan=plan)
    assert u.traffic_limit == 50 * GB


# ---------------------------------------------------------------- removed devices
def test_a_removed_device_stays_out_until_brought_back(client, db, server):
    client.get("/api/app/me", headers=tg(9201))
    _order(db, 9201)
    me = client.get("/api/app/me", headers=tg(9201)).json()
    path = me["sub_url"].replace("https://shop.example.com", "")
    assert client.get(path, headers={"X-HWID": "phone-1"}).status_code == 200
    me = client.get("/api/app/me", headers=tg(9201)).json()
    dev = me["devices"][0]["id"]
    r = client.post(f"/api/app/devices/{dev}/forget", headers=tg(9201)).json()
    assert r["devices"] == [] and r["removed_devices"][0]["id"] == dev
    # It refreshes again: refused with an explanation, and it is not back.
    import base64
    from urllib.parse import unquote
    body = client.get(path, headers={"X-HWID": "phone-1"}).text
    assert "отвязано" in unquote(base64.b64decode(body).decode())
    assert client.get("/api/app/me", headers=tg(9201)).json()["devices"] == []
    # Another device takes the free slot.
    assert client.get(path, headers={"X-HWID": "phone-2"}).status_code == 200
    r = client.post(f"/api/app/devices/{dev}/restore", headers=tg(9201))
    me = client.get("/api/app/me", headers=tg(9201)).json()
    if me["device_limit"] == 1:
        assert r.status_code == 400                           # no free slot
    else:
        assert r.status_code == 200 and len(r.json()["devices"]) == 2
    # Somebody else's device id is not found.
    assert client.post(f"/api/app/devices/{dev}/restore", headers=tg(9202)).status_code in (401, 404)


def test_new_link_forgets_removed_devices(client, db, server):
    from app.models import Device, User
    client.get("/api/app/me", headers=tg(9203))
    _order(db, 9203)
    me = client.get("/api/app/me", headers=tg(9203)).json()
    client.get(me["sub_url"].replace("https://shop.example.com", ""), headers={"X-HWID": "old"})
    dev = client.get("/api/app/me", headers=tg(9203)).json()["devices"][0]["id"]
    client.post(f"/api/app/devices/{dev}/forget", headers=tg(9203))
    r = client.post("/api/app/reissue", headers=tg(9203))
    assert r.status_code == 200 and not r.json()["partial"]
    user = db.query(User).filter(User.tg_id == 9203).one()
    assert db.query(Device).filter(Device.user_id == user.id).count() == 0


# ---------------------------------------------------------------- honest reissue
def test_reissue_keeps_the_old_link_when_no_server_takes_the_new_key(client, db, server):
    from app import access
    from app.models import Server, User
    client.get("/api/app/me", headers=tg(9301))
    _order(db, 9301)
    before = db.query(User).filter(User.tg_id == 9301).one()
    old = (before.xui_uuid, before.sub_token)
    s = db.get(Server, server["id"])
    good_url, s.url = s.url, "http://127.0.0.1:1/"
    db.commit()
    access._links_cache.clear()
    r = client.post("/api/app/reissue", headers=tg(9301))
    assert r.status_code == 503 and "прежней" in r.json()["detail"]
    db.expire_all()
    after = db.query(User).filter(User.tg_id == 9301).one()
    assert (after.xui_uuid, after.sub_token) == old
    s = db.get(Server, server["id"])
    s.url = good_url
    db.commit()
    # Not counted as a try: the person may try again right away.
    assert client.post("/api/app/reissue", headers=tg(9301)).status_code == 200


# ---------------------------------------------------------------- Remnawave
def _spec(uid, **kw):
    from app.providers import ClientSpec
    return ClientSpec(**{"email": f"tg{uid}", "uuid": str(uuid.uuid4()), "sub_id": "x", "tg_id": uid,
                         "expiry_ms": int((time.time() + 86400) * 1000), "total_bytes": 0,
                         "limit_ip": 0, "enable": True, **kw})


def test_switched_off_or_reissued_user_is_cut_off_now(rw):  # noqa: F811
    state = rw["fake"].state.data
    p = _panel(rw)
    spec = _spec(9401)
    p.ensure(spec)
    state["dropped"] = []
    p.ensure(spec)                                   # nothing changed: nobody is cut
    assert state["dropped"] == []
    spec.enable = False
    p.ensure(spec)                                   # blocked / expired / out of traffic
    assert len(state["dropped"]) == 1
    assert state["dropped"][0]["dropBy"]["by"] == "userIds"
    assert state["dropped"][0]["targetNodes"] == {"target": "allNodes"}
    spec.enable = True
    spec.uuid = str(uuid.uuid4())                    # a new key
    p.ensure(spec)
    assert len(state["dropped"]) == 2
    p.delete(spec)


def test_somebody_elses_panel_user_is_left_alone(rw):  # noqa: F811
    from app.providers import PanelError
    p = _panel(rw)
    theirs = _spec(9402)
    p.ensure(theirs)
    ours = _spec(9402, tg_id=123)                   # same name, another Telegram account
    with pytest.raises(PanelError, match="belongs to Telegram id 9402"):
        p.ensure(ours)
    with pytest.raises(PanelError):
        p.delete(ours)
    assert _user(rw, "tg9402") is not None
    p.delete(theirs)

    # A panel user named like our customer but owned by another account:
    # its keys are not handed out and its traffic is not counted as ours.
    other = _spec(9403, tg_id=777)
    p.ensure(other)
    with pytest.raises(PanelError, match="belongs to Telegram id 777"):
        p.links(_spec(9403))
    assert "tg9403" not in p.traffic_by_email()
    p.delete(other)


def test_a_blink_of_the_panel_is_asked_again_but_writes_are_not(rw):  # noqa: F811
    from app.providers import PanelError
    state = rw["fake"].state.data
    p = _panel(rw)
    state["flaky"] = 2
    assert p._squads(fresh=True)                     # two 502s, then the answer
    assert state["flaky"] == 0
    state["flaky"] = 5
    with pytest.raises(PanelError, match="502"):
        p._request("GET", "api/system/stats")
    state["flaky"] = 0
    before = len(state["requests"])
    with pytest.raises(PanelError):
        p._request("POST", "api/users", json={"username": "x"})
    assert len(state["requests"]) - before == 1      # one write, never repeated
