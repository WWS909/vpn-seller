"""
Bridge Node — the shop's own VPN server (scripts/bridge-node.py on the
server, installed by scripts/node.sh). No 3x-ui, no Marzban: the shop
talks to a small agent that owns Xray there.

The operator pastes one line the installer printed:

    bridge://TOKEN@1.2.3.4:23456#<sha256 of the agent's certificate>

It is kept as url = https://1.2.3.4:23456/#<fingerprint> and api_token =
TOKEN. The agent's certificate is its own (no domain needed); the shop
checks its fingerprint on every connection instead of a CA.

Inbounds are tags: "vless-tcp" and "vless-xhttp". None picked = all.

A bridge (the chain for the whitelists) is a node that sends everything on
to another node, the exit: link_chain() sets that up from the shop.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import ssl
import threading
import time
import uuid as uuidlib
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from .xui import VISION, ClientSpec, PanelError

TAGS = ("vless-xhttp", "vless-tcp")          # the order keys are given in
CHAIN_PREFIX = "chain-"


def parse_connect(raw: str) -> tuple[str, str | None]:
    """The pasted text → (url to store, token or None). Accepts the
    bridge:// line or an already stored https://host:port/#fp."""
    raw = (raw or "").strip()
    if not raw:
        raise PanelError("paste the connection line the installer printed")
    if raw.startswith("bridge://"):
        parts = urlsplit(raw)
        token = parts.username or ""
        host = parts.hostname or ""
        try:
            port = parts.port
        except ValueError as e:
            raise PanelError("bad port in the connection line") from e
        fp = _clean_fp(parts.fragment)
        if not (token and host and port and fp):
            raise PanelError("the connection line is cut — copy the whole bridge://… line")
        netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        return urlunsplit(("https", netloc, "/", "", fp)), token
    return normalize_url(raw), None


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("bridge://"):
        return parse_connect(url)[0]
    if not url:
        raise PanelError("node address is empty")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if not parts.hostname or not parts.port:
        raise PanelError("node address needs host and port, e.g. https://1.2.3.4:23456/")
    fp = _clean_fp(parts.fragment)
    return urlunsplit(("https", parts.netloc, "/", "", fp))


def _clean_fp(raw: str) -> str:
    fp = (raw or "").strip().lower().replace(":", "")
    if fp.startswith("sha256="):
        fp = fp[7:]
    if fp and (len(fp) != 64 or any(c not in "0123456789abcdef" for c in fp)):
        raise PanelError("bad certificate fingerprint in the node address")
    return fp


def flag(country: str) -> str:
    c = (country or "").upper()
    if len(c) != 2 or not c.isalpha():
        return ""
    return chr(0x1F1E6 + ord(c[0]) - 65) + chr(0x1F1E6 + ord(c[1]) - 65)


# The agent's /api/info changes only when the operator changes settings;
# every subscription refresh needs it, so it is kept for a short while.
_info_cache: dict[tuple, tuple[float, dict]] = {}
_info_lock = threading.Lock()
INFO_TTL = 30


def drop(server_id: int) -> None:
    with _info_lock:
        for k in [k for k in _info_cache if k[0] == server_id]:
            _info_cache.pop(k, None)


