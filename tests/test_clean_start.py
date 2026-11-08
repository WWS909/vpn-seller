"""A fresh install is empty: it sells nothing, gives no discounts and has
no documents until the owner decides. Templates are offered, not applied."""

from __future__ import annotations

import time

import pytest

from conftest import Fake, tg

pytestmark = pytest.mark.clean


def test_nothing_is_sold_or_given_away(client, admin):
    cfg = client.get("/api/app/config").json()
    assert cfg["plans"] == []                       # no plans, the trial is off
    assert cfg["referral"]["enabled"] is False
    assert cfg["sale"]["percent"] == 0
    assert cfg["docs"] is False
    pub = client.get("/api/public").json()
    assert pub["plans"] == [] and pub["docs"] == {}
    assert client.get("/api/app/referrals", headers=tg(501)).json() == {"enabled": False, "wallet": None}
    # The trial exists only as a switched-off row for /trial and the panel.
    tr = client.get("/api/admin/trial", headers=admin).json()
    assert tr["enabled"] is False


def test_no_discount_notices_by_default(client, admin, db):
    """"Вы заглянули в VPN, но ещё не подключились… скидка 10%" must not go
    out on its own: every notice with a discount is off."""
    warm = {t["key"]: t for t in client.get("/api/admin/warm", headers=admin).json()["triggers"]}
    for key in ("trial_end", "expired", "winback", "no_purchase"):
        assert warm[key]["on"] is False, key
    assert warm["expiring"]["on"] and warm["expiring"]["percent"] == 0     # service notices stay
    assert warm["traffic"]["on"] and warm["traffic"]["percent"] == 0
    assert warm["unpaid"]["on"] and warm["unpaid"]["percent"] == 0       # a plain reminder, no discount
    from app import jobs
    from app.models import Offer, User
    client.get("/api/app/me", headers=tg(502))
    u = db.query(User).filter(User.tg_id == 502).one()
    u.created_at = int(time.time()) - 2 * 86400        # came two days ago, never bought
    db.commit()
    before = len(Fake.sent("sendMessage"))
    jobs.job_warm(db)
    assert len(Fake.sent("sendMessage")) == before
    assert db.query(Offer).count() == 0


def test_templates_are_offered_not_applied(client, admin):
    tpl = client.get("/api/admin/templates", headers=admin).json()
    assert [p["days"] for p in tpl["plans"]] == [30, 90, 180, 365]
    assert [p["price"] for p in tpl["plans"]] == [199, 499, 899, 1490]     # RUB suggestions
    assert "{brand}" in tpl["docs"]["terms"]["ru"] and tpl["docs"]["privacy"]["en"]
    assert tpl["trial"] == {"days": 3, "devices": 1}
    assert client.get("/api/admin/plans", headers=admin).json() == []
    assert client.get("/api/app/config").json()["plans"] == []


def test_unknown_currency_gets_no_guessed_prices():
    from app import starter
    assert [p["price"] for p in starter.plan_templates("XYZ")] == [0, 0, 0, 0]
    assert starter.plan_templates("usd")[0]["price"] == 3


def test_empty_shop_page_still_opens(client):
    for path in ("/", "/legal", "/app"):
        assert client.get(path).status_code == 200


def test_documents_render_safely(client, admin):
    body = ("# Условия\nСервис {brand} от {owner}.\n\n- пункт один\n- **важно**\n\n"
            "<script>alert(1)</script>")
    r = client.post("/api/admin/settings", headers=admin, json={
        "brand_name": "Orbit", "legal_owner": "ИП <Иванов>", "terms_ru": body, "privacy_ru": "Текст."})
    assert r.status_code == 200
    docs = client.get("/api/public").json()["docs"]
    html = docs["terms"]["ru"]
    assert "<h3>Условия</h3>" in html and "Orbit" in html and "ИП &lt;Иванов&gt;" in html
    assert "<ul><li>пункт один</li><li><b>важно</b></li></ul>" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert docs["terms"]["en"] == html                  # no English text: the Russian one is shown
    assert client.get("/api/app/config").json()["docs"] is True
    too_long = client.post("/api/admin/settings", headers=admin, json={"terms_en": "x" * 20001})
    assert too_long.status_code == 400


def test_upgrade_keeps_an_existing_shop_as_it_was(client, db):
    """seed() only fills an empty database: plans an owner already has are
    left alone on the next start."""
    from app.main import seed
    from app.models import Plan
    db.add(Plan(name="Мой тариф", days=30, price=150))
    db.commit()
    count = db.query(Plan).count()
    seed(db)
    assert db.query(Plan).count() == count
