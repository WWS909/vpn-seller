"""
Who is calling.

* Mini app: every request carries Telegram's signed initData in the
  X-Init-Data header. Nothing personal is returned without it.
* Admin panel: login + password from .env, then a short-lived token.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import threading
import time
from urllib.parse import parse_qsl

import jwt
from fastapi import Header, HTTPException, Request

from .config import settings as env

INIT_DATA_MAX_AGE = 24 * 3600


def verify_init_data(init_data: str, bot_token: str | None = None,
                     max_age: int = INIT_DATA_MAX_AGE) -> dict:
    """
    Check Telegram WebApp initData (core.telegram.org/bots/webapps).
    secret = HMAC_SHA256(key="WebAppData", msg=bot_token)
    hash   = HMAC_SHA256(key=secret, msg=data_check_string)
    """
    token = bot_token if bot_token is not None else env.bot_token
    if not init_data or not token:
        raise HTTPException(401, "open the app from Telegram")
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        raise HTTPException(401, "malformed init data")
    received = pairs.pop("hash", "")
    # Every other field, "signature" included, is part of the check string.
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    # Bytes: a non-ASCII "hash" is a bad signature (401), not a crash (500).
    if not received or not hmac.compare_digest(calc.encode(), received.encode()):
        raise HTTPException(401, "bad signature")
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        auth_date = 0
    if max_age and time.time() - auth_date > max_age:
        raise HTTPException(401, "session expired, reopen the app")
    user = json.loads(pairs.get("user", "{}") or "{}")
    if not user.get("id"):
        raise HTTPException(401, "no user")
    # start_param is inside the signed data, so it can be trusted.
    user["_start"] = pairs.get("start_param", "")[:64]
    return user


async def tg_user(x_init_data: str = Header(default="")) -> dict:
    return verify_init_data(x_init_data)


# ---------------------------------------------------------------------------
#  Admin
# ---------------------------------------------------------------------------
ALGO = "HS256"
TOKEN_HOURS = 12

# Signed-out tokens (their id → when they would have expired). Kept in the
# database so a restart does not bring them back.
_revoked: dict[str, int] | None = None
_revoked_lock = threading.Lock()


def _key() -> str:
    """The signing key also depends on the admin credentials: a new
    password or a removed admin id ends every session made before."""
    who = f"{env.admin_login}\n{env.admin_password}\n{sorted(env.admin_telegram_ids)}"
    return env.secret_key + ":" + hashlib.sha256(who.encode()).hexdigest()


def _load_revoked() -> dict[str, int]:
    global _revoked
    with _revoked_lock:
        if _revoked is None:
            from . import core
            from .db import SessionLocal
            try:
                with SessionLocal() as db:
                    raw = core.get_json(db, "admin_revoked", {})
            except Exception:  # noqa: BLE001 — no database right now (first start, an outage)
                # Not remembered as "nothing revoked": the next request reads
                # the list again, so a signed-out token never comes back to life.
                return {}
            t = int(time.time())
            _revoked = {str(k): int(v) for k, v in (raw.items() if isinstance(raw, dict) else [])
                        if int(v) > t}
        return _revoked


def revoke(token: str) -> None:
    """Sign out: this token stops working at once, not in 12 hours."""
    try:
        data = jwt.decode(token, _key(), algorithms=[ALGO])
    except jwt.PyJWTError:
        return
    jti = str(data.get("jti") or "")
    if not jti:
        return
    from . import core
    from .db import SessionLocal
    revoked = _load_revoked()
    t = int(time.time())
    with _revoked_lock:
        revoked[jti] = int(data.get("exp") or t + TOKEN_HOURS * 3600)
        for k in [k for k, v in revoked.items() if v <= t]:
            revoked.pop(k, None)
        snapshot = json.dumps(revoked)
    with SessionLocal() as db:
        core.put(db, "admin_revoked", snapshot)


def create_token(login: str, team_tg: int = 0) -> str:
    """team_tg — a member of the owner's team (app/team.py): their sections are
    looked up on every request, so taking access away works at once."""
    claims = {"sub": login, "scope": "admin", "jti": secrets.token_urlsafe(12),
              "exp": int(time.time()) + TOKEN_HOURS * 3600}
    if team_tg:
        claims["team"] = int(team_tg)
    return jwt.encode(claims, _key(), algorithm=ALGO)


def token_claims(authorization: str) -> dict:
    """The claims of a valid, not revoked admin token, or {}."""
    if not authorization.startswith("Bearer ") or len(env.secret_key) < 32:
        return {}
    try:
        data = jwt.decode(authorization[7:], _key(), algorithms=[ALGO])
    except jwt.PyJWTError:
        return {}
    if data.get("scope") != "admin" or str(data.get("jti") or "") in _load_revoked():
        return {}
    return data


async def admin_required(request: Request, authorization: str = Header(default="")) -> str:
    if not authorization.startswith("Bearer ") or len(env.secret_key) < 32:
        raise HTTPException(401, "login required")
    try:
        data = jwt.decode(authorization[7:], _key(), algorithms=[ALGO])
    except jwt.PyJWTError:
        raise HTTPException(401, "login required")
    if data.get("scope") != "admin":
        raise HTTPException(403, "forbidden")
    if str(data.get("jti") or "") in _load_revoked():
        raise HTTPException(401, "login required")
    if data.get("team"):
        # A team member: only their sections, checked against the table now.
        from . import team
        from .db import SessionLocal
        db = SessionLocal()
        try:
            m = team.member(db, int(data["team"]))
            if m is None:
                raise HTTPException(401, "login required")
            if not team.allowed(m, team.section_of(request.url.path)):
                raise HTTPException(403, "this section is not open for you")
            team.touch(db, m)
        finally:
            db.close()
    return data.get("sub", "admin")


def check_credentials(login: str, password: str) -> bool:
    """Constant-time compare on bytes (works with non-ASCII passwords).
    An empty or short configured password never matches."""
    if len(env.admin_password) < 8 or not env.admin_login:
        return False
    ok_login = hmac.compare_digest(login.encode(), env.admin_login.encode())
    ok_pass = hmac.compare_digest(password.encode(), env.admin_password.encode())
    return ok_login and ok_pass


# Failed logins per address: 8 in a row → 15 minutes of silence. On top of
# that, a budget for everyone together: somebody with many addresses gets
# 60 tries per 15 minutes in all, not 8 per address. (Signing in from the
# bot, by Telegram's signature, keeps working during such a lock.)
_attempts: dict[str, list] = {}
_all_fails: list[float] = []
MAX_ATTEMPTS = 8
MAX_ALL = 60
LOCK_SECONDS = 900
MAX_TRACKED = 5000


def _from_proxy(host: str) -> bool:
    """A peer that may speak for someone else: a proxy on this machine or in
    the Docker network (Caddy, cloudflared, nginx on the host). An address
    on the internet may not — it could write any X-Forwarded-For."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True                      # a unix socket, the test client
    return ip.is_private or ip.is_loopback


