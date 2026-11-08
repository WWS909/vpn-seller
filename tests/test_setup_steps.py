"""The launch checklist: required and recommended steps, every tick from a fact."""

from __future__ import annotations

import pytest


REQUIRED = ["server", "plans", "pay", "docs"]
RECOMMENDED = ["brand", "logo", "trial", "contacts", "bot_texts", "faq", "referral", "warm",
               "domain", "backup"]


def _steps(client, admin) -> dict:
    d = client.get("/api/admin/setup", headers=admin).json()
    return {s["id"]: s for s in d["steps"]}


@pytest.mark.clean
def test_fresh_shop(client, admin):
    d = client.get("/api/admin/setup", headers=admin).json()
    assert [s["id"] for s in d["steps"] if s["group"] == "required"] == REQUIRED
    assert [s["id"] for s in d["steps"] if s["group"] == "recommended"] == RECOMMENDED
    s = {x["id"]: x for x in d["steps"]}
    assert not s["server"]["done"]
    assert not s["plans"]["done"]          # a fresh shop sells nothing
    assert s["pay"]["done"] and s["pay"]["note"] == "stars"   # Stars work out of the box
    assert not s["docs"]["done"]
    for key in ("brand", "logo", "trial", "contacts", "bot_texts", "faq", "referral", "warm"):
        assert not s[key]["done"], key
    assert s["backup"]["done"]             # the daily backup is on by default
    # The same list is in the overview, for the card on the first page.
    o = client.get("/api/admin/overview", headers=admin).json()
    assert [x["id"] for x in o["setup"]] == REQUIRED + RECOMMENDED and o["setup_hidden"] is False


@pytest.mark.clean
def test_steps_tick_from_facts(client, admin):
    tpl = client.get("/api/admin/templates", headers=admin).json()
    assert not _steps(client, admin)["plans"]["done"]          # looking at templates changes nothing
    p = tpl["plans"][0]
    r = client.post("/api/admin/plans", headers=admin, json={
        "name": p["name"], "name_en": p["name_en"], "days": p["days"], "price": p["price"], "devices": 3})
    assert r.status_code == 200, r.text
    s = _steps(client, admin)
    assert s["plans"]["done"] and s["plans"]["note"] == "1"
    client.post("/api/admin/settings", headers=admin, json={"terms_ru": tpl["docs"]["terms"]["ru"]})
    assert not _steps(client, admin)["docs"]["done"]           # both documents are needed
    client.post("/api/admin/settings", headers=admin, json={"privacy_ru": tpl["docs"]["privacy"]["ru"]})
    assert _steps(client, admin)["docs"]["done"]
    client.post("/api/admin/settings", headers=admin, json={
        "brand_name": "Orbit", "legal_owner": "ИП Иванов", "support_url": "https://t.me/orbit_help",
        "bot_about_ru": "Быстрый VPN", "faq_json": '[{"q":"Как?","a":"Так","lang":"ru"}]'})
    client.put("/api/admin/trial", headers=admin, json={"enabled": True, "days": 3, "devices": 1})
    s = _steps(client, admin)
    for key in ("brand", "contacts", "bot_texts", "faq", "trial"):
        assert s[key]["done"], key


@pytest.mark.clean
def test_referral_in_money_mode_needs_payout_methods(client, admin):
    client.post("/api/admin/settings", headers=admin, json={"ref_enabled": "1", "ref_mode": "money"})
    s = _steps(client, admin)["referral"]
    assert not s["done"] and s["note"] == "methods"
    client.post("/api/admin/settings", headers=admin, json={
        "ref_payout_methods": '[{"kind":"card","ru":"Карта","en":"Card"}]'})
    assert _steps(client, admin)["referral"]["done"]


def test_hidden_card_keeps_the_checklist(client, admin):
    r = client.post("/api/admin/settings", json={"setup_hidden": "1"}, headers=admin)
    assert r.status_code == 200
    d = client.get("/api/admin/setup", headers=admin).json()
    assert d["hidden"] is True and len(d["steps"]) == len(REQUIRED + RECOMMENDED)
    assert client.get("/api/admin/overview", headers=admin).json()["setup_hidden"] is True
