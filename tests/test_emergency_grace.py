"""
The days after the end of a subscription, the emergency mode and the
diagnostics.
"""

import base64
import time
from urllib.parse import unquote

import pytest

from conftest import Fake, tg
from test_remnawave import _user, rw  # noqa: F401

DAY = 86400


def _bot(client, update):
    from app import notify
    return client.post("/api/bot/webhook", json=update,
                       headers={"X-Telegram-Bot-Api-Secret-Token": notify.webhook_secret()})


@pytest.fixture()
def server(client, admin, rw):  # noqa: F811
    r = client.post("/api/admin/servers", headers=admin, json={
        "name": "Finland", "country": "FI", "kind": "remnawave", "url": rw["url"],
        "api_token": rw["token"], "inbound_ids": rw["squads"]})
    assert r.status_code == 200, r.text
    return r.json()


def _plan(client):
    return [p for p in client.get("/api/app/config").json()["plans"] if not p["is_trial"]][0]


def _buy(client, uid):
    r = client.post("/api/app/order", headers=tg(uid), json={"plan_id": _plan(client)["id"], "method": "stars"})
    assert r.status_code == 200, r.text
    return r.json()


def _customer(db, uid, ended_days_ago: float, trial: bool = False):
    """A customer whose paid time ended `ended_days_ago` days ago."""
    from app import access
    from app.models import Plan, User
    from app.routers.app_api import current_user
    user = current_user(db, {"id": uid, "first_name": "T", "language_code": "ru"})
    plan = db.query(Plan).filter(Plan.is_trial.is_(trial)).order_by(Plan.id).first()
    user.plan_id = plan.id
    user.expires_at = int(time.time() - ended_days_ago * DAY)
    db.commit()
    db.refresh(user)
    access.push(db, user)
    db.expire_all()
    return db.get(User, user.id)


