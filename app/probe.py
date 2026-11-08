"""
Checkers inside Russia: is a server reachable from there, or does the
operator's DPI cut it off?

The shop itself usually sits abroad and sees every server fine. What
matters is what a customer on a Russian mobile network sees. So the owner
runs a tiny script (scripts/ru-probe.py, Python with no dependencies) on any
cheap VPS in Russia — or several, one per city / operator. Every few
minutes it asks the shop which addresses to check, opens a TCP connection
to each, greets it with a TLS hello carrying the same name the VPN key uses,
and reports back:

    tcp_ok  — the connection opened (no block by address);
    tls_ok  — the server answered the hello (no block of the handshake).

A connection that opens but goes silent or is reset right after the hello
is what DPI blocking looks like. Two failed reports in a row from a checker
while the shop itself reaches the server, and the admins get a message.

The checker authenticates with a token the owner creates in the panel; it
can only read the list of addresses and write results — nothing else.
"""

from __future__ import annotations

import hmac
import secrets

from sqlalchemy.orm import Session

from . import access, core
from .models import ProbeResult, Server, now

FAILS_TO_ALERT = 2
STALE = 30 * 60            # a checker silent this long is shown as offline
INTERVAL = 300


def token(db: Session) -> str:
    return core.get(db, "probe_token")


def new_token(db: Session, actor: str) -> str:
    t = "rp_" + secrets.token_urlsafe(24)
    core.put(db, "probe_token", t)
    core.audit(db, "Russia checker token made", actor)
    return t


def drop_token(db: Session, actor: str) -> None:
    core.put(db, "probe_token", "")
    core.audit(db, "Russia checker token removed", actor)


def authorized(db: Session, header: str) -> bool:
    t = token(db)
    given = header[7:] if header.startswith("Bearer ") else ""
    return bool(t) and bool(given) and hmac.compare_digest(given.encode(), t.encode())


def targets(db: Session) -> list[dict]:
    """Every address of every enabled server, with the TLS name its keys use."""
    out = []
    for s in access.servers(db):
        names = dict(x.split("=", 1) for x in (s.vpn_sni or "").split(",") if "=" in x)
        for e in (s.endpoints or "").split(","):
            if e:
                out.append({"server_id": s.id, "name": s.name, "endpoint": e, "sni": names.get(e, "")})
    return out


def report(db: Session, probe: str, results: list[dict]) -> dict:
    from . import notify
    probe = " ".join(str(probe or "").split())[:60] or "checker"
    known = {(t["server_id"], t["endpoint"]) for t in targets(db)}
    t = now()
    saved, newly_blocked, back = 0, [], []
    for r in results[:200]:
        key = (int(r.get("server_id") or 0), str(r.get("endpoint") or "")[:80])
        if key not in known:
            continue                    # only addresses the shop asked about
        row = (db.query(ProbeResult)
               .filter(ProbeResult.probe == probe, ProbeResult.server_id == key[0],
                       ProbeResult.endpoint == key[1]).first())
        if row is None:
            row = ProbeResult(probe=probe, server_id=key[0], endpoint=key[1])
            db.add(row)
        row.tcp_ok, row.tls_ok = bool(r.get("tcp_ok")), bool(r.get("tls_ok"))
        row.error = str(r.get("error") or "")[:160]
        row.ms = max(0, min(60000, int(r.get("ms") or 0)))
        row.at = t
        ok = row.tcp_ok and row.tls_ok
        row.fails = 0 if ok else int(row.fails or 0) + 1
        if not ok and row.fails >= FAILS_TO_ALERT and not row.alerted_at:
            row.alerted_at = t
            newly_blocked.append(row)
        elif ok and row.alerted_at:
            row.alerted_at = 0
            back.append(row)
        saved += 1
    db.commit()
    names = {s.id: s.name for s in db.query(Server).all()}
    for row in newly_blocked:
        what = "не открывается соединение" if not row.tcp_ok else "обрывается после приветствия TLS — похоже на DPI"
        core.audit(db, "Blocked from Russia", probe, f"{names.get(row.server_id)}: {row.endpoint} — {row.error}",
                   level="error")
        notify.admins(f"Из России недоступен сервер {names.get(row.server_id)} ({row.endpoint}) — проверка "
                      f"«{probe}»: {what}. / Server unreachable from Russia ({probe}).")
    for row in back:
        notify.admins(f"Из России снова доступен сервер {names.get(row.server_id)} — проверка «{probe}». "
                      f"/ Reachable from Russia again ({probe}).")
    return {"saved": saved}


def status(db: Session) -> dict:
    """For the panel: the checkers, and per server what each one sees."""
    rows = db.query(ProbeResult).order_by(ProbeResult.probe).all()
    t = now()
    probes: dict[str, int] = {}
    for r in rows:
        probes[r.probe] = max(probes.get(r.probe, 0), int(r.at or 0))
    servers = []
    for s in access.servers(db):
        cells = []
        for p in probes:
            mine = [r for r in rows if r.probe == p and r.server_id == s.id]
            if not mine:
                cells.append({"probe": p, "state": "none"})
                continue
            fresh = t - max(int(r.at or 0) for r in mine) < STALE
            bad = [r for r in mine if not (r.tcp_ok and r.tls_ok)]
            state = "stale" if not fresh else ("ok" if not bad else
                                               ("blocked" if len(bad) == len(mine) else "partial"))
            worst = bad[0] if bad else mine[0]
            cells.append({"probe": p, "state": state, "ms": worst.ms, "error": worst.error,
                          "tcp_ok": worst.tcp_ok, "tls_ok": worst.tls_ok, "at": max(int(r.at or 0) for r in mine)})
        servers.append({"id": s.id, "name": s.name, "country": s.country, "endpoints": s.endpoints or "",
                        "cells": cells})
    return {"token": bool(token(db)), "interval": INTERVAL,
            "probes": [{"name": p, "seen": at, "online": t - at < STALE} for p, at in probes.items()],
            "servers": servers}


def forget(db: Session, probe: str) -> int:
    n = db.query(ProbeResult).filter(ProbeResult.probe == probe).delete()
    db.commit()
    return n
