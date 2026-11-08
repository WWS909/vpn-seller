"""
End-to-end tests against REAL 3x-ui panels — a 3.x one (API token) and a
2.x one (login + password) at the same time, the way a shop with two
servers works. Start them with scripts/dev-3xui.sh.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from conftest import V2, V3, Fake, panels, tg

pytestmark = panels


def _panel(p: dict, inbound: str):
    from app.providers import XuiPanel
    return XuiPanel(SimpleNamespace(id=hash(p["url"]) % 10000 + 50000, url=p["url"],
                                    username=p["user"], password=p["password"],
                                    api_token=p["token"], inbound_ids=inbound,
                                    sub_url=p["sub"], verify_tls=True, api_version=""))


def _client_on(p: dict, email: str) -> dict | None:
    """What the panel itself stores for this email."""
    panel = _panel(p, p["inbound"])
    if panel.version() == "3":
        data = panel._request("GET", f"panel/api/clients/get/{email}")
        return (data.get("obj") or {}).get("client") if data.get("success") else None
    ib = panel._ok("GET", f"panel/api/inbounds/get/{p['inbound']}")
    clients = json.loads(ib["settings"]).get("clients", [])
    return next((c for c in clients if c["email"] == email), None)


def _set_traffic(p: dict, email: str, down: int) -> None:
    panel = _panel(p, p["inbound"])
    path = ("panel/api/clients/updateTraffic/" if panel.version() == "3"
            else "panel/api/inbounds/updateClientTraffic/")
    panel._ok("POST", path + email, json={"upload": 0, "download": down})


def _sub(client, url: str, hwid: str = "", model: str = "") -> tuple[list[str], dict]:
    headers = {"User-Agent": "Happ/3.1"}
    if hwid:
        headers.update({"X-HWID": hwid, "X-Device-Model": model or hwid, "X-Device-OS": "Android"})
    r = client.get(url.replace("https://shop.example.com", ""), headers=headers)
    assert r.status_code == 200, r.text
    links = base64.b64decode(r.text).decode().splitlines()
    return links, dict(r.headers)


@pytest.fixture()
def uid():
    # A fresh Telegram id per test so panels never see leftovers.
    return int(time.time() * 1000) % 10_000_000 + 1_000_000


@pytest.fixture()
def test_mode(env, monkeypatch):
    monkeypatch.setenv("PAYMENTS_TEST_MODE", "1")
    env.__init__()


def _buy(client, uid, **extra):
    plan = client.get("/api/app/config").json()["plans"][0]
    body = {"plan_id": plan["id"], "method": "stars", **extra}
    r = client.post("/api/app/order", json=body, headers=tg(uid))
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
def test_servers_are_detected(client, admin, servers):
    for sid, version in zip(servers, ("3", "2")):
        r = client.post(f"/api/admin/servers/{sid}/check", headers=admin).json()
        assert r["ok"], r
        assert r["version"] == version
        assert any(i["protocol"] == "vless" for i in r["inbounds"])


def test_wrong_panel_credentials_are_reported_plainly(client, admin):
    r = client.post("/api/admin/servers/test", headers=admin, json={
        "name": "x", "url": V2["url"], "username": V2["user"], "password": "wrong",
        "inbound_ids": V2["inbound"]}).json()
    assert not r["ok"] and "login failed" in r["error"]
    r = client.post("/api/admin/servers/test", headers=admin, json={
        "name": "x", "url": V3["url"], "api_token": "not-a-token", "inbound_ids": "1"}).json()
    assert not r["ok"] and "token rejected" in r["error"]
    r = client.post("/api/admin/servers/test", headers=admin, json={
        "name": "x", "url": "http://127.0.0.1:1/", "username": "a", "password": "b"}).json()
    assert not r["ok"] and "unreachable" in r["error"]


def test_purchase_creates_the_client_on_both_panels(client, servers, test_mode, uid):
    order = _buy(client, uid)
    assert order["status"] == "success" and order["pay"]["type"] == "done"
    me = client.get("/api/app/me", headers=tg(uid)).json()
    assert me["active"] and me["device_limit"] == 3
    email = f"tg{uid}"
    for p in (V3, V2):
        c = _client_on(p, email)
        assert c, f"no client on {p['url']}"
        assert c["enable"] is True
        assert abs(int(c["expiryTime"]) // 1000 - me["expires_at"]) <= 1
        assert c["subId"]


def test_one_subscription_link_with_both_servers(client, servers, test_mode, uid):
    _buy(client, uid)
    me = client.get("/api/app/me", headers=tg(uid)).json()
    links, headers = _sub(client, me["sub_url"], hwid="phone-1")
    assert len(links) == 2
    names = [unquote(link.split("#", 1)[1]) for link in links]
    assert names == ["🇩🇪 Germany", "🇫🇮 Finland"]
    assert all(link.startswith("vless://") for link in links)
    info = headers["subscription-userinfo"]
    assert f"expire={me['expires_at']}" in info
    assert base64.b64decode(headers["profile-title"].split(":", 1)[1]).decode() == "VPN"


def test_renewal_adds_time_and_updates_panels(client, servers, test_mode, uid):
    _buy(client, uid)
    first = client.get("/api/app/me", headers=tg(uid)).json()["expires_at"]
    _buy(client, uid)
    second = client.get("/api/app/me", headers=tg(uid)).json()["expires_at"]
    assert second - first == 30 * 86400
    for p in (V3, V2):
        assert abs(int(_client_on(p, f"tg{uid}")["expiryTime"]) // 1000 - second) <= 1


def test_device_limit_via_subscription(client, servers, test_mode, uid):
    _buy(client, uid)                       # 3 devices
    me = client.get("/api/app/me", headers=tg(uid)).json()
    for n in range(3):
        links, _ = _sub(client, me["sub_url"], hwid=f"dev-{n}", model=f"Pixel {n}")
        assert len(links) == 2
    links, _ = _sub(client, me["sub_url"], hwid="dev-4")
    assert len(links) == 1 and "0.0.0.0:1" in links[0]           # a notice, not a server
    assert "Лимит устройств" in unquote(links[0])
    # A known device keeps working.
    assert len(_sub(client, me["sub_url"], hwid="dev-0", model="Pixel 0")[0]) == 2

    devices = client.get("/api/app/me", headers=tg(uid)).json()["devices"]
    assert {d["model"] for d in devices} == {"Pixel 0", "Pixel 1", "Pixel 2"}
    r = client.post(f"/api/app/devices/{devices[0]['id']}/forget", headers=tg(uid))
    assert r.status_code == 200 and len(r.json()["devices"]) == 2
    assert len(_sub(client, me["sub_url"], hwid="dev-4")[0]) == 2


def test_buying_extra_devices(client, servers, test_mode, uid):
    _buy(client, uid)
    r = client.post("/api/app/order", json={"kind": "devices", "qty": 2, "method": "stars"},
                    headers=tg(uid))
    assert r.json()["amount"] == 100 and r.json()["status"] == "success"
    assert client.get("/api/app/me", headers=tg(uid)).json()["device_limit"] == 5


def test_traffic_limit_blocks_and_top_up_unblocks(client, admin, servers, test_mode, uid, db):
    client.post("/api/admin/settings", headers=admin, json={"traffic_topup": "1"})   # off by default
    plans = client.get("/api/admin/plans", headers=admin).json()
    body = {k: plans[0][k] for k in ("name", "name_en", "days", "price", "devices")}
    plan = client.post("/api/admin/plans", headers=admin,
                       json={**body, "name": "Лайт", "traffic_gb": 1}).json()
    r = client.post("/api/app/order", json={"plan_id": plan["id"], "method": "stars"},
                    headers=tg(uid))
    assert r.status_code == 200
    email = f"tg{uid}"
    assert int(_client_on(V3, email)["totalGB"]) == 1024 ** 3

    # 0.6 GB on one server and 0.6 GB on the other: each panel alone is
    # under its limit, together they are over.
    _set_traffic(V3, email, int(0.6 * 1024 ** 3))
    _set_traffic(V2, email, int(0.6 * 1024 ** 3))
    from app import access
    res = access.collect_traffic(db)
    assert uid in res["over"]
    me = client.get("/api/app/me", headers=tg(uid)).json()
    assert me["traffic_blocked"] and not me["active"]
    assert _client_on(V3, email)["enable"] is False
    links, _ = _sub(client, me["sub_url"])
    assert len(links) == 1 and "0.0.0.0:1" in links[0]

    r = client.post("/api/app/order", json={"kind": "traffic", "gb": 10, "method": "stars"},
                    headers=tg(uid))
    assert r.status_code == 200 and r.json()["amount"] == 99
    me = client.get("/api/app/me", headers=tg(uid)).json()
    assert me["active"] and me["traffic_limit"] == 11 * 1024 ** 3
    assert _client_on(V3, email)["enable"] is True
    assert _client_on(V2, email)["enable"] is True


def test_one_server_down_does_not_stop_a_sale(client, admin, servers, test_mode, uid, db):
    """Access on the working server is enough; the broken one is retried."""
    client.put(f"/api/admin/servers/{servers[1]}", headers=admin, json={
        "name": "Finland", "country": "FI", "url": "http://127.0.0.1:1/", "username": "a",
        "password": "b", "inbound_ids": V2["inbound"]})
    order = _buy(client, uid)
    assert order["status"] == "success"
    from app.models import User
    user = db.query(User).filter(User.tg_id == uid).one()
    db.refresh(user)
    assert "Finland" in user.sync_error
    assert _client_on(V3, f"tg{uid}")


def test_all_servers_down_keeps_money_and_retries(client, admin, servers, env, monkeypatch, uid, db):
    for port, sid in enumerate(servers, start=1):             # two dead panels
        r = client.put(f"/api/admin/servers/{sid}", headers=admin, json={
            "name": "x", "url": f"http://127.0.0.1:{port}/", "username": "a", "password": "b",
            "inbound_ids": "1"})
        assert r.status_code == 200, r.text
    monkeypatch.setenv("CRYPTOBOT_TOKEN", "1:crypto")
    env.__init__()
    plan = client.get("/api/app/config").json()["plans"][0]
    order = client.post("/api/app/order", json={"plan_id": plan["id"], "method": "crypto"},
                        headers=tg(uid)).json()
    assert order["pay"]["type"] == "url"
    raw = json.dumps({"update_type": "invoice_paid", "payload": {
        "invoice_id": 777, "status": "paid", "currency_type": "fiat", "fiat": "RUB",
        "amount": "199.00", "payload": str(order["order_id"])}}).encode()
    sig = hmac.new(hashlib.sha256(b"1:crypto").digest(), raw, hashlib.sha256).hexdigest()
    r = client.post("/api/pay/cryptobot", content=raw, headers={"crypto-pay-api-signature": sig})
    assert r.status_code == 503                      # CryptoBot will retry
    from app.models import Transaction, User
    tx = db.get(Transaction, order["order_id"])
    db.refresh(tx)
    assert tx.status == "paid"
    user = db.query(User).filter(User.tg_id == uid).one()
    db.refresh(user)
    assert user.expires_at == 0, "no access must be recorded while no server accepted it"

    # Servers are back — the retry job grants the order.
    for sid, p, name in zip(servers, (V3, V2), ("Germany", "Finland")):
        client.put(f"/api/admin/servers/{sid}", headers=admin, json={
            "name": name, "url": p["url"], "username": p["user"], "password": p["password"],
            "api_token": p["token"], "inbound_ids": p["inbound"], "sub_url": p["sub"]})
    from app import billing
    assert billing.retry_paid(db) == 1
    assert client.get("/api/app/me", headers=tg(uid)).json()["active"]
    # A late duplicate of the notification changes nothing.
    r = client.post("/api/pay/cryptobot", content=raw, headers={"crypto-pay-api-signature": sig})
    assert r.status_code == 200
    assert db.query(Transaction).filter(Transaction.status == "success").count() == 1


def test_gift_bought_by_one_redeemed_by_another(client, servers, test_mode, uid):
    plan = client.get("/api/app/config").json()["plans"][0]
    r = client.post("/api/app/order", json={"kind": "gift", "plan_id": plan["id"],
                                            "method": "stars"}, headers=tg(uid))
    order = r.json()
    status = client.get(f"/api/app/order/{order['order_id']}", headers=tg(uid)).json()
    code = status["gift"]["code"]
    assert status["gift"]["link"].startswith("https://t.me/shop_test_bot?start=gift")
    assert not client.get("/api/app/me", headers=tg(uid)).json()["active"]

    friend = uid + 1
    r = client.post("/api/app/gift/redeem", json={"code": code.replace("-", "").lower()},
                    headers=tg(friend))
    assert r.status_code == 200, r.text
    assert client.get("/api/app/me", headers=tg(friend)).json()["active"]
    assert _client_on(V3, f"tg{friend}")
    r = client.post("/api/app/gift/redeem", json={"code": code}, headers=tg(friend + 1))
    assert r.status_code == 400


def test_promo_days_and_one_use_per_person(client, admin, servers, test_mode, uid):
    client.post("/api/admin/promos", headers=admin,
                json={"code": "WEEK", "kind": "days", "value": 7})
    _buy(client, uid, promo="week")
    me = client.get("/api/app/me", headers=tg(uid)).json()
    assert me["expires_at"] - int(time.time()) > 36 * 86400
    r = client.post("/api/app/order", json={"plan_id": me["plan"]["id"], "method": "stars",
                                            "promo": "WEEK"}, headers=tg(uid))
    assert r.status_code == 400


def test_trial_only_once(client, admin, servers, test_mode, uid):
    r = client.put("/api/admin/trial", headers=admin,
                   json={"enabled": True, "days": 5, "devices": 2, "traffic_gb": 7})
    assert r.json() == {"enabled": True, "days": 5, "devices": 2, "traffic_gb": 7}
    trial = next(p for p in client.get("/api/app/config").json()["plans"] if p["is_trial"])
    r = client.post("/api/app/order", json={"plan_id": trial["id"]}, headers=tg(uid))
    assert r.json()["status"] == "success"
    me = client.get("/api/app/me", headers=tg(uid)).json()
    assert me["device_limit"] == 2 and me["traffic_limit"] == 7 * 1024 ** 3
    assert 4 * 86400 < me["expires_at"] - time.time() <= 5 * 86400
    assert client.get("/api/admin/trial", headers=admin).json()["used"] == 1
    r = client.post("/api/app/order", json={"plan_id": trial["id"]}, headers=tg(uid))
    assert r.status_code == 400


def test_admin_block_disables_on_panels(client, admin, servers, test_mode, uid):
    _buy(client, uid)
    r = client.post(f"/api/admin/users/{uid}/action", headers=admin, json={"action": "block"})
    assert r.json()["sync"]["ok"] == 2
    assert _client_on(V3, f"tg{uid}")["enable"] is False
    assert _client_on(V2, f"tg{uid}")["enable"] is False
    me = client.get("/api/app/me", headers=tg(uid)).json()
    assert "Доступ заблокирован" in unquote(_sub(client, me["sub_url"])[0][0])
    client.post(f"/api/admin/users/{uid}/action", headers=admin, json={"action": "unblock"})
    assert _client_on(V3, f"tg{uid}")["enable"] is True


def test_reissue_changes_keys_and_link(client, servers, test_mode, uid):
    _buy(client, uid)
    old = client.get("/api/app/me", headers=tg(uid)).json()
    old_links, _ = _sub(client, old["sub_url"])
    r = client.post("/api/app/reissue", headers=tg(uid))
    assert r.status_code == 200
    new_url = r.json()["sub_url"]
    assert new_url != old["sub_url"]
    assert client.get(old["sub_url"].replace("https://shop.example.com", "")).status_code == 404
    new_links, _ = _sub(client, new_url)
    assert new_links[0].split("@")[0] != old_links[0].split("@")[0]


def test_new_server_gets_existing_customers(client, admin, test_mode, uid):
    """Adding a server and pressing Sync puts every current customer on it."""
    client.post("/api/admin/servers", headers=admin, json={
        "name": "Germany", "country": "DE", "url": V3["url"], "api_token": V3["token"],
        "inbound_ids": V3["inbound"]}).json()
    _buy(client, uid)
    s2 = client.post("/api/admin/servers", headers=admin, json={
        "name": "Finland", "country": "FI", "url": V2["url"], "username": V2["user"],
        "password": V2["password"], "inbound_ids": V2["inbound"], "sub_url": V2["sub"]}).json()
    assert _client_on(V2, f"tg{uid}") is None             # bought before the server existed
    r = client.post(f"/api/admin/servers/{s2['id']}/sync", headers=admin).json()
    assert r["running"] is True                          # runs in the background
    import time
    for _ in range(100):
        r = client.get(f"/api/admin/servers/{s2['id']}/sync", headers=admin).json()
        if not r["running"]:
            break
        time.sleep(0.2)
    assert not r["running"] and r["done"] >= 1 and r["done"] == r["total"] and not r["errors"]
    assert _client_on(V2, f"tg{uid}")["enable"] is True


def test_background_jobs_on_live_panels(client, admin, servers, test_mode, uid, db):
    """The scheduled jobs, run once each, against the real panels."""
    from app import jobs
    from app.models import Broadcast, Server, User
    _buy(client, uid)
    # health: versions and client counts come from the panels themselves
    jobs.job_health(db)
    rows = db.query(Server).order_by(Server.id).all()
    assert [r.api_version for r in rows] == ["3", "2"]
    assert all(r.last_ok_at and not r.last_error for r in rows)

    # reminder: the subscription ends in a day → one message, not two
    user = db.query(User).filter(User.tg_id == uid).one()
    user.expires_at = int(time.time()) + 86400
    db.commit()
    Fake.calls.clear()
    jobs.job_warm(db)
    jobs.job_warm(db)
    # It comes as a Stars invoice for the same plan: renewing is one tap.
    reminders = [m for m in Fake.sent("sendInvoice") if m["chat_id"] == uid]
    assert len(reminders) == 1 and "заканчивается" in reminders[0]["description"]
    assert not [m for m in Fake.sent("sendMessage") if m["chat_id"] == uid]

    # broadcast: goes out in portions and finishes
    r = client.post("/api/admin/broadcasts", headers=admin,
                    json={"text": "Новые серверы", "audience": "active"})
    assert r.json()["total"] >= 1
    for _ in range(5):
        jobs.job_broadcast(db)
    cast = db.query(Broadcast).one()
    db.refresh(cast)
    assert cast.status == "done" and cast.sent >= 1

    # traffic: collected from both panels in one request each
    _set_traffic(V3, f"tg{uid}", 3 * 1024 ** 2)
    _set_traffic(V2, f"tg{uid}", 2 * 1024 ** 2)
    jobs.job_traffic(db)
    db.refresh(user)
    assert user.traffic_used == 5 * 1024 ** 2


def test_referral_rewards_both_sides_once(client, admin, servers, test_mode, uid, db):
    """The friend's first payment: +3 days to them, +7 to the inviter (who
    never bought and so gets access at all). A second payment gives nothing
    more unless "every payment" is on."""
    from app.models import User
    client.get("/api/app/me", headers=tg(uid))
    inviter = db.query(User).filter(User.tg_id == uid).one()
    friend = uid + 1
    client.get("/api/app/me", headers=tg(friend, start=f"r{inviter.id}"))
    plan = client.get("/api/app/config").json()["plans"][0]
    r = client.post("/api/app/order", json={"plan_id": plan["id"]}, headers=tg(friend))
    assert r.json()["status"] == "success"
    me = client.get("/api/app/me", headers=tg(friend)).json()
    assert (plan["days"] + 3 - 1) * 86400 < me["expires_at"] - time.time() <= (plan["days"] + 3) * 86400
    inv = client.get("/api/app/me", headers=tg(uid)).json()
    assert inv["active"] and 6 * 86400 < inv["expires_at"] - time.time() <= 7 * 86400
    assert inv["device_limit"] == 1
    assert _client_on(V3, f"tg{uid}")["enable"] is True          # the inviter really got access
    assert any(m["chat_id"] == uid and "+7" in m["text"] for m in Fake.sent("sendMessage"))
    stats = client.get("/api/app/referrals", headers=tg(uid)).json()
    assert (stats["invited"], stats["paid"], stats["days"]) == (1, 1, 7)
    client.post("/api/app/order", json={"plan_id": plan["id"]}, headers=tg(friend))
    assert client.get("/api/app/referrals", headers=tg(uid)).json()["days"] == 7


def test_sale_price_is_charged_and_offer_is_spent(client, admin, servers, test_mode, uid, db):
    from app import marketing
    from app.models import Transaction, User
    client.get("/api/app/me", headers=tg(uid))
    user = db.query(User).filter(User.tg_id == uid).one()
    offer = marketing.give_offer(db, user, 40, 2, "test")
    plan = client.get("/api/app/config").json()["plans"][0]
    r = client.post("/api/app/order", json={"plan_id": plan["id"]}, headers=tg(uid)).json()
    tx = db.get(Transaction, r["order_id"])
    assert tx.amount == int(plan["price"] * 0.6) and tx.discount == 40 and tx.offer_id == offer.id
    db.refresh(offer)
    assert offer.used_at and offer.tx_id == tx.id
    assert client.get("/api/app/me", headers=tg(uid)).json()["offer"] is None
