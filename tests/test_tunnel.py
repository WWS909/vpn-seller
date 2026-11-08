"""Tunnel mode: the shop follows the changing Cloudflare quick-tunnel address."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import Fake


class _Metrics(BaseHTTPRequestHandler):
    hostname = ""

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path != "/quicktunnel":
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps({"hostname": _Metrics.hostname}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def metrics():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Metrics)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture()
def tunnel_env(env, metrics, monkeypatch):
    monkeypatch.setenv("TUNNEL_METRICS_URL", metrics)
    monkeypatch.setenv("PUBLIC_URL", "")
    monkeypatch.setenv("DOMAIN", "")
    monkeypatch.setenv("BOT_SETUP", "1")
    env.__init__()
    return env


def test_follows_new_tunnel_address_and_reregisters_bot(tunnel_env, db):
    from app import notify
    assert not tunnel_env.https                      # nothing known yet

    _Metrics.hostname = "first-words-here.trycloudflare.com"
    assert notify.follow_tunnel(db) is True
    assert tunnel_env.public_url == "https://first-words-here.trycloudflare.com"
    hook = Fake.sent("setWebhook")[-1]
    assert hook["url"] == "https://first-words-here.trycloudflare.com/api/bot/webhook"
    menu = Fake.sent("setChatMenuButton")[-1]
    assert menu["menu_button"]["web_app"]["url"] == "https://first-words-here.trycloudflare.com/app"

    # Same address again: nothing to do, Telegram is not called.
    before = len(Fake.calls)
    assert notify.follow_tunnel(db) is False
    assert len(Fake.calls) == before

    # The tunnel restarted with a new address.
    _Metrics.hostname = "second-other-words.trycloudflare.com"
    assert notify.follow_tunnel(db) is True
    assert Fake.sent("setWebhook")[-1]["url"].startswith("https://second-other-words.")


def test_tunnel_not_ready_keeps_current_address(tunnel_env, db):
    from app import notify
    _Metrics.hostname = ""
    assert notify.follow_tunnel(db) is False
    assert Fake.sent("setWebhook") == []


def test_garbage_from_metrics_is_ignored(tunnel_env, db):
    from app import notify
    _Metrics.hostname = "evil.com/steal"
    assert notify.follow_tunnel(db) is False


def test_no_tunnel_configured_does_nothing(env, db):
    from app import notify
    assert env.tunnel_metrics_url == ""
    assert notify.tunnel_address() == ""
    assert notify.follow_tunnel(db) is False
