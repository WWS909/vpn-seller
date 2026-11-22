#!/usr/bin/env python3
"""
VPN Seller Node — the shop's own VPN server agent (instead of 3x-ui).

One small program on each VPN server. It owns Xray there: writes its
config, adds and removes customers live through Xray's API, counts their
traffic, stops those whose time or gigabytes ran out, and — on a bridge —
sends everything on to an exit server (the chain for the whitelists).

The shop talks to it over HTTPS with a token. The certificate is the
agent's own; the shop pins its fingerprint, so no domain is needed.

Only the Python standard library is used: nothing to install with pip.

    python3 agent.py init [--sni dl.google.com] [--host 1.2.3.4]
    python3 agent.py serve
    python3 agent.py connect       # prints the line to paste into the shop
    python3 agent.py status

The paths can be moved with environment variables (used by the tests):
BN_STATE_DIR, BN_XRAY_BIN, BN_XRAY_CONF, BN_XRAY_ASSETS, BN_RESTART_CMD,
BN_API_ADDR, BN_POLL.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import ssl
import subprocess
import sys
import shutil
import tempfile
import threading
import time
import uuid as uuidlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

VERSION = "1.1.0"

STATE_DIR = os.environ.get("BN_STATE_DIR", "/etc/vpn-seller-node")
XRAY_BIN = os.environ.get("BN_XRAY_BIN", "/usr/local/bin/xray")
XRAY_CONF = os.environ.get("BN_XRAY_CONF", "/usr/local/etc/xray/config.json")
XRAY_ASSETS = os.environ.get("BN_XRAY_ASSETS", "/usr/local/share/xray")
RESTART_CMD = os.environ.get("BN_RESTART_CMD", "systemctl restart xray")
API_ADDR = os.environ.get("BN_API_ADDR", "127.0.0.1:10085")
POLL = float(os.environ.get("BN_POLL", "30"))

STATE_FILE = os.path.join(STATE_DIR, "state.json")
CERT_FILE = os.path.join(STATE_DIR, "cert.pem")
KEY_FILE = os.path.join(STATE_DIR, "key.pem")

VISION = "xtls-rprx-vision"
TAG_TCP = "vless-tcp"
TAG_XHTTP = "vless-xhttp"
TAGS = (TAG_TCP, TAG_XHTTP)

# Names Reality may borrow. They answer TLS 1.3 + h2 from most places and,
# for a bridge in Russia, sit on the operators' whitelists.
DEFAULT_SNI = "dl.google.com"
FINGERPRINTS = ("chrome", "firefox", "safari", "edge", "ios", "android", "random", "randomized")

PRIVATE_CIDRS = ["0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
                 "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15",
                 "224.0.0.0/4", "240.0.0.0/4", "::1/128", "fc00::/7", "fe80::/10"]

EMAIL_RE = re.compile(r"^[A-Za-z0-9_.@+\-]{1,96}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,253}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9_\-/]{0,63}$")
SID_RE = re.compile(r"^[0-9a-f]{0,16}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{43}$")


class AgentError(Exception):
    """Something the caller asked cannot be done; the text goes back to the shop."""


def now_ms() -> int:
    return int(time.time() * 1000)


def log(*parts) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *parts, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
#  Files
# ---------------------------------------------------------------------------
def write_atomic(path: str, data: str, mode: int = 0o600) -> None:
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_state() -> dict:
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    write_atomic(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------------------
#  Xray command line
# ---------------------------------------------------------------------------
def xray(*args, timeout: float = 10, input_text: str | None = None) -> str:
    try:
        r = subprocess.run([XRAY_BIN, *args], capture_output=True, text=True,
                           timeout=timeout, input=input_text)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AgentError(f"xray {args[0] if args else ''}: {e.__class__.__name__}") from e
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        raise AgentError(f"xray {' '.join(args[:2])}: {(msg[-1] if msg else 'failed')[:200]}")
    return r.stdout


def xray_version() -> str:
    try:
        first = xray("version", timeout=5).splitlines()[0]
    except (AgentError, IndexError):
        return ""
    m = re.search(r"Xray\s+([0-9][\w.\-]*)", first)
    return m.group(1) if m else first[:40]


def gen_x25519() -> tuple[str, str]:
    """Reality key pair. Xray prints either "Private key: …/Public key: …"
    (old) or "PrivateKey: …/Password (PublicKey): …" (new)."""
    out = xray("x25519")
    values = [ln.split(":", 1)[1].strip() for ln in out.splitlines() if ":" in ln]
    if len(values) < 2 or not KEY_RE.match(values[0]) or not KEY_RE.match(values[1]):
        raise AgentError("could not read the key pair from `xray x25519`")
    return values[0], values[1]


def restart_xray() -> None:
    try:
        r = subprocess.run(shlex.split(RESTART_CMD), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AgentError(f"restart failed: {e.__class__.__name__}") from e
    if r.returncode != 0:
        raise AgentError("restart failed: " + (r.stderr or r.stdout or "").strip()[-200:])


def has_geo() -> bool:
    return os.path.isfile(os.path.join(XRAY_ASSETS, "geoip.dat")) and \
        os.path.isfile(os.path.join(XRAY_ASSETS, "geosite.dat"))


# ---------------------------------------------------------------------------
#  State
# ---------------------------------------------------------------------------
def new_state(host: str, sni: str, token: str | None = None) -> dict:
    private, public = gen_x25519()
    return {
        "version": 1,
        "token": token or secrets.token_hex(32),
        "host": host,
        "reality": {
            "private": private,
            "public": public,
            "short_ids": ["", secrets.token_hex(4), secrets.token_hex(8)],
            "sni": sni,
            "target": f"{sni}:443",
            "fp": "chrome",
        },
        "inbounds": {
            "tcp": {"enabled": True, "port": 443},
            "xhttp": {"enabled": True, "port": 8443, "path": "/" + secrets.token_hex(6),
                      "mode": "auto"},
        },
        "options": {"block_torrent": True, "ru_direct": True, "block_private": True},
        "chain": None,
        "clients": {},
        "traffic": {},
    }


def used(state: dict, email: str) -> int:
    t = state["traffic"].get(email) or {}
    return int(t.get("up") or 0) + int(t.get("down") or 0)


def is_active(state: dict, email: str, at_ms: int | None = None) -> bool:
    c = state["clients"].get(email)
    if not c or not c.get("enable"):
        return False
    at_ms = now_ms() if at_ms is None else at_ms
    if c.get("expiry_ms") and int(c["expiry_ms"]) <= at_ms:
        return False
    if c.get("total_bytes") and used(state, email) >= int(c["total_bytes"]):
        return False
    return True


def enabled_tags(state: dict) -> list[str]:
    ib = state["inbounds"]
    out = []
    if ib["tcp"].get("enabled"):
        out.append(TAG_TCP)
    if ib["xhttp"].get("enabled"):
        out.append(TAG_XHTTP)
    return out


def client_tags(state: dict, email: str) -> list[str]:
    c = state["clients"][email]
    want = [t for t in (c.get("tags") or []) if t in TAGS]
    have = enabled_tags(state)
    return [t for t in have if not want or t in want]


def desired_users(state: dict) -> dict[tuple[str, str], str]:
    """(tag, email) → uuid for everyone who must be let in right now."""
    out = {}
    at = now_ms()
    for email, c in state["clients"].items():
        if is_active(state, email, at):
            for tag in client_tags(state, email):
                out[(tag, email)] = c["uuid"]
    return out


def user_obj(tag: str, email: str, uid: str) -> dict:
    u = {"id": uid, "email": email, "level": 0}
    if tag == TAG_TCP:
        u["flow"] = VISION
    return u


# ---------------------------------------------------------------------------
#  The Xray config
# ---------------------------------------------------------------------------
def reality_server(state: dict) -> dict:
    r = state["reality"]
    return {"show": False, "target": r["target"], "xver": 0, "serverNames": [r["sni"]],
            "privateKey": r["private"], "shortIds": r["short_ids"]}


def build_config(state: dict, users: dict | None = None) -> dict:
    users = desired_users(state) if users is None else users
    sniff = {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True}
    inbounds = [{
        "tag": "api", "listen": API_ADDR.rsplit(":", 1)[0], "port": int(API_ADDR.rsplit(":", 1)[1]),
        "protocol": "dokodemo-door", "settings": {"address": API_ADDR.rsplit(":", 1)[0]},
    }]
    ib = state["inbounds"]
    if ib["tcp"].get("enabled"):
        inbounds.append({
            "tag": TAG_TCP, "listen": "0.0.0.0", "port": int(ib["tcp"]["port"]), "protocol": "vless",
            "settings": {"clients": [user_obj(TAG_TCP, e, u) for (t, e), u in sorted(users.items())
                                     if t == TAG_TCP], "decryption": "none"},
            "streamSettings": {"network": "raw", "security": "reality",
                               "realitySettings": reality_server(state)},
            "sniffing": sniff,
        })
    if ib["xhttp"].get("enabled"):
        inbounds.append({
            "tag": TAG_XHTTP, "listen": "0.0.0.0", "port": int(ib["xhttp"]["port"]), "protocol": "vless",
            "settings": {"clients": [user_obj(TAG_XHTTP, e, u) for (t, e), u in sorted(users.items())
                                     if t == TAG_XHTTP], "decryption": "none"},
            "streamSettings": {"network": "xhttp", "security": "reality",
                               "xhttpSettings": {"path": ib["xhttp"]["path"],
                                                 "mode": ib["xhttp"].get("mode") or "auto"},
                               "realitySettings": reality_server(state)},
            "sniffing": sniff,
        })

    direct = {"tag": "direct", "protocol": "freedom", "settings": {"domainStrategy": "UseIPv4v6"}}
    block = {"tag": "block", "protocol": "blackhole"}
    outbounds = [direct, block]
    chain = state.get("chain")
    if chain:
        # The first outbound is the default: on a bridge everything goes on
        # to the exit server unless a rule below says otherwise.
        outbounds.insert(0, chain_outbound(chain))

    opts = state["options"]
    rules = [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"}]
    if opts.get("block_private", True):
        rules.append({"type": "field", "ip": PRIVATE_CIDRS, "outboundTag": "block"})
    if opts.get("block_torrent"):
        rules.append({"type": "field", "protocol": ["bittorrent"], "outboundTag": "block"})
    if chain and opts.get("ru_direct"):
        # Russian sites straight from the bridge: faster, and they do not
        # see a foreign address.
        rules.append({"type": "field", "domain": ["regexp:\\.ru$", "regexp:\\.su$",
                                                  "regexp:\\.xn--p1ai$"], "outboundTag": "direct"})
        if has_geo():
            rules.append({"type": "field", "ip": ["geoip:ru"], "outboundTag": "direct"})

    return {
        "log": {"loglevel": "warning", "access": "none"},
        "api": {"tag": "api", "services": ["HandlerService", "StatsService", "LoggerService"]},
        "stats": {},
        "policy": {
            "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True,
                             "statsUserOnline": True, "handshake": 4, "connIdle": 300,
                             "uplinkOnly": 1, "downlinkOnly": 1}},
            "system": {"statsInboundUplink": True, "statsInboundDownlink": True,
                       "statsOutboundUplink": True, "statsOutboundDownlink": True},
        },
        "dns": {"servers": ["1.1.1.1", "8.8.8.8", "localhost"], "queryStrategy": "UseIP"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "routing": {"domainStrategy": "IPIfNonMatch", "rules": rules},
    }


def chain_outbound(c: dict) -> dict:
    net = "xhttp" if c.get("network") == "xhttp" else "raw"
    user = {"id": c["uuid"], "encryption": "none", "level": 0}
    if net == "raw":
        user["flow"] = VISION
    stream = {"network": net, "security": "reality",
              "realitySettings": {"serverName": c["sni"], "fingerprint": c.get("fp") or "chrome",
                                  "publicKey": c["public"], "shortId": c.get("short_id", ""),
                                  "spiderX": "/"}}
    if net == "xhttp":
        stream["xhttpSettings"] = {"path": c.get("path") or "/", "mode": c.get("mode") or "auto"}
    return {"tag": "chain", "protocol": "vless",
            "settings": {"vnext": [{"address": c["address"], "port": int(c["port"]), "users": [user]}]},
            "streamSettings": stream,
            "mux": {"enabled": False}}


# ---------------------------------------------------------------------------
#  The running agent
# ---------------------------------------------------------------------------
class Node:
    def __init__(self, state: dict):
        self.state = state
        self.lock = threading.RLock()
        self.live: dict[tuple[str, str], str] = {}     # what Xray has right now
        self.online: set[str] = set()
        self.started = time.time()
        self.last_poll = 0.0
        self.last_error = ""
        self.fails = 0                  # stats calls that failed in a row (Xray down?)
        self.restarts = 0               # Xray restarted by the watchdog

    # ---------------------------------------------------------- apply
    def write_config(self) -> dict:
        users = desired_users(self.state)
        cfg = build_config(self.state, users)
        write_atomic(XRAY_CONF, json.dumps(cfg, indent=1), mode=0o644)
        return users

    def apply_full(self) -> None:
        """Rewrite the config and restart Xray. Counters live in Xray's
        memory, so they are collected first."""
        self.poll(enforce=False)
        users = self.write_config()
        restart_xray()
        self.live = dict(users)
        save_state(self.state)

    def apply_users(self) -> None:
        """Bring Xray's users to the state without a restart (nobody else is
        cut off); a restart only when the live API refuses."""
        users = self.write_config()
        save_state(self.state)
        gone = [k for k in self.live if k not in users or self.live[k] != users[k]]
        new = [k for k in users if k not in self.live or self.live[k] != users[k]]
        try:
            by_tag: dict[str, list[str]] = {}
            for tag, email in gone:
                by_tag.setdefault(tag, []).append(email)
            for tag, emails in by_tag.items():
                xray("api", "rmu", f"--server={API_ADDR}", f"-tag={tag}", *emails)
                for e in emails:
                    self.live.pop((tag, e), None)
            if new:
                add = {"inbounds": []}
                for tag in TAGS:
                    clients = [user_obj(tag, e, users[(t, e)]) for (t, e) in new if t == tag]
                    if clients:
                        add["inbounds"].append({"tag": tag, "protocol": "vless",
                                                "settings": {"clients": clients, "decryption": "none"}})
                fd, tmp = tempfile.mkstemp(suffix=".json")
                try:
                    with os.fdopen(fd, "w") as f:
                        json.dump(add, f)
                    out = xray("api", "adu", f"--server={API_ADDR}", tmp)
                finally:
                    os.unlink(tmp)
                m = re.search(r"Added\s+(\d+)", out)
                if m and int(m.group(1)) < len(new):
                    # A user that is still there with an old key — let a
                    # restart sort it out.
                    raise AgentError("not every user was added live")
                for k in new:
                    self.live[k] = users[k]
        except AgentError as e:
            log("live update failed, restarting xray:", e)
            self.poll(enforce=False)
            restart_xray()
            self.live = dict(users)

    # ---------------------------------------------------------- stats
    def poll(self, enforce: bool = True) -> None:
        """Collect the counters (and zero them in Xray), note who is online,
        and stop those whose time or traffic ran out."""
        with self.lock:
            try:
                out = xray("api", "statsquery", f"--server={API_ADDR}", "-pattern", "user>>>",
                           "-reset", timeout=8)
            except AgentError as e:
                self.last_error = str(e)
                self.fails += 1
                # Xray does not answer for a few rounds in a row: it crashed
                # or hangs. Start it again from the config we keep.
                if self.fails >= 3:
                    log("xray is not answering, restarting it")
                    self.fails = 0
                    try:
                        self.write_config()
                        restart_xray()
                        self.live = desired_users(self.state)
                        self.restarts += 1
                    except AgentError as e2:
                        self.last_error = f"xray restart failed: {e2}"
                return
            self.fails = 0
            stats = parse_stats(out)
            touched = set()
            for (email, direction), value in stats.items():
                if value <= 0:
                    continue
                t = self.state["traffic"].setdefault(email, {"up": 0, "down": 0})
                t["up" if direction == "uplink" else "down"] = \
                    int(t.get("up" if direction == "uplink" else "down") or 0) + value
                touched.add(email)
            online = touched
            try:
                users = json.loads(xray("api", "statsgetallonlineusers",
                                        f"--server={API_ADDR}", timeout=5) or "{}").get("users") or []
                found = {u.split(">>>")[1] for u in users if u.count(">>>") >= 2}
                online = found or touched
            except (AgentError, ValueError, AttributeError):
                pass
            self.online = online
            self.last_poll = time.time()
            self.last_error = ""
            if touched:
                save_state(self.state)
            if enforce:
                want = desired_users(self.state)
                if any(k not in want for k in self.live):
                    self.apply_users()

    def poller(self) -> None:
        while True:
            time.sleep(POLL)
            try:
                self.poll()
            except Exception as e:  # noqa: BLE001 — the loop must live on
                self.last_error = f"poll: {e}"
                log("poll failed:", e)

    # ---------------------------------------------------------- API
    def info(self) -> dict:
        s = self.state
        r = s["reality"]
        inbounds = []
        counts = {t: 0 for t in TAGS}
        for (tag, _e) in desired_users(s):
            counts[tag] += 1
        for key, tag in (("tcp", TAG_TCP), ("xhttp", TAG_XHTTP)):
            ib = s["inbounds"][key]
            row = {"id": tag, "remark": "VLESS Reality " + ("TCP" if key == "tcp" else "XHTTP"),
                   "protocol": "vless", "network": key, "security": "reality",
                   "port": int(ib["port"]), "enable": bool(ib.get("enabled")),
                   "clients": counts[tag], "sni": r["sni"], "pbk": r["public"],
                   "sid": next((x for x in r["short_ids"] if x), ""), "fp": r["fp"],
                   "flow": VISION if key == "tcp" else ""}
            if key == "xhttp":
                row["path"] = ib["path"]
                row["mode"] = ib.get("mode") or "auto"
            inbounds.append(row)
        chain = s.get("chain")
        return {
            "agent": VERSION, "xray": xray_version(), "host": s.get("host", ""),
            "role": "bridge" if chain else "exit",
            "inbounds": inbounds,
            "chain": ({k: chain[k] for k in ("address", "port", "network", "sni")} if chain else None),
            "options": dict(s["options"]),
            "clients": len(s["clients"]),
            "active": len({e for (_t, e) in desired_users(s)}),
            "online": len(self.online),
            "uptime": int(time.time() - self.started),
            "last_poll": int(self.last_poll), "last_error": self.last_error,
            "geo": has_geo(),
            "watchdog_restarts": self.restarts,
            "system": system_stats(),
        }

    def ensure(self, body: dict) -> dict:
        email = str(body.get("email") or "")
        if not EMAIL_RE.match(email):
            raise AgentError("bad email")
        try:
            uid = str(uuidlib.UUID(str(body.get("uuid") or "")))
        except ValueError as e:
            raise AgentError("bad uuid") from e
        tags = [t for t in (body.get("tags") or []) if t in TAGS]
        c = {
            "uuid": uid,
            "enable": bool(body.get("enable", True)),
            "expiry_ms": max(0, int(body.get("expiry_ms") or 0)),
            "total_bytes": max(0, int(body.get("total_bytes") or 0)),
            "tags": tags,
            "comment": str(body.get("comment") or "")[:120],
        }
        with self.lock:
            self.state["clients"][email] = c
            self.apply_users()
            return {"email": email, "active": is_active(self.state, email)}

    def delete(self, body: dict) -> dict:
        email = str(body.get("email") or "")
        with self.lock:
            found = self.state["clients"].pop(email, None) is not None
            self.state["traffic"].pop(email, None)
            if found:
                self.apply_users()
            return {"deleted": found}

    def traffic(self) -> dict:
        with self.lock:
            return {e: used(self.state, e) for e in set(self.state["traffic"]) | set(self.state["clients"])}

    def reset_traffic(self, body: dict) -> dict:
        email = str(body.get("email") or "")
        with self.lock:
            self.poll(enforce=False)
            self.state["traffic"][email] = {"up": 0, "down": 0}
            self.apply_users()        # someone stopped at the limit comes back
            return {"ok": True}

    def settings(self, body: dict) -> dict:
        """Change how the server looks from outside. Everyone reconnects
        (a restart), and the keys of every customer change — the shop
        hands out the new ones through the subscription."""
        with self.lock:
            s = copy.deepcopy(self.state)
            r, ib, opts = s["reality"], s["inbounds"], s["options"]
            if "sni" in body:
                sni = str(body["sni"]).strip().lower()
                if not re.match(r"^[a-z0-9.\-]{3,253}$", sni) or "." not in sni:
                    raise AgentError("bad SNI")
                r["sni"] = sni
                r["target"] = str(body.get("target") or f"{sni}:443")
            elif "target" in body:
                r["target"] = str(body["target"])
            if not re.match(r"^[A-Za-z0-9.\-]+:\d{1,5}$", r["target"]):
                raise AgentError("bad target (host:port)")
            if "fp" in body:
                if body["fp"] not in FINGERPRINTS:
                    raise AgentError("bad fingerprint")
                r["fp"] = body["fp"]
            if body.get("new_keys"):
                r["private"], r["public"] = gen_x25519()
                r["short_ids"] = ["", secrets.token_hex(4), secrets.token_hex(8)]
            for key in ("tcp", "xhttp"):
                if f"{key}_enabled" in body:
                    ib[key]["enabled"] = bool(body[f"{key}_enabled"])
                if f"{key}_port" in body:
                    port = int(body[f"{key}_port"])
                    if not 1 <= port <= 65535:
                        raise AgentError("bad port")
                    ib[key]["port"] = port
            if "xhttp_path" in body:
                path = str(body["xhttp_path"])
                if not PATH_RE.match(path):
                    raise AgentError("bad XHTTP path")
                ib["xhttp"]["path"] = path
            if "xhttp_mode" in body:
                if body["xhttp_mode"] not in ("auto", "packet-up", "stream-up", "stream-one"):
                    raise AgentError("bad XHTTP mode")
                ib["xhttp"]["mode"] = body["xhttp_mode"]
            if not (ib["tcp"]["enabled"] or ib["xhttp"]["enabled"]):
                raise AgentError("at least one way in must stay on")
            if ib["tcp"]["enabled"] and ib["xhttp"]["enabled"] and ib["tcp"]["port"] == ib["xhttp"]["port"]:
                raise AgentError("TCP and XHTTP need different ports")
            for k in ("block_torrent", "ru_direct", "block_private"):
                if k in body:
                    opts[k] = bool(body[k])
            if "host" in body:
                host = str(body["host"]).strip()
                if host and not HOST_RE.match(host):
                    raise AgentError("bad host")
                s["host"] = host
            self.state = s
            self.apply_full()
            return self.info()

    def set_chain(self, body: dict) -> dict:
        """Make this server a bridge to an exit server, or (empty body /
        off) a plain exit again."""
        with self.lock:
            s = copy.deepcopy(self.state)
            if not body or body.get("off"):
                s["chain"] = None
            else:
                c = {
                    "address": str(body.get("address") or "").strip(),
                    "port": int(body.get("port") or 0),
                    "uuid": str(uuidlib.UUID(str(body.get("uuid") or ""))),
                    "public": str(body.get("public") or ""),
                    "short_id": str(body.get("short_id") or ""),
                    "sni": str(body.get("sni") or "").strip(),
                    "fp": str(body.get("fp") or "chrome"),
                    "network": "xhttp" if body.get("network") == "xhttp" else "tcp",
                    "path": str(body.get("path") or "/"),
                    "mode": str(body.get("mode") or "auto"),
                }
                if not HOST_RE.match(c["address"]) or not 1 <= c["port"] <= 65535:
                    raise AgentError("bad exit address")
                if not KEY_RE.match(c["public"]) or not SID_RE.match(c["short_id"]):
                    raise AgentError("bad exit Reality key")
                if not c["sni"] or c["fp"] not in FINGERPRINTS or not PATH_RE.match(c["path"]):
                    raise AgentError("bad exit settings")
                s["chain"] = c
            self.state = s
            self.apply_full()
            return self.info()


def system_stats() -> dict:
    """How the machine itself is doing — for the shop's server card."""
    out = {}
    try:
        out["load"] = round(os.getloadavg()[0], 2)
        out["cpus"] = os.cpu_count() or 1
    except OSError:
        pass
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for ln in f:
                k, v = ln.split(":", 1)
                mem[k] = int(v.split()[0]) * 1024
        out["mem_total"] = mem.get("MemTotal", 0)
        out["mem_used"] = mem.get("MemTotal", 0) - mem.get("MemAvailable", 0)
    except (OSError, ValueError, IndexError):
        pass
    try:
        du = shutil.disk_usage("/")
        out["disk_total"], out["disk_used"] = du.total, du.used
    except OSError:
        pass
    try:
        rx = tx = 0
        with open("/proc/net/dev") as f:
            for ln in f.readlines()[2:]:
                name, data = ln.split(":", 1)
                if name.strip() == "lo":
                    continue
                cols = data.split()
                rx += int(cols[0])
                tx += int(cols[8])
        out["net_rx"], out["net_tx"] = rx, tx
    except (OSError, ValueError, IndexError):
        pass
    try:
        with open("/proc/uptime") as f:
            out["uptime"] = int(float(f.read().split()[0]))
    except (OSError, ValueError):
        pass
    return out


