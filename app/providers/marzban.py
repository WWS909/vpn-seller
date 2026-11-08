"""
Marzban panel client (Gozargah/Marzban 0.8.x REST API).

The same interface as XuiPanel, so the rest of the service does not care
which panel a server runs:

    test() version() inbounds() ensure() disable() usage()
    traffic_by_email() reset_traffic() links() delete()

In Marzban a customer is a "user" identified by a username (our email,
e.g. tg123456) with proxies per protocol and a list of inbound tags. The
share links come straight from the API, no subscription server is needed.

Auth: the admin username and password of the panel (POST /api/admin/token,
a bearer token for a day). The token is cached per server and taken again
once it is refused.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
import time
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .xui import VISION, ClientSpec, PanelError

KIND = "marzban"

_tokens: dict[tuple, tuple[str, float]] = {}
_inbounds_cache: dict[tuple, tuple[float, dict]] = {}
_lock = threading.Lock()


def normalize_url(url: str) -> str:
    """https://host:8000/dashboard/#/ or https://host:8000 → https://host:8000/"""
    url = (url or "").strip()
    if not url:
        raise PanelError("panel URL is empty")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    path = parts.path or "/"
    for cut in ("/dashboard", "/api/", "/docs"):
        idx = path.find(cut)
        if idx >= 0:
            path = path[:idx]
    if not path.endswith("/"):
        path += "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def parse_tags(raw) -> list[str]:
    """Inbound tags, comma-separated as the admin typed or picked them."""
    return [t.strip() for t in str(raw or "").split(",") if t.strip()]


def username_of(email: str) -> str:
    """Marzban usernames are 3–32 of a-z, 0-9 and _."""
    name = re.sub(r"[^a-z0-9_]", "_", (email or "").lower())[:32]
    return name if len(name) >= 3 else (name + "___")[:3]


