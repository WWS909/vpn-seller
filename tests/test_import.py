"""
Moving customers in from a panel (app/importer.py).

The "old" panel is the Remnawave stand-in (tests/fake_remnawave.py) with
clients made by hand; the shop sells on the same panel, as an owner who
starts using the shop on the panel they already had would.
"""

from __future__ import annotations

import time

import httpx
import pytest

from conftest import Fake
from test_remnawave import rw  # noqa: F401 — the fixture

DAY = 86400
GB = 1024 ** 3


def _bot(client, update):
    from app import notify
    return client.post("/api/bot/webhook", json=update,
                       headers={"X-Telegram-Bot-Api-Secret-Token": notify.webhook_secret()})


def _iso(ts: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _old_client(rw, name, days, tg=None, limit_gb=0, used_gb=0, status="ACTIVE"):  # noqa: F811
    body = {"username": name, "expireAt": _iso(time.time() + days * DAY), "status": status,
            "trafficLimitBytes": limit_gb * GB}
    if tg:
        body["telegramId"] = tg
    r = httpx.post(rw["url"] + "api/users", json=body, trust_env=False,
                   headers={"Authorization": "Bearer " + rw["token"], "X-Forwarded-Proto": "https",
                            "X-Forwarded-For": "127.0.0.1"})
    assert r.status_code == 201, r.text
    if used_gb:
        state = rw["fake"].state.data
        next(u for u in state["users"].values() if u["username"] == name)["_used"] = used_gb * GB


@pytest.fixture()
def panel(client, admin, rw):  # noqa: F811
    if not rw["fake"]:
        pytest.skip("needs the stand-in panel")
    rw["fake"].state.data["users"].clear()             # the stand-in lives for the whole module
    r = client.post("/api/admin/servers", headers=admin, json={
        "name": "Finland", "country": "FI", "kind": "remnawave", "url": rw["url"],
        "api_token": rw["token"], "inbound_ids": rw["squads"]})
    assert r.status_code == 200, r.text
    _old_client(rw, "alice_old", 20, limit_gb=50, used_gb=10)
    _old_client(rw, "201234567", 40)                      # the name is the Telegram ID
    _old_client(rw, "bob", 10, tg=7001)
    _old_client(rw, "gone", -3, tg=7002)
    _old_client(rw, "paused", 15, tg=7003, status="DISABLED")
    _old_client(rw, "tg9999", 30, tg=9999)                # one the shop made itself
    return r.json()["id"]


def _by(rows, name):
    return next(r for r in rows if r["name"] == name)


def test_preview_tells_what_will_happen(client, admin, panel):
    r = client.get(f"/api/admin/import/{panel}", headers=admin)
    assert r.status_code == 200, r.text
    d = r.json()
    names = {c["name"] for c in d["clients"]}
    assert "tg9999" not in names                          # the shop's own clients are not offered
    assert _by(d["clients"], "alice_old")["state"] == "ok" and _by(d["clients"], "alice_old")["tg_id"] == 0
    assert _by(d["clients"], "alice_old")["left"] == 40 * GB
    assert _by(d["clients"], "201234567")["tg_id"] == 201234567
    assert _by(d["clients"], "201234567")["id_from"] == "name"
    assert _by(d["clients"], "bob")["tg_id"] == 7001 and _by(d["clients"], "bob")["id_from"] == "panel"
    assert _by(d["clients"], "gone")["state"] == "ended"
    assert _by(d["clients"], "paused")["state"] == "off"
    assert d["counts"]["with_id"] == 2 and d["counts"]["no_id"] == 1 and d["counts"]["ended"] == 1


def test_move_by_id_and_by_link(client, admin, panel, db):
    from app.models import ImportClaim, User
    r = client.post(f"/api/admin/import/{panel}", headers=admin,
                    json={"names": ["alice_old", "201234567", "bob", "gone"], "devices": 2})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["added"] == 2 and res["skipped"] == 1 and len(res["links"]) == 1
    assert "start=imp" in res["links"][0]["link"]

    db.expire_all()
    bob = db.query(User).filter(User.tg_id == 7001).one()
    assert abs(bob.expires_at - (time.time() + 10 * DAY)) < 120
    assert bob.device_limit == 2 and bob.source == "import"
    assert db.query(User).filter(User.tg_id == 201234567).one().expires_at > time.time() + 39 * DAY

    # the person without an ID opens their link: the days are theirs, once
    code = db.query(ImportClaim).one().code
    d = client.get(f"/api/admin/import/{panel}", headers=admin).json()
    assert _by(d["clients"], "alice_old")["state"] == "link"        # the link waits; not offered as new
    again = client.post(f"/api/admin/import/{panel}", headers=admin, json={"names": ["alice_old"], "devices": 2}).json()
    assert again["links"][0]["link"] == res["links"][0]["link"]   # moving again keeps the same link
    Fake.reset()
    _bot(client, {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 8001, "type": "private"},
                                              "from": {"id": 8001, "first_name": "Alice", "language_code": "ru"},
                                              "text": f"/start imp{code}"}})
    db.expire_all()
    alice = db.query(User).filter(User.tg_id == 8001).one()
    assert abs(alice.expires_at - (time.time() + 20 * DAY)) < 120
    assert alice.traffic_limit == 40 * GB and alice.device_limit == 2
    assert any("перенесена" in m["text"] for m in Fake.sent("sendMessage"))

    Fake.reset()
    _bot(client, {"update_id": 2, "message": {"message_id": 2, "chat": {"id": 8002, "type": "private"},
                                              "from": {"id": 8002, "first_name": "Eve", "language_code": "ru"},
                                              "text": f"/start imp{code}"}})
    db.expire_all()
    assert not db.query(User).filter(User.tg_id == 8002).one().expires_at
    assert any("уже использована" in m["text"] for m in Fake.sent("sendMessage"))

    # moved people now show as already in the shop; a second run adds nothing
    d = client.get(f"/api/admin/import/{panel}", headers=admin).json()
    assert _by(d["clients"], "bob")["state"] == "in_shop"
    assert _by(d["clients"], "alice_old")["state"] == "in_shop"     # she opened her link
    again = client.post(f"/api/admin/import/{panel}", headers=admin, json={"names": ["bob"]}).json()
    assert again["added"] == 0

    links = client.get("/api/admin/import/links", headers=admin).json()
    assert links[0]["claimed"] is True