def client_ip(request: Request) -> str:
    # The LAST address of X-Forwarded-For is the one our own reverse proxy
    # saw. Caddy replaces the header entirely; nginx with
    # $proxy_add_x_forwarded_for appends to what the client sent — earlier
    # entries can be forged, the last one cannot. The header counts only
    # when a proxy sent it: should the app ever be reached from the internet
    # directly, a made-up header must not dodge the login rate limit.
    # (uvicorn already took the address from a trusted proxy's header —
    # Dockerfile: --forwarded-allow-ips lists the private networks only.)
    peer = request.client.host if request.client else ""
    fwd = request.headers.get("x-forwarded-for", "")
    if not fwd or not _from_proxy(peer):
        return peer
    return fwd.split(",")[-1].strip() or peer


def locked(ip: str, password: bool = True) -> int:
    t = time.time()
    if password:
        _all_fails[:] = [x for x in _all_fails if t - x < LOCK_SECONDS]
        if len(_all_fails) >= MAX_ALL:
            return int(_all_fails[0] + LOCK_SECONDS - t) + 1
    rec = _attempts.get(ip)
    if not rec or rec[0] < MAX_ATTEMPTS:
        return 0
    left = int(rec[1] + LOCK_SECONDS - t)
    if left <= 0:
        _attempts.pop(ip, None)
        return 0
    return left


def failed(ip: str, password: bool = True) -> str:
    """Count a wrong attempt. Returns "ip" when this very attempt shut the
    address, "all" when it shut password sign-in for everyone, else ""
    (so the admins are told once, at the moment it happens)."""
    t = time.time()
    shut = ""
    if password:
        _all_fails[:] = [x for x in _all_fails if t - x < LOCK_SECONDS]
        _all_fails.append(t)
        if len(_all_fails) == MAX_ALL:
            shut = "all"
    if len(_attempts) >= MAX_TRACKED:
        # Many addresses: forget the oldest half rather than grow forever.
        for k, _ in sorted(_attempts.items(), key=lambda kv: kv[1][1])[:MAX_TRACKED // 2]:
            _attempts.pop(k, None)
    rec = _attempts.get(ip)
    if not rec or t - rec[1] > LOCK_SECONDS:
        _attempts[ip] = rec = [1, t]
    else:
        rec[0] += 1
    if rec[0] == MAX_ATTEMPTS and not shut:
        shut = "ip"
    return shut


def device(user_agent: str) -> str:
    """"Chrome · Windows" from a User-Agent: enough to recognise one's own."""
    ua = user_agent or ""
    browser = next((name for mark, name in (("Telegram", "Telegram"), ("YaBrowser", "Yandex Browser"),
                                             ("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
                                             ("Chrome/", "Chrome"), ("Safari/", "Safari")) if mark in ua), "")
    system = next((name for mark, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                            ("Windows", "Windows"), ("Mac OS X", "macOS"), ("Linux", "Linux"))
                   if mark in ua), "")
    return " · ".join(x for x in (browser, system) if x)


def succeeded(ip: str) -> None:
    _attempts.pop(ip, None)
