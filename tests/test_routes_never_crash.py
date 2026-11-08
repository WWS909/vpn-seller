"""Every route with empty / odd input must answer 2xx/4xx, never 500."""
import re
from conftest import tg

ODD = [{}, {"x": 1}, {"id": "abc", "amount": -1, "days": 10**12, "text": "<b>" * 5000, "code": "", "gb": -5},
       {"values": {"CARD_GATEWAY": None}}, [], "x"]


def test_no_route_crashes(client, admin):
    from app.routers import admin_api, app_api, pay
    routes = []
    for mod in (app_api, admin_api, pay):
        for r in mod.router.routes:
            routes.append((r.path, r.methods))
    bad, n, codes = [], 0, {}
    who = tg(4242)
    client.get("/api/app/me", headers=who)
    for path, methods in routes:
        if "webhook" in path or path.endswith(("/logout", "/restore")):
            continue
        for m in sorted(methods or []):
            if m in ("HEAD", "OPTIONS"):
                continue
            for pid in ("1", "999999", "abc"):
                url = re.sub(r"\{[^}]+\}", pid, path)
                h = admin if path.startswith("/api/admin") else who
                for body in (ODD if m in ("POST", "PUT", "PATCH") else [None]):
                    n += 1
                    try:
                        res = client.request(m, url, headers=h, json=body) if body is not None else client.request(m, url, headers=h)
                    except Exception as e:      # an exception escaping the app is a crash too
                        bad.append((m, url, repr(body)[:40], "EXC " + repr(e)[:160]))
                        continue
                    codes[res.status_code] = codes.get(res.status_code, 0) + 1
                    if res.status_code >= 500 and res.status_code not in (502, 503):
                        bad.append((m, url, repr(body)[:40], res.status_code, res.text[:160]))
    print("requests:", n, codes)
    assert n > 500
    assert not bad, "\n".join(map(str, bad[:40]))