class MarzbanPanel:
    def __init__(self, server, timeout: float = 15.0):
        self.server = server
        self.base = normalize_url(server.url)
        self.timeout = timeout
        self.key = (server.id, self.base, server.username, server.password, bool(server.verify_tls))

    # ------------------------------------------------------------ http
    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, verify=bool(self.server.verify_tls),
                            trust_env=False, follow_redirects=False)

    def _login(self, c: httpx.Client) -> str:
        if not (self.server.username and self.server.password):
            raise PanelError("Marzban needs the admin username and password")
        try:
            r = c.post(self.base + "api/admin/token",
                       data={"username": self.server.username, "password": self.server.password})
        except httpx.HTTPError as e:
            raise PanelError(f"panel unreachable: {e.__class__.__name__}") from e
        if r.status_code == 404:
            raise PanelError("Marzban API not found — check the panel address")
        if r.status_code in (401, 403, 422):
            raise PanelError("login failed — wrong username or password")
        try:
            token = r.json()["access_token"]
        except (ValueError, KeyError) as e:
            raise PanelError(f"unexpected login answer (HTTP {r.status_code})") from e
        with _lock:
            _tokens[self.key] = (token, time.time() + 20 * 3600)
        return token

    def _token(self, c: httpx.Client, fresh: bool = False) -> str:
        with _lock:
            cached = _tokens.get(self.key)
        if cached and not fresh and cached[1] > time.time():
            return cached[0]
        return self._login(c)

    def _request(self, method: str, path: str, *, ok404: bool = False, **kw):
        with self._client() as c:
            for attempt in (0, 1):
                token = self._token(c, fresh=attempt == 1)
                try:
                    r = c.request(method, self.base + path,
                                  headers={"Authorization": f"Bearer {token}"}, **kw)
                except httpx.HTTPError as e:
                    raise PanelError(f"panel unreachable: {e.__class__.__name__}") from e
                if r.status_code == 401 and attempt == 0:
                    continue                    # the token expired or was revoked
                break
        if r.status_code == 404 and ok404:
            return None
        if r.status_code >= 400:
            detail = ""
            try:
                detail = str(r.json().get("detail", ""))[:160]
            except (ValueError, AttributeError):
                pass
            raise PanelError(f"HTTP {r.status_code} on {path}" + (f": {detail}" if detail else ""))
        try:
            return r.json() if r.content else {}
        except ValueError as e:
            raise PanelError(f"not a JSON answer on {path}") from e

    # ------------------------------------------------------------ panel
    def version(self) -> str:
        data = self._request("GET", "api/system") or {}
        v = "m" + str(data.get("version", ""))[:7]
        self.server.api_version = v
        return v

    def _inbound_map(self) -> dict:
        """{protocol: [{tag, network, tls, port}]}, cached for a few minutes."""
        with _lock:
            hit = _inbounds_cache.get(self.key)
        if hit and time.time() - hit[0] < 300:
            return hit[1]
        data = self._request("GET", "api/inbounds") or {}
        with _lock:
            _inbounds_cache[self.key] = (time.time(), data)
        return data

    def inbounds(self) -> list[dict]:
        out = []
        for proto, rows in (self._inbound_map() or {}).items():
            for ib in rows or []:
                out.append({"id": ib.get("tag"), "remark": ib.get("tag", ""), "protocol": proto,
                            "port": ib.get("port"), "enable": True, "network": ib.get("network", ""),
                            "security": ib.get("tls", ""), "clients": 0})
        return out

    def online(self) -> int | None:
        """People connected right now (Marzban counts them in its system stats)."""
        system = self._request("GET", "api/system") or {}
        value = system.get("online_users")
        return int(value) if isinstance(value, (int, float)) else None

    def test(self) -> dict:
        system = self._request("GET", "api/system") or {}
        v = "m" + str(system.get("version", ""))[:7]
        self.server.api_version = v
        with _lock:
            _inbounds_cache.pop(self.key, None)
        return {"version": v, "inbounds": self.inbounds(), "clients": int(system.get("total_user") or 0),
                "kind": KIND}

    # ------------------------------------------------------------ users
    def _secret(self, spec: ClientSpec, what: str) -> str:
        """Trojan / Shadowsocks passwords derived from the person's uuid: a
        reissue (new uuid) changes them together with the VLESS id."""
        return hmac.new(spec.uuid.encode(), f"vpnshop-{what}".encode(), hashlib.sha256).hexdigest()[:24]

    def _payload(self, spec: ClientSpec) -> dict:
        tags = parse_tags(self.server.inbound_ids)
        if not tags:
            raise PanelError("no inbounds selected for this server")
        by_proto: dict[str, list[str]] = {}
        vision = True
        for proto, rows in (self._inbound_map() or {}).items():
            for ib in rows or []:
                if ib.get("tag") in tags:
                    by_proto.setdefault(proto, []).append(ib["tag"])
                    if proto == "vless" and not (ib.get("network") == "tcp"
                                                 and ib.get("tls") in ("tls", "reality")):
                        vision = False
        missing = set(tags) - {t for ts in by_proto.values() for t in ts}
        if missing:
            raise PanelError("no such inbounds on the panel: " + ", ".join(sorted(missing)))
        proxies: dict[str, dict] = {}
        for proto in by_proto:
            if proto == "vless":
                proxies[proto] = {"id": spec.uuid, "flow": VISION if vision else ""}
            elif proto == "vmess":
                proxies[proto] = {"id": spec.uuid}
            elif proto == "trojan":
                proxies[proto] = {"password": self._secret(spec, "trojan")}
            elif proto == "shadowsocks":
                proxies[proto] = {"password": self._secret(spec, "ss"),
                                  "method": "chacha20-ietf-poly1305"}
            else:
                proxies[proto] = {}
        expire = int(spec.expiry_ms // 1000) if spec.expiry_ms else 0
        return {"proxies": proxies, "inbounds": by_proto, "expire": expire,
                "data_limit": int(spec.total_bytes or 0), "data_limit_reset_strategy": "no_reset",
                "status": "active" if spec.enable else "disabled", "note": spec.comment[:500]}

    def ensure(self, spec: ClientSpec, flow: str = VISION) -> None:
        name = username_of(spec.email)
        body = self._payload(spec)
        current = self._request("GET", f"api/user/{quote(name)}", ok404=True)
        if current is None:
            self._request("POST", "api/user", json={"username": name, **body})
            return
        self._request("PUT", f"api/user/{quote(name)}", json=body)

    def disable(self, spec: ClientSpec) -> None:
        spec.enable = False
        self.ensure(spec)

    def usage(self, spec: ClientSpec) -> int:
        u = self._request("GET", f"api/user/{quote(username_of(spec.email))}", ok404=True)
        return int((u or {}).get("used_traffic") or 0)

    def traffic_by_email(self) -> dict[str, int]:
        """Used traffic of every user, a page of 500 at a time."""
        out: dict[str, int] = {}
        offset = 0
        while True:
            data = self._request("GET", "api/users", params={"offset": offset, "limit": 500}) or {}
            users = data.get("users") or []
            for u in users:
                out[u.get("username", "")] = int(u.get("used_traffic") or 0)
            offset += len(users)
            if not users or offset >= int(data.get("total") or 0):
                return out

    def reset_traffic(self, spec: ClientSpec) -> None:
        self._request("POST", f"api/user/{quote(username_of(spec.email))}/reset")

    def links(self, spec: ClientSpec) -> list[str]:
        u = self._request("GET", f"api/user/{quote(username_of(spec.email))}", ok404=True)
        if u is None:
            raise PanelError("the customer is not on this panel yet")
        return [x for x in (u.get("links") or []) if isinstance(x, str) and "://" in x]

    def delete(self, spec: ClientSpec) -> None:
        self._request("DELETE", f"api/user/{quote(username_of(spec.email))}", ok404=True)


def drop(server_id: int) -> None:
    with _lock:
        for store in (_tokens, _inbounds_cache):
            for key in [k for k in store if k[0] == server_id]:
                store.pop(key, None)
