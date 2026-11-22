#!/usr/bin/env python3
"""
A stand-in for the xray binary in the VPN Seller Node tests: the same command
lines and the same output shapes, with the "running" Xray kept in a JSON
file (FAKE_XRAY_STATE). `fake_xray.py restart` plays systemctl restart.
Traffic is injected by the tests through the same file.
"""

import json
import os
import sys

STATE = os.environ["FAKE_XRAY_STATE"]
CONF = os.environ.get("BN_XRAY_CONF", "")


def load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"running": False, "users": {}, "stats": {}, "calls": [], "restarts": 0,
                "fail_api": False}


def save(s):
    with open(STATE, "w") as f:
        json.dump(s, f)


def main(argv):
    s = load()
    s["calls"].append(argv[1:3])
    cmd = argv[1]
    if cmd == "version":
        print("Xray 26.1.1 (Xray, Penetrates Everything.) Custom (go1.25 linux/amd64)")
    elif cmd == "x25519":
        import base64
        import secrets
        k = lambda: base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")  # noqa: E731
        print(f"PrivateKey: {k()}\nPassword (PublicKey): {k()}\nHash32: x")
    elif cmd == "run":
        with open(argv[argv.index("-c") + 1]) as f:
            cfg = json.load(f)
        assert any(i["tag"] == "api" for i in cfg["inbounds"])
        print("Configuration OK.")
    elif cmd == "restart":
        with open(CONF) as f:
            cfg = json.load(f)
        s["users"] = {}
        for ib in cfg["inbounds"]:
            if ib["tag"].startswith("vless"):
                s["users"][ib["tag"]] = {c["email"]: c["id"] for c in ib["settings"]["clients"]}
        s["running"] = True
        s["restarts"] += 1
        s["stats"] = {}
    elif cmd == "api":
        if not s["running"] or s.get("fail_api"):
            save(s)
            print("failed to dial", file=sys.stderr)
            return 1
        sub = argv[2]
        if sub == "adu":
            n = 0
            for path in [a for a in argv[3:] if not a.startswith("-")]:
                with open(path) as f:
                    cfg = json.load(f)
                for ib in cfg["inbounds"]:
                    tag_users = s["users"].setdefault(ib["tag"], {})
                    for c in ib["settings"]["clients"]:
                        if c["email"] not in tag_users:
                            tag_users[c["email"]] = c["id"]
                            n += 1
            print("Added", n, "user(s) in total.")
        elif sub == "rmu":
            tag = next(a.split("=", 1)[1] for a in argv[3:] if a.startswith("-tag="))
            for e in [a for a in argv[3:] if not a.startswith("-")]:
                s["users"].get(tag, {}).pop(e, None)
                print("remove user:", e)
        elif sub == "statsquery":
            stat = [{"name": f"user>>>{e}>>>traffic>>>{d}", "value": str(v)}
                    for e, dirs in s["stats"].items() for d, v in dirs.items()]
            if "-reset" in argv:
                s["stats"] = {}
            print(json.dumps({"stat": stat}))
        elif sub == "statsgetallonlineusers":
            print(json.dumps({"users": [f"user>>>{e}>>>online" for e in s.get("online", [])]}))
        else:
            return 1
    else:
        return 1
    save(s)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
