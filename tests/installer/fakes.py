"""
Stand-ins for the internet while testing install.sh on one machine:

  :18081  api.telegram.org  — getMe, webhooks, getUpdates (a message from the
                              owner, for the "detect my ID" step), invoices
  :18082  cloudflared metrics — /quicktunnel with a *.trycloudflare.com name

Every Bot API call is appended to $FAKE_DIR/tg.jsonl for the checks.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

GOOD_TOKEN = os.environ.get("FAKE_TOKEN", "7777777777:AAHfakeTokenForInstallerTests_123456")
OWNER_ID = int(os.environ.get("FAKE_OWNER_ID", "555666777"))
TUNNEL_HOST = os.environ.get("FAKE_TUNNEL_HOST", "sandbox-test-tunnel.trycloudflare.com")
LOG = os.path.join(os.environ.get("FAKE_DIR", "/tmp"), "tg.jsonl")
state = {"webhook": "", "menu": ""}
lock = threading.Lock()


def reply(h: BaseHTTPRequestHandler, obj, code: int = 200) -> None:
    data = json.dumps(obj).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


class Telegram(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.do_POST()

    def do_POST(self):
        parts = urlsplit(self.path).path.strip("/").split("/")
        if len(parts) != 2 or not parts[0].startswith("bot"):
            return reply(self, {"ok": False}, 404)
        token, method = parts[0][3:], parts[1]
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0) or 0)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        with lock, open(LOG, "a") as f:
            f.write(json.dumps({"method": method, "body": body}) + "\n")
        if token != GOOD_TOKEN:
            return reply(self, {"ok": False, "error_code": 401, "description": "Unauthorized"}, 401)
        if method == "getMe":
            return reply(self, {"ok": True, "result": {"id": 7777777777, "is_bot": True,
                                                       "username": "sandbox_shop_bot"}})
        if method == "setWebhook":
            state["webhook"] = body.get("url", "")
            return reply(self, {"ok": True, "result": True})
        if method == "deleteWebhook":
            state["webhook"] = ""
            return reply(self, {"ok": True, "result": True})
        if method == "getWebhookInfo":
            return reply(self, {"ok": True, "result": {"url": state["webhook"],
                                                       "pending_update_count": 0}})
        if method == "setChatMenuButton":
            state["menu"] = ((body.get("menu_button") or {}).get("web_app") or {}).get("url", "")
            return reply(self, {"ok": True, "result": True})
        if method == "getUpdates":
            if state["webhook"]:
                return reply(self, {"ok": False, "error_code": 409,
                                    "description": "Conflict: webhook is active"}, 409)
            offset = int(body.get("offset") or 0)
            old = {"update_id": 41, "message": {"message_id": 1, "from": {"id": 111},
                                                "chat": {"id": 111}, "text": "old"}}
            if offset <= 41:
                return reply(self, {"ok": True, "result": [old]})
            new = {"update_id": 42, "message": {"message_id": 2,
                                                "from": {"id": OWNER_ID, "first_name": "Owner"},
                                                "chat": {"id": OWNER_ID}, "text": "/start"}}
            return reply(self, {"ok": True, "result": [new] if offset == 42 else []})
        if method == "createInvoiceLink":
            return reply(self, {"ok": True, "result": "https://t.me/$sandbox-" + body.get("payload", "")})
        if method in ("sendMessage", "copyMessage", "sendDocument"):
            return reply(self, {"ok": True, "result": {"message_id": 1, "chat": {"id": body.get("chat_id")}}})
        return reply(self, {"ok": True, "result": True})


class Metrics(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/quicktunnel":
            return reply(self, {"hostname": TUNNEL_HOST})
        reply(self, {}, 404)


def main() -> None:
    servers = [ThreadingHTTPServer(("127.0.0.1", 18081), Telegram),
               ThreadingHTTPServer(("127.0.0.1", 18082), Metrics)]
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    print("fakes up", flush=True)
    servers[0].serve_forever()


if __name__ == "__main__":
    sys.exit(main())
