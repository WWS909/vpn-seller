"""
Test fixtures.

Unit-level tests run everywhere. Tests marked `panel` talk to REAL 3x-ui
panels and are skipped unless these are set (scripts/dev-3xui.sh starts
two local panels and prints them):

    XUI_TEST_V3_URL, XUI_TEST_V3_TOKEN, XUI_TEST_V3_INBOUND
    XUI_TEST_V2_URL, XUI_TEST_V2_USER, XUI_TEST_V2_PASS, XUI_TEST_V2_INBOUND, XUI_TEST_V2_SUB

Telegram and the payment gateways are replaced by a local fake server,
so nothing leaves the machine.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BOT_TOKEN = "123456:TEST-token-for-shop"
# Run the same tests on PostgreSQL:
#   TEST_DATABASE_URL=postgresql+psycopg://user:pass@host/db python -m pytest
TEST_DB = os.getenv("TEST_DATABASE_URL", "")


# ---------------------------------------------------------------------------
#  A fake of api.telegram.org and of the payment gateways
# ---------------------------------------------------------------------------
class Fake:
    calls: list = []
    yookassa: dict = {}
    paypalych: dict = {}
    cryptobot: dict = {}
    platega: dict = {}
    members: set = set()            # user ids in the channel @testchannel
    bot_is_admin: bool = True
    message_id: int = 1000

    @classmethod
    def reset(cls):
        cls.calls = []
        cls.yookassa = {}
        cls.paypalych = {}
        cls.cryptobot = {}
        cls.platega = {}
        cls.members = set()
        cls.bot_is_admin = True

    @classmethod
    def sent(cls, method: str) -> list:
        return [body for m, body in cls.calls if m == method]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0) or 0)
        if not raw:
            return {}
        if "json" in (self.headers.get("Content-Type") or ""):
            return json.loads(raw)
        if "multipart" in (self.headers.get("Content-Type") or ""):
            import re as _re
            chat = _re.search(rb'name="chat_id"\r\n\r\n(-?\d+)', raw)
            return {"multipart": True, "size": len(raw), "chat_id": int(chat.group(1)) if chat else None,
                    "zip": raw.find(b"PK\x03\x04") >= 0}
        return {k: v[0] for k, v in parse_qs(raw.decode()).items()}

    def do_GET(self):
        path = urlsplit(self.path)
        if path.path.startswith("/yookassa/payments/"):
            pid = path.path.rsplit("/", 1)[1]
            return self._json(Fake.yookassa.get(pid, {}), 200 if pid in Fake.yookassa else 404)
        if path.path == "/paypalych/bill/status":
            bill = parse_qs(path.query).get("id", [""])[0]
            return self._json(Fake.paypalych.get(bill, {"status": "NEW"}))
        if path.path == "/cryptobot/getInvoices":
            ids = parse_qs(path.query).get("invoice_ids", [""])[0].split(",")
            items = [Fake.cryptobot[i] for i in ids if i in Fake.cryptobot]
            return self._json({"ok": True, "result": {"items": items}})
        if path.path.startswith("/platega/transaction/"):
            tid = path.path.rsplit("/", 1)[1]
            return self._json(Fake.platega.get(tid, {}), 200 if tid in Fake.platega else 404)
        self._json({"ok": False}, 404)

    def do_POST(self):
        path = urlsplit(self.path).path
        body = self._body()
        if path.startswith("/bot"):
            method = path.rsplit("/", 1)[1]
            Fake.calls.append((method, body))
            if method == "getMe":
                return self._json({"ok": True, "result": {"id": 1, "username": "shop_test_bot"}})
            if method == "getChatMember":
                uid = int(body.get("user_id", 0))
                if uid == 1:
                    return self._json({"ok": True, "result": {
                        "status": "administrator" if Fake.bot_is_admin else "left"}})
                return self._json({"ok": True, "result": {
                    "status": "member" if uid in Fake.members else "left"}})
            if method == "getChat":
                return self._json({"ok": True, "result": {"id": -1001, "title": "Test channel",
                                                          "username": "testchannel"}})
            if method == "createInvoiceLink":
                return self._json({"ok": True, "result": "https://t.me/$invoice-" + body["payload"]})
            if method == "sendMessage" and body.get("parse_mode") and "<oops" in body.get("text", ""):
                return self._json({"ok": False, "error_code": 400,
                                   "description": "Bad Request: can't parse entities"})
            if method == "sendMessage" and body.get("chat_id") == 403:
                return self._json({"ok": False, "error_code": 403,
                                   "description": "Forbidden: bot was blocked by the user"})
            if method in ("sendMessage", "copyMessage", "sendDocument"):
                Fake.message_id += 1
                return self._json({"ok": True, "result": {"message_id": Fake.message_id,
                                                          "chat": {"id": body.get("chat_id")}}})
            return self._json({"ok": True, "result": True})
        Fake.calls.append((path, body))
        if path == "/cryptobot/createInvoice":
            return self._json({"ok": True, "result": {
                "invoice_id": 777, "mini_app_invoice_url": "https://t.me/CryptoBot/app?startapp=x"}})
        if path == "/yookassa/payments":
            pid = f"yk-{len(Fake.yookassa) + 1}"
            Fake.yookassa[pid] = {"id": pid, "status": "pending", "paid": False,
                                  "amount": body["amount"], "metadata": body["metadata"]}
            return self._json({"id": pid, "confirmation": {"confirmation_url": f"https://yoomoney/{pid}"}})
        if path == "/platega/transaction/process":
            return self._json({"transactionId": "pl-1", "redirect": "https://platega/pay/pl-1"})
        if path == "/paypalych/bill/create":
            return self._json({"bill_id": "pp-" + body["order_id"],
                               "link_page_url": "https://pally/pay/" + body["order_id"]})
        self._json({"ok": False}, 404)


@pytest.fixture(scope="session")
def fake_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


# ---------------------------------------------------------------------------
#  The app with a fresh database per test
# ---------------------------------------------------------------------------
@pytest.fixture()
def env(fake_server, monkeypatch, tmp_path):
    values = {
        "BOT_TOKEN": BOT_TOKEN,
        "SECRET_KEY": "x" * 40,
        "ADMIN_LOGIN": "admin",
        "ADMIN_PASSWORD": "correct-horse-battery",
        "ADMIN_TELEGRAM_IDS": "999",
        "PUBLIC_URL": "https://shop.example.com",
        "DATABASE_URL": TEST_DB or f"sqlite:///{tmp_path}/test.db",
        "DATA_DIR": str(tmp_path),
        "SHOP_NO_JOBS": "1",
        "BOT_SETUP": "0",
        "TG_API_BASE": fake_server,
        "PAYMENTS_TEST_MODE": "0",
        "CARD_GATEWAY": "",
        "CRYPTOBOT_TOKEN": "",
        "XUI_URL": "",
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    if TEST_DB:
        # One shared PostgreSQL database: start every test from empty tables.
        from sqlalchemy import create_engine
        from app.db import Base
        from app import models  # noqa: F401
        eng = create_engine(TEST_DB)
        Base.metadata.drop_all(eng)
        eng.dispose()
    from app import config
    config.reload()
    from app import notify
    monkeypatch.setattr(notify, "API_BASE", fake_server)
    notify._me.clear()
    from app.payments import cryptobot, platega, yookassa
    monkeypatch.setattr(cryptobot, "_api", lambda m: f"{fake_server}/cryptobot/{m}")
    monkeypatch.setattr(yookassa, "API", f"{fake_server}/yookassa")
    monkeypatch.setattr(platega, "API", f"{fake_server}/platega")
    Fake.reset()
    from app import access, auth
    from app.routers import app_api
    access._links_cache.clear()
    app_api._recent.clear()
    app_api._reissued.clear()
    auth._attempts.clear()
    auth._all_fails.clear()
    auth._revoked = None
    access._sync_cursor = 0
    from app.routers import pay
    pay._noted.clear()
    pay._pp_checked.clear()
    from app.routers import bot
    bot._seen.clear()
    bot._busy.clear()
    # GitHub is never asked from the tests (test_updates.py brings its own fake)
    import httpx
    from app import updates
    monkeypatch.setattr(updates, "_http_get", lambda url, headers: httpx.Response(404))
    yield config.settings


def demo_shop() -> None:
    """What the owner of a working shop has set up: four plans, the trial,
    the referral program in days. A fresh install has none of it (see
    test_clean_start.py); most tests are about a shop that is running."""
    from app import billing, core
    from app.db import SessionLocal
    from app.models import Plan
    db = SessionLocal()
    try:
        db.add_all([
            Plan(name="1 месяц", name_en="1 month", days=30, price=199, devices=3, sort=1),
            Plan(name="3 месяца", name_en="3 months", days=90, price=499, old_price=597, devices=3, sort=2),
            Plan(name="6 месяцев", name_en="6 months", days=180, price=899, old_price=1194,
                 badge="ХИТ", devices=3, sort=3),
            Plan(name="1 год", name_en="1 year", days=365, price=1490, old_price=2388,
                 badge="−38%", devices=3, sort=4),
        ])
        db.commit()
        billing.set_trial(db, enabled=True, days=3, devices=1)
        core.put(db, "ref_enabled", "1")
    finally:
        db.close()


@pytest.fixture()
def client(env, request):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        if not request.node.get_closest_marker("clean"):
            demo_shop()
        yield c


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    yield s
    s.close()


# ---------------------------------------------------------------------------
#  Telegram initData
# ---------------------------------------------------------------------------
def init_data(user_id: int, token: str = BOT_TOKEN, lang: str = "ru",
              username: str = "", auth_date: int | None = None, start: str = "") -> str:
    user = {"id": user_id, "first_name": f"User{user_id}", "language_code": lang}
    if username:
        user["username"] = username
    fields = {"auth_date": str(auth_date or int(time.time())), "query_id": "AAE",
              "user": json.dumps(user, separators=(",", ":"))}
    if start:
        fields["start_param"] = start
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def tg(user_id: int, **kw) -> dict:
    return {"X-Init-Data": init_data(user_id, **kw)}


@pytest.fixture()
def admin(client):
    r = client.post("/api/admin/login", json={"login": "admin", "password": "correct-horse-battery"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


# ---------------------------------------------------------------------------
#  Real 3x-ui panels
# ---------------------------------------------------------------------------
def _panel_env(prefix: str) -> dict | None:
    url = os.getenv(f"XUI_TEST_{prefix}_URL")
    return {"url": url,
            "token": os.getenv(f"XUI_TEST_{prefix}_TOKEN", ""),
            "user": os.getenv(f"XUI_TEST_{prefix}_USER", ""),
            "password": os.getenv(f"XUI_TEST_{prefix}_PASS", ""),
            "inbound": os.getenv(f"XUI_TEST_{prefix}_INBOUND", ""),
            "sub": os.getenv(f"XUI_TEST_{prefix}_SUB", "")} if url else None


V3 = _panel_env("V3")
V2 = _panel_env("V2")
panels = pytest.mark.skipif(not (V3 and V2), reason="real 3x-ui panels are not configured")


@pytest.fixture()
def servers(client, admin):
    """Both live panels registered as servers in the shop."""
    ids = []
    for name, country, p in (("Germany", "DE", V3), ("Finland", "FI", V2)):
        body = {"name": name, "country": country, "url": p["url"], "username": p["user"],
                "password": p["password"], "api_token": p["token"],
                "inbound_ids": p["inbound"], "sub_url": p["sub"]}
        r = client.post("/api/admin/servers", json=body, headers=admin)
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    return ids
