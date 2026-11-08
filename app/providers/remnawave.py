"""
Remnawave panel client (REST API of remnawave/backend 2.x and 3.x).

The same interface as XuiPanel and MarzbanPanel:

    test() version() inbounds() ensure() disable() usage()
    traffic_by_email() reset_traffic() links() delete()

In Remnawave a customer is a "user" (username = our email, e.g. tg123456)
who gets access through internal squads — groups of inbounds on the nodes.
The server's "inbounds" here are therefore squad UUIDs. The share links come
from the API (GET /api/subscriptions/by-username/…), the shop builds its own
subscription from them as with the other panels.

Auth: an API token from the panel (Remnawave → API tokens), sent as a Bearer
token — or, without a token, the admin login and password.

Remnawave refuses plain-HTTP requests that did not pass a reverse proxy. The
shop usually talks to it directly (same host or Docker network), so every
request carries X-Forwarded-Proto: https and X-Forwarded-For, as a proxy in
front of the panel would add.

Reissuing a key (a new uuid on our side) revokes the user's subscription in
Remnawave: new passwords and UUID, the old links stop working, and the
traffic counters stay. The key the shop last pushed is kept in the user's
description ("key:1a2b3c4d") so this happens once, not on every sync.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import threading
import time
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .xui import ClientSpec, PanelError

KIND = "remnawave"
NEVER = "2099-12-31T00:00:00.000Z"      # Remnawave needs an expiry date

_tokens: dict[tuple, tuple[str, float]] = {}
_squads_cache: dict[tuple, tuple[float, list]] = {}
_lock = threading.Lock()


def normalize_url(url: str) -> str:
    """https://panel.example.com/dashboard or …/api/ → https://panel.example.com/"""
    url = (url or "").strip()
    if not url:
        raise PanelError("panel URL is empty")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    path = parts.path or "/"
    for cut in ("/api/", "/api", "/dashboard", "/auth", "/users", "/nodes", "/hosts"):
        idx = path.find(cut)
        if idx >= 0 and (idx + len(cut) == len(path) or path[idx + len(cut)] in "/#?"):
            path = path[:idx]
            break
    if not path.endswith("/"):
        path += "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def parse_ids(raw) -> list[str]:
    return [t.strip() for t in str(raw or "").split(",") if t.strip()]


def username_of(email: str) -> str:
    """Remnawave usernames are 3–36 of letters, digits, _ and -."""
    name = re.sub(r"[^A-Za-z0-9_-]", "_", email or "")[:36]
    return name if len(name) >= 3 else (name + "___")[:3]


def _iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _marker(spec: ClientSpec) -> str:
    return "key:" + spec.uuid.replace("-", "")[:8]


