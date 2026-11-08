"""
The load of servers, the speed-up script, and a live check that the keys
the shop hands out really carry traffic.

The live part runs a real Xray client with the key from the subscription
against the local 3x-ui panel (scripts/dev-3xui.sh), opens a page through
it, and checks that the panel then reports the person online and that the
shop's VPN check finds the port. It is skipped without the panels.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from conftest import tg
from test_remnawave import rw  # noqa: F401 — the fixture

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- load
def test_load_levels_and_freshness(db):
    from app import access
    from app.models import Server, now
    s = Server(name="x", url="http://x/", online=10, online_at=now(), capacity=100)
    assert access.load_of(s) == {"level": "low", "share": 0.1}
    s.online = 60
    assert access.load_of(s)["level"] == "mid"
    s.online = 130
    assert access.load_of(s) == {"level": "high", "share": 1.0}
    s.capacity = 0                              # the default capacity
    s.online = access.DEFAULT_CAPACITY // 4
    assert access.load_of(s)["level"] == "low"
    s.online_at = now() - access.LOAD_FRESH - 5          # too old: nothing is shown
    assert access.load_of(s) is None
    s.online, s.online_at = -1, now()                    # the panel cannot tell
    assert access.load_of(s) is None


def test_load_reaches_the_app_and_can_be_hidden(client, admin, db, rw):  # noqa: F811
    from app import access
    from app.models import Server
    r = client.post("/api/admin/servers", headers=admin, json={
        "name": "Finland", "country": "FI", "kind": "remnawave", "url": rw["url"],
        "api_token": rw["token"], "inbound_ids": rw["squads"], "capacity": 50})
    assert r.status_code == 200, r.text
    assert access.refresh_load(db) == 1                  # the fake panel reports 0 online
    db.expire_all()
    s = db.query(Server).one()
    assert s.online == 0 and s.online_at > 0
    srv = client.get("/api/app/config").json()["servers"][0]
    assert srv["load"] == {"level": "low", "share": 0.0}
    client.post("/api/admin/settings", headers=admin, json={"servers_load": "0"})
    assert client.get("/api/app/config").json()["servers"][0]["load"] is None
    assert client.get("/api/admin/servers", headers=admin).json()[0]["online"] == 0


def test_scripts_are_served_by_the_shop(client):
    r = client.get("/scripts/tune.sh")
    assert r.status_code == 200 and r.text.startswith("#!/usr/bin/env bash") and "bbr" in r.text
    for bad in ("relay.sh", "publish-github.sh", "dev-3xui.sh", "..%2F.env", "..%2Fapp%2Fconfig.py"):
        assert client.get("/scripts/" + bad).status_code == 404, bad


def test_tune_script_writes_its_files_and_undoes_them(tmp_path):
    env = {**os.environ, "VPNSHOP_ETC": str(tmp_path), "VPNSHOP_NO_SYSTEMD": "1",
           "PATH": f"{tmp_path}/bin:" + os.environ["PATH"]}
    # sysctl -w is faked: this test must not change the machine it runs on.
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/sysctl").write_text('#!/bin/sh\ncase "$*" in *available*) echo "reno cubic bbr";; '
                                          '*-w*) echo "$*" >> "$VPNSHOP_ETC/sysctl.log";; *) echo 1;; esac\n')
    (tmp_path / "bin/modprobe").write_text("#!/bin/sh\nexit 0\n")
    for f in ("sysctl", "modprobe"):
        (tmp_path / "bin" / f).chmod(0o755)
    r = subprocess.run(["bash", str(ROOT / "scripts/tune.sh")], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    conf = (tmp_path / "sysctl.d/99-vpnshop-tune.conf").read_text()
    assert "tcp_congestion_control = bbr" in conf and "rmem_max = 16777216" in conf
    applied = (tmp_path / "sysctl.log").read_text()
    assert "net.ipv4.tcp_congestion_control=bbr" in applied and "net.ipv4.tcp_mtu_probing=1" in applied
    assert "1048576" in (tmp_path / "security/limits.d/99-vpnshop-tune.conf").read_text()
    r = subprocess.run(["bash", str(ROOT / "scripts/tune.sh"), "--undo"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and not (tmp_path / "sysctl.d/99-vpnshop-tune.conf").exists()


# ---------------------------------------------------------------- live: the keys really work
XRAY = ROOT / ".dev/3xui/v3/x-ui/bin/xray-linux-amd64"
WEB = "45.83.0.77"


def _why_not_live() -> str:
    if not XRAY.exists() or not os.getenv("XUI_TEST_V3_URL"):
        return "needs the local 3x-ui panels (scripts/dev-3xui.sh)"
    if not shutil.which("curl") or not shutil.which("ip") or os.geteuid() != 0:
        return "needs root, iproute2 and curl"
    try:
        socket.create_connection(("127.0.0.1", 24443), timeout=2).close()
    except OSError:
        return "the panel's Xray is not listening on 24443"
    return ""


class _Page(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        data = b"hello through the VPN"
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _xray_client(link: str, socks_port: int) -> dict:
    u = urlsplit(link)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    user = {"id": unquote(u.username), "encryption": "none"}
    if q.get("flow"):
        user["flow"] = q["flow"]
    return {"log": {"loglevel": "warning"},
            "inbounds": [{"listen": "127.0.0.1", "port": socks_port, "protocol": "socks",
                          "settings": {"udp": False}}],
            "outbounds": [{"protocol": "vless",
                           "settings": {"vnext": [{"address": u.hostname, "port": u.port, "users": [user]}]},
                           "streamSettings": {"network": q.get("type", "tcp"),
                                              "security": q.get("security", "none")}}]}


def _curl(socks: int, url: str) -> subprocess.CompletedProcess:
    # No proxy settings from the environment: the request must go through our socks port.
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    return subprocess.run(["curl", "-sS", "--noproxy", "", "--max-time", "8", "--socks5-hostname",
                           f"127.0.0.1:{socks}", url], capture_output=True, text=True, env=env)


@pytest.mark.panel
def test_live_key_carries_traffic(client, admin, db):
    """The whole promise of the shop: the key from the subscription works in
    a real VPN client. Then the panel sees the person online (the load in
    the app) and the shop's VPN check finds the port answering."""
    why = _why_not_live()
    if why:
        pytest.skip(why)
    from app import access, vpncheck
    from app.models import Server
    srv = client.post("/api/admin/servers", headers=admin, json={
        "name": "Germany", "country": "DE", "url": os.environ["XUI_TEST_V3_URL"],
        "api_token": os.environ["XUI_TEST_V3_TOKEN"], "inbound_ids": os.environ["XUI_TEST_V3_INBOUND"]}).json()
    uid = 8_800_000 + int(time.time()) % 100_000
    client.get("/api/app/me", headers=tg(uid))
    r = client.post(f"/api/admin/users/{uid}/action", headers=admin, json={"action": "extend", "days": 3})
    assert r.status_code == 200, r.text
    sub = client.get("/api/app/me", headers=tg(uid)).json()["sub_url"]
    body = client.get("/sub/" + sub.rsplit("/sub/", 1)[1], headers={"X-HWID": "live-1"}).text
    links = base64.b64decode(body).decode().splitlines()
    assert links and links[0].startswith("vless://"), links
    assert "Germany" in unquote(links[0])

    # The panel's Xray, like a real server, refuses private addresses, so
    # "the internet" is a page on a public-looking address of this host.
    subprocess.run(["ip", "addr", "add", f"{WEB}/32", "dev", "lo"], capture_output=True)
    page = ThreadingHTTPServer((WEB, 0), _Page)
    threading.Thread(target=page.serve_forever, daemon=True).start()
    url = f"http://{WEB}:{page.server_address[1]}/"
    etc = tempfile.mkdtemp()
    cfg = Path(etc) / "client.json"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        socks = s.getsockname()[1]
    cfg.write_text(json.dumps(_xray_client(links[0], socks)))
    xray = subprocess.Popen([str(XRAY), "run", "-c", str(cfg)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", socks), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        got = _curl(socks, url)
        assert got.stdout == "hello through the VPN", got.stderr

        seen = 0
        for _ in range(12):
            _curl(socks, url)
            if access.refresh_load(db, [db.get(Server, srv["id"])]) and db.get(Server, srv["id"]).online:
                seen = db.get(Server, srv["id"]).online
                break
            time.sleep(2.5)
        assert seen >= 1, "the panel never reported the client online"

        db.expire_all()
        s = db.get(Server, srv["id"])
        assert s.endpoints, "the VPN address was not learned from the keys"
        vpncheck.check(db, [s])
        db.expire_all()
        assert db.get(Server, srv["id"]).vpn_ok_at > 0 and not db.get(Server, srv["id"]).vpn_down_at

        # A key that was reissued stops working: the old one is refused by the panel.
        client.post(f"/api/admin/users/{uid}/action", headers=admin, json={"action": "reissue"})
        time.sleep(1.5)
        assert _curl(socks, url).stdout != "hello through the VPN"
    finally:
        xray.terminate()
        xray.wait(5)
        page.shutdown()
        subprocess.run(["ip", "addr", "del", f"{WEB}/32", "dev", "lo"], capture_output=True)
