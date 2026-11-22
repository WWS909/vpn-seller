"""
VPN Seller Node: the shop's own VPN server.

The real agent (scripts/vpn-seller-node.py) runs here as a process, over real
HTTPS with its own certificate; only the xray binary is replaced by
tests/fake_xray.py, which answers the same command lines and keeps the
"running" Xray in a JSON file the tests can read and poke.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from conftest import tg

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "scripts" / "vpn-seller-node.py"
FAKE = ROOT / "tests" / "fake_xray.py"


def _agent_version() -> str:
    import re
    return re.search(r'^VERSION = "([^"]+)"', AGENT.read_text(), re.M).group(1)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Agent:
    def __init__(self, folder: Path):
        self.dir = folder
        folder.mkdir(parents=True, exist_ok=True)
        self.port = _free_port()
        # its own copy: the agent can replace itself (the update test)
        self.agent = folder / "agent.py"
        self.agent.write_text(AGENT.read_text())
        self.fake_state = folder / "xray-state.json"
        self.conf = folder / "xray" / "config.json"
        wrapper = folder / "xray-bin"
        wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
        wrapper.chmod(0o755)
        self.env = {**os.environ,
                    "BN_STATE_DIR": str(folder / "state"), "BN_XRAY_BIN": str(wrapper),
                    "BN_XRAY_CONF": str(self.conf), "BN_XRAY_ASSETS": str(folder / "assets"),
                    "BN_RESTART_CMD": f"{wrapper} restart", "BN_POLL": "3600",
                    "BN_API_ADDR": "127.0.0.1:10085", "FAKE_XRAY_STATE": str(self.fake_state)}
        (folder / "state").mkdir(mode=0o700, exist_ok=True)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                        "ec_paramgen_curve:prime256v1", "-nodes", "-days", "30", "-subj", "/CN=vpn-seller-node",
                        "-keyout", str(folder / "state" / "key.pem"), "-out", str(folder / "state" / "cert.pem")],
                       check=True, capture_output=True)
        self.run("init", "--host", "127.0.0.1", "--listen", f"127.0.0.1:{self.port}")
        self.start()
        self.line = self.run("connect").strip()

    def start(self):
        """What systemd does: (re)start the agent and wait for its port."""
        self.proc = subprocess.Popen([sys.executable, str(self.agent), "serve"], env=self.env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)

    def run(self, *args) -> str:
        r = subprocess.run([sys.executable, str(self.agent), *args], env=self.env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout

    def xray(self) -> dict:
        return json.loads(self.fake_state.read_text())

    def poke(self, **kw) -> None:
        s = self.xray()
        s.update(kw)
        self.fake_state.write_text(json.dumps(s))

    def config(self) -> dict:
        return json.loads(self.conf.read_text())

    def stop(self):
        self.proc.terminate()
        self.proc.wait(5)


@pytest.fixture(scope="module")
def agents(tmp_path_factory):
    base = tmp_path_factory.mktemp("nodes")
    a, b = Agent(base / "a"), Agent(base / "b")
    yield a, b
    a.stop()
    b.stop()


def _server(agent: Agent, sid: int = 7001, **kw):
    from app.providers.node import parse_connect
    url, token = parse_connect(agent.line)
    return SimpleNamespace(**{"id": sid, "name": "Latvia", "country": "LV", "url": url, "username": "",
                              "password": "", "api_token": token, "inbound_ids": "", "sub_url": "",
                              "verify_tls": True, "api_version": "", "kind": "node", **kw})


def _panel(agent, sid=7001, **kw):
    from app.providers import panel_for
    return panel_for(_server(agent, sid, **kw))


def _spec(email, **kw):
    from app.providers import ClientSpec
    return ClientSpec(**{"email": email, "uuid": str(uuid.uuid4()), "sub_id": "s", "tg_id": 1,
                         "expiry_ms": 0, "total_bytes": 0, "limit_ip": 0, "enable": True, **kw})


# ---------------------------------------------------------------------------
def test_connection_line_is_parsed_and_checked():
    from app.providers import PanelError, normalize_url
    from app.providers.node import parse_connect
    fp = "ab" * 32
    url, token = parse_connect(f"vsnode://tok123@1.2.3.4:23456#{fp}")
    assert url == f"https://1.2.3.4:23456/#{fp}" and token == "tok123"
    assert parse_connect(url) == (url, None)              # an edit keeps the stored token
    assert normalize_url(f"vsnode://t@[2001:db8::1]:9000#{fp}", "node") == f"https://[2001:db8::1]:9000/#{fp}"
    for bad in ("vsnode://1.2.3.4:23456#" + fp, "vsnode://t@1.2.3.4#" + fp, "vsnode://t@1.2.3.4:1#zz", ""):
        with pytest.raises(PanelError):
            parse_connect(bad)


def test_install_gives_a_working_reality_server(agents):
    a, _ = agents
    cfg = a.config()
    tags = {i["tag"]: i for i in cfg["inbounds"]}
    assert set(tags) == {"api", "vless-tcp", "vless-xhttp"}
    tcp, xh = tags["vless-tcp"], tags["vless-xhttp"]
    assert tcp["port"] == 443 and xh["port"] == 8443
    assert tcp["streamSettings"]["network"] == "raw" and xh["streamSettings"]["network"] == "xhttp"
    rs = tcp["streamSettings"]["realitySettings"]
    assert rs["target"] == "dl.google.com:443" and rs["serverNames"] == ["dl.google.com"]
    assert len(rs["privateKey"]) == 43 and "" in rs["shortIds"]
    assert cfg["outbounds"][0]["tag"] == "direct"          # a plain exit server
    assert {"HandlerService", "StatsService"} <= set(cfg["api"]["services"])
    assert cfg["policy"]["levels"]["0"]["statsUserUplink"] is True
    # no geo files here: no geoip rules that would stop Xray from starting
    assert "geoip" not in json.dumps(cfg["routing"])
    assert a.xray()["running"] and a.xray()["restarts"] == 1
    st = json.loads((a.dir / "state" / "state.json").read_text())
    assert oct((a.dir / "state" / "state.json").stat().st_mode & 0o777) == "0o600"
    assert st["token"] not in a.conf.read_text()


def test_shop_talks_to_node_with_pinned_certificate(agents):
    a, b = agents
    from app.providers import PanelError
    p = _panel(a)
    res = p.test()
    assert res["version"] == "n" + _agent_version() and {i["id"] for i in res["inbounds"]} == {"vless-tcp", "vless-xhttp"}
    # the certificate of another node under this address → refused
    from app.providers.node import parse_connect
    burl, _t = parse_connect(b.line)
    fp_b = urlsplit(burl).fragment
    with pytest.raises(PanelError, match="certificate"):
        _panel(a, 7002, url=_server(a).url.split("#")[0] + "#" + fp_b).test()
    with pytest.raises(PanelError, match="token"):
        _panel(a, 7003, api_token="wrong").test()


def test_customer_added_live_counted_limited_and_removed(agents):
    a, _ = agents
    p = _panel(a)
    restarts = a.xray()["restarts"]
    spec = _spec("tg555", total_bytes=1000)
    p.ensure(spec)
    x = a.xray()
    assert x["users"]["vless-tcp"]["tg555"] == spec.uuid and x["users"]["vless-xhttp"]["tg555"] == spec.uuid
    assert x["restarts"] == restarts                    # nobody else was cut off
    tcp_users = {c["email"]: c for c in next(i for i in a.config()["inbounds"]
                                               if i["tag"] == "vless-tcp")["settings"]["clients"]}
    assert tcp_users["tg555"]["flow"] == "xtls-rprx-vision"

    # traffic: counted, kept across Xray's own zeroing
    a.poke(stats={"tg555": {"uplink": 300, "downlink": 200}})
    p._call("POST", "/api/poll", {})
    assert p.traffic_by_email()["tg555"] == 500 and p.usage(spec) == 500
    a.poke(stats={"tg555": {"downlink": 600}})          # over the 1000-byte limit
    p._call("POST", "/api/poll", {})
    assert p.usage(spec) == 1100
    x = a.xray()
    assert "tg555" not in x["users"]["vless-tcp"] and "tg555" not in x["users"]["vless-xhttp"]
    # a new period: counter to zero, the customer is back
    p.reset_traffic(spec)
    assert p.usage(spec) == 0 and "tg555" in a.xray()["users"]["vless-tcp"]

    # expired → out; renewed → in
    spec.expiry_ms = int((time.time() - 60) * 1000)
    p.ensure(spec)
    assert "tg555" not in a.xray()["users"]["vless-tcp"]
    spec.expiry_ms = int((time.time() + 86400) * 1000)
    p.ensure(spec)
    assert "tg555" in a.xray()["users"]["vless-tcp"]

    # reissued key: the old uuid stops working at once
    old = spec.uuid
    spec.uuid = str(uuid.uuid4())
    p.ensure(spec)
    assert a.xray()["users"]["vless-tcp"]["tg555"] == spec.uuid != old

    p.disable(spec)
    assert "tg555" not in a.xray()["users"]["vless-xhttp"]
    p.delete(spec)
    assert "tg555" not in p.traffic_by_email()


def test_only_picked_inbounds_and_links(agents):
    a, _ = agents
    p = _panel(a, inbound_ids="vless-xhttp")
    spec = _spec("tg556")
    p.ensure(spec)
    x = a.xray()
    assert "tg556" in x["users"]["vless-xhttp"] and "tg556" not in x["users"]["vless-tcp"]
    links = p.links(spec)
    assert len(links) == 1
    u = urlsplit(links[0])
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert u.scheme == "vless" and u.username == spec.uuid and u.hostname == "127.0.0.1" and u.port == 8443
    assert q["type"] == "xhttp" and q["security"] == "reality" and q["sni"] == "dl.google.com"
    assert len(q["pbk"]) == 43 and q["path"].startswith("/") and "flow" not in q
    assert unquote(u.fragment) == "🇱🇻 Latvia · XHTTP"
    both = _panel(a).links(spec)
    assert [parse_qs(urlsplit(x).query)["type"][0] for x in both] == ["xhttp", "tcp"]
    assert parse_qs(urlsplit(both[1]).query)["flow"][0] == "xtls-rprx-vision"
    p.delete(spec)


def test_live_api_failure_falls_back_to_restart(agents):
    a, _ = agents
    p = _panel(a)
    before = a.xray()["restarts"]
    a.poke(fail_api=True)
    spec = _spec("tg557")
    p.ensure(spec)
    a.poke(fail_api=False)
    x = a.xray()
    assert x["restarts"] == before + 1 and x["users"]["vless-tcp"]["tg557"] == spec.uuid
    p.delete(spec)


def test_settings_change_sni_ports_and_keys(agents):
    a, _ = agents
    from app.providers import PanelError
    p = _panel(a)
    old_pbk = p.info(fresh=True)["inbounds"][0]["pbk"]
    info = p.settings({"sni": "www.microsoft.com", "xhttp_port": 2083, "new_keys": True})
    xh = next(i for i in info["inbounds"] if i["id"] == "vless-xhttp")
    assert xh["sni"] == "www.microsoft.com" and xh["port"] == 2083 and xh["pbk"] != old_pbk
    rs = next(i for i in a.config()["inbounds"] if i["tag"] == "vless-tcp")["streamSettings"]["realitySettings"]
    assert rs["target"] == "www.microsoft.com:443"
    with pytest.raises(PanelError, match="different ports"):
        p.settings({"tcp_port": 2083})
    with pytest.raises(PanelError, match="at least one"):
        p.settings({"tcp_enabled": False, "xhttp_enabled": False})
    with pytest.raises(PanelError, match="SNI"):
        p.settings({"sni": "bad sni; rm -rf /"})
    # a refused change leaves everything as it was
    assert next(i for i in p.info(fresh=True)["inbounds"] if i["id"] == "vless-tcp")["port"] == 443


def test_chain_bridge_to_exit(agents):
    a, b = agents
    from app.providers import PanelError
    from app.providers.node import link_chain, unlink_chain
    bridge, exit_ = _server(a, 7101), _server(b, 7102)
    info = link_chain(bridge, exit_)
    assert info["role"] == "bridge" and info["chain"]["address"] == "127.0.0.1"
    cfg = a.config()
    chain = cfg["outbounds"][0]
    assert chain["tag"] == "chain" and chain["protocol"] == "vless"
    xinfo = _panel(b, 7102).info(fresh=True)
    xh = next(i for i in xinfo["inbounds"] if i["id"] == "vless-xhttp")
    assert chain["streamSettings"]["network"] == "xhttp"
    assert chain["streamSettings"]["realitySettings"]["publicKey"] == xh["pbk"]
    assert chain["streamSettings"]["xhttpSettings"]["path"] == xh["path"]
    user = chain["settings"]["vnext"][0]
    assert user["port"] == xh["port"]
    # the exit lets the bridge in with the key the bridge was given
    assert b.xray()["users"]["vless-xhttp"]["chain-7101"] == user["users"][0]["id"]
    # Russian sites go straight from the bridge
    assert any(r.get("outboundTag") == "direct" and "regexp:\\.ru$" in r.get("domain", [])
               for r in cfg["routing"]["rules"])
    # the service client is not a customer: not in the shop's traffic
    b.poke(stats={"chain-7101": {"uplink": 10_000}})
    _panel(b, 7102)._call("POST", "/api/poll", {})
    assert "chain-7101" not in _panel(b, 7102).traffic_by_email()
    # a bridge cannot be an exit for a third server
    with pytest.raises(PanelError, match="itself a bridge"):
        link_chain(exit_, bridge)
    info = unlink_chain(bridge, [exit_])
    assert info["role"] == "exit" and a.config()["outbounds"][0]["tag"] == "direct"
    assert "chain-7101" not in b.xray()["users"].get("vless-xhttp", {})


def test_agent_survives_restart_of_itself(agents, tmp_path):
    """The agent restarted (update, reboot): customers and counters are kept,
    Xray is not restarted for nothing."""
    a, _ = agents
    p = _panel(a)
    spec = _spec("tg558")
    p.ensure(spec)
    a.poke(stats={"tg558": {"uplink": 42}})
    p._call("POST", "/api/poll", {})
    restarts = a.xray()["restarts"]
    a.stop()
    a.start()
    assert p.usage(spec) == 42 and a.xray()["restarts"] == restarts
    # init again (the installer run twice) keeps keys and customers
    pbk = p.info(fresh=True)["inbounds"][0]["pbk"]
    a.run("init", "--host", "127.0.0.1")
    from app.providers import node
    node.drop(7001)
    assert p.info(fresh=True)["inbounds"][0]["pbk"] == pbk and p.usage(spec) == 42
    p.delete(spec)


# ---------------------------------------------------------------------------
#  Through the shop: add by the pasted line, sell, subscription, chain
# ---------------------------------------------------------------------------
def test_shop_adds_node_sells_and_builds_chain(client, admin, env, monkeypatch, agents):
    a, b = agents
    monkeypatch.setenv("PAYMENTS_TEST_MODE", "1")
    env.__init__()
    t = client.post("/api/admin/servers/test", headers=admin,
                    json={"name": "x", "kind": "node", "url": b.line}).json()
    assert t["ok"] and t["version"] == "n" + _agent_version(), t
    ids = {}
    for name, country, ag in (("Exit", "LV", b), ("Bridge", "RU", a)):
        r = client.post("/api/admin/servers", headers=admin, json={
            "name": name, "country": country, "kind": "node", "url": ag.line})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["has_token"] and out["url"].startswith("https://127.0.0.1:")
        assert "@" not in out["url"]                       # the token is not shown in the list
        ids[name] = out["id"]
    # the same node twice is refused
    assert client.post("/api/admin/servers", headers=admin, json={
        "name": "dup", "kind": "node", "url": a.line}).status_code == 409
    # editing without pasting the line again keeps the token
    srv = next(s for s in client.get("/api/admin/servers", headers=admin).json() if s["id"] == ids["Exit"])
    r = client.put(f"/api/admin/servers/{ids['Exit']}", headers=admin, json={
        "name": "Exit", "country": "LV", "kind": "node", "url": srv["url"], "inbound_ids": ""})
    assert r.status_code == 200 and r.json()["has_token"]
    assert client.post(f"/api/admin/servers/{ids['Exit']}/check", headers=admin).json()["ok"]

    uid = 7_700_001
    plan = client.get("/api/app/config").json()["plans"][0]
    r = client.post("/api/app/order", json={"plan_id": plan["id"]}, headers=tg(uid))
    assert r.json()["status"] == "success", r.text
    from app.config import settings
    email = f"{settings.xui_email_prefix}{uid}"
    assert email in a.xray()["users"]["vless-tcp"] and email in b.xray()["users"]["vless-xhttp"]
    me = client.get("/api/app/me", headers=tg(uid)).json()
    body = client.get(me["sub_url"].replace("https://shop.example.com", ""),
                      headers={"User-Agent": "Happ/3", "X-HWID": "n1"}).text
    links = base64.b64decode(body).decode().splitlines()
    names = sorted(unquote(x.rsplit("#", 1)[1]) for x in links)
    assert names == ["🇱🇻 Exit · TCP", "🇱🇻 Exit · XHTTP", "🇷🇺 Bridge · TCP", "🇷🇺 Bridge · XHTTP"]

    # node settings and chain from the panel
    n = client.get(f"/api/admin/servers/{ids['Bridge']}/node", headers=admin).json()
    assert n["latest"] and n["outdated"] is False
    assert n["ok"] and n["info"]["role"] == "exit" and n["others"][0]["id"] == ids["Exit"]
    r = client.post(f"/api/admin/servers/{ids['Bridge']}/node/chain", headers=admin,
                    json={"exit_id": ids["Exit"]})
    assert r.status_code == 200 and r.json()["info"]["role"] == "bridge", r.text
    assert a.config()["outbounds"][0]["tag"] == "chain"
    r = client.post(f"/api/admin/servers/{ids['Bridge']}/node/settings", headers=admin,
                    json={"sni": "ya.ru", "token": "ignored"})
    assert r.status_code == 200 and r.json()["info"]["inbounds"][0]["sni"] == "ya.ru"
    body = client.get(me["sub_url"].replace("https://shop.example.com", ""),
                      headers={"User-Agent": "Happ/3", "X-HWID": "n1"}).text
    bridge_links = [x for x in base64.b64decode(body).decode().splitlines() if "Bridge" in unquote(x)]
    assert all("sni=ya.ru" in x for x in bridge_links)   # new keys at once, not after the cache
    # the same from the chat with the bot
    from app import botadmin
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        text, rows = botadmin.screen_node(db, "ru", ids["Bridge"])
        assert "мост → Exit" in text and "Онлайн" in text
        flat = [cb for row in rows for _l, cb in row]
        assert f"a:nr:{ids['Bridge']}" in flat and f"a:nc:{ids['Bridge']}:0" in flat
        assert any(f"a:nd:{ids['Bridge']}" == cb for row in botadmin.screen_servers(db, "ru")[1] for _l, cb in row)
    finally:
        db.close()
    r = client.post(f"/api/admin/servers/{ids['Bridge']}/node/chain", headers=admin, json={"exit_id": None})
    assert r.json()["info"]["role"] == "exit"
    assert client.post(f"/api/admin/servers/{ids['Bridge']}/node/restart", headers=admin).json()["ok"]
    # put the agent back the way other tests expect it
    client.post(f"/api/admin/servers/{ids['Bridge']}/node/settings", headers=admin, json={"sni": "dl.google.com"})


def test_watchdog_restarts_a_silent_xray_and_reports_the_machine(agents):
    a, _ = agents
    p = _panel(a)
    info = p.info(fresh=True)
    m = info["system"]
    assert m["mem_total"] > 0 and m["disk_total"] > 0 and m["cpus"] >= 1 and "uptime" in m
    before = a.xray()["restarts"]
    a.poke(running=False)                       # Xray died
    for _ in range(3):
        p._call("POST", "/api/poll", {})
    x = a.xray()
    assert x["running"] and x["restarts"] == before + 1
    assert p.info(fresh=True)["watchdog_restarts"] == 1


def test_agent_updates_itself_from_the_shop(tmp_path):
    """The shop hands out a newer agent: the node checks its hash, replaces
    itself and comes back as the new version with everything kept."""
    import hashlib
    import threading
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    from app.providers import PanelError
    ag = Agent(tmp_path / "n")
    try:
        p = _panel(ag, 7301)
        spec = _spec("tg901")
        p.ensure(spec)
        web = tmp_path / "web" / "scripts"
        web.mkdir(parents=True)
        new = AGENT.read_text().replace('VERSION = "', 'VERSION = "9.', 1)
        (web / "vpn-seller-node.py").write_text(new)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(tmp_path / "web")))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}/scripts/vpn-seller-node.py"
        with pytest.raises(PanelError, match="checksum"):
            p._call("POST", "/api/update", {"url": url, "sha256": "0" * 64})
        res = p._call("POST", "/api/update", {"url": url, "sha256": hashlib.sha256(new.encode()).hexdigest()})
        assert res["version"].startswith("9.")
        ag.proc.wait(5)                         # it exits; systemd starts the new one
        ag.start()
        from app.providers import node
        node.drop(7301)
        assert p.info(fresh=True)["agent"].startswith("9.")
        assert "tg901" in ag.xray()["users"]["vless-tcp"]
        httpd.shutdown()
    finally:
        ag.stop()


def test_shop_keeps_xray_for_servers_without_github(client, env, monkeypatch):
    """node.sh asks the shop for Xray when GitHub does not answer: the shop
    fetches the release once, checks it against its .dgst and hands it out;
    with GitHub gone later, the kept copy is still given."""
    import hashlib

    import httpx

    from app import nodefiles
    zip_body = b"PK-fake-xray-zip"
    dgst = f"MD5= x\nSHA2-256= {hashlib.sha256(zip_body).hexdigest()}\n".encode()
    calls = []

    def fake_get(url, limit):
        calls.append(url)
        if not fake_get.up:
            raise httpx.HTTPError("down")
        return dgst if url.endswith(".dgst") else zip_body
    fake_get.up = True
    monkeypatch.setattr(nodefiles, "_get", fake_get)
    r = client.get("/node-files/Xray-linux-64.zip")
    assert r.status_code == 200 and r.content == zip_body
    assert client.get("/node-files/Xray-linux-64.zip.dgst").content == dgst
    assert len(calls) == 2                        # fetched once, then kept
    fake_get.up = False
    import os
    old = time.time() - 8 * 86400
    os.utime(nodefiles.folder() / "Xray-linux-64.zip", (old, old))
    assert client.get("/node-files/Xray-linux-64.zip").content == zip_body   # stale but given
    assert client.get("/node-files/Xray-linux-arm64-v8a.zip").status_code == 503
    assert client.get("/node-files/evil.sh").status_code == 404
    assert client.get("/node-files/..%2Fsecret").status_code == 404
    # a release whose file does not match its checksum is not kept
    fake_get.up = True
    monkeypatch.setattr(nodefiles, "_get", lambda url, limit: dgst if url.endswith(".dgst") else b"tampered")
    assert client.get("/node-files/Xray-linux-arm64-v8a.zip").status_code == 503