def self_update(body: dict) -> dict:
    """Replace this program with a newer one from the shop. The shop sends
    the address and the SHA-256 of the file; anything else is refused.
    systemd starts the new one after this process exits."""
    url = str(body.get("url") or "")
    want = str(body.get("sha256") or "").lower()
    if not url.startswith(("https://", "http://")) or not re.match(r"^[0-9a-f]{64}$", want):
        raise AgentError("bad update request")
    try:
        with urlopen(url, timeout=30) as r:          # noqa: S310 — checked by the hash below
            data = r.read(2_000_000)
    except OSError as e:
        raise AgentError(f"download failed: {e.__class__.__name__}") from e
    if hashlib.sha256(data).hexdigest() != want:
        raise AgentError("the file does not match its checksum")
    text = data.decode("utf-8")
    try:
        compile(text, "agent", "exec")
    except SyntaxError as e:
        raise AgentError("the new agent does not compile") from e
    m = re.search(r'^VERSION = "([^"]+)"', text, re.M)
    write_atomic(os.path.abspath(__file__), text, mode=0o755)
    log("updated to", m.group(1) if m else "?", "— restarting")
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return {"ok": True, "version": m.group(1) if m else ""}


def parse_stats(out: str) -> dict[tuple[str, str], int]:
    """`xray api statsquery` → {(email, "uplink"|"downlink"): bytes}."""
    try:
        data = json.loads(out or "{}")
    except ValueError:
        return {}
    res = {}
    for item in data.get("stat") or []:
        name = str(item.get("name") or "")
        parts = name.split(">>>")
        if len(parts) == 4 and parts[0] == "user" and parts[2] == "traffic":
            try:
                res[(parts[1], parts[3])] = int(item.get("value") or 0)
            except (TypeError, ValueError):
                continue
    return res


