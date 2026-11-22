"""The owner's own access (app/vip.py): no end, no limits, in no list."""

from __future__ import annotations

from conftest import tg


def test_owner_gets_access_without_end_or_limits(client, admin, db):
    from app import vip
    from app.models import User
    r = client.get("/api/app/me", headers=tg(999))
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["vip"] is True and me["active"] is True and me["device_limit"] == 0 and me["traffic_limit"] == 0
    assert me["expires_at"] >= vip.FOREVER
    # an admin edit that shortens it is undone on the next opening
    u = db.query(User).filter(User.tg_id == 999).one()
    u.expires_at, u.device_limit = 1, 2
    db.commit()
    me = client.get("/api/app/me", headers=tg(999)).json()
    assert me["expires_at"] >= vip.FOREVER and me["device_limit"] == 0
    # nobody else gets it, and it is not counted as a customer
    other = client.get("/api/app/me", headers=tg(4242)).json()
    assert other["vip"] is False and not other["active"]
    plans = client.get("/api/app/config").json()["plans"]
    assert not any("vip" in (p.get("name") or "").lower() for p in plans)
    stats = client.get("/api/admin/overview", headers=admin).json()
    assert stats["active"] == 0


def test_app_has_no_wide_telegram_button_and_refreshes_itself(client):
    page = client.get("/app").text
    assert "tg.MainButton.hide()" in page and "mb.setText" not in page
    assert 'tg.onEvent("activated", backAgain)' in page and "function celebrateTrial(" in page
    assert "async function reload()" in page
