"""Source links, the check of the VPN itself, renewing in one tap."""

from __future__ import annotations

import base64
import socket
import time

import pytest

from conftest import Fake, tg
from test_remnawave import rw  # noqa: F401 — the fixture


def _bot(client, update):
    from app import notify
    return client.post("/api/bot/webhook", json=update,
                       headers={"X-Telegram-Bot-Api-Secret-Token": notify.webhook_secret()})


def _user(db, tg_id):
    from app.models import User
    db.expire_all()
    return db.query(User).filter(User.tg_id == tg_id).one()


# ---------------------------------------------------------------- source links
def test_source_links_count_people_and_money(client, admin, db):
    from app.models import Transaction
    r = client.post("/api/admin/sources", headers=admin, json={"name": "Ролик №5 в TikTok", "cost": 500})
    assert r.status_code == 200, r.text
    src = r.json()
    assert src["code"] == "rolik-5-v-tiktok"
    assert src["link"] == "https://t.me/shop_test_bot?start=src_rolik-5-v-tiktok"
    assert client.post("/api/admin/sources", headers=admin,
                       json={"name": "again", "code": src["code"]}).status_code == 400
    assert client.post("/api/admin/sources", headers=admin,
                       json={"name": "bad", "code": "Не латиница"}).status_code == 400
    client.post("/api/admin/sources", headers=admin, json={"name": "Блогер Петя", "code": "petya"})

    code = "src_" + src["code"]
    client.get("/api/app/me", headers=tg(7001, start=code))                 # the mini app
    _bot(client, {"update_id": 70001, "message": {"message_id": 1, "text": f"/start {code}",
                                                  "chat": {"id": 7002, "type": "private"},
                                                  "from": {"id": 7002, "first_name": "B"}}})
    client.get("/api/app/me", headers=tg(7003, start="src_unknown1"))      # a code nobody made
    client.get("/api/app/me", headers=tg(7004, start="src_BAD!!"))         # not a code
    client.get("/api/app/me", headers=tg(7005))                            # no label
    client.get("/api/app/me", headers=tg(7006, start="src_petya"))
    assert _user(db, 7001).source == src["code"] and _user(db, 7002).source == src["code"]
    assert _user(db, 7004).source == ""
    # First touch only: a known person coming through a link again is not re-labelled.
    client.get("/api/app/me", headers=tg(7005, start=code))
    assert _user(db, 7005).source == ""
    # One of them paid twice.
    uid = _user(db, 7001).id
    for amount in (199, 499):
        db.add(Transaction(user_id=uid, kind="purchase", amount=amount, status="success", method="stars"))
    db.commit()

    rows = {r["code"]: r for r in client.get("/api/admin/sources", headers=admin).json()["items"]}
    v = rows[src["code"]]
    assert (v["came"], v["paid"], v["revenue"], v["cost"], v["per_customer"]) == (2, 1, 698, 500, 500)
    assert v["conversion"] == 50 and v["known"] and v["name"] == "Ролик №5 в TikTok"
    assert rows["unknown1"]["came"] == 1 and rows["unknown1"]["known"] is False
    assert rows["_direct"]["came"] >= 2 and rows["petya"]["came"] == 1
    order = [r["code"] for r in client.get("/api/admin/sources", headers=admin).json()["items"]]
    assert order[0] == src["code"] and order[-1] in ("_direct", "_ref")     # by money, the rest last
    # Renaming keeps the code (links are already out); deleting keeps people's labels.
    client.put(f"/api/admin/sources/{src['id']}", headers=admin, json={"name": "Ролик 5", "cost": 700})
    client.delete(f"/api/admin/sources/{rows['petya']['id']}", headers=admin)
    rows = {r["code"]: r for r in client.get("/api/admin/sources", headers=admin).json()["items"]}
    assert rows[src["code"]]["name"] == "Ролик 5" and rows["petya"]["known"] is False
    assert _user(db, 7006).source == "petya"


def test_referrals_are_their_own_row(client, admin, db):
    client.get("/api/app/me", headers=tg(7101))
    inviter = _user(db, 7101)
    client.get("/api/app/me", headers=tg(7102, start=f"r{inviter.id}"))
    rows = {r["code"]: r for r in client.get("/api/admin/sources", headers=admin).json()["items"]}
    assert rows["_ref"]["came"] == 1


