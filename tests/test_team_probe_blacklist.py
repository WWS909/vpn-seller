"""
The automatic emergency mode, shared blacklists, the checkers inside
Russia and DPI//CHECKER, Telegram-only access after the end, and the
owner's team with sections.
"""

import base64
import importlib.util
import socket
import threading
import time
from pathlib import Path
from urllib.parse import unquote

import pytest

from conftest import Fake, init_data, tg
from test_remnawave import SQUAD, SQUAD_SS, _user, rw  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def server(client, admin, rw):  # noqa: F811
    r = client.post("/api/admin/servers", headers=admin, json={
        "name": "Finland", "country": "FI", "kind": "remnawave", "url": rw["url"],
        "api_token": rw["token"], "inbound_ids": SQUAD})
    assert r.status_code == 200, r.text
    return r.json()


def _plans(client):
    return client.get("/api/app/config").json()["plans"]


def _customer(db, uid, ended_hours_ago: float):
    from app import access
    from app.models import Plan, User
    from app.routers.app_api import current_user
    user = current_user(db, {"id": uid, "first_name": "T", "language_code": "ru"})
    user.plan_id = db.query(Plan).filter(Plan.is_trial.is_(False)).order_by(Plan.id).first().id
    user.expires_at = int(time.time() - ended_hours_ago * 3600)
    db.commit()
    access.push(db, user)
    db.expire_all()
    return db.get(User, user.id)


# ------------------------------------------------------------------ the emergency mode by itself
def test_emergency_turns_on_and_off_by_itself(client, db, admin, server):
    from app import core, emergency
    from app.models import Server
    client.post("/api/admin/emergency", headers=admin, json={"auto": True, "auto_minutes": 5, "stop_sales": True})
    s = db.get(Server, server["id"])
    s.down_alerted_at = int(time.time()) - 120            # down for two minutes: not yet
    db.commit()
    assert emergency.auto_check(db) == "" and not emergency.is_on(db)
    s.down_alerted_at = int(time.time()) - 600            # ten minutes
    db.commit()
    assert emergency.auto_check(db) == "on" and emergency.is_on(db)
    assert emergency.state(db)["auto_on"] is True
    assert "не отвечают" in client.get("/api/app/config").json()["emergency"]["text"]["ru"]
    assert client.post("/api/app/order", headers=tg(7300001), json={"plan_id": _plans(client)[0]["id"]}).status_code == 503
    s.down_alerted_at = 0                                  # back
    db.commit()
    assert emergency.auto_check(db) == "off" and not emergency.is_on(db)
    assert core.get(db, "emergency_auto_on") == "0"


def test_a_mode_the_owner_turned_on_stays(client, db, admin, server):
    from app import emergency
    client.post("/api/admin/emergency", headers=admin, json={"auto": True, "on": True})
    assert emergency.auto_check(db) == "" and emergency.is_on(db)


def test_auto_mode_is_off_by_default_and_needs_servers(client, db):
    from app import emergency
    assert emergency.auto_check(db) == ""
    from app import core
    core.put(db, "emergency_auto", "1")
    assert emergency.auto_check(db) == ""                  # no servers: nothing to be down


# ------------------------------------------------------------------ shared blacklists
@pytest.fixture()
def lists(monkeypatch):
    files = {"https://example.org/bl.txt": "# a shared list\n7400001  # trial farming\n7400002\nnot a number\n999\n"}

    def download(url):
        if url not in files:
            raise ValueError("HTTP 404")
        return files[url]
    from app import blacklist
    monkeypatch.setattr(blacklist, "_download", download)
    return files


