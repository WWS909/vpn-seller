"""
3x-ui panel client.

Supports both API generations that are in use:

* 3.x — clients are first-class objects keyed by email and attached to
  several inbounds at once (/panel/api/clients/*). Auth: an API token
  (Bearer, recommended) or login + CSRF token.
* 2.x — clients live inside each inbound's settings JSON
  (/panel/api/inbounds/addClient, updateClient/:id). Auth: login cookie.

The version is detected once and remembered on the Server row.

Everything here is synchronous and short: each call does a few HTTP
requests to one panel. Callers (services.access) decide what a failure
means for the customer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

VISION = "xtls-rprx-vision"


class PanelError(Exception):
    """The panel refused, is unreachable, or answered something unexpected."""


@dataclass
class ClientSpec:
    """Desired state of one customer on one panel."""
    email: str               # base email; v2 adds a suffix per extra inbound
    uuid: str
    sub_id: str
    tg_id: int
    expiry_ms: int           # 0 = never
    total_bytes: int         # 0 = unlimited
    limit_ip: int            # 0 = no limit
    enable: bool
    comment: str = ""


def normalize_url(url: str) -> str:
    """
    Accept whatever the operator pasted: with or without a trailing slash,
    or even a copied browser address like https://host:2053/secret/panel/inbounds.
    Returns the panel base with a trailing slash.
    """
    url = (url or "").strip()
    if not url:
        raise PanelError("panel URL is empty")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    path = parts.path or "/"
    if not path.endswith("/"):
        path += "/"
    # Cut a copied UI address (…/panel/, …/panel/inbounds) back to the base.
    # The last "/panel/" segment is the UI root; a base path may itself start
    # with "panel" (e.g. /panel3/), so match the whole segment only.
    idx = path.rfind("/panel/")
    if idx >= 0:
        path = path[:idx + 1]
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


# ----------------------------------------------------------------------
# Sessions are cached per server so every request does not log in again.
# ----------------------------------------------------------------------
class _Session:
    def __init__(self, base: str, verify: bool, timeout: float):
        self.base = base
        self.client = httpx.Client(timeout=timeout, verify=verify, trust_env=False,
                                   follow_redirects=False)
        self.csrf = ""
        self.logged_in = False
        self.version = ""
        self.lock = threading.Lock()
        self.created = time.time()

    def close(self):
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


_sessions: dict[tuple, _Session] = {}
_sessions_lock = threading.Lock()


def _session_key(server) -> tuple:
    # Credentials are part of the key: editing a server drops its old session.
    return (server.id, server.url, server.username, server.password,
            server.api_token, bool(server.verify_tls))


def drop_session(server_id: int) -> None:
    with _sessions_lock:
        for key in [k for k in _sessions if k[0] == server_id]:
            _sessions.pop(key).close()


class XuiPanel:
    def __init__(self, server, timeout: float = 15.0):
        self.server = server
        self.base = normalize_url(server.url)
        key = _session_key(server)
        with _sessions_lock:
            sess = _sessions.get(key)
            if sess is None:
                for old in [k for k in _sessions if k[0] == server.id]:
                    _sessions.pop(old).close()
                sess = _Session(self.base, bool(server.verify_tls), timeout)
                _sessions[key] = sess
        self.s = sess
        # The session is shared; the timeout is this caller's (a /sub refresh
        # waits less than a background job).
        self.timeout = timeout
        if server.api_version and not sess.version:
            sess.version = server.api_version

    # ------------------------------------------------------------ auth
    @property
    def _bearer(self) -> dict:
        tok = (self.server.api_token or "").strip()
        return {"Authorization": f"Bearer {tok}"} if tok else {}

    def _login(self) -> None:
        if self._bearer:
            return
        if not (self.server.username and self.server.password):
            raise PanelError("no API token and no username/password for this panel")
        c = self.s.client
        # 3.x wants a CSRF token even for the login form; 2.x has no such route.
        csrf = ""
        try:
            r = c.get(self.base + "csrf-token", timeout=self.timeout)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
                csrf = (r.json() or {}).get("obj") or ""
        except httpx.HTTPError as e:
            raise PanelError(f"panel unreachable: {e.__class__.__name__}") from e
        creds = {"username": self.server.username, "password": self.server.password}
        try:
            if csrf:
                r = c.post(self.base + "login", json=creds, headers={"X-CSRF-Token": csrf},
                           timeout=self.timeout)
            else:
                r = c.post(self.base + "login", data=creds, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise PanelError(f"panel unreachable: {e.__class__.__name__}") from e
        ok = False
        try:
            ok = r.status_code == 200 and bool(r.json().get("success"))
        except ValueError:
            pass
        if not ok:
            if r.status_code == 404:
                raise PanelError("login page not found — check the panel URL and base path")
            raise PanelError("login failed — wrong username/password, or 2FA is on "
                             "(use an API token for panels with 2FA)")
        self.s.csrf = csrf
        self.s.logged_in = True

    def _request(self, method: str, path: str, **kw) -> dict:
        """A read that hit a blink of the panel (a restart, a cut answer, a
        5xx) is asked once more; a write never is."""
        if method != "GET":
            return self._request_once(method, path, **kw)
        try:
            return self._request_once(method, path, **kw)
        except PanelError as e:
            blink = isinstance(e.__cause__, (httpx.HTTPError, ValueError)) \
                or any(f"HTTP {c} " in str(e) for c in (500, 502, 503, 504))
            if not blink or isinstance(e.__cause__, (httpx.ConnectError, httpx.TimeoutException)):
                raise
        time.sleep(0.5)
        return self._request_once(method, path, **kw)

    def _request_once(self, method: str, path: str, *, retry: bool = True, **kw) -> dict:
        """One API call. Logs in when needed and retries once on an expired session."""
        with self.s.lock:
            if not self._bearer and not self.s.logged_in:
                self._login()
        extra = kw.pop("headers", None) or {}
        headers = dict(extra)
        headers.update(self._bearer)
        if method != "GET" and self.s.csrf and not self._bearer:
            headers["X-CSRF-Token"] = self.s.csrf
        try:
            r = self.s.client.request(method, self.base + path, headers=headers,
                                      timeout=self.timeout, **kw)
        except httpx.HTTPError as e:
            raise PanelError(f"panel unreachable: {e.__class__.__name__}") from e

        # An expired cookie session looks like 404/401/403 or a redirect to the
        # login page. Log in again once.
        if r.status_code in (401, 403, 404, 302, 307) and retry and not self._bearer:
            with self.s.lock:
                self.s.logged_in = False
                self._login()
            return self._request_once(method, path, retry=False, headers=extra, **kw)
        if r.status_code == 401:
            raise PanelError("API token rejected")
        if r.status_code == 404:
            raise PanelError(f"not found: {path}")
        if r.status_code >= 400:
            raise PanelError(f"HTTP {r.status_code} on {path}")
        try:
            return r.json()
        except ValueError as e:
            raise PanelError(f"not a JSON answer on {path}") from e

    def _ok(self, method: str, path: str, **kw):
        data = self._request(method, path, **kw)
        if not data.get("success"):
            raise PanelError((data.get("msg") or "panel said no").strip()[:200])
        return data.get("obj")

    # ------------------------------------------------------------ version
    def version(self) -> str:
        if self.s.version:
            return self.s.version
        try:
            self._request("GET", "panel/api/clients/list", retry=True)
            v = "3"
        except PanelError as e:
            if "not found" in str(e):
                v = "2"
            else:
                raise
        self.s.version = v
        self.server.api_version = v
        return v

    # ------------------------------------------------------------ inbounds
    def inbounds(self) -> list[dict]:
        rows = self._ok("GET", "panel/api/inbounds/list") or []
        out = []
        for ib in rows:
            settings = _as_obj(ib.get("settings"))
            stream = _as_obj(ib.get("streamSettings"))
            out.append({
                "id": ib.get("id"),
                "remark": ib.get("remark", ""),
                "protocol": ib.get("protocol", ""),
                "port": ib.get("port"),
                "enable": bool(ib.get("enable")),
                "network": stream.get("network", ""),
                "security": stream.get("security", ""),
                "clients": len(settings.get("clients") or []) or len(ib.get("clientStats") or []),
            })
        return out

    def online(self) -> int:
        """People connected right now, on the whole panel (3.x moved the
        list from inbounds to clients)."""
        path = "panel/api/clients/onlines" if self.version() == "3" else "panel/api/inbounds/onlines"
        return len(self._ok("POST", path) or [])

    def test(self) -> dict:
        """Log in, detect the version, list inbounds. Used by the admin panel
        and the health job — the version is detected anew each time, so a
        panel upgraded from 2.x to 3.x is noticed by itself."""
        self.s.version = ""
        v = self.version()
        return {"version": v, "inbounds": self.inbounds()}

    def _redetect(self) -> bool:
        """The panel answered "not found" on a route of the remembered
        version: it may have been upgraded. True if the version changed."""
        old = self.s.version
        self.s.version = ""
        try:
            return self.version() != old
        except PanelError:
            self.s.version = old
            return False

    # ------------------------------------------------------------ clients
    def ensure(self, spec: ClientSpec, flow: str = VISION, _again: bool = True) -> list[str]:
        """Create the client or bring it to `spec` on every configured inbound.
        Returns the inbounds that failed while others worked (2.x), so the
        sync job can try them again; raises when nothing worked."""
        inbound_ids = parse_ids(self.server.inbound_ids)
        if not inbound_ids:
            raise PanelError("no inbounds selected for this server")
        try:
            if self.version() == "3":
                self._ensure_v3(spec, inbound_ids, flow)
                return []
            return self._ensure_v2(spec, inbound_ids)
        except PanelError as e:
            if _again and "not found" in str(e) and self._redetect():
                return self.ensure(spec, flow, _again=False)
            raise

    def disable(self, spec: ClientSpec) -> None:
        spec.enable = False
        self.ensure(spec)

    def usage(self, spec: ClientSpec) -> int:
        """Bytes used (up + down) by this customer on this panel."""
        total = 0
        if self.version() == "3":
            obj = self._request("GET", f"panel/api/clients/traffic/{quote(spec.email)}").get("obj")
            if obj:
                total += int(obj.get("up") or 0) + int(obj.get("down") or 0)
            return total
        for i, _ in enumerate(parse_ids(self.server.inbound_ids)):
            email = _v2_email(spec.email, i)
            data = self._request("GET", f"panel/api/inbounds/getClientTraffics/{quote(email)}")
            obj = data.get("obj") if data.get("success") else None
            if obj:
                total += int(obj.get("up") or 0) + int(obj.get("down") or 0)
        return total

    def traffic_by_email(self) -> dict[str, int]:
        """
        Bytes used per client email on the whole panel, in ONE request —
        the traffic job runs over every customer and must not make a call
        per person. The same email can appear under several inbounds (3.x
        attaches one client to many); it is the same counter, so it is
        taken once, not summed.
        """
        out: dict[str, int] = {}
        for ib in self._ok("GET", "panel/api/inbounds/list") or []:
            for st in ib.get("clientStats") or []:
                email = st.get("email")
                if email:
                    out[email] = int(st.get("up") or 0) + int(st.get("down") or 0)
        return out

    def reset_traffic(self, spec: ClientSpec) -> None:
        """Zero the traffic counter — done when a new period with a limit starts."""
        if self.version() == "3":
            self._ok("POST", f"panel/api/clients/resetTraffic/{quote(spec.email)}")
            return
        for i, inbound_id in enumerate(parse_ids(self.server.inbound_ids)):
            email = _v2_email(spec.email, i)
            data = self._request("POST",
                                 f"panel/api/inbounds/{inbound_id}/resetClientTraffic/{quote(email)}")
            if not data.get("success") and "not found" not in (data.get("msg") or "").lower():
                raise PanelError((data.get("msg") or "reset failed")[:200])

    def links(self, spec: ClientSpec) -> list[str]:
        """Share links (vless://, trojan://, ...) of this customer on this panel."""
        if self.version() == "3":
            obj = self._ok("GET", f"panel/api/clients/subLinks/{quote(spec.sub_id)}") or []
            return [x for x in obj if isinstance(x, str) and "://" in x]
        base = (self.server.sub_url or "").strip()
        if not base:
            raise PanelError("3x-ui 2.x needs the subscription URL of the panel (Sub URL)")
        if not base.endswith("/"):
            base += "/"
        try:
            r = self.s.client.get(base + quote(spec.sub_id), timeout=self.timeout)
        except httpx.HTTPError as e:
            raise PanelError(f"subscription server unreachable: {e.__class__.__name__}") from e
        if r.status_code != 200:
            raise PanelError(f"subscription server answered HTTP {r.status_code}")
        return decode_sub(r.text)

    def delete(self, spec: ClientSpec) -> None:
        """Remove the customer from the panel. 2.x cannot remove the last
        client of an inbound, so there the client is disabled instead."""
        if self.version() == "3":
            try:
                self._ok("POST", f"panel/api/clients/del/{quote(spec.email)}")
            except PanelError as e:
                if "not found" not in str(e):
                    raise
            return
        spec.enable = False
        self._ensure_v2(spec, parse_ids(self.server.inbound_ids))

    # ------------------------------------------------------------ v3
    def _ensure_v3(self, spec: ClientSpec, inbound_ids: list[int], flow: str) -> None:
        payload = {
            "email": spec.email,
            "subId": spec.sub_id,
            "id": spec.uuid,
            "uuid": spec.uuid,
            "flow": flow,
            "totalGB": int(spec.total_bytes),
            "expiryTime": int(spec.expiry_ms),
            "limitIp": int(spec.limit_ip),
            "limitHwid": 0,         # the device limit is enforced by our /sub endpoint
            "tgId": int(spec.tg_id),
            "comment": spec.comment,
            "enable": bool(spec.enable),
        }
        data = self._request("GET", f"panel/api/clients/get/{quote(spec.email)}")
        existing = (data.get("obj") or {}) if data.get("success") else {}
        if not existing:
            self._ok("POST", "panel/api/clients/add",
                     json={"client": payload, "inboundIds": inbound_ids})
            return
        # "id" is the protocol UUID as a string (the numeric id in GET
        # answers is the panel's row id and is rejected here).
        self._ok("POST", f"panel/api/clients/update/{quote(spec.email)}", json=payload)
        attached = set(int(x) for x in (existing.get("inboundIds") or []))
        missing = [i for i in inbound_ids if i not in attached]
        if missing:
            self._ok("POST", f"panel/api/clients/{quote(spec.email)}/attach",
                     json={"inboundIds": missing})

    # ------------------------------------------------------------ v2
    def _ensure_v2(self, spec: ClientSpec, inbound_ids: list[int]) -> list[str]:
        errors = []
        for i, inbound_id in enumerate(inbound_ids):
            try:
                self._ensure_v2_one(spec, inbound_id, _v2_email(spec.email, i))
            except PanelError as e:
                errors.append(f"inbound {inbound_id}: {e}")
        if errors and len(errors) == len(inbound_ids):
            raise PanelError("; ".join(errors)[:250])
        return errors

    def _ensure_v2_one(self, spec: ClientSpec, inbound_id: int, email: str) -> None:
        inbound = self._ok("GET", f"panel/api/inbounds/get/{inbound_id}")
        if not inbound:
            raise PanelError("inbound not found")
        protocol = inbound.get("protocol", "")
        settings = _as_obj(inbound.get("settings"))
        stream = _as_obj(inbound.get("streamSettings"))
        client = {
            "email": email,
            "subId": spec.sub_id,
            "totalGB": int(spec.total_bytes),
            "expiryTime": int(spec.expiry_ms),
            "limitIp": int(spec.limit_ip),
            "tgId": int(spec.tg_id),
            "enable": bool(spec.enable),
            "comment": spec.comment,
            "reset": 0,
        }
        secret = hmac.new(spec.uuid.encode(), b"vpnshop", hashlib.sha256).digest()
        if protocol in ("vless", "vmess"):
            client["id"] = spec.uuid
            if protocol == "vless":
                client["flow"] = VISION if _can_vision(stream) else ""
            else:
                client["security"] = "auto"
            key_field = "id"
        elif protocol == "trojan":
            client["password"] = secret.hex()[:24]
            key_field = "password"
        elif protocol == "shadowsocks":
            client["password"] = _ss_key(settings.get("method", ""), secret)
            client["method"] = ""
            key_field = "email"
        elif protocol in ("hysteria", "hysteria2"):
            client["auth"] = secret.hex()[:24]
            key_field = "auth"
        else:
            raise PanelError(f"protocol {protocol!r} is not supported on 3x-ui 2.x")

        current = next((c for c in settings.get("clients") or []
                        if c.get("email") == email), None)
        body = {"id": inbound_id, "settings": json.dumps({"clients": [client]})}
        if current is None:
            self._ok("POST", "panel/api/inbounds/addClient", json=body)
            return
        old_key = current.get(key_field) or email
        self._ok("POST", f"panel/api/inbounds/updateClient/{quote(str(old_key))}", json=body)


# ----------------------------------------------------------------------
def parse_ids(raw) -> list[int]:
    out = []
    for x in str(raw or "").replace(" ", "").split(","):
        if x.isdigit() and int(x) not in out:
            out.append(int(x))
    return out


def _as_obj(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            obj = json.loads(value)
            return obj if isinstance(obj, dict) else {}
        except ValueError:
            return {}
    return {}


def _v2_email(base: str, index: int) -> str:
    # 2.x wants emails unique across the whole panel, so the second and
    # following inbounds get a suffix. The subscription id stays the same.
    return base if index == 0 else f"{base}-{index + 1}"


def _can_vision(stream: dict) -> bool:
    return stream.get("network", "tcp") in ("tcp", "raw") and \
        stream.get("security") in ("tls", "reality")


def _ss_key(method: str, secret: bytes) -> str:
    size = {"2022-blake3-aes-128-gcm": 16,
            "2022-blake3-aes-256-gcm": 32,
            "2022-blake3-chacha20-poly1305": 32}.get(method)
    if size:
        return base64.b64encode(secret[:size]).decode()
    return secret.hex()[:24]


def decode_sub(text: str) -> list[str]:
    """A subscription body: base64 of newline-separated links, or plain links."""
    raw = (text or "").strip()
    if "://" not in raw:
        try:
            padded = raw + "=" * (-len(raw) % 4)
            raw = base64.b64decode(padded, altchars=b"-_" if "-" in raw or "_" in raw else None
                                   ).decode("utf-8", "replace")
        except (ValueError, UnicodeDecodeError):
            return []
    return [ln.strip() for ln in raw.splitlines() if "://" in ln]