def test_a_longer_subscription_is_kept(client, admin, panel, db):
    from app import importer
    from app.models import User
    user = User(tg_id=7001, expires_at=int(time.time()) + 90 * DAY, device_limit=5)
    db.add(user)
    db.commit()
    res = client.post(f"/api/admin/import/{panel}", headers=admin, json={"names": ["bob"]}).json()
    assert res["added"] == 0
    db.expire_all()
    assert db.query(User).filter(User.tg_id == 7001).one().expires_at > time.time() + 89 * DAY
    assert importer.give(db, user, int(time.time()) + DAY, 0, 1) is False


def test_link_unknown_and_owner_only(client, admin, panel, db):
    from app import importer, team
    from app.models import User
    u = User(tg_id=8100)
    db.add(u)
    db.commit()
    assert importer.claim(db, u, "zzzzzzzzzz") == "bad"
    assert importer.claim(db, u, "x") == "bad"
    assert team.section_of("/api/admin/import/1") == "owner"


def test_xui_listing_merges_one_person_over_inbounds():
    """3x-ui: the same subscription id on two inbounds is one person; the
    "starts on first use" length becomes a date; traffic comes from stats."""
    from types import SimpleNamespace

    from app.providers.xui import XuiPanel
    p = XuiPanel.__new__(XuiPanel)
    now_ms = int(time.time() * 1000)
    inbounds = [
        {"settings": '{"clients":[{"email":"ivan","subId":"s1","tgId":"","expiryTime":%d,"totalGB":%d,"enable":true},'
                     '{"email":"tg555","subId":"x","tgId":555,"expiryTime":0,"totalGB":0,"enable":true}]}'
                     % (now_ms + 5 * DAY * 1000, 10 * GB),
         "clientStats": [{"email": "ivan", "up": GB, "down": GB}]},
        {"settings": {"clients": [{"email": "ivan-2", "subId": "s1", "tgId": 4242, "expiryTime": now_ms + 6 * DAY * 1000,
                                   "totalGB": 10 * GB, "enable": True},
                                  {"email": "trial", "subId": "s2", "expiryTime": -3 * DAY * 1000, "enable": True}]},
         "clientStats": []},
    ]
    p._ok = lambda method, path, **kw: inbounds if "inbounds" in path else []
    p.version = lambda: "2"
    p.server = SimpleNamespace()
    rows = {r["name"]: r for r in p.clients()}
    assert set(rows) == {"ivan", "tg555", "trial"}
    assert rows["ivan"]["tg_id"] == 4242 and rows["ivan"]["used"] == 2 * GB
    assert abs(rows["ivan"]["expires"] - (now_ms // 1000 + 6 * DAY)) < 5
    assert abs(rows["trial"]["expires"] - (time.time() + 3 * DAY)) < 5
    assert rows["tg555"]["expires"] == 0
