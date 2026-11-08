"""
Is the VPN itself up — not the panel, the address customers connect to?

A panel can answer while its VPN core is dead, and the other way round.
So the shop learns, from the keys it hands out, which host:port each
server is reached at, and every two minutes opens a TCP connection there.

Three failed checks in a row (about six minutes, so a short hiccup does
not count) and the server is left out of subscriptions — VPN apps then
stop offering a dead server — and the admins get a message. When it
answers again it comes back by itself, with another message. A server is
never left out when no other one answers either: then the problem is more
likely on the shop's side, and an empty subscription would help nobody.

UDP transports (Hysteria, TUIC, WireGuard, mKCP, QUIC) cannot be checked
with a TCP connection. A server that has any of them is not checked at all:
its TCP part could be down while the UDP part works, and hiding it would
cut off people who connect fine.
"""

from __future__ import annotations

import base64
import json
import socket
from urllib.parse import parse_qs, urlsplit

from sqlalchemy.orm import Session

from . import core
from .models import Server, now

FAILS_TO_DOWN = 3
MAX_ENDPOINTS = 8
UDP_SCHEMES = {"hysteria", "hysteria2", "hy2", "tuic", "wireguard", "wg"}
UDP_NETS = {"kcp", "mkcp", "quic"}

def endpoint(link: str) -> str | None:
    """"host:port" a TCP key connects to, or None (UDP, a notice, unknown)."""
    link = (link or "").strip()
    scheme = link.split("://", 1)[0].lower() if "://" in link else ""
    if not scheme or scheme in UDP_SCHEMES:
        return None
    try:
        if scheme == "vmess":
            raw = link[8:]
            data = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode())
            if str(data.get("net", "")).lower() in UDP_NETS:
                return None
            host, port = str(data.get("add") or ""), int(data.get("port") or 0)
        else:
            u = urlsplit(link)
            q = parse_qs(u.query)
            if (q.get("type") or [""])[0].lower() in UDP_NETS:
                return None
            host, port = u.hostname or "", u.port or 0
    except (ValueError, UnicodeDecodeError, TypeError):
        return None
    if not host or not port or host in ("0.0.0.0", "::"):
        return None
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def sni_of(link: str) -> str:
    """The TLS name a key presents (Reality / TLS "sni", vmess "sni" or
    "host") — the Russia probes greet the server with it, as a VPN app does."""
    link = (link or "").strip()
    try:
        if link.startswith("vmess://"):
            raw = link[8:]
            data = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode())
            return str(data.get("sni") or data.get("host") or "")[:120]
        q = parse_qs(urlsplit(link).query)
        return str((q.get("sni") or q.get("peer") or [""])[0])[:120]
    except (ValueError, UnicodeDecodeError, TypeError):
        return ""


def learn(server: Server, links: list[str]) -> bool:
    """Remember where this server's keys point. Returns True if it changed.

    The list is replaced every time, also by an empty one (the server went
    UDP-only): an old address left behind would be checked forever and hide
    a working server. New addresses start with a clean record."""
    keys = [x for x in links if "://" in (x or "")]
    if not keys:
        return False                    # nothing learned (no keys at all)
    found = [endpoint(x) for x in keys]
    value = "" if None in found else ",".join(sorted(set(found))[:MAX_ENDPOINTS])
    names = {}
    for x, e in zip(keys, found):
        if e and e not in names and sni_of(x):
            names[e] = sni_of(x)
    sni = ",".join(f"{e}={n}" for e, n in sorted(names.items()))[:512]
    if sni != (getattr(server, "vpn_sni", "") or ""):
        server.vpn_sni = sni
        if value == (server.endpoints or ""):
            return True                 # only the names changed
    if value == (server.endpoints or ""):
        return False
    server.endpoints = value
    server.vpn_fails = 0
    server.vpn_down_at = 0
    return True


def _split(hostport: str) -> tuple[str, int]:
    host, _, port = hostport.rpartition(":")
    return host.strip("[]"), int(port)


def reachable(hostport: str, timeout: float = 4.0) -> bool:
    try:
        host, port = _split(hostport)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def is_down(server: Server) -> bool:
    return bool(server.vpn_down_at)


def check(db: Session, rows: list[Server] | None = None) -> dict:
    """Check every enabled server that has known endpoints; returns what changed."""
    from . import access, notify
    rows = access.servers(db) if rows is None else rows
    jobs: list[tuple[Server, str]] = []                # (server, host:port)
    for s in rows:
        jobs += [(s, e) for e in (s.endpoints or "").split(",") if e]
    if not jobs:
        return {"down": [], "up": []}
    access.release(db)
    results = access._parallel(lambda j: reachable(j[1]), jobs, timeout=10)
    alive: dict[int, bool] = {}
    for (s, _), ok in zip(jobs, results):
        alive[s.id] = alive.get(s.id, False) or ok is True

    t = now()
    went_down, came_up = [], []
    for s in {j[0].id: j[0] for j in jobs}.values():
        if alive.get(s.id):
            s.vpn_fails, s.vpn_ok_at = 0, t
            if s.vpn_down_at:
                s.vpn_down_at = 0
                came_up.append(s)
        else:
            s.vpn_fails = int(s.vpn_fails or 0) + 1
            if s.vpn_fails >= FAILS_TO_DOWN and not s.vpn_down_at:
                s.vpn_down_at = t
                went_down.append(s)
    db.commit()
    for s in went_down:
        core.audit(db, "VPN not answering", "system", f"{s.name}: {s.endpoints}", level="error")
        notify.admins(f"VPN не отвечает / VPN down — {s.name} ({s.endpoints}). "
                      "Сервер временно убран из подписок клиентов; вернётся сам, когда ответит.")
    for s in came_up:
        core.audit(db, "VPN answering again", "system", s.name)
        notify.admins(f"VPN снова работает / VPN is back — {s.name}. Сервер вернулся в подписки.")
    return {"down": [s.name for s in went_down], "up": [s.name for s in came_up]}
