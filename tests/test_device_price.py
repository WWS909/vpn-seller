"""An extra device costs its price for every month of the term."""
from __future__ import annotations

import pytest

from app import billing
from conftest import tg


def test_months_of_a_term():
    assert billing.device_months(30) == 1 and billing.device_months(31) == 1
    assert billing.device_months(90) == 3 and billing.device_months(180) == 6
    assert billing.device_months(365) == 12 and billing.device_months(366) == 12
    assert billing.device_months(7) == pytest.approx(7 / 30)
    assert billing.device_months(0) == 0


@pytest.mark.parametrize("days, extra, want", [(365, 1, 600), (30, 2, 100), (90, 1, 150), (7, 3, 35)])
def test_quote_counts_every_month(client, admin, days, extra, want):
    client.post("/api/admin/settings", headers=admin, json={"device_price": "50", "max_devices": "10"})
    r = client.post("/api/admin/plans", headers=admin, json={
        "name": f"P{days}", "name_en": f"P{days}", "days": days, "price": 1000, "devices": 2})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    who = tg(81000 + days)
    client.get("/api/app/me", headers=who)
    q = client.post("/api/app/quote", headers=who, json={"plan_id": pid, "devices": 2 + extra}).json()
    assert q["base"] == pytest.approx(1000 + want), q
    assert q["extra_devices"] == extra
