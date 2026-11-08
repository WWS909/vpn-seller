#!/usr/bin/env python3
"""
A checker for VPN Seller that runs inside Russia: can a customer here reach
each of the shop's servers, or does the operator cut it off?

Put it on any cheap VPS in Russia (one per city / operator if you like) and
install it with one command — the panel shows it in «Авария» → «Доступность
из России»:

    curl -fsSL https://SHOP/probe/ru-probe.py -o /usr/local/bin/ru-probe.py
    sudo python3 /usr/local/bin/ru-probe.py --install --shop https://SHOP --token TOKEN --name "Москва · МТС"

Every 5 minutes it asks the shop which addresses to check, opens a TCP
connection to each, greets it with a TLS hello carrying the name the VPN
key uses, and reports what happened. Only Python 3.8+ is needed; it sends
nothing but the results, and the token can do nothing but this.

    ru-probe.py --shop URL --token T --name NAME      one round now (prints the results)
    ru-probe.py --install --shop URL --token T --name NAME   every 5 minutes (systemd)
    ru-probe.py --uninstall
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.request

VERSION = "1"
UNIT = "vpnshop-ru-probe"
CONF = "/etc/vpnshop-ru-probe.json"


def _api(shop: str, token: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(shop.rstrip("/") + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                                          "User-Agent": "vpnshop-ru-probe/" + VERSION})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode() or "{}")


def check(endpoint: str, sni: str, timeout: float = 6.0) -> dict:
    """TCP connect, then a TLS hello with the key's name. A server that
    answers the hello in any way (even with an error) is reachable; silence
    or a reset right after the hello is what a DPI block looks like."""
    host, _, port = endpoint.rpartition(":")
    host = host.strip("[]")
    started = time.monotonic()
    out = {"endpoint": endpoint, "tcp_ok": False, "tls_ok": False, "error": "", "ms": 0}
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
    except socket.timeout:
        out["error"] = "tcp: timeout"
        return out
    except OSError as e:
        out["error"] = f"tcp: {e.strerror or e.__class__.__name__}"
        return out
    out["tcp_ok"] = True
    out["ms"] = int((time.monotonic() - started) * 1000)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE         # Reality shows a borrowed certificate: only the answer matters
    try:
        sock.settimeout(timeout)
        with ctx.wrap_socket(sock, server_hostname=sni or None) as tls:
            tls.do_handshake()
        out["tls_ok"] = True
    except ssl.SSLError:
        out["tls_ok"] = True                # the server answered, just not with what we expected
    except socket.timeout:
        out["error"] = "tls: no answer to the hello"
    except ConnectionResetError:
        out["error"] = "tls: connection reset after the hello"
    except OSError as e:
        out["error"] = f"tls: {e.strerror or e.__class__.__name__}"
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return out


def round_once(shop: str, token: str, name: str, quiet: bool = False) -> int:
    data = _api(shop, token, "/api/probe/targets")
    results = []
    for t in data.get("targets", []):
        r = check(t["endpoint"], t.get("sni", ""))
        r["server_id"] = t["server_id"]
        results.append(r)
        if not quiet:
            state = "OK" if r["tcp_ok"] and r["tls_ok"] else "FAIL"
            print(f"{state:4}  {t['name']:<24} {t['endpoint']:<28} {r['ms']:>5} ms  {r['error']}")
    _api(shop, token, "/api/probe/report", {"probe": name, "results": results})
    if not quiet:
        print(f"reported {len(results)} results as “{name}”")
    return 0


def install(shop: str, token: str, name: str) -> int:
    if os.geteuid() != 0:
        print("run with sudo to install", file=sys.stderr)
        return 1
    round_once(shop, token, name)           # a first round now: fails loudly if something is wrong
    with open(CONF, "w") as f:
        json.dump({"shop": shop, "token": token, "name": name}, f)
    os.chmod(CONF, 0o600)
    me = os.path.abspath(__file__)
    with open(f"/etc/systemd/system/{UNIT}.service", "w") as f:
        f.write(f"[Unit]\nDescription=VPN Seller checker inside Russia\nAfter=network-online.target\n\n"
                f"[Service]\nType=oneshot\nExecStart={sys.executable} {me} --config {CONF} --quiet\n")
    with open(f"/etc/systemd/system/{UNIT}.timer", "w") as f:
        f.write("[Unit]\nDescription=VPN Seller checker every 5 minutes\n\n"
                "[Timer]\nOnBootSec=1min\nOnUnitActiveSec=5min\nRandomizedDelaySec=20\n\n"
                "[Install]\nWantedBy=timers.target\n")
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", f"{UNIT}.timer"], check=True)
    print("installed: runs every 5 minutes (systemctl status vpnshop-ru-probe.timer)")
    return 0


def uninstall() -> int:
    subprocess.run(["systemctl", "disable", "--now", f"{UNIT}.timer"], check=False)
    for path in (f"/etc/systemd/system/{UNIT}.service", f"/etc/systemd/system/{UNIT}.timer", CONF):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    print("removed")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="VPN Seller checker inside Russia")
    ap.add_argument("--shop"), ap.add_argument("--token"), ap.add_argument("--name")
    ap.add_argument("--config"), ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--install", action="store_true"), ap.add_argument("--uninstall", action="store_true")
    a = ap.parse_args(argv)
    if a.uninstall:
        return uninstall()
    if a.config:
        with open(a.config) as f:
            c = json.load(f)
        a.shop, a.token, a.name = a.shop or c["shop"], a.token or c["token"], a.name or c["name"]
    if not (a.shop and a.token and a.name):
        ap.error("--shop, --token and --name are needed")
    if not a.shop.startswith("https://"):
        ap.error("--shop must be the https:// address of the shop")
    return install(a.shop, a.token, a.name) if a.install else round_once(a.shop, a.token, a.name, a.quiet)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
