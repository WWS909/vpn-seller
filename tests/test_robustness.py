"""
Panels, background jobs, reminders: cases a review found going wrong.

A slow or dead panel, one panel added twice, reminders that never reached
people after the first 300, an alert about a dead server that never came,
VPN checks against an address the server no longer uses.
"""

import base64
import time
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from conftest import Fake, tg
from test_remnawave import rw  # noqa: F401


@pytest.fixture()
def server(client, admin, rw):  # noqa: F811
    r = client.post("/api/admin/servers", headers=admin, json={
        "name": "Finland", "country": "FI", "kind": "remnawave", "url": rw["url"],
        "api_token": rw["token"], "inbound_ids": rw["squads"]})
    assert r.status_code == 200, r.text
    return r.json()


def _buy(client, uid):
    from app import core
    from app.db import SessionLocal
    with SessionLocal() as s:
        core.put(s, "renew_invoice", "1")
    plan = [p for p in client.get("/api/app/config").json()["plans"] if not p["is_trial"]][0]
    from app import billing
    from app.models import Plan, User
    client.get("/api/app/me", headers=tg(uid))
    with SessionLocal() as s:
        user = s.query(User).filter(User.tg_id == uid).one()
        tx = billing.new_order(s, user, "purchase", "admin", plan=s.get(Plan, plan["id"]))
        billing.finish(s, tx)
    return client.get("/api/app/me", headers=tg(uid)).json()


# ---------------------------------------------------------------- one panel, one server
def test_the_same_panel_cannot_be_added_twice(client, admin, rw, server):  # noqa: F811
    for url in (rw["url"], rw["url"].rstrip("/"), rw["url"].upper().replace("HTTP://", "http://")):
        r = client.post("/api/admin/servers", headers=admin, json={
            "name": "Netherlands", "country": "NL", "kind": "remnawave", "url": url,
            "api_token": rw["token"], "inbound_ids": rw["squads"]})
        assert r.status_code == 409 and "Finland" in r.json()["detail"], (url, r.text)


def test_one_panel_behind_two_rows_counts_traffic_once(client, db, monkeypatch):
    from app import access
    from app.models import Server, User
    from app.providers import remnawave
    db.add_all([Server(name="A", kind="remnawave", url="https://panel.example.com/", api_token="t"),
                Server(name="B", kind="remnawave", url="https://PANEL.example.com", api_token="t")])
    db.add(User(tg_id=4242, expires_at=int(time.time()) + 86400, traffic_limit=10 * 1024 ** 3))
    db.commit()
    calls = []

    def traffic(self):
        calls.append(self.server.name)
        return {"tg4242": 3 * 1024 ** 3}
    monkeypatch.setattr(remnawave.RemnawavePanel, "traffic_by_email", traffic)
    access.collect_traffic(db)
    db.expire_all()
    assert calls == ["A"]
    assert db.query(User).filter(User.tg_id == 4242).one().traffic_used == 3 * 1024 ** 3


def test_traffic_is_matched_in_the_panels_own_spelling(monkeypatch):
    from app import access
    from app.config import settings
    monkeypatch.setattr(settings, "xui_email_prefix", "VPN-")
    assert access.user_prefix("3xui") == "VPN-"
    assert access.user_prefix("marzban") == "vpn_"                      # lower case, "_" for "-"
    assert access.user_prefix("remnawave") == "VPN-"


# ---------------------------------------------------------------- slow and dead panels
def test_a_hanging_panel_does_not_hold_the_caller():
    from app import access
    from app.providers import PanelError
    started = time.monotonic()
    out = access._parallel(lambda x: time.sleep(3) or x, [1, 2], timeout=0.5)
    assert time.monotonic() - started < 1.5
    assert all(isinstance(r, PanelError) for r in out)


def test_subscription_keeps_working_keys_when_no_panel_answers(client, admin, db, server):
    from app import access
    from app.models import Server
    me = _buy(client, 4301)
    me2 = _buy(client, 4302)                                             # bought, never connected
    path = me["sub_url"].replace("https://shop.example.com", "")
    r = client.get(path, headers={"X-HWID": "phone"})
    assert r.status_code == 200 and "vless://" in base64.b64decode(r.text).decode()
    s = db.get(Server, server["id"])
    s.url = "http://127.0.0.1:1/"                                        # the panel's API is gone
    db.commit()
    access._links_cache.clear()                                           # e.g. after a restart
    r = client.get(path, headers={"X-HWID": "phone"})
    assert r.status_code == 503                                           # the app keeps its keys
    # Somebody who never connected gets the explanation instead.
    r = client.get(me2["sub_url"].replace("https://shop.example.com", ""), headers={"X-HWID": "x"})
    assert r.status_code == 200 and "⚠" in unquote(base64.b64decode(r.text).decode())


