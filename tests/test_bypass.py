"""
Whitelist-bypass servers against REAL 3x-ui panels: Finland (2.x) is the
bypass server, Germany (3.x) the usual one. Bypass traffic is sold by the
gigabyte: without gigabytes the server is off for the person and its keys
are not in the subscription; the gigabytes run out — it goes off again,
the other server keeps working.
"""
from __future__ import annotations

import pytest

from conftest import V2, V3, panels, tg
from test_panels import _buy, _client_on, _set_traffic, _sub, test_mode, uid  # noqa: F401

pytestmark = panels
GB = 1024 ** 3


@pytest.fixture()
def shop(client, admin, servers):
    fin = servers[1]
    s = next(x for x in client.get("/api/admin/servers", headers=admin).json() if x["id"] == fin)
    body = {**{k: s[k] for k in ("name", "country", "url", "username", "inbound_ids", "sub_url",
                                 "verify_tls", "enabled", "sort", "kind", "capacity")}, "bypass": True}
    r = client.put(f"/api/admin/servers/{fin}", headers=admin, json=body)
    assert r.status_code == 200 and r.json()["bypass"] is True, r.text
    client.post("/api/admin/settings", headers=admin, json={"bypass_packs": "[[10, 150], [50, 600]]"})
    return servers


def _me(client, who):
    return client.get("/api/app/me", headers=tg(who)).json()


def test_no_gigabytes_no_bypass_server(client, shop, test_mode, uid):  # noqa: F811
    cfg = client.get("/api/app/config").json()
    assert cfg["bypass"] is True and cfg["bypass_packs"][0] == {"gb": 10, "price": 150}
    _buy(client, uid)                                   # the plan has no bypass GB
    me = _me(client, uid)
    assert me["bypass_limit"] == 0
    assert _client_on(V3, f"tg{uid}")["enable"] is True
    assert _client_on(V2, f"tg{uid}")["enable"] is False        # bypass: off without gigabytes
    links, _ = _sub(client, me["sub_url"], hwid="p1")
    assert len(links) == 1                                      # only Germany

    r = client.post("/api/app/order", headers=tg(uid), json={"kind": "bypass", "gb": 10, "method": "stars"})
    assert r.status_code == 200 and r.json()["status"] == "success", r.text
    me = _me(client, uid)
    assert me["bypass_limit"] - me["bypass_used"] == 10 * GB
    c = _client_on(V2, f"tg{uid}")
    assert c["enable"] is True and int(c["totalGB"]) == me["bypass_limit"]   # the panel stops it itself too
    assert len(_sub(client, me["sub_url"], hwid="p1")[0]) == 2
    # a pack that is not on sale
    r = client.post("/api/app/order", headers=tg(uid), json={"kind": "bypass", "gb": 7, "method": "stars"})
    assert r.status_code == 400


def test_gigabytes_run_out_only_bypass_goes_off(client, admin, shop, test_mode, uid):  # noqa: F811
    _buy(client, uid)
    client.post("/api/app/order", headers=tg(uid), json={"kind": "bypass", "gb": 10, "method": "stars"})
    email = f"tg{uid}"
    _set_traffic(V3, email, 40 * GB)                    # the usual server: not counted as bypass
    _set_traffic(V2, email, 11 * GB)                    # more than bought on the bypass server
    from app import access
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        res = access.collect_traffic(db)
    finally:
        db.close()
    assert uid in res["bypass_over"]
    me = _me(client, uid)
    assert me["bypass_used"] == 11 * GB and me["traffic_used"] == 40 * GB
    assert _client_on(V2, email)["enable"] is False
    assert _client_on(V3, email)["enable"] is True
    # topping up turns it back on
    client.post("/api/app/order", headers=tg(uid), json={"kind": "bypass", "gb": 10, "method": "stars"})
    me = _me(client, uid)
    assert me["bypass_limit"] == 21 * GB and _client_on(V2, email)["enable"] is True
    # the admin sets what is left
    r = client.post(f"/api/admin/users/{uid}/action", headers=admin, json={"action": "bypass", "gb": 3})
    assert r.status_code == 200
    me = _me(client, uid)
    assert me["bypass_limit"] - me["bypass_used"] == 3 * GB


def test_plan_with_bypass_gigabytes_and_renewal(client, admin, shop, test_mode, uid):  # noqa: F811
    plan = client.get("/api/app/config").json()["plans"][0]
    p = next(x for x in client.get("/api/admin/plans", headers=admin).json() if x["id"] == plan["id"])
    r = client.put(f"/api/admin/plans/{p['id']}", headers=admin, json={**p, "bypass_gb": 5})
    assert r.status_code == 200 and r.json()["bypass_gb"] == 5, r.text
    assert client.get("/api/app/config").json()["plans"][0]["bypass_gb"] == 5
    _buy(client, uid)
    me = _me(client, uid)
    assert me["bypass_limit"] == 5 * GB and _client_on(V2, f"tg{uid}")["enable"] is True
    _set_traffic(V2, f"tg{uid}", 2 * GB)
    from app import access
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        access.collect_traffic(db)
    finally:
        db.close()
    _buy(client, uid)                                    # renewal: 3 left + 5 new
    me = _me(client, uid)
    assert me["bypass_limit"] - me["bypass_used"] == 8 * GB