# ---------------------------------------------------------------- the VPN itself
def test_endpoints_are_read_from_keys():
    from app import vpncheck
    vm = base64.b64encode(b'{"add":"vm.example.com","port":"8443","net":"ws"}').decode()
    kcp = base64.b64encode(b'{"add":"vm.example.com","port":"8443","net":"kcp"}').decode()
    assert vpncheck.endpoint("vless://u@de.example.com:443?type=tcp&security=reality#a") == "de.example.com:443"
    assert vpncheck.endpoint("trojan://p@[2001:db8::1]:443#t") == "[2001:db8::1]:443"
    assert vpncheck.endpoint("vmess://" + vm) == "vm.example.com:8443"
    for udp in ("hysteria2://p@h:443#x", "tuic://u:p@h:443#x", "vless://u@h:443?type=kcp#x", "vmess://" + kcp,
                "vless://00000000-0000-0000-0000-000000000000@0.0.0.0:1?type=tcp#notice", "garbage"):
        assert vpncheck.endpoint(udp) is None, udp


class _Port:
    """A TCP port that answers or not, as the test says."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(16)

    def close(self):
        self.sock.close()

    def reopen(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", self.port))
        self.sock.listen(16)


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _names(client, uid):
    from urllib.parse import unquote
    sub = client.get("/api/app/me", headers=tg(uid)).json()["sub_url"]
    body = client.get("/sub/" + sub.rsplit("/sub/", 1)[1], headers={"X-HWID": f"h{uid}"}).text
    return [unquote(x.split("#", 1)[1]) for x in base64.b64decode(body).decode().splitlines()]


def test_dead_vpn_is_left_out_and_comes_back(client, admin, db, rw, monkeypatch):  # noqa: F811
    from app import access, vpncheck
    from app.models import Server
    # Two panels (the second is the same fake under another address: one
    # panel cannot be added twice).
    for name, url in (("Alpha", rw["url"]), ("Beta", rw["url"].replace("127.0.0.1", "localhost"))):
        r = client.post("/api/admin/servers", headers=admin, json={
            "name": name, "country": "FI", "kind": "remnawave", "url": url,
            "api_token": rw["token"], "inbound_ids": rw["squads"]})
        assert r.status_code == 200, r.text
    client.get("/api/app/me", headers=tg(7201))
    client.post("/api/admin/users/7201/action", headers=admin, json={"action": "extend", "days": 5})
    assert any("Alpha" in n for n in _names(client, 7201)) and any("Beta" in n for n in _names(client, 7201))
    db.expire_all()
    a, b = db.query(Server).order_by(Server.id).all()
    assert a.endpoints and ":" in a.endpoints                 # learned from the keys
    # From here on the test decides where the servers' VPN is; the fake
    # panel's keys point at an address that does not exist here.
    monkeypatch.setattr(vpncheck, "learn", lambda server, links: False)

    up, down = _Port(), _Port()
    try:
        a.endpoints, b.endpoints = f"127.0.0.1:{up.port}", f"127.0.0.1:{down.port}"
        db.commit()
        down.close()
        access._links_cache.clear()
        for i in range(vpncheck.FAILS_TO_DOWN):
            res = vpncheck.check(db)
        db.expire_all()
        assert res["down"] == ["Beta"] and db.get(Server, b.id).vpn_down_at > 0
        assert db.get(Server, a.id).vpn_ok_at > 0 and not db.get(Server, a.id).vpn_down_at
        assert any("VPN не отвечает" in m["text"] and "Beta" in m["text"]
                   for m in Fake.sent("sendMessage") if m["chat_id"] == 999)
        names = _names(client, 7201)
        assert any("Alpha" in n for n in names) and not any("Beta" in n for n in names)
        srv = {s["name"]: s for s in client.get("/api/admin/servers", headers=admin).json()}
        assert srv["Beta"]["vpn_down_at"] and srv["Beta"]["endpoints"] == f"127.0.0.1:{down.port}"

        # Everything down: more likely the shop's own network — nobody is left without keys.
        up.close()
        for _ in range(vpncheck.FAILS_TO_DOWN):
            vpncheck.check(db)
        names = _names(client, 7201)
        assert any("Alpha" in n for n in names) and any("Beta" in n for n in names)

        # Back up: back in the subscription, and the admins are told.
        up.reopen()
        down.reopen()
        res = vpncheck.check(db)
        assert sorted(res["up"]) == ["Alpha", "Beta"]
        assert any("снова работает" in m["text"] for m in Fake.sent("sendMessage") if m["chat_id"] == 999)

        # The owner can keep dead servers in the subscription.
        b2 = db.get(Server, b.id)
        b2.endpoints = f"127.0.0.1:{_closed_port()}"
        db.commit()
        for _ in range(vpncheck.FAILS_TO_DOWN):
            vpncheck.check(db)
        assert not any("Beta" in n for n in _names(client, 7201))
        client.post("/api/admin/settings", headers=admin, json={"vpn_autohide": "0"})
        assert any("Beta" in n for n in _names(client, 7201))
    finally:
        up.close()
        down.close()


# ---------------------------------------------------------------- renew in one tap
@pytest.fixture()
def expiring(client, admin, db, rw):  # noqa: F811
    from app.models import Plan
    client.post("/api/admin/servers", headers=admin, json={
        "name": "Finland", "country": "FI", "kind": "remnawave", "url": rw["url"],
        "api_token": rw["token"], "inbound_ids": rw["squads"]})
    client.get("/api/app/me", headers=tg(7401))
    plan = db.query(Plan).filter(Plan.is_trial.is_(False), Plan.days == 30).one()
    u = _user(db, 7401)
    u.plan_id, u.device_limit, u.expires_at = plan.id, 3, int(time.time()) + 86400 + 600
    db.commit()
    return plan


def test_reminder_carries_a_stars_invoice(client, db, expiring):
    from app import marketing
    from app.models import Transaction
    before = len(Fake.calls)
    assert marketing.run_triggers(db, only="expiring") == {"expiring": 1}
    calls = Fake.calls[before:]
    inv = [b for m, b in calls if m == "sendInvoice"]
    assert len(inv) == 1 and inv[0]["chat_id"] == 7401 and inv[0]["currency"] == "XTR"
    assert not [b for m, b in calls if m == "sendMessage" and b.get("chat_id") == 7401]
    tx = db.get(Transaction, int(inv[0]["payload"].split(":")[1]))
    assert tx.status == "pending" and tx.plan_id == expiring.id and tx.amount == expiring.price
    assert inv[0]["prices"][0]["amount"] == tx.stars
    pay = inv[0]["reply_markup"]["inline_keyboard"][0][0]
    assert pay["pay"] is True and "⭐" in pay["text"] and str(tx.stars) in pay["text"]
    assert "заканчивается" in inv[0]["description"] and "<" not in inv[0]["description"]
    # One tap: Telegram's two updates, and the subscription is renewed.
    old = _user(db, 7401).expires_at
    _bot(client, {"update_id": 74001, "pre_checkout_query": {
        "id": "pq", "from": {"id": 7401}, "currency": "XTR", "total_amount": tx.stars,
        "invoice_payload": f"tx:{tx.id}"}})
    assert [b for m, b in Fake.calls if m == "answerPreCheckoutQuery"][-1]["ok"] is True
    _bot(client, {"update_id": 74002, "message": {
        "message_id": 9, "chat": {"id": 7401, "type": "private"}, "from": {"id": 7401},
        "successful_payment": {"currency": "XTR", "total_amount": tx.stars, "invoice_payload": f"tx:{tx.id}",
                               "telegram_payment_charge_id": "ch-renew"}}})
    db.expire_all()
    assert db.get(Transaction, tx.id).status == "success"
    assert _user(db, 7401).expires_at >= old + 30 * 86400 - 5


def test_reminder_without_invoice_when_turned_off(client, admin, db, expiring):
    from app import marketing
    client.post("/api/admin/settings", headers=admin, json={"renew_invoice": "0"})
    before = len(Fake.calls)
    marketing.run_triggers(db, only="expiring")
    calls = Fake.calls[before:]
    assert not [1 for m, _ in calls if m == "sendInvoice"]
    assert [b for m, b in calls if m == "sendMessage" and b.get("chat_id") == 7401]
