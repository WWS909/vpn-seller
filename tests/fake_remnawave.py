"""
A stand-in for the Remnawave panel API (remnawave/backend 3.4.x), written
from its public contract (@remnawave/backend-contract): the same paths,
bodies, wrappers ({"response": …}), error codes and validation rules the
shop depends on — including the ones that bite:

  * requests without X-Forwarded-Proto: https / X-Forwarded-For are refused
  * users are addressed by numeric id in the paths (3.x)
  * PATCH /api/users identifies the user by username (or id)
  * expireAt in the past is refused on update
  * unknown internal squads are refused

Used by tests/test_remnawave.py and for trying the admin panel by hand:

    python -m tests.fake_remnawave --port 3010 --token secret-token

It is not the real panel; with a real one, set REMNAWAVE_TEST_URL and
REMNAWAVE_TEST_TOKEN (and REMNAWAVE_TEST_SQUAD) to run the same tests live.
"""

from __future__ import annotations

import base64
import datetime as dt
import re
import secrets
import uuid as uuidlib

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

VERSION = "3.4.4"
USERNAME = re.compile(r"^[a-zA-Z0-9_-]+$")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_dt(value) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        d = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"message": message, "errorCode": code, "statusCode": status}, status)


def _zod(message: str) -> JSONResponse:
    return JSONResponse({"message": "[ZodValidationException] " + message, "statusCode": 400}, 400)


