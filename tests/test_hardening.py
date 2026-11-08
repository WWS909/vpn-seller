"""
Protection against people who poke at a public shop: huge requests, fake
payment notifications, guessing passwords and promo codes, a stolen admin
token, a picture built to eat the memory, a hacked script CDN.
"""

import struct
import zlib
from pathlib import Path

from conftest import tg

WEB = Path(__file__).resolve().parent.parent / "web"


def test_huge_request_is_refused_before_it_is_read(client):
    r = client.post("/api/admin/login", content=b"x" * (7 * 1024 * 1024),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_mini_app_loads_scripts_only_from_the_shop_and_telegram(client):
    r = client.get("/app")
    csp = r.headers.get("content-security-policy", "")
    assert "connect-src 'self'" in csp and "https://telegram.org" in csp
    assert "cdnjs" not in r.text and "/vendor/qrcode.js" in r.text
    js = client.get("/vendor/qrcode.js")
    assert js.status_code == 200 and "qrcode" in js.text
    assert client.get("/vendor/other.js").status_code == 404


def test_fake_paypalych_notices_do_not_fill_the_log(client, db):
    from app.models import Log
    before = db.query(Log).count()
    for path in ("/api/pay/paypalych", "/api/pay/paypalych/refund", "/api/pay/paypalych/chargeback"):
        for _ in range(20):
            assert client.post(path, data={"InvId": "1", "x": "fake"}).status_code == 404   # not even set up
    db.expire_all()
    assert db.query(Log).count() == before


def test_rejected_notices_are_logged_once_per_address(client, db, env, monkeypatch):
    from app.models import Log
    monkeypatch.setenv("CRYPTOBOT_TOKEN", "1:crypto")
    env.__init__()
    for _ in range(30):
        r = client.post("/api/pay/cryptobot", content=b"{}", headers={"crypto-pay-api-signature": "bad"})
        assert r.status_code == 403
    db.expire_all()
    assert db.query(Log).filter(Log.action == "Rejected CryptoBot notification").count() == 1


def test_sign_out_ends_the_token_at_once(client, admin):
    assert client.get("/api/admin/settings", headers=admin).status_code == 200
    assert client.post("/api/admin/logout", headers=admin).status_code == 200
    assert client.get("/api/admin/settings", headers=admin).status_code == 401
    from app import auth
    auth._revoked = None                                   # as after a restart: still signed out
    assert client.get("/api/admin/settings", headers=admin).status_code == 401


def test_new_admin_password_ends_old_sessions(client, admin, env, monkeypatch):
    assert client.get("/api/admin/settings", headers=admin).status_code == 200
    monkeypatch.setattr(env, "admin_password", "a-brand-new-password")
    assert client.get("/api/admin/settings", headers=admin).status_code == 401


def test_password_guessing_from_many_addresses_is_stopped(client):
    for i in range(60):
        r = client.post("/api/admin/login", json={"login": "admin", "password": f"guess{i}"},
                        headers={"X-Forwarded-For": f"10.0.{i // 250}.{i % 250}"})
        assert r.status_code == 401
    r = client.post("/api/admin/login", json={"login": "admin", "password": "correct-horse-battery"},
                    headers={"X-Forwarded-For": "10.9.9.9"})
    assert r.status_code == 429                            # everyone waits; the bot sign-in still works


def _png_header(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00" * 64)) + \
        chunk(b"IEND", b"")


def test_logo_claiming_a_giant_size_is_refused_without_decoding(client, admin):
    r = client.post("/api/admin/brand/logo", headers=admin,
                    files={"file": ("logo.png", _png_header(30000, 30000), "image/png")})
    assert r.status_code == 400 and "2048" in r.text


def test_promo_codes_cannot_be_guessed_fast(client):
    plan = client.get("/api/app/config").json()["plans"][0]
    codes = [client.post("/api/app/quote", headers=tg(8801), json={"plan_id": plan["id"], "promo": f"G{i}"})
             .status_code for i in range(25)]
    assert 429 in codes and codes.count(429) >= 4


def test_panel_data_is_escaped_in_the_admin_page():
    """A hostile VPN panel names an inbound `x" onfocus=...`: the admin
    page must show it as text, not run it."""
    page = (WEB / "admin.html").read_text(encoding="utf-8")
    assert 'value="${i.id}"' not in page and ":${i.port}" not in page and '" · " + i.clients' not in page


def _admin_notes():
    from conftest import Fake
    return [m["text"] for m in Fake.sent("sendMessage") if m["chat_id"] == 999]


def test_sign_in_from_a_new_address_is_reported(client):
    from app import auth
    auth._attempts.clear(); auth._all_fails.clear()
    login = {"login": "admin", "password": "correct-horse-battery"}
    chrome = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/141.0 Safari/537.36"}
    # the very first sign-in after install is not news
    assert client.post("/api/admin/login", json=login, headers={"X-Forwarded-For": "1.1.1.1", **chrome}).status_code == 200
    assert not _admin_notes()
    # the same address again: nothing; another one: the admins are told, with the device
    assert client.post("/api/admin/login", json=login, headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert not _admin_notes()
    assert client.post("/api/admin/login", json=login, headers={"X-Forwarded-For": "5.6.7.8", **chrome}).status_code == 200
    notes = _admin_notes()
    assert len(notes) == 1 and "5.6.7.8" in notes[0] and "Chrome · Windows" in notes[0] and "vpn-seller" in notes[0]
    assert auth.device("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 "
                       "Mobile/15E148 Safari/604.1") == "Safari · iPhone"


def test_password_guessing_is_reported_once(client):
    from app import auth
    auth._attempts.clear(); auth._all_fails.clear()
    for _ in range(auth.MAX_ATTEMPTS):
        client.post("/api/admin/login", json={"login": "admin", "password": "nope"},
                    headers={"X-Forwarded-For": "6.6.6.6"})
    for _ in range(3):                                     # shut now: no more notes
        assert client.post("/api/admin/login", json={"login": "admin", "password": "nope"},
                           headers={"X-Forwarded-For": "6.6.6.6"}).status_code == 429
    notes = _admin_notes()
    assert len(notes) == 1 and "подбирает пароль" in notes[0] and "6.6.6.6" in notes[0]
    auth._attempts.clear(); auth._all_fails.clear()


def test_guessing_from_many_addresses_is_reported_once(client):
    from app import auth
    auth._attempts.clear(); auth._all_fails.clear()
    for i in range(auth.MAX_ALL + 3):
        client.post("/api/admin/login", json={"login": "admin", "password": f"g{i}"},
                    headers={"X-Forwarded-For": f"10.1.{i // 250}.{i % 250}"})
    notes = [n for n in _admin_notes() if "разных адресов" in n]
    assert len(notes) == 1 and "/admin" in notes[0]
    auth._attempts.clear(); auth._all_fails.clear()
