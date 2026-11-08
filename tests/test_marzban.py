"""
A real Marzban panel next to a real 3x-ui 3.x panel.

Needs scripts/dev-3xui.sh and scripts/dev-marzban.sh (they print the env
vars, also saved in .dev/env.sh). Skipped otherwise.
"""

from __future__ import annotations

import base64
import os
import time
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from conftest import V3, tg

MZ = {"url": os.getenv("MARZBAN_TEST_URL", ""), "user": os.getenv("MARZBAN_TEST_USER", ""),
      "password": os.getenv("MARZBAN_TEST_PASS", ""), "inbounds": os.getenv("MARZBAN_TEST_INBOUNDS", "")}
pytestmark = pytest.mark.skipif(not (MZ["url"] and V3), reason="Marzban or 3x-ui test panels are not running")


def _mz():
    from app.providers import panel_for
    return panel_for(SimpleNamespace(id=4242, url=MZ["url"], username=MZ["user"], password=MZ["password"],
                                     api_token="", inbound_ids=MZ["inbounds"], sub_url="",
                                     verify_tls=True, api_version="", kind="marzban"))


def _mz_user(name: str) -> dict | None:
    return _mz()._request("GET", f"api/user/{name}", ok404=True)


@pytest.fixture()
def uid():
    return int(time.time() * 1000) % 10_000_000 + 3_000_000


@pytest.fixture()
def mixed(client, admin, env, monkeypatch):
    """A Marzban server and a 3x-ui server, orders free (test mode)."""
    monkeypatch.setenv("PAYMENTS_TEST_MODE", "1")
    env.__init__()
    ids = []
    for body in ({"name": "Netherlands", "country": "NL", "kind": "marzban", "url": MZ["url"] + "dashboard/",
                  "username": MZ["user"], "password": MZ["password"], "inbound_ids": MZ["inbounds"]},
                 {"name": "Germany", "country": "DE", "url": V3["url"], "api_token": V3["token"],
                  "inbound_ids": V3["inbound"]}):
        r = client.post("/api/admin/servers", headers=admin, json=body)
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    return ids


def test_marzban_is_detected_and_lists_inbound_tags(client, admin):
    body = {"name": "x", "kind": "marzban", "url": MZ["url"], "username": MZ["user"],
            "password": MZ["password"]}
    r = client.post("/api/admin/servers/test", headers=admin, json=body).json()
    assert r["ok"] and r["version"].startswith("m0.8")
    assert {i["id"] for i in r["inbounds"]} >= set(MZ["inbounds"].split(","))
    bad = client.post("/api/admin/servers/test", headers=admin, json={**body, "password": "wrong"}).json()
    assert not bad["ok"] and "login failed" in bad["error"]
    # The address is stored without the dashboard part.
    s = client.post("/api/admin/servers", headers=admin, json={**body, "url": MZ["url"] + "dashboard/#/"}).json()
    assert s["url"] == MZ["url"] and s["kind"] == "marzban"


def test_purchase_on_marzban_and_3xui_in_one_subscription(client, mixed, uid):
    plan = client.get("/api/app/config").json()["plans"][0]
    r = client.post("/api/app/order", json={"plan_id": plan["id"]}, headers=tg(uid))
    assert r.json()["status"] == "success", r.text
    me = client.get("/api/app/me", headers=tg(uid)).json()
    u = _mz_user(f"tg{uid}")
    assert u and u["status"] == "active" and abs(u["expire"] - me["expires_at"]) <= 2
    body = client.get(me["sub_url"].replace("https://shop.example.com", ""),
                      headers={"User-Agent": "Happ/3", "X-HWID": "p1"}).text
    links = base64.b64decode(body).decode().splitlines()
    names = [unquote(link.rsplit("#", 1)[1]) for link in links]
    assert sum(n.startswith("🇳🇱 Netherlands") for n in names) == 2      # VLESS + Shadowsocks
    assert sum(n.startswith("🇩🇪 Germany") for n in names) == 1


def test_block_reissue_and_renewal_reach_marzban(client, admin, mixed, uid):
    plan = client.get("/api/app/config").json()["plans"][0]
    client.post("/api/app/order", json={"plan_id": plan["id"]}, headers=tg(uid))
    old_id = _mz_user(f"tg{uid}")["proxies"]["vless"]["id"]
    client.post(f"/api/admin/users/{uid}/action", headers=admin, json={"action": "block"})
    assert _mz_user(f"tg{uid}")["status"] == "disabled"
    client.post(f"/api/admin/users/{uid}/action", headers=admin, json={"action": "unblock"})
    assert _mz_user(f"tg{uid}")["status"] == "active"
    r = client.post("/api/app/reissue", headers=tg(uid))
    assert r.status_code == 200
    assert _mz_user(f"tg{uid}")["proxies"]["vless"]["id"] != old_id
    before = _mz_user(f"tg{uid}")["expire"]
    client.post("/api/app/order", json={"plan_id": plan["id"]}, headers=tg(uid))
    assert _mz_user(f"tg{uid}")["expire"] - before == plan["days"] * 86400


def test_traffic_and_new_server_sync_with_marzban(client, admin, env, monkeypatch, uid, db):
    """A customer bought before the Marzban server existed; pressing Sync
    puts them there. The traffic job reads Marzban without errors."""
    from app import access
    monkeypatch.setenv("PAYMENTS_TEST_MODE", "1")
    env.__init__()
    client.post("/api/admin/servers", headers=admin, json={
        "name": "Germany", "url": V3["url"], "api_token": V3["token"], "inbound_ids": V3["inbound"]})
    plan = client.get("/api/app/config").json()["plans"][0]
    client.post("/api/app/order", json={"plan_id": plan["id"]}, headers=tg(uid))
    assert _mz_user(f"tg{uid}") is None
    s = client.post("/api/admin/servers", headers=admin, json={
        "name": "NL", "kind": "marzban", "url": MZ["url"], "username": MZ["user"],
        "password": MZ["password"], "inbound_ids": MZ["inbounds"]}).json()
    client.post(f"/api/admin/servers/{s['id']}/sync", headers=admin)
    for _ in range(50):
        if not client.get(f"/api/admin/servers/{s['id']}/sync", headers=admin).json()["running"]:
            break
        time.sleep(0.2)
    assert _mz_user(f"tg{uid}")["status"] == "active"
    res = access.collect_traffic(db)
    assert isinstance(res["changed"], dict)
    assert client.post(f"/api/admin/servers/{s['id']}/check", headers=admin).json()["ok"]