class NodePanel:
    def __init__(self, server, timeout: float = 15.0):
        self.server = server
        self.timeout = timeout
        self.base = normalize_url(server.url)
        parts = urlsplit(self.base)
        self.host = parts.hostname
        self.port = parts.port
        self.fp = parts.fragment
        self.token = (server.api_token or "").strip()

    # ------------------------------------------------------------ transport
    def _conn(self) -> http.client.HTTPSConnection:
        if self.fp:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE        # replaced by the fingerprint check below
        elif self.server.verify_tls:
            ctx = ssl.create_default_context()
        else:
            ctx = ssl._create_unverified_context()  # noqa: S323 — the operator turned checks off
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=ctx)
        try:
            conn.connect()
        except (OSError, ssl.SSLError) as e:
            raise PanelError(f"node unreachable: {e.__class__.__name__}") from e
        if self.fp:
            der = conn.sock.getpeercert(binary_form=True) or b""
            if hashlib.sha256(der).hexdigest() != self.fp:
                conn.close()
                raise PanelError("node certificate does not match — reinstalled? paste the new line")
        return conn

    def _call(self, method: str, path: str, body: dict | None = None, ok404: bool = False):
        if not self.token:
            raise PanelError("no node token — paste the bridge://… line again")
        conn = self._conn()
        try:
            data = json.dumps(body).encode() if body is not None else None
            headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=headers)
            r = conn.getresponse()
            raw = r.read(4_000_000)
        except (OSError, http.client.HTTPException, socket.timeout) as e:
            raise PanelError(f"node unreachable: {e.__class__.__name__}") from e
        finally:
            conn.close()
        try:
            obj = json.loads(raw or b"{}")
        except ValueError as e:
            raise PanelError(f"not a JSON answer on {path}") from e
        if r.status == 404 and ok404:
            return None
        if r.status == 401:
            raise PanelError("node token rejected")
        if r.status >= 400:
            msg = obj.get("error") if isinstance(obj, dict) else ""
            raise PanelError(f"node: {msg or 'HTTP ' + str(r.status)}"[:250])
        return obj

    # ------------------------------------------------------------ info
    def info(self, fresh: bool = False) -> dict:
        key = (self.server.id, self.base, self.token)
        with _info_lock:
            hit = _info_cache.get(key)
        if hit and not fresh and time.time() - hit[0] < INFO_TTL:
            return hit[1]
        data = self._call("GET", "/api/info")
        with _info_lock:
            _info_cache[key] = (time.time(), data)
        return data

    def version(self) -> str:
        v = "n" + str(self.info().get("agent") or "")
        self.server.api_version = v
        return v

    def inbounds(self) -> list[dict]:
        return [{k: ib.get(k) for k in ("id", "remark", "protocol", "port", "enable", "network",
                                        "security", "clients")}
                for ib in self.info().get("inbounds") or []]

    def online(self) -> int | None:
        return int(self.info(fresh=True).get("online") or 0)

    def test(self) -> dict:
        info = self.info(fresh=True)
        self.server.api_version = "n" + str(info.get("agent") or "")
        return {"version": self.server.api_version, "inbounds": self.inbounds(),
                "clients": int(info.get("active") or 0), "node": info}

    # ------------------------------------------------------------ clients
    def _tags(self) -> list[str]:
        return [t.strip() for t in str(self.server.inbound_ids or "").split(",") if t.strip() in TAGS]

    def ensure(self, spec: ClientSpec, flow: str = VISION) -> list[str]:
        self._call("POST", "/api/clients/ensure", {
            "email": spec.email, "uuid": spec.uuid, "enable": bool(spec.enable),
            "expiry_ms": int(spec.expiry_ms or 0), "total_bytes": int(spec.total_bytes or 0),
            "tags": self._tags(), "comment": spec.comment,
        })
        return []

    def disable(self, spec: ClientSpec) -> None:
        spec.enable = False
        self.ensure(spec)

    def usage(self, spec: ClientSpec) -> int:
        c = self._call("GET", "/api/client?email=" + quote(spec.email), ok404=True)
        return int((c or {}).get("used") or 0)

    def traffic_by_email(self) -> dict[str, int]:
        data = self._call("GET", "/api/traffic") or {}
        return {str(k): int(v or 0) for k, v in data.items() if not str(k).startswith(CHAIN_PREFIX)}

    def reset_traffic(self, spec: ClientSpec) -> None:
        self._call("POST", "/api/traffic/reset", {"email": spec.email})

    def delete(self, spec: ClientSpec) -> None:
        self._call("POST", "/api/clients/delete", {"email": spec.email})

    def links(self, spec: ClientSpec) -> list[str]:
        info = self.info()
        want = self._tags()
        name = f"{flag(getattr(self.server, 'country', ''))} {getattr(self.server, 'name', '')}".strip()
        out = []
        by_id = {ib.get("id"): ib for ib in info.get("inbounds") or []}
        for tag in TAGS:
            ib = by_id.get(tag)
            if not ib or not ib.get("enable") or (want and tag not in want):
                continue
            out.append(share_link(ib, spec.uuid, self.host,
                                  f"{name} · {'XHTTP' if tag == 'vless-xhttp' else 'TCP'}".strip(" ·")))
        return out

    # ------------------------------------------------------------ server control
    def settings(self, body: dict) -> dict:
        res = self._call("POST", "/api/settings", body)
        drop(self.server.id)
        return res

    def set_chain(self, body: dict) -> dict:
        res = self._call("POST", "/api/chain", body)
        drop(self.server.id)
        return res

    def restart(self) -> None:
        self._call("POST", "/api/restart", {})


