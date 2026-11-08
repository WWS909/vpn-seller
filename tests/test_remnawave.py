"""
Remnawave panels.

By default these run against tests/fake_remnawave.py — a stand-in written
from the Remnawave 3.4 API contract, started on a local port. With a real
panel, set

    REMNAWAVE_TEST_URL     https://panel.example.com/
    REMNAWAVE_TEST_TOKEN   an API token from Remnawave → API tokens
    REMNAWAVE_TEST_SQUAD   the uuid of an internal squad to give out

and the same tests run against it (the traffic test needs the stand-in).
"""

from __future__ import annotations

import base64
import socket
import threading
import time
from types import SimpleNamespace
from urllib.parse import unquote

import httpx
import pytest

from conftest import tg

import os

LIVE = {"url": os.getenv("REMNAWAVE_TEST_URL", ""), "token": os.getenv("REMNAWAVE_TEST_TOKEN", ""),
        "squad": os.getenv("REMNAWAVE_TEST_SQUAD", "")}
SQUAD = "0b1c7d8e-1111-4a2b-9c3d-000000000001"
SQUAD_SS = "0b1c7d8e-2222-4a2b-9c3d-000000000002"


@pytest.fixture(scope="module")
def rw():
    if LIVE["url"]:
        yield {"url": LIVE["url"], "token": LIVE["token"], "squads": LIVE["squad"], "fake": None}
        return
    import uvicorn

    from fake_remnawave import create_app
    app = create_app("secret-token")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield {"url": f"http://127.0.0.1:{port}/", "token": "secret-token",
           "squads": f"{SQUAD},{SQUAD_SS}", "fake": app}
    server.should_exit = True
    th.join(5)


def _panel(rw, **kw):
    from app.providers import panel_for
    return panel_for(SimpleNamespace(**{"id": 5151, "url": rw["url"], "username": "", "password": "",
                                        "api_token": rw["token"], "inbound_ids": rw["squads"], "sub_url": "",
                                        "verify_tls": True, "api_version": "", "kind": "remnawave", **kw}))


def _user(rw, name):
    return _panel(rw)._get(name)


@pytest.fixture()
def uid():
    return int(time.time() * 1000) % 10_000_000 + 5_000_000


@pytest.fixture()
def shop(client, admin, env, monkeypatch, rw):
    monkeypatch.setenv("PAYMENTS_TEST_MODE", "1")
    env.__init__()
    r = client.post("/api/admin/servers", headers=admin, json={
        "name": "Finland", "country": "FI", "kind": "remnawave", "url": rw["url"] + "dashboard",
        "api_token": rw["token"], "inbound_ids": rw["squads"]})
    assert r.status_code == 200, r.text
    return r.json()


def test_address_and_username_rules():
    from app.providers import normalize_url
    from app.providers.remnawave import username_of
    for raw in ("panel.example.com", "https://panel.example.com/dashboard", "https://panel.example.com/api/",
                "https://panel.example.com/auth/login", "https://panel.example.com/users#x"):
        assert normalize_url(raw, "remnawave") == "https://panel.example.com/", raw
    assert normalize_url("https://h.example.com/panel/", "remnawave") == "https://h.example.com/panel/"
    assert username_of("tg123456") == "tg123456"
    assert username_of("tg1.2@x") == "tg1_2_x"
    assert username_of("a") == "a__"


def test_detected_squads_listed_and_bad_token_explained(client, admin, rw):
    body = {"name": "x", "kind": "remnawave", "url": rw["url"], "api_token": rw["token"]}
    r = client.post("/api/admin/servers/test", headers=admin, json=body).json()
    assert r["ok"], r
    assert r["kind"] == "remnawave" and r["version"].startswith("r")
    assert set(rw["squads"].split(",")) <= {i["id"] for i in r["inbounds"]}
    bad = client.post("/api/admin/servers/test", headers=admin, json={**body, "api_token": "nope"}).json()
    assert not bad["ok"] and "API token" in bad["error"]


def test_login_by_password_when_no_token(rw):
    if not rw["fake"]:
        pytest.skip("the stand-in knows the admin password")
    p = _panel(rw, api_token="", username="admin", password="Admin-pass-1")
    assert p.test()["kind"] == "remnawave"
    from app.providers import PanelError
    with pytest.raises(PanelError, match="login failed"):
        _panel(rw, id=5152, api_token="", username="admin", password="wrong").test()


def test_panel_refuses_requests_without_proxy_headers(rw):
    """Why the client adds X-Forwarded-*: without them Remnawave drops the request."""
    if not rw["fake"]:
        pytest.skip("depends on how the real panel is exposed")
    r = httpx.get(rw["url"] + "api/system/stats", headers={"Authorization": "Bearer " + rw["token"]})
    assert r.status_code == 400