def create_app(token: str = "secret-token", admin=("admin", "Admin-pass-1"), host: str = "vpn.example.com"):
    app = FastAPI()
    state = {"users": {}, "next_id": 1, "sessions": set(), "requests": []}
    squads = {
        "0b1c7d8e-1111-4a2b-9c3d-000000000001": {
            "name": "Default-Squad",
            "inbounds": [{"tag": "VLESS_REALITY", "type": "vless", "network": "tcp", "security": "reality", "port": 443},
                         {"tag": "TROJAN_TLS", "type": "trojan", "network": "tcp", "security": "tls", "port": 8443}]},
        "0b1c7d8e-2222-4a2b-9c3d-000000000002": {
            "name": "Shadowsocks",
            "inbounds": [{"tag": "SS", "type": "shadowsocks", "network": "tcp", "security": None, "port": 8388}]},
    }
    app.state.data = state
    app.state.squads = squads

    @app.middleware("http")
    async def guard(request: Request, call_next):
        state["requests"].append((request.method, request.url.path))
        if state.get("flaky") and request.method == "GET" and not request.url.path.startswith("/_test"):
            state["flaky"] -= 1
            return JSONResponse({"message": "Bad gateway"}, 502)
        if request.url.path.startswith("/_test"):
            return await call_next(request)
        if request.headers.get("x-forwarded-proto") != "https" or not request.headers.get("x-forwarded-for"):
            return JSONResponse({"message": "Reverse proxy and HTTPS are required."}, 400)
        if request.url.path != "/api/auth/login":
            auth = request.headers.get("authorization", "")
            bearer = auth[7:] if auth.startswith("Bearer ") else ""
            if not bearer or (bearer != token and bearer not in state["sessions"]):
                return _err(401, "A001", "Unauthorized")
        return await call_next(request)

    # ------------------------------------------------------------ helpers
    def out(u: dict) -> dict:
        return {**{k: v for k, v in u.items() if not k.startswith("_")},
                "subscriptionUrl": f"https://{host}/api/sub/{u['shortUuid']}",
                "activeInternalSquads": [{"uuid": s, "name": squads[s]["name"]} for s in u["_squads"]],
                "userTraffic": {"usedTrafficBytes": u["_used"], "lifetimeUsedTrafficBytes": u["_used"],
                                "onlineAt": None, "firstConnectedAt": None, "lastConnectedNodeUuid": None}}

    def by_name(name: str):
        return next((u for u in state["users"].values() if u["username"] == name), None)

    def check_common(body: dict, creating: bool):
        if "status" in body and body["status"] not in ("ACTIVE", "DISABLED"):
            return "status"
        if "trafficLimitBytes" in body and (not isinstance(body["trafficLimitBytes"], int)
                                             or body["trafficLimitBytes"] < 0):
            return "trafficLimitBytes"
        if "trafficLimitStrategy" in body and body["trafficLimitStrategy"] not in (
                "NO_RESET", "DAY", "WEEK", "MONTH", "MONTH_ROLLING"):
            return "trafficLimitStrategy"
        if "expireAt" in body or creating:
            d = _parse_dt(body.get("expireAt"))
            if d is None:
                return "expireAt"
            if not creating and d <= _now():
                return "Expiration date cannot be in the past"
        for sq in body.get("activeInternalSquads") or []:
            try:
                uuidlib.UUID(sq)
            except (ValueError, TypeError, AttributeError):
                return "activeInternalSquads"
        if "telegramId" in body and body["telegramId"] is not None and not isinstance(body["telegramId"], int):
            return "telegramId"
        return None

    def apply(u: dict, body: dict):
        for k in ("status", "trafficLimitBytes", "trafficLimitStrategy", "description", "tag",
                  "telegramId", "email", "hwidDeviceLimit"):
            if k in body:
                u[k] = body[k]
        if "expireAt" in body:
            u["expireAt"] = _parse_dt(body["expireAt"]).isoformat().replace("+00:00", "Z")
        if "activeInternalSquads" in body:
            u["_squads"] = list(body["activeInternalSquads"])
        u["updatedAt"] = _now().isoformat().replace("+00:00", "Z")

    # ------------------------------------------------------------ auth/system
    @app.post("/api/auth/login")
    async def login(request: Request):
        body = await request.json()
        if (body.get("username"), body.get("password")) != tuple(admin):
            return _err(401, "A003", "Invalid credentials")
        session = secrets.token_urlsafe(24)
        state["sessions"].add(session)
        return {"response": {"accessToken": session}}

    @app.get("/api/system/metadata")
    def metadata():
        return {"response": {"version": VERSION, "build": {"time": "", "number": "0"}}}

    @app.get("/api/system/stats")
    def stats():
        users = list(state["users"].values())
        counts = {s: sum(1 for u in users if u["status"] == s)
                  for s in ("ACTIVE", "DISABLED", "LIMITED", "EXPIRED")}
        return {"response": {"cpu": {"cores": 2}, "memory": {"total": 1, "free": 1, "used": 0},
                             "uptime": 1, "timestamp": 0,
                             "users": {"statusCounts": counts, "totalUsers": len(users)},
                             "onlineStats": {"lastDay": 0, "lastWeek": 0, "neverOnline": 0, "onlineNow": 0},
                             "nodes": {"totalOnline": 1, "totalBytesLifetime": "0"}}}

    @app.get("/api/internal-squads")
    def list_squads():
        rows = []
        for i, (sid, sq) in enumerate(squads.items()):
            members = sum(1 for u in state["users"].values() if sid in u["_squads"])
            rows.append({"uuid": sid, "viewPosition": i, "name": sq["name"], "tags": [],
                         "info": {"membersCount": members, "inboundsCount": len(sq["inbounds"])},
                         "inbounds": [{"uuid": str(uuidlib.uuid4()), "profileUuid": str(uuidlib.uuid4()),
                                       "rawInbound": None, **ib} for ib in sq["inbounds"]],
                         "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z"})
        return {"response": {"total": len(rows), "internalSquads": rows}}

    # ------------------------------------------------------------ users
    @app.post("/api/users", status_code=201)
    async def create_user(request: Request):
        body = await request.json()
        name = body.get("username")
        if not isinstance(name, str) or not 3 <= len(name) <= 36 or not USERNAME.match(name):
            return _zod("username")
        bad = check_common(body, creating=True)
        if bad:
            return _zod(bad)
        for k in ("trojanPassword", "ssPassword"):
            if k in body and not (isinstance(body[k], str) and 8 <= len(body[k]) <= 32):
                return _zod(k)
        if "vlessUuid" in body:
            try:
                uuidlib.UUID(body["vlessUuid"])
            except (ValueError, TypeError, AttributeError):
                return _zod("vlessUuid")
        if by_name(name):
            return _err(400, "A019", "User username already exists")
        for sq in body.get("activeInternalSquads") or []:
            if sq not in squads:
                return _err(400, "A142", "Internal squad not found")
        uid = state["next_id"]
        state["next_id"] += 1
        stamp = _now().isoformat().replace("+00:00", "Z")
        u = {"id": uid, "shortUuid": secrets.token_urlsafe(12),
             "username": name, "status": "ACTIVE", "trafficLimitBytes": 0,
             "trafficLimitStrategy": "NO_RESET", "expireAt": None, "telegramId": None, "email": None,
             "description": None, "tag": None, "hwidDeviceLimit": None, "externalSquadUuid": None,
             "trojanPassword": body.get("trojanPassword") or secrets.token_hex(12),
             "vlessUuid": body.get("vlessUuid") or str(uuidlib.uuid4()),
             "ssPassword": body.get("ssPassword") or secrets.token_hex(12),
             "lastTriggeredThreshold": 0, "subRevokedAt": None, "lastTrafficResetAt": None,
             "createdAt": stamp, "updatedAt": stamp, "_squads": [], "_used": 0}
        apply(u, body)
        state["users"][uid] = u
        return {"response": out(u)}

    @app.patch("/api/users")
    async def update_user(request: Request):
        body = await request.json()
        body = {k: v for k, v in body.items() if k != "uuid"}     # zod strips unknown keys
        if not body.get("username") and body.get("id") is None:
            return _zod("At least one of username, id must be provided")
        bad = check_common(body, creating=False)
        if bad:
            return _zod(bad)
        u = state["users"].get(body["id"]) if body.get("id") is not None else by_name(body["username"])
        if not u:
            return _err(404, "A025", "User not found")
        for sq in body.get("activeInternalSquads") or []:
            if sq not in squads:
                return _err(400, "A142", "Internal squad not found")
        apply(u, {k: v for k, v in body.items() if k not in ("username", "id")})
        return {"response": out(u)}

    @app.get("/api/users")
    def list_users(start: int = 0, size: int = 25):
        if not 1 <= size <= 1000:
            return _zod("size")
        users = sorted(state["users"].values(), key=lambda u: u["id"])
        return {"response": {"users": [out(u) for u in users[start:start + size]], "total": len(users)}}

    @app.get("/api/users/by-username/{username}")
    def get_by_username(username: str):
        u = by_name(username)
        return {"response": out(u)} if u else _err(404, "A025", "User not found")

    def by_id(user_id: str):
        return state["users"].get(int(user_id)) if user_id.isdigit() else None

    @app.delete("/api/users/{user_id}")
    def delete_user(user_id: str):
        if not user_id.isdigit():
            return _zod("userId")
        u = by_id(user_id)
        if not u:
            return _err(404, "A025", "User not found")
        del state["users"][u["id"]]
        return {"response": {"isDeleted": True}}

    @app.post("/api/users/{user_id}/actions/revoke")
    async def revoke(user_id: str):
        u = by_id(user_id)
        if not u:
            return _err(404, "A025", "User not found") if user_id.isdigit() else _zod("userId")
        u.update(vlessUuid=str(uuidlib.uuid4()), trojanPassword=secrets.token_hex(12),
                 ssPassword=secrets.token_hex(12), shortUuid=secrets.token_urlsafe(12),
                 subRevokedAt=_now().isoformat().replace("+00:00", "Z"))
        return {"response": out(u)}

    @app.post("/api/users/{user_id}/actions/reset-traffic")
    def reset(user_id: str):
        u = by_id(user_id)
        if not u:
            return _err(404, "A025", "User not found") if user_id.isdigit() else _zod("userId")
        u["_used"] = 0
        return {"response": out(u)}

    @app.get("/api/subscriptions/by-username/{username}")
    def subscription(username: str):
        u = by_name(username)
        if not u:
            return _err(404, "A025", "User not found")
        links = []
        if u["status"] == "ACTIVE":
            for sid in u["_squads"]:
                for ib in squads[sid]["inbounds"]:
                    tag = ib["tag"]
                    if ib["type"] == "vless":
                        links.append(f"vless://{u['vlessUuid']}@{host}:{ib['port']}?security=reality"
                                     f"&type=tcp&flow=xtls-rprx-vision#{tag}")
                    elif ib["type"] == "trojan":
                        links.append(f"trojan://{u['trojanPassword']}@{host}:{ib['port']}?security=tls#{tag}")
                    elif ib["type"] == "shadowsocks":
                        cred = base64.urlsafe_b64encode(
                            f"chacha20-ietf-poly1305:{u['ssPassword']}".encode()).decode().rstrip("=")
                        links.append(f"ss://{cred}@{host}:{ib['port']}#{tag}")
        return {"response": {"isFound": True, "links": links, "ssConfLinks": {},
                             "subscriptionUrl": f"https://{host}/api/sub/{u['shortUuid']}",
                             "user": {"shortUuid": u["shortUuid"], "username": u["username"],
                                      "userStatus": u["status"], "isActive": u["status"] == "ACTIVE"}}}

    @app.post("/api/connections/drop")
    async def drop_connections(request: Request):
        body = await request.json()
        state.setdefault("dropped", []).append(body)
        return {"response": {"eventSent": True}}

    # ------------------------------------------------------------ test hooks
    @app.post("/_test/traffic")
    async def set_traffic(request: Request):
        body = await request.json()
        u = by_name(body["username"])
        u["_used"] = int(body["bytes"])
        return {"ok": True}

    return app


if __name__ == "__main__":
    import argparse

    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3010)
    ap.add_argument("--token", default="secret-token")
    a = ap.parse_args()
    uvicorn.run(create_app(a.token), host="127.0.0.1", port=a.port, log_level="warning")