# ---------------------------------------------------------------------------
#  HTTP
# ---------------------------------------------------------------------------
NODE: Node | None = None
_bad_auth: dict[str, list[float]] = {}


class Handler(BaseHTTPRequestHandler):
    server_version = "vpn-seller-node"
    sys_version = ""

    def log_message(self, fmt, *args):  # quiet: no access log with addresses
        pass

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        ip = self.client_address[0]
        recent = [t for t in _bad_auth.get(ip, []) if t > time.time() - 600]
        if len(recent) >= 20:
            self._send(429, {"error": "too many attempts"})
            return False
        got = self.headers.get("Authorization", "")
        want = "Bearer " + NODE.state["token"]
        if hmac.compare_digest(got.encode(), want.encode()):
            return True
        recent.append(time.time())
        if len(_bad_auth) > 5000:
            _bad_auth.clear()
        _bad_auth[ip] = recent
        time.sleep(0.5)
        self._send(401, {"error": "bad token"})
        return False

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            raise AgentError("body too large")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise AgentError("not JSON") from e
        if not isinstance(data, dict):
            raise AgentError("not an object")
        return data

    def _route(self, method: str) -> None:
        if not self._authed():
            return
        url = urlsplit(self.path)
        q = parse_qs(url.query)
        node = NODE
        try:
            if method == "GET" and url.path == "/api/info":
                return self._send(200, node.info())
            if method == "GET" and url.path == "/api/traffic":
                return self._send(200, node.traffic())
            if method == "GET" and url.path == "/api/client":
                email = (q.get("email") or [""])[0]
                with node.lock:
                    c = node.state["clients"].get(email)
                    if not c:
                        return self._send(404, {"error": "no such client"})
                    return self._send(200, {"email": email, **c, "used": used(node.state, email),
                                            "active": is_active(node.state, email),
                                            "online": email in node.online})
            if method == "POST" and url.path == "/api/clients/ensure":
                return self._send(200, node.ensure(self._body()))
            if method == "POST" and url.path == "/api/clients/delete":
                return self._send(200, node.delete(self._body()))
            if method == "POST" and url.path == "/api/traffic/reset":
                return self._send(200, node.reset_traffic(self._body()))
            if method == "POST" and url.path == "/api/settings":
                return self._send(200, node.settings(self._body()))
            if method == "POST" and url.path == "/api/chain":
                return self._send(200, node.set_chain(self._body()))
            if method == "POST" and url.path == "/api/restart":
                with node.lock:
                    node.apply_full()
                return self._send(200, {"ok": True})
            if method == "POST" and url.path == "/api/update":
                return self._send(200, self_update(self._body()))
            if method == "POST" and url.path == "/api/poll":
                node.poll()
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except AgentError as e:
            return self._send(400, {"error": str(e)})
        except (ValueError, TypeError, KeyError) as e:
            return self._send(400, {"error": f"bad request: {e.__class__.__name__}"})
        except Exception as e:  # noqa: BLE001
            log("error:", repr(e))
            return self._send(500, {"error": "internal error"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


def listen_addr() -> tuple[str, int]:
    raw = os.environ.get("BN_LISTEN", "")
    if not raw:
        try:
            raw = load_state().get("listen", "")
        except (OSError, ValueError):
            raw = ""
    raw = raw or "0.0.0.0:62050"
    host, port = raw.rsplit(":", 1)
    return host.strip("[]"), int(port)


def serve() -> None:
    global NODE
    NODE = Node(load_state())
    # Xray starts from the config on disk: what it has is what the file says.
    try:
        with open(XRAY_CONF, encoding="utf-8") as f:
            cfg = json.load(f)
        live = {}
        for ib in cfg.get("inbounds") or []:
            if ib.get("tag") in TAGS:
                for c in (ib.get("settings") or {}).get("clients") or []:
                    live[(ib["tag"], c.get("email"))] = c.get("id")
        NODE.live = live
    except (OSError, ValueError):
        NODE.apply_full()
    if NODE.live != desired_users(NODE.state):
        NODE.apply_users()
    threading.Thread(target=NODE.poller, daemon=True).start()
    host, port = listen_addr()
    httpd = ThreadingHTTPServer((host, port), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(CERT_FILE, KEY_FILE)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    log(f"vpn-seller-node {VERSION} on {host}:{port}")
    httpd.serve_forever()


# ---------------------------------------------------------------------------
#  Command line
# ---------------------------------------------------------------------------
def cert_fingerprint() -> str:
    with open(CERT_FILE, encoding="ascii") as f:
        pem = f.read()
    der = ssl.PEM_cert_to_DER_cert(pem)
    return hashlib.sha256(der).hexdigest()


def connect_line() -> str:
    s = load_state()
    host = s.get("host") or "SERVER_IP"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    _h, port = listen_addr()
    return f"vsnode://{s['token']}@{host}:{port}#{cert_fingerprint()}"


def cmd_init(args: list[str]) -> None:
    opts = dict(zip(args[::2], args[1::2]))
    host = opts.get("--host", "")
    sni = opts.get("--sni", DEFAULT_SNI)
    if os.path.exists(STATE_FILE):
        state = load_state()
        if host:
            state["host"] = host
        log("state exists, keeping keys and customers")
    else:
        state = new_state(host, sni)
        log("new state created")
    if "--listen" in opts:
        state["listen"] = opts["--listen"]
    if "--tcp-port" in opts:
        state["inbounds"]["tcp"]["port"] = int(opts["--tcp-port"])
    if "--xhttp-port" in opts:
        state["inbounds"]["xhttp"]["port"] = int(opts["--xhttp-port"])
    save_state(state)
    node = Node(state)
    users = node.write_config()
    node.live = dict(users)
    try:
        xray("run", "-test", "-c", XRAY_CONF, timeout=20)
    except AgentError as e:
        raise SystemExit(f"Xray rejected the config: {e}")
    restart_xray()


def main(argv: list[str]) -> None:
    cmd = argv[1] if len(argv) > 1 else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "init":
        cmd_init(argv[2:])
    elif cmd == "connect":
        print(connect_line())
    elif cmd == "status":
        s = load_state()
        print(json.dumps({"host": s.get("host"), "role": "bridge" if s.get("chain") else "exit",
                          "clients": len(s["clients"]),
                          "inbounds": s["inbounds"], "sni": s["reality"]["sni"]}, indent=1))
    elif cmd == "version":
        print(VERSION)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