def test_purchase_creates_user_with_squads_and_links(client, shop, rw, uid):
    plan = client.get("/api/app/config").json()["plans"][0]
    r = client.post("/api/app/order", json={"plan_id": plan["id"]}, headers=tg(uid))
    assert r.json()["status"] == "success", r.text
    me = client.get("/api/app/me", headers=tg(uid)).json()
    u = _user(rw, f"tg{uid}")
    assert u and u["status"] == "ACTIVE"
    assert {s["uuid"] for s in u["activeInternalSquads"]} == set(rw["squads"].split(","))
    assert u["telegramId"] == uid
    expire = time.mktime(time.strptime(u["expireAt"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    assert abs(expire - me["expires_at"]) <= 2
    body = client.get(me["sub_url"].replace("https://shop.example.com", ""),
                      headers={"User-Agent": "Happ/3", "X-HWID": "rw1"}).text
    links = base64.b64decode(body).decode().splitlines()
    names = [unquote(link.rsplit("#", 1)[1]) for link in links]
    if rw["fake"]:
        assert len(links) == 3 and all(n.startswith("🇫🇮 Finland") for n in names)
        assert {link.split("://")[0] for link in links} == {"vless", "trojan", "ss"}
    else:
        assert links


def test_block_reissue_renewal_and_delete(client, admin, shop, rw, uid):
    plan = client.get("/api/app/config").json()["plans"][0]
    client.post("/api/app/order", json={"plan_id": plan["id"]}, headers=tg(uid))
    name = f"tg{uid}"
    old = _user(rw, name)
    client.post(f"/api/admin/users/{uid}/action", headers=admin, json={"action": "block"})
    assert _user(rw, name)["status"] == "DISABLED"
    client.post(f"/api/admin/users/{uid}/action", headers=admin, json={"action": "unblock"})
    assert _user(rw, name)["status"] == "ACTIVE"

    assert client.post("/api/app/reissue", headers=tg(uid)).status_code == 200
    after = _user(rw, name)
    assert after["vlessUuid"] != old["vlessUuid"] and after["trojanPassword"] != old["trojanPassword"]
    # A later sync does not revoke again.
    client.post(f"/api/admin/users/{uid}/action", headers=admin, json={"action": "sync"})
    assert _user(rw, name)["vlessUuid"] == after["vlessUuid"]

    before = time.mktime(time.strptime(after["expireAt"][:19], "%Y-%m-%dT%H:%M:%S"))
    client.post("/api/app/order", json={"plan_id": plan["id"]}, headers=tg(uid))
    now_exp = time.mktime(time.strptime(_user(rw, name)["expireAt"][:19], "%Y-%m-%dT%H:%M:%S"))
    assert round(now_exp - before) == plan["days"] * 86400

    from app import access
    from app.db import SessionLocal
    from app.models import User
    s = SessionLocal()
    try:
        access.remove(s, s.query(User).filter(User.tg_id == uid).one())
    finally:
        s.close()
    assert _user(rw, name) is None


def test_expired_customer_is_disabled_not_refused(rw, uid):
    """Remnawave refuses an expiry date in the past; the shop disables instead."""
    from app.providers import ClientSpec
    import uuid
    p = _panel(rw)
    spec = ClientSpec(email=f"tg{uid}", uuid=str(uuid.uuid4()), sub_id="x", tg_id=uid,
                      expiry_ms=int((time.time() + 86400) * 1000), total_bytes=0, limit_ip=0, enable=True)
    p.ensure(spec)
    spec.expiry_ms = int((time.time() - 3600) * 1000)
    p.ensure(spec)
    assert _user(rw, f"tg{uid}")["status"] == "DISABLED"
    spec.expiry_ms = 0                      # "forever"
    spec.enable = True
    p.ensure(spec)
    u = _user(rw, f"tg{uid}")
    assert u["status"] == "ACTIVE" and u["expireAt"].startswith("2099")
    p.delete(spec)


def test_unknown_squad_is_a_clear_error(rw, uid):
    from app.providers import ClientSpec, PanelError
    import uuid
    p = _panel(rw, id=5153, inbound_ids="00000000-0000-4000-8000-000000000000")
    spec = ClientSpec(email=f"tg{uid}", uuid=str(uuid.uuid4()), sub_id="x", tg_id=uid,
                      expiry_ms=0, total_bytes=0, limit_ip=0, enable=True)
    with pytest.raises(PanelError, match="no such internal squads"):
        p.ensure(spec)


def test_traffic_reaches_the_shop_and_limit_blocks(client, admin, shop, rw, uid, db):
    if not rw["fake"]:
        pytest.skip("needs the stand-in to fake traffic")
    from app import access
    from app.models import GB, User
    plan = client.get("/api/app/config").json()["plans"][0]
    client.post("/api/app/order", json={"plan_id": plan["id"]}, headers=tg(uid))
    user = db.query(User).filter(User.tg_id == uid).one()
    user.traffic_limit = 5 * GB
    db.commit()
    access.push(db, user)
    assert _user(rw, f"tg{uid}")["trafficLimitBytes"] == 5 * GB
    httpx.post(rw["url"] + "_test/traffic", json={"username": f"tg{uid}", "bytes": 6 * GB})
    res = access.collect_traffic(db)
    assert res["changed"].get(uid) == 6 * GB and uid in res["over"]
    assert _user(rw, f"tg{uid}")["status"] == "DISABLED"
    assert client.post(f"/api/admin/servers/{shop['id']}/check", headers=admin).json()["ok"]