def _expire_of(rw, uid):  # noqa: F811
    u = _user(rw, f"tg{uid}")
    return u["status"], time.mktime(time.strptime(u["expireAt"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone


def _sub_lines(client, db, uid):
    from app.models import User
    db.expire_all()
    user = db.query(User).filter(User.tg_id == uid).first()
    r = client.get(f"/sub/{user.sub_token}", headers={"User-Agent": "Happ/3", "X-HWID": f"h{uid}"})
    assert r.status_code == 200, r.text
    return [unquote(x.rsplit("#", 1)[-1]) for x in base64.b64decode(r.text).decode().splitlines()], r.headers


# ------------------------------------------------------------------ the days after the end
def test_no_days_after_the_end_by_default(client, db, server, rw):  # noqa: F811
    from app import access, core
    assert core.get(db, "grace_days") == "0"
    user = _customer(db, 6100001, ended_days_ago=0.5)
    assert access.access_until(db, user) == user.expires_at
    assert not access.has_access(db, user) and access.grace_until(db, user) == 0
    assert _expire_of(rw, 6100001)[0] == "DISABLED"
    lines, _ = _sub_lines(client, db, 6100001)
    assert len(lines) == 1 and "закончилась" in lines[0]


def test_days_after_the_end_keep_the_vpn(client, db, admin, server, rw):  # noqa: F811
    from app import access
    assert client.post("/api/admin/settings", headers=admin, json={"grace_days": "3"}).status_code == 200
    user = _customer(db, 6100002, ended_days_ago=1)
    until = access.access_until(db, user)
    assert until == user.expires_at + 3 * DAY
    assert access.has_access(db, user) and access.grace_until(db, user) == until
    # the panel holds the VPN on until then
    status, expire = _expire_of(rw, 6100002)
    assert status == "ACTIVE" and abs(expire - until) <= 2
    # the app knows: not "active" (the paid time is over), but still working
    me = client.get("/api/app/me", headers=tg(6100002)).json()
    assert me["active"] is False and me["grace_until"] == until
    # the VPN app gets the keys, with a line on top saying until when
    lines, headers = _sub_lines(client, db, 6100002)
    assert len(lines) > 1 and "VPN работает до" in lines[0]
    assert f"expire={until}" in headers["subscription-userinfo"]
    # after those days — off
    late = _customer(db, 6100003, ended_days_ago=3.5)
    assert not access.has_access(db, late) and _expire_of(rw, 6100003)[0] == "DISABLED"


def test_no_days_after_a_free_trial(client, db, admin, server):
    from app import access
    client.post("/api/admin/settings", headers=admin, json={"grace_days": "5"})
    user = _customer(db, 6100004, ended_days_ago=1, trial=True)
    assert access.access_until(db, user) == user.expires_at and not access.has_access(db, user)


def test_renewing_in_those_days_counts_from_today(client, db, admin, server):
    from app.models import User
    client.post("/api/admin/settings", headers=admin, json={"grace_days": "7", })
    from app import core
    core.put(db, "require_hwid", "0")
    _customer(db, 6100005, ended_days_ago=2)
    import app.config as cfg
    old = cfg.settings.payments_test_mode
    cfg.settings.payments_test_mode = True          # every order is free and granted at once
    try:
        _buy(client, 6100005)
    finally:
        cfg.settings.payments_test_mode = old
    db.expire_all()
    user = db.query(User).filter(User.tg_id == 6100005).first()
    plan_days = _plan(client)["days"]
    # from now, not from the old end plus the free days
    assert abs(user.expires_at - (time.time() + plan_days * DAY)) < 60


def test_days_setting_is_checked(client, admin):
    for bad in ("31", "-1", "abc", "2.5"):
        assert client.post("/api/admin/settings", headers=admin, json={"grace_days": bad}).status_code == 400, bad
    assert client.post("/api/admin/settings", headers=admin, json={"grace_days": ""}).status_code == 200
    assert client.get("/api/admin/settings", headers=admin).json()["values"]["grace_days"] == "0"


def test_sync_reaches_back_over_those_days(client, db, admin):
    from app import access
    base = access.sync_floor(db)
    client.post("/api/admin/settings", headers=admin, json={"grace_days": "10"})
    assert abs((base - access.sync_floor(db)) - 10 * DAY) < 5


# ------------------------------------------------------------------ the emergency mode
def test_emergency_stops_sales_and_tells_everyone(client, db, admin, server):
    r = client.post("/api/admin/emergency", headers=admin, json={
        "on": True, "text_ru": "Сервер в Финляндии чиним, к вечеру вернём", "text_en": "", "stop_sales": True})
    assert r.status_code == 200 and r.json()["on"] and r.json()["since"]
    cfg = client.get("/api/app/config").json()
    assert cfg["emergency"]["stop_sales"] is True
    assert cfg["emergency"]["text"]["ru"].startswith("Сервер в Финляндии")
    assert cfg["emergency"]["text"]["en"].startswith("Сервер")      # no English text: the Russian one
    # no new orders, no trials
    r = client.post("/api/app/order", headers=tg(6200001), json={"plan_id": _plan(client)["id"]})
    assert r.status_code == 503 and "технические работы" in r.json()["detail"]
    # the bot says it after /start
    Fake.reset()
    _bot(client, {"update_id": 91001, "message": {"chat": {"id": 6200001, "type": "private"},
                                                  "from": {"id": 6200001, "language_code": "ru"}, "text": "/start"}})
    sent = Fake.sent("sendMessage")
    assert sent and "Сервер в Финляндии" in sent[-1]["text"]
    # the dashboard warns the owner
    assert any(p["code"] == "emergency" for p in client.get("/api/admin/stats", headers=admin).json().get("problems", [])) \
        or any(p["code"] == "emergency" for p in client.get("/api/admin/settings", headers=admin).json()["env"]["problems"])
    # off: sales are back, the message is gone
    client.post("/api/admin/emergency", headers=admin, json={"on": False})
    assert client.get("/api/app/config").json()["emergency"] is None
    assert client.post("/api/app/order", headers=tg(6200001), json={"plan_id": _plan(client)["id"]}).status_code == 200


def test_emergency_refuses_stars_before_money_but_grants_paid(client, db, admin, server):
    from app.models import Transaction
    from app.payments import stars
    order = _buy(client, 6200002)
    tx_id = order["order_id"]
    client.post("/api/admin/emergency", headers=admin, json={"on": True, "stop_sales": True})
    tx = db.get(Transaction, tx_id)
    ok, reason = stars.pre_checkout(db, {"invoice_payload": f"tx:{tx.id}", "currency": "XTR",
                                         "total_amount": tx.stars, "from": {"language_code": "ru"}})
    assert ok is False and "технические работы" in reason
    # but money that already came (Telegram charged before the mode) is granted
    upd = {"update_id": 91002, "message": {
        "chat": {"id": 6200002, "type": "private"}, "from": {"id": 6200002},
        "successful_payment": {"currency": "XTR", "total_amount": tx.stars, "invoice_payload": f"tx:{tx.id}",
                               "telegram_payment_charge_id": "ch-em-1"}}}
    assert _bot(client, upd).status_code == 200
    db.expire_all()
    assert db.get(Transaction, tx_id).status == "success"


def test_emergency_without_stopping_sales(client, admin, server):
    client.post("/api/admin/emergency", headers=admin, json={"on": True, "stop_sales": False})
    cfg = client.get("/api/app/config").json()
    assert cfg["emergency"]["stop_sales"] is False
    assert cfg["emergency"]["text"]["ru"]                              # the standard text
    assert client.post("/api/app/order", headers=tg(6200003), json={"plan_id": _plan(client)["id"]}).status_code == 200


def test_keep_access_holds_who_had_the_vpn(client, db, admin, server, rw):  # noqa: F811
    from app import access, emergency
    from app.models import User
    had = _customer(db, 6200004, ended_days_ago=-0.2)       # ends in a few hours
    gone = _customer(db, 6200005, ended_days_ago=2)          # ended before the emergency
    client.post("/api/admin/emergency", headers=admin, json={"on": True, "keep_access": True})
    from app import core
    core.put(db, "emergency_since", str(int(time.time()) - 2 * 3600))   # the mode went on two hours ago
    db.expire_all()
    had, gone = db.get(User, had.id), db.get(User, gone.id)
    had.expires_at = int(time.time()) - 3600                 # and an hour ago its time ran out
    db.commit()
    assert access.has_access(db, had)
    assert access.access_until(db, had) >= time.time() + emergency.KEEP_AHEAD - 5
    assert not access.has_access(db, gone)
    access.push(db, had)
    assert _expire_of(rw, 6200004)[0] == "ACTIVE"
    # off: the real end again
    client.post("/api/admin/emergency", headers=admin, json={"on": False})
    db.expire_all()
    had = db.get(User, had.id)
    assert not access.has_access(db, had)
    access.push(db, had)
    assert _expire_of(rw, 6200004)[0] == "DISABLED"


def test_emergency_is_not_set_through_plain_settings(client, admin):
    client.post("/api/admin/settings", headers=admin, json={"emergency": "1", "emergency_text_ru": "x"})
    assert client.get("/api/admin/emergency", headers=admin).json()["on"] is False


def test_emergency_needs_the_admin(client):
    assert client.post("/api/admin/emergency", json={"on": True}).status_code == 401
    assert client.post("/api/admin/diagnostics").status_code == 401


def test_warm_up_waits_while_sales_are_stopped(client, db, admin, monkeypatch):
    from app import jobs, marketing
    ran = []
    monkeypatch.setattr(marketing, "run_triggers", lambda db: ran.append(1))
    client.post("/api/admin/emergency", headers=admin, json={"on": True, "stop_sales": True})
    jobs.job_warm(db)
    assert not ran
    client.post("/api/admin/emergency", headers=admin, json={"on": False})
    jobs.job_warm(db)
    assert ran == [1]


# ------------------------------------------------------------------ diagnostics
def test_diagnostics_checks_for_real(client, admin, server, monkeypatch):
    from app import diagnostics
    from app.config import settings as env
    monkeypatch.setattr(diagnostics, "_cert_expiry", lambda host, port=443: int(time.time()) + 60 * DAY)
    assert client.get("/api/admin/diagnostics", headers=admin).json() is None     # not run yet
    r = client.post("/api/admin/diagnostics", headers=admin)
    assert r.status_code == 200, r.text
    d = r.json()
    by = {c["id"]: c for c in d["checks"]}
    assert by["db"]["status"] == "ok"
    assert by["bot"]["status"] == "ok" and "@shop_test_bot" in by["bot"]["detail"]["ru"]
    # the fake Telegram returns no webhook: that is reported, not hidden
    assert by["webhook"]["status"] == "fail" and env.public_url in by["webhook"]["detail"]["ru"]
    assert by["https"]["status"] == "ok" and "осталось" in by["https"]["detail"]["ru"]
    assert by[f"panel:{server['id']}"]["status"] == "ok"
    assert by["emergency"]["status"] == "ok"
    assert by["jobs"]["status"] == "fail"                    # tests run with SHOP_NO_JOBS
    assert sum(d["summary"].values()) == len(d["checks"])
    for c in d["checks"]:
        assert c["status"] in ("ok", "warn", "fail", "off") and c["title"]["ru"] and c["title"]["en"]
    # the last result is kept for the page
    assert client.get("/api/admin/diagnostics", headers=admin).json()["at"] == d["at"]


def test_diagnostics_reports_a_dead_panel_and_a_bad_certificate(client, admin, monkeypatch):
    import ssl

    from app import diagnostics

    def bad(host, port=443):
        e = ssl.SSLCertVerificationError("certificate verify failed")
        e.verify_message = "certificate has expired"
        raise e
    monkeypatch.setattr(diagnostics, "_cert_expiry", bad)
    client.post("/api/admin/servers", headers=admin, json={
        "name": "Dead", "country": "DE", "kind": "remnawave", "url": "http://127.0.0.1:9/",
        "api_token": "x", "inbound_ids": "a"})
    d = client.post("/api/admin/diagnostics", headers=admin).json()
    by = {c["id"]: c for c in d["checks"]}
    assert by["https"]["status"] == "fail" and "expired" in by["https"]["detail"]["en"]
    dead = [c for c in d["checks"] if c["id"].startswith("panel:")]
    assert dead and dead[0]["status"] == "fail" and dead[0]["fix"]["ru"]
    assert d["worst"] == "fail"


def test_diagnostics_and_emergency_from_the_bot(client, db, monkeypatch):
    from app import botadmin, diagnostics
    monkeypatch.setattr(diagnostics, "_cert_expiry", lambda host, port=443: int(time.time()) + 60 * DAY)
    out = []
    monkeypatch.setattr(botadmin, "_send", lambda chat, text, rows=None: out.append(text))
    msg = lambda text: {"from": {"id": 999, "language_code": "ru"}, "chat": {"id": 999, "type": "private"}, "text": text}  # noqa: E731
    assert botadmin.handle_command(db, msg("/diag"))
    assert "Диагностика" in out[-1] and "ошибок:" in out[-1]
    assert botadmin.handle_command(db, msg("/emergency on Чиним <b>сервер</b>"))
    assert "Включён" in out[-1] and "&lt;b&gt;" in out[-1]           # the owner's text is escaped
    from app import emergency
    assert emergency.is_on(db) and emergency.message(db, "ru") == "Чиним <b>сервер</b>"
    assert botadmin.handle_command(db, msg("/emergency off"))
    assert "Выключен" in out[-1] and not emergency.is_on(db)


def test_page_scripts_parse():
    """A missing comma in a page's dictionary breaks the whole panel: every
    inline script of every page must at least parse."""
    import re
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    web = Path(__file__).resolve().parents[1] / "web"
    for page in ("index.html", "admin.html", "legal.html"):
        html = (web / page).read_text()
        for i, code in enumerate(re.findall(r"<script>([\s\S]*?)</script>", html)):
            r = subprocess.run([node, "-e", "new Function(require('fs').readFileSync(0, 'utf8'))"],
                               input=code, capture_output=True, text=True, timeout=30)
            assert r.returncode == 0, f"{page} script {i + 1}: {r.stderr[-400:]}"
