"""
DPI//CHECKER (dpichecker.st): a paid service that connects to a VPN key
from many points inside Russia (cities, mobile and home operators) and
says where it works. Turned on by DPICHECKER_API_KEY in .env.

The service needs real keys. The shop does not lend a customer's: it makes
a service client of its own on the chosen server ("<prefix>dpicheck", 1 GB,
three hours), takes its keys, and lets it expire by itself.

Every check is paid by the owner's DPI//CHECKER balance, so the panel first
asks the price (estimate) and starts only after the owner confirms. A start
carries an idempotency key: a repeated click or a lost answer does not pay
twice.

Built from the service's API as other open projects use it (X-API-Key,
/pops/optimal, /checks/estimate, /checks/vpn, /checks/{id}); the answers
are read defensively and shown as they come when a field is missing.
"""

from __future__ import annotations

import json
import secrets
import uuid as uuidlib

import httpx
from sqlalchemy.orm import Session

from . import access, core
from .config import settings as env
from .models import GB, Server, now
from .providers import ClientSpec, PanelError, panel_for

LOCATION = "russia"
FINISHED = {"completed", "failed", "cancelled"}


class DpiError(Exception):
    pass


def configured() -> bool:
    return bool(env.dpichecker_api_key)


def _call(method: str, path: str, body: dict | None = None, idem: str = "", timeout: float = 30) -> dict:
    if not configured():
        raise DpiError("DPICHECKER_API_KEY is not set")
    headers = {"X-API-Key": env.dpichecker_api_key, "Accept": "application/json"}
    if idem:
        headers["Idempotency-Key"] = idem
    try:
        r = httpx.request(method, env.dpichecker_url + path, json=body, headers=headers, timeout=timeout)
    except httpx.HTTPError as e:
        raise DpiError(f"no answer from DPI//CHECKER: {e.__class__.__name__}") from e
    try:
        data = r.json() if r.content else {}
    except ValueError:
        data = None
    if r.status_code >= 400 or data is None:
        msg = (data or {}).get("error") or (data or {}).get("code") if isinstance(data, dict) else ""
        raise DpiError(f"DPI//CHECKER: {msg or 'HTTP ' + str(r.status_code)}")
    return data if isinstance(data, dict) else {"items": data}


def _money(d: dict) -> float | None:
    for k in ("usd_cost", "usd", "cost", "price", "total_usd", "amount"):
        v = d.get(k)
        if isinstance(v, (int, float, str)):
            try:
                return round(float(v), 4)
            except ValueError:
                continue
    return None


def account() -> dict:
    q = _call("GET", "/quota")
    return {"balance": _money(q) if _money(q) is not None else q.get("balance"), "raw": q}


# ---------------------------------------------------------------------------
#  The service client whose keys are checked
# ---------------------------------------------------------------------------
def _spec(db: Session) -> ClientSpec:
    uid = core.get(db, "dpi_uuid")
    if not uid:
        uid = str(uuidlib.uuid4())
        core.put(db, "dpi_uuid", uid)
    sub = core.get(db, "dpi_sub")
    if not sub:
        sub = secrets.token_hex(8)
        core.put(db, "dpi_sub", sub)
    return ClientSpec(email=f"{env.xui_email_prefix}dpicheck", uuid=uid, sub_id=sub, tg_id=0,
                      expiry_ms=(now() + 3 * 3600) * 1000, total_bytes=GB, limit_ip=0,
                      enable=True, comment="DPI check")


def keys(db: Session, server: Server) -> list[str]:
    spec = _spec(db)
    snap = access._snap(server)
    access.release(db)
    try:
        panel = panel_for(snap)
        panel.ensure(ClientSpec(**spec.__dict__))
        links = panel.links(ClientSpec(**spec.__dict__))
    except PanelError as e:
        raise DpiError(f"{server.name}: {e}") from e
    links = [x for x in links if "://" in x][:20]
    if not links:
        raise DpiError(f"{server.name}: the panel gave no keys")
    return links


def _body(db: Session, server: Server) -> dict:
    pops = _call("GET", f"/pops/optimal?location={LOCATION}").get("pop_ids") or []
    if not pops:
        raise DpiError("DPI//CHECKER offered no check points in Russia")
    return {"location": LOCATION, "pop_ids": [int(p) for p in pops], "keys": keys(db, server)}


def _server(db: Session, server_id: int) -> Server:
    s = db.get(Server, server_id)
    if not s or not s.enabled:
        raise DpiError("no such enabled server")
    return s


def estimate(db: Session, server_id: int) -> dict:
    s = _server(db, server_id)
    body = _body(db, s)
    res = _call("POST", "/checks/estimate", {"check_type": "vpn", **body})
    return {"usd": _money(res), "points": len(body["pop_ids"]), "keys": len(body["keys"]), "raw": res}


def start(db: Session, server_id: int, actor: str) -> dict:
    s = _server(db, server_id)
    body = _body(db, s)
    idem = f"vpnshop-{s.id}-{now() // 120}"            # one paid start per server per two minutes
    res = _call("POST", "/checks/vpn", body, idem=idem)
    cid = res.get("id") or (res.get("check") or {}).get("id")
    if not cid:
        raise DpiError("DPI//CHECKER did not return a check id")
    core.audit(db, "DPI//CHECKER check started", actor, f"{s.name}: #{cid}, {len(body['pop_ids'])} points")
    _remember(db, s.id, {"id": int(cid), "status": res.get("status") or "pending", "at": now()})
    return {"id": int(cid), "status": res.get("status") or "pending"}


def _remember(db: Session, server_id: int, value: dict) -> None:
    try:
        last = json.loads(core.get(db, "dpi_last_json") or "{}")
    except ValueError:
        last = {}
    last[str(server_id)] = {**last.get(str(server_id), {}), **value}
    core.put(db, "dpi_last_json", json.dumps(last)[:20000])


def last(db: Session) -> dict:
    try:
        return json.loads(core.get(db, "dpi_last_json") or "{}")
    except ValueError:
        return {}


def result(db: Session, check_id: int) -> dict:
    """The check as the panel shows it: one row per point inside Russia."""
    c = _call("GET", f"/checks/{int(check_id)}")
    rows = []
    for r in c.get("results") or []:
        if r.get("is_direct"):
            continue                    # the service's own control connection, not a point in Russia
        rows.append({"region": str(r.get("pop_name") or r.get("region") or r.get("pop_id") or "?")[:80],
                     "ok": bool(r.get("connected")),
                     "ms": r.get("latency_ms") or r.get("ping_avg_ms"),
                     "error": str(r.get("verdict") or r.get("error") or r.get("error_code") or "")[:160]})
    rows.sort(key=lambda x: (x["ok"], x["region"].casefold()))
    status = str(c.get("status") or "")
    out = {"id": int(check_id), "status": status, "done": status in FINISHED, "usd": _money(c),
           "rows": rows, "ok": sum(1 for r in rows if r["ok"]), "total": len(rows)}
    for sid, v in last(db).items():
        if v.get("id") == int(check_id):
            _remember(db, int(sid), {"status": status, "ok": out["ok"], "total": out["total"], "done_at": now()
                                     if out["done"] else 0})
    return out
