"""The VPN apps offered for connecting: the owner's choice and order, the
device-id rule, and the one-tap links each app understands."""
from __future__ import annotations

from urllib.parse import quote

from app import clients

SUB = "https://shop.example/sub/abc123DEF456ghi789"


def _link(app: dict, url: str, name: str) -> str:
    # the same substitution the mini app does (appLink in web/index.html)
    return (app["link"].replace("{enc}", quote(url, safe=""))
            .replace("{url}", url).replace("{name}", quote(name, safe="")))


def test_default_list_and_links(client):
    apps = client.get("/api/app/config").json()["apps"]
    assert [a["id"] for a in apps] == ["happ", "v2raytun", "incy", "onexray", "karing", "hiddify"]
    by = {a["id"]: a for a in apps}
    for a in apps:
        assert set(a["get"]) == {"ios", "android", "windows", "macos"}, a["id"]
        assert all(u.startswith("https://") for u in a["get"].values()), a["id"]
    enc = quote(SUB, safe="")
    assert _link(by["happ"], SUB, "Stars") == "happ://add/" + SUB
    assert _link(by["v2raytun"], SUB, "Stars") == "v2raytun://import/" + SUB
    assert _link(by["incy"], SUB, "Stars") == "incy://add/" + SUB
    assert _link(by["onexray"], SUB, "Stars") == f"onexray://onexray.com/sub/add?url={enc}#Stars"
    assert _link(by["karing"], SUB, "Stars") == f"karing://install-config?url={enc}&name=Stars"
    assert _link(by["hiddify"], SUB, "Stars") == f"hiddify://import/{SUB}#Stars"
    # a shop name with spaces and Cyrillic stays one piece of the link
    assert _link(by["onexray"], SUB, "Мой VPN").endswith("#%D0%9C%D0%BE%D0%B9%20VPN")


def test_owner_picks_and_orders_apps(client, admin):
    assert client.post("/api/admin/settings", headers=admin,
                       json={"connect_apps": " OneXray, incy ,onexray,nope"}).status_code == 200
    assert [a["id"] for a in client.get("/api/app/config").json()["apps"]] == ["onexray", "incy"]
    s = client.get("/api/admin/settings", headers=admin).json()
    assert s["values"]["connect_apps"] == "onexray,incy"
    assert {a["id"] for a in s["apps"]} == set(clients.APPS)
    # nothing known: refused, the saved list stays
    r = client.post("/api/admin/settings", headers=admin, json={"connect_apps": "clash, sing-box"})
    assert r.status_code == 400
    assert [a["id"] for a in client.get("/api/app/config").json()["apps"]] == ["onexray", "incy"]


def test_device_id_required_offers_only_apps_that_send_it(client, admin):
    client.post("/api/admin/settings", headers=admin,
                json={"connect_apps": "hiddify,incy,onexray,happ", "require_hwid": "1"})
    assert [a["id"] for a in client.get("/api/app/config").json()["apps"]] == ["incy", "happ"]
    # only apps without a device id chosen: the ones that send it are offered instead
    client.post("/api/admin/settings", headers=admin, json={"connect_apps": "onexray,karing"})
    assert [a["id"] for a in client.get("/api/app/config").json()["apps"]] == ["happ", "v2raytun", "incy"]
    client.post("/api/admin/settings", headers=admin, json={"require_hwid": "0"})
    assert [a["id"] for a in client.get("/api/app/config").json()["apps"]] == ["onexray", "karing"]


def test_app_and_panel_have_the_pieces(client):
    app, panel = client.get("/app").text, client.get("/admin").text
    assert "function appLink(" in app and "function pickApp(" in app and 'appPick:"Приложение"' in app
    assert 'appPick:"App"' in app
    assert 'data-k="connect_apps"' in panel and "function drawApps(" in panel
    assert 'apT:"Приложения для подключения"' in panel and 'apT:"Apps for connecting"' in panel


# ---------------------------------------------------------------- one code field
def test_code_field_takes_promo_codes_of_any_shape(client, admin):
    from conftest import tg
    for code in ("SALE-2026!", "Весна", "q", "ЛЕТО_50%"):
        r = client.post("/api/admin/promos", headers=admin, json={"code": code, "kind": "percent", "value": 20})
        assert r.status_code == (422 if len(code) < 2 else 200), (code, r.text)
    who = tg(70707)
    client.get("/api/app/me", headers=who)
    for typed in ("sale-2026!", " ЛЕТО_50% ", "весна"):
        r = client.post("/api/app/code", json={"code": typed}, headers=who)
        assert r.status_code == 200, (typed, r.text)
        assert r.json()["kind"] == "promo" and r.json()["promo"]["value"] == 20
    # a code shaped like a gift but unknown, and anything else unknown
    for typed in ("ABCD-EFGH", "nope"):
        r = client.post("/api/app/code", json={"code": typed}, headers=who)
        assert r.status_code == 404 and "кода нет" in r.json()["detail"].lower(), r.text
    # spent or wrong ones say why
    client.post("/api/admin/promos", headers=admin, json={"code": "OLD", "kind": "percent", "value": 5,
                                                          "expires_at": 1000})
    r = client.post("/api/app/code", json={"code": "old"}, headers=who)
    assert r.status_code == 400


# ---------------------------------------------------------------- the /open bridge
def test_open_page_hands_over_only_vpn_app_links(client):
    from urllib.parse import quote
    link = "onexray://onexray.com/sub/add?url=" + quote(SUB, safe="") + "#Stars"
    r = client.get("/open", params={"to": link}, headers={"Accept-Language": "ru-RU"})
    assert r.status_code == 200 and "Открываем приложение" in r.text
    assert 'href="onexray://onexray.com/sub/add?url=' in r.text and '"' + link + '"' in r.text
    assert "script-src 'unsafe-inline'" in r.headers["content-security-policy"]
    for bad in ("https://evil.example/", "javascript:alert(1)", "happ://add/x\"><script>", "tg://resolve",
                "", "happ:add"):
        assert client.get("/open", params={"to": bad}).status_code == 404, bad
    assert "Opening the app" in client.get("/open", params={"to": "happ://add/" + SUB}).text
