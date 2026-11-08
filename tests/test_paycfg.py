"""Payment gateways set up in the panel: laid over .env, applied at once,
secrets never sent back."""
from __future__ import annotations

import os

from conftest import tg


def _methods(client):
    return [m["gateway"] for m in client.get("/api/admin/payments", headers=client.admin_h).json()["methods"]]


def test_gateway_keys_from_the_panel(client, admin):
    client.admin_h = admin
    v = client.get("/api/admin/payments", headers=admin).json()
    ids = [g["id"] for g in v["gateways"]]
    assert {"cryptobot", "yookassa", "freekassa", "platega", "paypalych", "lava", "heleket", "lavepay"} <= set(ids)
    assert v["stars"] is True and "cryptobot" not in _methods(client)
    # CryptoBot from the panel: works at once, the token never comes back
    r = client.post("/api/admin/payments", headers=admin, json={"values": {"CRYPTOBOT_TOKEN": "123:SECRETabc"}})
    assert r.status_code == 200, r.text
    assert "SECRETabc" not in r.text
    cb = next(g for g in r.json()["gateways"] if g["id"] == "cryptobot")
    assert cb["ready"] and cb["active"] and cb["fields"][0] == {**cb["fields"][0], "set": True, "value": "", "from": "panel"}
    assert cb["hook"].endswith("/api/pay/cryptobot")
    assert "cryptobot" in _methods(client)
    assert any(m["id"] == "crypto" for m in client.get("/api/app/config").json()["methods"])
    # an empty secret keeps the stored one
    client.post("/api/admin/payments", headers=admin, json={"values": {"CRYPTOBOT_TOKEN": ""}})
    assert "cryptobot" in _methods(client)
    # card: YooKassa chosen and filled
    client.post("/api/admin/payments", headers=admin, json={"values": {
        "YOOKASSA_SHOP_ID": "12345", "YOOKASSA_SECRET_KEY": "live_x", "CARD_GATEWAY": "yookassa"}})
    assert "yookassa" in _methods(client)
    # "off" turns it off, Stars can be turned off too
    client.post("/api/admin/payments", headers=admin, json={"values": {"CARD_GATEWAY": "off", "STARS_ENABLED": "false"}})
    assert set(_methods(client)) == {"cryptobot"}
    # removing the panel's keys brings back what .env had (nothing here)
    client.post("/api/admin/payments", headers=admin, json={"values": {"CRYPTOBOT_TOKEN": None, "STARS_ENABLED": None,
                                                                         "CARD_GATEWAY": None}})
    assert "cryptobot" not in _methods(client) and "stars" in _methods(client)
    assert not os.environ.get("CRYPTOBOT_TOKEN")
    logs = client.get("/api/admin/logs?limit=50", headers=admin).json()
    assert any(r["action"] == "Payment settings changed" and "SECRET" not in r["details"] for r in logs)


def test_bad_values_are_refused(client, admin):
    for values in ({"CARD_GATEWAY": "paypal"}, {"NALOG_INN": "123"}, {"BOT_TOKEN": "x"},
                   {"STARS_ENABLED": "maybe"}, {"CRYPTOBOT_TOKEN": "a\nb"}):
        r = client.post("/api/admin/payments", headers=admin, json={"values": values})
        assert r.status_code == 400, values
    assert client.get("/api/admin/payments").status_code == 401


def test_panel_keys_survive_a_restart(env, admin, client):
    client.post("/api/admin/payments", headers=admin, json={"values": {"CRYPTOBOT_TOKEN": "123:abc"}})
    os.environ.pop("CRYPTOBOT_TOKEN", None)
    env.__init__()
    assert not env.cryptobot_token
    from app import paycfg
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        paycfg.apply(db)              # what the start of the shop does
    finally:
        db.close()
    assert env.cryptobot_token == "123:abc"
    client.post("/api/admin/payments", headers=admin, json={"values": {"CRYPTOBOT_TOKEN": None}})
    assert not env.cryptobot_token
    assert client.get("/api/app/me", headers=tg(5)).status_code == 200


def test_several_gateways_and_the_new_ones(client, admin):
    r = client.post("/api/admin/payments", headers=admin, json={"values": {
        "YOOKASSA_SHOP_ID": "1", "YOOKASSA_SECRET_KEY": "s",
        "ROLLYPAY_API_KEY": "rpk_live_x", "ROLLYPAY_SIGNING_SECRET": "whs",
        "CISPAY_SHOP_ID": "u-1", "CISPAY_API_KEY": "cis_sec_x",
        "CARD_GATEWAY": "rollypay, yookassa,cispay,rollypay", "STARS_ENABLED": "false"}})
    assert r.status_code == 200, r.text
    ms = client.get("/api/app/config").json()["methods"]
    assert [(m["id"], m["gateway"]) for m in ms] == [("card", "rollypay"), ("yookassa", "yookassa"),
                                                      ("cispay", "cispay")]
    assert ms[2]["name"] == "cisPay"
    v = r.json()
    assert {g["id"] for g in v["gateways"] if g["active"]} == {"rollypay", "yookassa", "cispay"}
    assert v["card"] == "rollypay,yookassa,cispay"
    assert client.post("/api/admin/payments", headers=admin,
                       json={"values": {"CARD_GATEWAY": "rollypay,paypal"}}).status_code == 400
    client.post("/api/admin/payments", headers=admin, json={"values": {k: None for k in (
        "YOOKASSA_SHOP_ID", "YOOKASSA_SECRET_KEY", "ROLLYPAY_API_KEY", "ROLLYPAY_SIGNING_SECRET",
        "CISPAY_SHOP_ID", "CISPAY_API_KEY", "CARD_GATEWAY", "STARS_ENABLED")}})
    assert [m["id"] for m in client.get("/api/app/config").json()["methods"]] == ["stars"]


def test_saving_keeps_the_tunnel_address(client, admin, env):
    env.public_url = "https://moved-along.trycloudflare.com"      # what follow_tunnel does at run time
    client.post("/api/admin/payments", headers=admin, json={"values": {"CRYPTOBOT_TOKEN": "1:x"}})
    assert env.public_url == "https://moved-along.trycloudflare.com"
    assert env.cryptobot_token == "1:x"
    client.post("/api/admin/payments", headers=admin, json={"values": {"CRYPTOBOT_TOKEN": None}})