def test_keys_of_an_old_uuid_are_never_served(client, admin, db, server):
    from app import access
    from app.models import User
    _buy(client, 4401)
    user = db.query(User).filter(User.tg_id == 4401).one()
    access._links_cache[(server["id"], user.id, "")] = (time.time(), ["vless://old@1.2.3.4:443#old"], "other-uuid")
    links = access.collect_links(db, user)
    assert links and not any("old@" in x for x in links)


def test_failed_syncs_are_retried_in_turns(client, db, monkeypatch):
    from app import access
    from app.models import User
    t = int(time.time()) + 86400
    db.add_all([User(tg_id=5000 + i, expires_at=t, sync_error="dead server") for i in range(5)])
    db.commit()
    seen = []
    monkeypatch.setattr(access, "push", lambda db, user, **kw: seen.append(user.tg_id) or
                        {"ok": 1, "failed": 1, "errors": ["x"]})
    for _ in range(3):
        access.sync_users(db, only_failed=True, limit=2)
    assert sorted(set(seen)) == [5000 + i for i in range(5)]             # everybody got a turn


# ---------------------------------------------------------------- alerts
def test_server_down_alert_comes_even_after_other_jobs_saw_the_error(client, db, monkeypatch):
    from app import access, jobs
    from app.models import Server
    from app.providers import xui
    db.add(Server(name="Dead", url="http://127.0.0.1:1/", username="a", password="b", inbound_ids="1"))
    db.commit()
    access.collect_traffic(db)                       # every 5 min: writes the error first
    assert db.query(Server).one().last_error
    Fake.calls.clear()
    jobs.job_health(db)
    assert any("Server down" in m["text"] for m in Fake.sent("sendMessage"))
    Fake.calls.clear()
    jobs.job_health(db)
    assert not Fake.sent("sendMessage")             # once, not every 15 minutes
    monkeypatch.setattr(xui.XuiPanel, "test", lambda self: {"version": "3", "inbounds": []})
    jobs.job_health(db)
    assert any("Server is back" in m["text"] for m in Fake.sent("sendMessage"))


# ---------------------------------------------------------------- reminders
def test_reminders_reach_everyone_not_only_the_first_300(client, db):
    from app import core, marketing
    from app.models import Plan, User, WarmSent
    core.put(db, "renew_invoice", "0")
    plan = db.query(Plan).filter(Plan.is_trial.is_(False)).first()
    t = int(time.time()) + 86400
    db.add_all([User(tg_id=100000 + i, expires_at=t + i, plan_id=plan.id) for i in range(350)])
    db.commit()
    for _ in range(2):
        marketing.run_triggers(db, "expiring")
    assert db.query(WarmSent).filter(WarmSent.trigger == "expiring").count() == 350


# ---------------------------------------------------------------- VPN checks
def test_vpn_check_follows_the_keys():
    from app import vpncheck
    s = SimpleNamespace(endpoints="", vpn_fails=0, vpn_down_at=0)
    assert vpncheck.learn(s, ["vless://u@1.2.3.4:443?type=tcp#a"]) and s.endpoints == "1.2.3.4:443"
    s.vpn_fails, s.vpn_down_at = 3, 123                     # marked down at the old address
    assert vpncheck.learn(s, ["vless://u@1.2.3.4:8443?type=tcp#a"])
    assert s.endpoints == "1.2.3.4:8443" and s.vpn_fails == 0 and not s.vpn_down_at
    # Moved to UDP only: the old TCP address is forgotten, nothing is checked.
    assert vpncheck.learn(s, ["hysteria2://p@1.2.3.4:443#h"]) and s.endpoints == ""
    # TCP and UDP together: not checked either — a dead TCP part must not
    # hide a server whose UDP part works.
    vpncheck.learn(s, ["vless://u@1.2.3.4:443#a", "hy2://p@1.2.3.4:443#h"])
    assert s.endpoints == ""
    assert not vpncheck.learn(s, [])                        # no keys at all: nothing learned


# ---------------------------------------------------------------- the bot
def test_broadcast_from_the_bot_keeps_bold_and_links():
    from app.botadmin import entities_html
    text = "Скидка 🔥 30% по ссылке"
    def at(sub):
        i = text.index(sub)
        return len(text[:i].encode("utf-16-le")) // 2, len(sub.encode("utf-16-le")) // 2
    (o1, l1), (o2, l2) = at("30%"), at("ссылке")
    html = entities_html(text, [{"type": "bold", "offset": o1, "length": l1},
                                {"type": "text_link", "offset": o2, "length": l2, "url": "https://t.me/x?a=1&b=2"},
                                {"type": "text_link", "offset": 0, "length": 3, "url": "javascript:alert(1)"}])
    assert html == 'Скидка 🔥 <b>30%</b> по <a href="https://t.me/x?a=1&amp;b=2">ссылке</a>'