def share_link(ib: dict, uid: str, host: str, remark: str) -> str:
    q = {"type": "xhttp" if ib.get("network") == "xhttp" else "tcp",
         "security": "reality", "encryption": "none",
         "pbk": ib.get("pbk", ""), "fp": ib.get("fp") or "chrome",
         "sni": ib.get("sni", ""), "sid": ib.get("sid", ""), "spx": "/"}
    if q["type"] == "xhttp":
        q["path"] = ib.get("path") or "/"
        q["mode"] = ib.get("mode") or "auto"
    else:
        q["flow"] = ib.get("flow") or VISION
    h = f"[{host}]" if ":" in host else host
    return f"vless://{uid}@{h}:{int(ib['port'])}?{urlencode(q, quote_via=quote)}#{quote(remark)}"


def link_chain(bridge, exit_server, timeout: float = 20.0) -> dict:
    """Make `bridge` send its customers' traffic on to `exit_server`. Both
    are Bridge Nodes. On the exit, a service client "chain-<bridge id>"
    with a fresh key is made; the bridge gets its address and Reality
    settings. Returns the bridge's new info."""
    if getattr(bridge, "id", None) == getattr(exit_server, "id", None):
        raise PanelError("a server cannot be its own exit")
    b = NodePanel(bridge, timeout=timeout)
    x = NodePanel(exit_server, timeout=timeout)
    xinfo = x.info(fresh=True)
    if xinfo.get("role") == "bridge":
        raise PanelError("the exit server is itself a bridge — pick a plain one")
    by_id = {ib.get("id"): ib for ib in xinfo.get("inbounds") or []}
    ib = next((by_id[t] for t in TAGS if by_id.get(t, {}).get("enable")), None)
    if not ib:
        raise PanelError("the exit server has no way in turned on")
    uid = str(uuidlib.uuid4())
    x._call("POST", "/api/clients/ensure", {
        "email": f"{CHAIN_PREFIX}{bridge.id}", "uuid": uid, "enable": True,
        "expiry_ms": 0, "total_bytes": 0, "tags": [ib["id"]],
        "comment": f"chain from {getattr(bridge, 'name', '')}"[:120]})
    return b.set_chain({
        "address": x.host, "port": int(ib["port"]), "uuid": uid,
        "public": ib.get("pbk", ""), "short_id": ib.get("sid", ""), "sni": ib.get("sni", ""),
        "fp": ib.get("fp") or "chrome", "network": ib.get("network"),
        "path": ib.get("path") or "/", "mode": ib.get("mode") or "auto"})


def unlink_chain(bridge, exits: list, timeout: float = 15.0) -> dict:
    """The bridge becomes a plain exit again; its service client is
    removed from whichever exit has it (best effort)."""
    res = NodePanel(bridge, timeout=timeout).set_chain({"off": True})
    for x in exits:
        try:
            NodePanel(x, timeout=timeout)._call("POST", "/api/clients/delete",
                                                {"email": f"{CHAIN_PREFIX}{bridge.id}"})
        except PanelError:
            pass
    return res