class RemnawavePanel:
    def __init__(self, server, timeout: float = 15.0):
        self.server = server
        self.base = normalize_url(server.url)
        self.timeout = timeout
        self.key = (server.id, self.base, server.username, server.password,
                    getattr(server, "api_token", "") or "", bool(server.verify_tls))

    # ------------------------------------------------------------ http
    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, verify=bool(self.server.verify_tls),
                            trust_env=False, follow_redirects=False,
                            headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "127.0.0.1",
                                     "Accept": "application/json"})

    def _login(self, c: httpx.Client) -> str:
        if not (self.server.username and self.server.password):
            raise PanelError("Remnawave needs an API token (or the admin login and password)")
        try:
            r = c.post(self.base + "api/auth/login",
                       json={"username": self.server.username, "password": self.server.password})
        except httpx.HTTPError as e:
            raise PanelError(f"panel unreachable: {e.__class__.__name__}") from e
        if r.status_code == 404:
            raise PanelError("Remnawave API not found — check the panel address")
        if r.status_code in (400, 401, 403):
            raise PanelError("login failed — wrong username or password (or login by password "
                             "is off: use an API token)")
        try:
            token = r.json()["response"]["accessToken"]
        except (ValueError, KeyError, TypeError) as e:
            raise PanelError(f"unexpected login answer (HTTP {r.status_code})") from e
        with _lock:
            _tokens[self.key] = (token, time.time() + 6 * 3600)
        return token

    def _token(self, c: httpx.Client, fresh: bool = False) -> str:
        api_token = (getattr(self.server, "api_token", "") or "").strip()
        if api_token:
            return api_token
        with _lock:
            cached = _tokens.get(self.key)
        if cached and not fresh and cached[1] > time.time():
            return cached[0]
        return self._login(c)

    # A read that hit a blink of the panel (a restart, a busy proxy, a cut
    # answer) is asked again a moment later. A write never is: it may have
    # been done, and doing it twice is not always harmless.
    RETRY_STATUS = (408, 425, 429, 500, 502, 503, 504)
    RETRY_PAUSES = (0.4, 1.0)

    def _request(self, method: str, path: str, *, ok404: bool = False, **kw):
        pauses = list(self.RETRY_PAUSES) if method == "GET" else []
        while True:
            try:
                r = self._send(method, path, **kw)
            except PanelError as e:
                # Only a network failure is worth asking again; a refused
                # login or an http:// address will not fix itself.
                # A timeout is not asked again either: the caller's time
                # budget (a subscription refresh waits ~12 s) is already spent.
                cause = e.__cause__
                if not pauses or not isinstance(cause, httpx.HTTPError) \
                        or isinstance(cause, (httpx.RemoteProtocolError, httpx.TimeoutException)):
                    raise
                time.sleep(pauses.pop(0))
                continue
            if r.status_code in self.RETRY_STATUS and pauses:
                time.sleep(pauses.pop(0))
                continue
            try:
                return self._answer(r, path, ok404)
            except PanelError:
                if not pauses or r.status_code >= 400:
                    raise
                time.sleep(pauses.pop(0))      # an answer cut in the middle

    def _send(self, method: str, path: str, **kw) -> httpx.Response:
        with self._client() as c:
            for attempt in (0, 1):
                token = self._token(c, fresh=attempt == 1)
                try:
                    r = c.request(method, self.base + path,
                                  headers={"Authorization": f"Bearer {token}"}, **kw)
                except httpx.RemoteProtocolError as e:
                    raise PanelError("the panel dropped the connection — Remnawave does this to "
                                     "requests without HTTPS; use the https:// address") from e
                except httpx.HTTPError as e:
                    raise PanelError(f"panel unreachable: {e.__class__.__name__}") from e
                if r.status_code == 401 and attempt == 0 and not getattr(self.server, "api_token", ""):
                    continue                    # the session token expired
                return r
        return r

    @staticmethod
    def _answer(r: httpx.Response, path: str, ok404: bool):
        if r.status_code == 404 and ok404:
            return None
        if r.status_code in (401, 403):
            raise PanelError("the panel refused the API token — create a new one in "
                             "Remnawave → API tokens")
        if r.status_code >= 400:
            detail = ""
            try:
                detail = str(r.json().get("message", ""))[:160]
            except (ValueError, AttributeError):
                pass
            raise PanelError(f"HTTP {r.status_code} on {path}" + (f": {detail}" if detail else ""))
        try:
            data = r.json() if r.content else {}
        except ValueError as e:
            raise PanelError(f"not a JSON answer on {path} — is it the Remnawave address?") from e
        return data.get("response", data) if isinstance(data, dict) else data

    # ------------------------------------------------------------ panel
    def version(self) -> str:
        v = ""
        meta = self._request("GET", "api/system/metadata", ok404=True)
        if isinstance(meta, dict):
            v = str(meta.get("version") or "")
        v = "r" + v[:14]
        self.server.api_version = v
        return v

    def _squads(self, fresh: bool = False) -> list[dict]:
        with _lock:
            hit = _squads_cache.get(self.key)
        if hit and not fresh and time.time() - hit[0] < 300:
            return hit[1]
        data = self._request("GET", "api/internal-squads") or {}
        squads = data.get("internalSquads") or []
        with _lock:
            _squads_cache[self.key] = (time.time(), squads)
        return squads

    def inbounds(self) -> list[dict]:
        """Internal squads, in the shape the admin panel lists inbounds."""
        out = []
        for sq in self._squads():
            info = sq.get("info") or {}
            protos = sorted({str(i.get("type") or "").lower() for i in sq.get("inbounds") or []
                             if i.get("type")})
            out.append({"id": sq.get("uuid"), "remark": sq.get("name", ""),
                        "protocol": ", ".join(protos) or "squad", "port": "",
                        "enable": True, "network": str(int(info.get("inboundsCount") or 0)),
                        "security": "", "clients": int(info.get("membersCount") or 0)})
        return out

    def online(self) -> int | None:
        """People connected right now, over all nodes of the panel."""
        stats = self._request("GET", "api/system/stats") or {}
        value = (stats.get("onlineStats") or {}).get("onlineNow")
        return int(value) if isinstance(value, (int, float)) else None

    def test(self) -> dict:
        stats = self._request("GET", "api/system/stats") or {}
        self.version()
        with _lock:
            _squads_cache.pop(self.key, None)
        users = (stats.get("users") or {}).get("totalUsers") or 0
        return {"version": self.server.api_version, "inbounds": self.inbounds(),
                "clients": int(users), "kind": KIND}

    # ------------------------------------------------------------ users
    def _secret(self, spec: ClientSpec, what: str) -> str:
        return hmac.new(spec.uuid.encode(), f"vpnshop-{what}".encode(), hashlib.sha256).hexdigest()[:24]

    def _get(self, name: str):
        return self._request("GET", f"api/users/by-username/{quote(name)}", ok404=True)

    @staticmethod
    def _ref(user: dict) -> str:
        """Path id of a user: the numeric id in 3.x, the uuid in 2.x."""
        ref = user.get("id") if user.get("id") is not None else user.get("uuid")
        if ref is None:
            raise PanelError("unexpected user object from the panel")
        return quote(str(ref))

    def _fields(self, spec: ClientSpec) -> dict:
        squads = parse_ids(self.server.inbound_ids)
        if not squads:
            raise PanelError("no internal squads selected for this server")
        known = {s.get("uuid") for s in self._squads()}
        missing = [s for s in squads if s not in known]
        if missing and known:
            known = {s.get("uuid") for s in self._squads(fresh=True)}
            missing = [s for s in squads if s not in known]
        if missing:
            raise PanelError("no such internal squads on the panel: " + ", ".join(missing))
        now_ms = time.time() * 1000
        active = spec.enable and (not spec.expiry_ms or spec.expiry_ms > now_ms + 60_000)
        body = {"status": "ACTIVE" if active else "DISABLED",
                "trafficLimitBytes": int(spec.total_bytes or 0),
                "trafficLimitStrategy": "NO_RESET",
                "description": ((spec.comment[:400] + "\n") if spec.comment else "") + _marker(spec),
                "activeInternalSquads": squads}
        if spec.tg_id:
            body["telegramId"] = int(spec.tg_id)
        # The panel refuses an expiry in the past; an expired customer is
        # simply disabled and keeps the old date.
        if not spec.expiry_ms:
            body["expireAt"] = NEVER
        elif spec.expiry_ms > now_ms + 60_000:
            body["expireAt"] = _iso(spec.expiry_ms)
        return body

    def _mine(self, current: dict, spec: ClientSpec, name: str) -> None:
        """A panel user with our name but somebody else's Telegram id belongs
        to another system (or was renamed by hand): it is never changed."""
        theirs = current.get("telegramId")
        if spec.tg_id and theirs not in (None, "", 0) and str(theirs) != str(spec.tg_id):
            raise PanelError(f"the panel user {name} belongs to Telegram id {theirs}, not to this "
                             "customer — rename it in the panel or change the user prefix")

    def drop_connections(self, current: dict) -> bool:
        """Cut the user's live connections on every node right away (Remnawave
        2.2+). Best effort: an older panel without it just keeps them until
        the client reconnects."""
        if current.get("id") is not None and str(current["id"]).isdigit():
            by = {"by": "userIds", "userIds": [int(current["id"])]}
        elif current.get("uuid"):
            by = {"by": "userUuids", "userUuids": [current["uuid"]]}
        else:
            return False
        try:
            r = self._send("POST", "api/connections/drop",
                           json={"dropBy": by, "targetNodes": {"target": "allNodes"}})
        except PanelError:
            return False
        return r.status_code < 400

    def ensure(self, spec: ClientSpec, flow: str = "") -> None:
        name = username_of(spec.email)
        body = self._fields(spec)
        current = self._get(name)
        if current is None:
            create = {"username": name, "vlessUuid": spec.uuid,
                      "trojanPassword": self._secret(spec, "trojan"),
                      "ssPassword": self._secret(spec, "ss"), **body}
            create.setdefault("expireAt", _iso(time.time() * 1000 + 3600_000))
            self._request("POST", "api/users", json=create)
            return
        self._mine(current, spec, name)
        ref = self._ref(current)
        cut = False
        if current.get("vlessUuid") != spec.uuid and _marker(spec) not in (current.get("description") or ""):
            # The key was reissued in the shop: new secrets, old links die.
            self._request("POST", f"api/users/{ref}/actions/revoke", json={})
            cut = True
        patch = {"username": name, **body}
        if current.get("uuid"):
            patch["uuid"] = current["uuid"]         # 2.x identifies by uuid
        self._request("PATCH", "api/users", json=patch)
        # Switched off (expired, blocked, traffic used up) or reissued: the
        # connections that are open now end now, not at the next reconnect.
        if cut or (str(current.get("status") or "").upper() == "ACTIVE" and body["status"] == "DISABLED"):
            self.drop_connections(current)

    def disable(self, spec: ClientSpec) -> None:
        spec.enable = False
        self.ensure(spec)

    @staticmethod
    def _used(u: dict) -> int:
        traffic = u.get("userTraffic") or {}
        return int(traffic.get("usedTrafficBytes") or u.get("usedTrafficBytes") or 0)

    def usage(self, spec: ClientSpec) -> int:
        u = self._get(username_of(spec.email))
        return self._used(u) if u else 0

    def traffic_by_email(self) -> dict[str, int]:
        out: dict[str, int] = {}
        start = 0
        while True:
            data = self._request("GET", "api/users", params={"start": start, "size": 500}) or {}
            users = data.get("users") or []
            for u in users:
                name = u.get("username", "")
                tid = u.get("telegramId")
                digits = re.sub(r"\D", "", name)
                if tid not in (None, "", 0) and digits and str(tid) != digits:
                    continue                         # same name, another person: not our traffic
                out[name] = self._used(u)
            start += len(users)
            if not users or start >= int(data.get("total") or 0):
                return out

    def reset_traffic(self, spec: ClientSpec) -> None:
        u = self._get(username_of(spec.email))
        if u:
            self._request("POST", f"api/users/{self._ref(u)}/actions/reset-traffic")

    def links(self, spec: ClientSpec) -> list[str]:
        name = username_of(spec.email)
        current = self._get(name)
        if current is None:
            raise PanelError("the customer is not on this panel yet")
        self._mine(current, spec, name)          # never hand out somebody else's keys
        info = self._request("GET", f"api/subscriptions/by-username/{quote(name)}", ok404=True)
        if not info or info.get("isFound") is False:
            raise PanelError("the customer is not on this panel yet")
        return [x for x in (info.get("links") or []) if isinstance(x, str) and "://" in x]

    def delete(self, spec: ClientSpec) -> None:
        name = username_of(spec.email)
        u = self._get(name)
        if u:
            self._mine(u, spec, name)
            self.drop_connections(u)
            self._request("DELETE", f"api/users/{self._ref(u)}", ok404=True)


def drop(server_id: int) -> None:
    with _lock:
        for store in (_tokens, _squads_cache):
            for key in [k for k in store if k[0] == server_id]:
                store.pop(key, None)