def test_blacklist_refuses_the_trial(client, db, admin, lists, server):
    r = client.post("/api/admin/blacklist", headers=admin, json={"urls": ["https://example.org/bl.txt"], "action": "trial"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 3 and d["status"]["https://example.org/bl.txt"]["count"] == 3
    me = client.get("/api/app/me", headers=tg(7400001)).json()
    assert me["trial_available"] is False
    assert client.get("/api/app/me", headers=tg(7400003)).json()["trial_available"] is True
    trial = [p for p in _plans(client) if p["is_trial"]][0]
    assert client.post("/api/app/order", headers=tg(7400001), json={"plan_id": trial["id"]}).status_code == 400
    # buying is still allowed with "trial"
    paid = [p for p in _plans(client) if not p["is_trial"]][0]
    assert client.post("/api/app/order", headers=tg(7400001), json={"plan_id": paid["id"]}).status_code == 200
    # the admin card says so
    card = client.get("/api/admin/users/7400001", headers=admin).json()
    assert card["blacklisted"]["reason"] == "trial farming"


def test_blacklist_block_and_admins_are_never_refused(client, db, admin, lists, server):
    client.post("/api/admin/blacklist", headers=admin, json={"urls": ["https://example.org/bl.txt"], "action": "block"})
    paid = [p for p in _plans(client) if not p["is_trial"]][0]
    r = client.post("/api/app/order", headers=tg(7400002), json={"plan_id": paid["id"]})
    assert r.status_code == 403 and "недоступны" in r.json()["detail"]
    assert client.post("/api/app/order", headers=tg(999), json={"plan_id": paid["id"]}).status_code == 200


def test_a_failing_list_keeps_what_it_had(client, db, admin, lists):
    from app import blacklist
    client.post("/api/admin/blacklist", headers=admin, json={"urls": ["https://example.org/bl.txt"]})
    del lists["https://example.org/bl.txt"]
    st = blacklist.refresh(db)
    assert st["https://example.org/bl.txt"]["ok"] is False and blacklist.info(db)["total"] == 3
    # a list taken off is forgotten
    client.post("/api/admin/blacklist", headers=admin, json={"urls": []})
    assert blacklist.info(db)["total"] == 0


def test_blacklist_addresses_are_checked(client, admin):
    for bad in (["http://example.org/x"], ["ftp://x"], ["https://"]):
        assert client.post("/api/admin/blacklist", headers=admin, json={"urls": bad}).status_code == 400, bad
    assert client.post("/api/admin/blacklist", headers=admin, json={"action": "kill"}).status_code == 400


def test_own_list_is_published_only_when_asked(client, db, admin):
    from app.models import User
    from app.routers.app_api import current_user
    u = current_user(db, {"id": 7400010, "first_name": "Bad"})
    u.is_blocked = True
    db.commit()
    assert client.get("/blacklist.txt").status_code == 404
    client.post("/api/admin/blacklist", headers=admin, json={"publish": True})
    body = client.get("/blacklist.txt").text
    assert "7400010" in body and "Bad" not in body          # numbers only
    assert db.query(User).count() >= 1


# ------------------------------------------------------------------ the checkers inside Russia
def _probe(client, token, path, body=None):
    h = {"Authorization": "Bearer " + token}
    return client.post(path, headers=h, json=body) if body is not None else client.get(path, headers=h)


def test_russia_checker_reports_and_alerts(client, db, admin, server):
    from app.models import Server
    s = db.get(Server, server["id"])
    s.endpoints, s.vpn_sni = "1.2.3.4:443", "1.2.3.4:443=www.example.com"
    db.commit()
    assert client.get("/api/probe/targets").status_code == 401
    token = client.post("/api/admin/probe/token", headers=admin).json()["token"]
    assert "probe_token" not in client.get("/api/admin/settings", headers=admin).json()["values"]
    t = _probe(client, token, "/api/probe/targets").json()["targets"]
    assert t == [{"server_id": server["id"], "name": "Finland", "endpoint": "1.2.3.4:443", "sni": "www.example.com"}]
    bad = {"probe": "Москва · МТС", "results": [
        {"server_id": server["id"], "endpoint": "1.2.3.4:443", "tcp_ok": True, "tls_ok": False, "error": "tls: reset"},
        {"server_id": server["id"], "endpoint": "6.6.6.6:1", "tcp_ok": True, "tls_ok": True}]}   # not asked: ignored
    Fake.reset()
    assert _probe(client, token, "/api/probe/report", bad).json() == {"saved": 1}
    assert not [m for m in Fake.sent("sendMessage") if "недоступен" in m["text"]]          # one is not enough
    _probe(client, token, "/api/probe/report", bad)
    alerts = [m for m in Fake.sent("sendMessage") if "недоступен" in m["text"]]
    assert alerts and "DPI" in alerts[0]["text"]
    st = client.get("/api/admin/probe", headers=admin).json()
    assert st["probes"][0]["name"] == "Москва · МТС" and st["probes"][0]["online"]
    assert st["servers"][0]["cells"][0]["state"] == "blocked"
    # the diagnostics see it too
    from app import diagnostics
    assert {c["id"]: c for c in diagnostics.run(db)["checks"]}["russia"]["status"] == "fail"
    good = {"probe": "Москва · МТС", "results": [{"server_id": server["id"], "endpoint": "1.2.3.4:443",
                                                  "tcp_ok": True, "tls_ok": True, "ms": 41}]}
    Fake.reset()
    _probe(client, token, "/api/probe/report", good)
    assert [m for m in Fake.sent("sendMessage") if "снова доступен" in m["text"]]
    # a new token: the old one stops at once
    client.post("/api/admin/probe/token", headers=admin)
    assert _probe(client, token, "/api/probe/targets").status_code == 401


def test_the_checker_script(client):
    assert client.get("/probe/ru-probe.py").status_code == 200
    spec = importlib.util.spec_from_file_location("ru_probe", ROOT / "scripts" / "ru-probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # a closed port: no connection
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    r = mod.check(f"127.0.0.1:{closed}", "", timeout=2)
    assert r["tcp_ok"] is False and r["error"].startswith("tcp")
    # a server that answers the hello (with nonsense): reachable
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def answer():
        c, _ = srv.accept()
        c.recv(1024)
        c.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        c.close()
    threading.Thread(target=answer, daemon=True).start()
    r = mod.check(f"127.0.0.1:{srv.getsockname()[1]}", "www.example.com", timeout=3)
    srv.close()
    assert r["tcp_ok"] and r["tls_ok"], r
    # a server that accepts and goes silent after the hello: what DPI looks like
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    held = []
    threading.Thread(target=lambda: held.append(srv.accept()), daemon=True).start()
    r = mod.check(f"127.0.0.1:{srv.getsockname()[1]}", "www.example.com", timeout=1.5)
    srv.close()
    assert r["tcp_ok"] and not r["tls_ok"] and "no answer" in r["error"]


# ------------------------------------------------------------------ DPI//CHECKER
def test_dpichecker_needs_a_key(client, admin, server):
    r = client.post("/api/admin/dpicheck/estimate", headers=admin, json={"server_id": server["id"]})
    assert r.status_code == 502 and "DPICHECKER_API_KEY" in r.json()["detail"]
    assert client.get("/api/admin/probe", headers=admin).json()["dpi"]["on"] is False


def test_dpichecker_estimate_start_result(client, db, admin, server, monkeypatch):
    from app import dpicheck
    from app.config import settings as env
    monkeypatch.setattr(env, "dpichecker_api_key", "k")
    calls = []

    def fake(method, path, body=None, idem="", timeout=30):
        calls.append((method, path, body, idem))
        if path.startswith("/pops/optimal"):
            return {"pop_ids": [11, 12, 13]}
        if path == "/checks/estimate":
            return {"usd_cost": "0.42"}
        if path == "/checks/vpn":
            return {"id": 555, "status": "pending"}
        if path == "/checks/555":
            return {"id": 555, "status": "completed", "usd_cost": 0.42, "results": [
                {"pop_name": "Москва · МТС", "connected": True, "latency_ms": 38},
                {"pop_name": "Казань · Билайн", "connected": False, "verdict": "blocked by SNI"},
                {"pop_name": "control", "connected": True, "is_direct": True}]}
        raise AssertionError(path)
    monkeypatch.setattr(dpicheck, "_call", fake)
    e = client.post("/api/admin/dpicheck/estimate", headers=admin, json={"server_id": server["id"]}).json()
    assert e["usd"] == 0.42 and e["points"] == 3 and e["keys"] >= 1
    est_keys = [c for c in calls if c[1] == "/checks/estimate"][0][2]["keys"]
    assert all("://" in k for k in est_keys)                # real keys of the service client
    r = client.post("/api/admin/dpicheck/start", headers=admin, json={"server_id": server["id"]}).json()
    assert r["id"] == 555
    start = [c for c in calls if c[1] == "/checks/vpn"][0]
    assert start[3].startswith("vpnshop-") and start[2]["location"] == "russia"   # idempotent start
    res = client.get("/api/admin/dpicheck/555", headers=admin).json()
    assert res["done"] and res["ok"] == 1 and res["total"] == 2
    assert res["rows"][0]["region"] == "Казань · Билайн" and not res["rows"][0]["ok"]
    assert client.get("/api/admin/probe", headers=admin).json()["dpi"]["last"][str(server["id"])]["total"] == 2


# ------------------------------------------------------------------ Telegram only, after the end
def test_tg_inbounds_must_differ_from_customers(client, admin, rw):  # noqa: F811
    r = client.post("/api/admin/servers", headers=admin, json={
        "name": "X", "kind": "remnawave", "url": rw["url"], "api_token": rw["token"],
        "inbound_ids": SQUAD, "tg_inbounds": SQUAD})
    assert r.status_code == 400


def test_telegram_only_after_the_end(client, db, admin, rw):  # noqa: F811
    from app import access, jobs
    from app.models import User
    r = client.post("/api/admin/servers", headers=admin, json={
        "name": "Finland", "country": "FI", "kind": "remnawave", "url": rw["url"], "api_token": rw["token"],
        "inbound_ids": SQUAD, "tg_inbounds": SQUAD_SS})
    assert r.status_code == 200, r.text
    assert client.post("/api/admin/settings", headers=admin, json={"tg_only_hours": "200"}).status_code == 400
    client.post("/api/admin/settings", headers=admin, json={"tg_only_hours": "6"})
    user = _customer(db, 7500001, ended_hours_ago=1)
    end = access.tg_until(db, user)
    assert end == user.expires_at + 6 * 3600
    # the real client is off; a separate Telegram-only client holds the special squad
    real, tgc = _user(rw, "tg7500001"), _user(rw, "tg7500001_tg")
    assert real["status"] == "DISABLED"
    assert tgc and tgc["status"] == "ACTIVE"
    assert {s["uuid"] for s in tgc["activeInternalSquads"]} == {SQUAD_SS}
    db.expire_all()
    assert db.get(User, user.id).tg_only_until == end
    # the app and the VPN app say so
    me = client.get("/api/app/me", headers=tg(7500001)).json()
    assert me["active"] is False and me["tg_only_until"] == end
    sub = client.get(f"/sub/{user.sub_token}", headers={"User-Agent": "Happ/3", "X-HWID": "tg1"})
    names = [unquote(x.rsplit("#", 1)[-1]) for x in base64.b64decode(sub.text).decode().splitlines()]
    assert "только Telegram" in names[0] and len(names) > 1
    # the job does not push again what the panels already hold
    pushed = []
    orig = access.push
    access.push = lambda db, u, **kw: pushed.append(u.id) or orig(db, u, **kw)
    try:
        jobs.job_tg_only(db)
    finally:
        access.push = orig
    assert pushed == []
    # long after: nothing
    late = _customer(db, 7500002, ended_hours_ago=7)
    assert access.tg_until(db, late) == 0 and _user(rw, "tg7500002_tg") is None


def test_telegram_only_is_off_by_default(client, db, server):
    from app import access
    user = _customer(db, 7500003, ended_hours_ago=1)
    assert access.tg_until(db, user) == 0
    assert client.get("/api/app/me", headers=tg(7500003)).json()["tg_only_until"] == 0


# ------------------------------------------------------------------ the team
def _member_token(client, tg_id):
    r = client.post("/api/admin/tg-login", headers={"X-Init-Data": init_data(tg_id)})
    return r


def test_team_member_sees_only_their_sections(client, db, admin):
    assert _member_token(client, 7600001).status_code == 403          # not on the team yet
    r = client.post("/api/admin/team", headers=admin, json={"tg_id": 7600001, "name": "Оля", "sections": ["support", "nope"]})
    assert r.status_code == 200 and r.json()["sections"] == ["support"]
    tok = _member_token(client, 7600001).json()["token"]
    h = {"Authorization": "Bearer " + tok}
    me = client.get("/api/admin/me", headers=h).json()
    assert me == {"name": me["name"], "owner": False, "sections": ["support"]} and "Оля" in me["name"]
    assert client.get("/api/admin/support", headers=h).status_code == 200
    for path in ("/api/admin/orders", "/api/admin/overview", "/api/admin/settings", "/api/admin/team",
                 "/api/admin/servers", "/api/admin/backups", "/api/admin/payments"):
        assert client.get(path, headers=h).status_code == 403, path
    assert client.post("/api/admin/emergency", headers=h, json={"on": True}).status_code == 403
    # the owner sees everything
    assert client.get("/api/admin/me", headers=admin).json()["owner"] is True
    # more sections: at once, with the same token
    client.post("/api/admin/team", headers=admin, json={"tg_id": 7600001, "name": "Оля", "sections": ["support", "sales"]})
    assert client.get("/api/admin/orders", headers=h).status_code == 200
    # switched off or removed: the token stops at once
    client.post("/api/admin/team", headers=admin, json={"tg_id": 7600001, "sections": ["support"], "enabled": False})
    assert client.get("/api/admin/support", headers=h).status_code == 401
    client.delete("/api/admin/team/7600001", headers=admin)
    assert _member_token(client, 7600001).status_code == 403


def test_team_cannot_add_an_owner_and_members_cannot_manage_the_team(client, admin):
    assert client.post("/api/admin/team", headers=admin, json={"tg_id": 999, "sections": ["support"]}).status_code == 400
    client.post("/api/admin/team", headers=admin, json={"tg_id": 7600002, "sections": list(("stats", "sales", "people", "support", "system"))})
    h = {"Authorization": "Bearer " + _member_token(client, 7600002).json()["token"]}
    assert client.get("/api/admin/settings", headers=h).status_code == 200
    assert client.post("/api/admin/team", headers=h, json={"tg_id": 7600003, "sections": ["system"]}).status_code == 403


def test_support_questions_reach_support_members(client, db, admin):
    from app import team
    client.post("/api/admin/team", headers=admin, json={"tg_id": 7600004, "sections": ["support"]})
    client.post("/api/admin/team", headers=admin, json={"tg_id": 7600005, "sections": ["sales"]})
    ids = team.support_ids(db)
    assert 7600004 in ids and 999 in ids and 7600005 not in ids
    assert team.can(db, 7600004, "support") and not team.can(db, 7600005, "support")


def test_every_admin_route_has_a_section():
    """A new route that nobody mapped would be open to the owner only —
    safe, but the team would miss it: this lists such routes."""
    import re as _re

    from app import team
    src = "".join((ROOT / "app" / "routers" / f).read_text() for f in ("admin_api.py", "export.py"))
    firsts = set(_re.findall(r'@router\.(?:get|post|put|delete|patch)\("/([a-z0-9_-]+)', src))
    owner_only = {"team", "login", "tg-login", "logout", "payments", "updates"}   # payment keys, updates: the owner only
    assert sorted(firsts - set(team.SECTION_OF) - owner_only) == []
