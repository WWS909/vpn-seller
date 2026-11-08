"""
New versions from GitHub: the check, the owners' message (once per version),
the request the server picks up, the night auto-update, the message about how
an update ended, and the panel's page. GitHub itself is replaced here.
"""
from __future__ import annotations

import json
import time

import pytest

from conftest import Fake, init_data


class _Resp:
    def __init__(self, code: int, text: str = ""):
        self.status_code, self.text = code, text


CHANGELOG = """# Что нового

## 1.3.0 — 2026-12-20
- Третья штука

## 1.2.0 — 2026-12-01
- Новая оплата
- Быстрее главная

## 1.1.0 — 2026-11-08
- Уже есть у вас
"""


@pytest.fixture()
def github(monkeypatch):
    """A fake GitHub: files by name; `calls` keeps the asked URLs and headers."""
    from app import updates
    state = {"files": {"VERSION": "1.2.0\n", "CHANGELOG.md": CHANGELOG,
                       "CHANGELOG.en.md": CHANGELOG.replace("Новая оплата", "New payments")},
             "code": 200, "calls": []}

    def get(url, headers):
        state["calls"].append((url, headers))
        if state["code"] != 200:
            return _Resp(state["code"])
        name = url.split("/contents/", 1)[1].split("?", 1)[0]
        return _Resp(200, state["files"][name]) if name in state["files"] else _Resp(404)

    monkeypatch.setattr(updates, "_http_get", get)
    from app import release
    monkeypatch.setattr(release, "version", lambda: "1.1.0")     # as if 1.1.0 were installed
    monkeypatch.setenv("UPDATE_REPO", "https://github.com/ivan/vpn-seller.git")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("UPDATE_BRANCH", raising=False)
    return state


def _ready(env):
    (env.data_dir / "updater.json").write_text('{"installed": 1}')


def _status(env, **kw):
    (env.data_dir / "update.status").write_text(json.dumps({"at": int(time.time()), "log": "", "reason": "", **kw}))


def _owner_msgs():
    return [b for b in Fake.sent("sendMessage") if b.get("chat_id") == 999]


# ---------------------------------------------------------------- version and notes
def test_version_comes_from_the_file(client):
    from app import release
    v = (release.ROOT / "VERSION").read_text().strip()
    assert release.SEMVER.match(v) and release.version() == v
    assert client.get("/health").json()["version"] == v
    # the image carries the files the shop reads
    docker = (release.ROOT / "Dockerfile").read_text()
    assert "COPY VERSION CHANGELOG.md CHANGELOG.en.md ./" in docker
    # this version is described in both changelogs
    for name in ("CHANGELOG.md", "CHANGELOG.en.md"):
        assert release.notes((release.ROOT / name).read_text(), "0.0.0", v)[0]["version"] == v, name


def test_versions_compare_and_repo_forms(monkeypatch):
    from app import release
    assert release.newer("1.10.0", "1.9.9") and not release.newer("1.2.0", "1.2.0")
    assert not release.newer("v2", "1.0.0") and not release.newer("", "1.0.0")
    for raw in ("ivan/vpn-seller", "https://github.com/ivan/vpn-seller", "github.com/ivan/vpn-seller/",
                "git@github.com:ivan/vpn-seller.git", "https://github.com/ivan/vpn-seller.git"):
        monkeypatch.setenv("UPDATE_REPO", raw)
        assert release.repo() == "ivan/vpn-seller", raw
    for bad in ("OWNER/vpn-seller", "just-a-name", "a/b/c", "ivan/vpn seller"):
        monkeypatch.setenv("UPDATE_REPO", bad)
        assert release.repo() == "", bad
    monkeypatch.delenv("UPDATE_REPO")
    assert release.repo() == ("" if release.REPO.startswith("OWNER/") else release.REPO)


def test_notes_between_versions():
    from app import release
    got = release.notes(CHANGELOG, "1.1.0", "1.2.0")
    assert [s["version"] for s in got] == ["1.2.0"]
    assert got[0]["items"] == ["Новая оплата", "Быстрее главная"] and got[0]["date"] == "2026-12-01"
    assert [s["version"] for s in release.notes(CHANGELOG, "1.0.0", "9.0.0")] == ["1.3.0", "1.2.0", "1.1.0"]
    assert release.notes(CHANGELOG, "1.3.0", "1.3.0") == []


# ---------------------------------------------------------------- the check
def test_check_finds_a_new_version_and_tells_the_owners_once(client, admin, env, github, monkeypatch):
    _ready(env)
    Fake.reset()
    d = client.post("/api/admin/updates/check", headers=admin).json()
    assert d["available"] and d["latest"] == "1.2.0" and d["repo"] == "ivan/vpn-seller" and not d["error"]
    assert [s["version"] for s in d["notes"]["ru"]] == ["1.2.0"] and "New payments" in d["notes"]["en"][0]["items"]
    url, headers = github["calls"][0]
    assert url == "https://api.github.com/repos/ivan/vpn-seller/contents/VERSION?ref=main"
    assert "Authorization" not in headers and headers["Accept"] == "application/vnd.github.raw"
    msgs = _owner_msgs()
    assert len(msgs) == 1
    m = msgs[0]
    assert "1.2.0" in m["text"] and "Новая оплата" in m["text"] and "копия базы" in m["text"]
    buttons = [b for row in m["reply_markup"]["inline_keyboard"] for b in row]
    assert {"text": "⬆️ Обновить сейчас", "callback_data": "a:upd"} in buttons
    # the same version again: no second message
    client.post("/api/admin/updates/check", headers=admin)
    assert len(_owner_msgs()) == 1
    # a private repository: the token goes along
    from app import updates
    github["calls"].clear()
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    updates.fetch("1.1.0")
    assert github["calls"][0][1]["Authorization"] == "Bearer ghp_x"


def test_check_without_the_server_button_says_how(client, admin, env, github):
    Fake.reset()
    client.post("/api/admin/updates/check", headers=admin)
    m = _owner_msgs()[0]
    assert "vpn-seller update" in m["text"]
    assert not any(b.get("callback_data") == "a:upd" for row in (m.get("reply_markup") or {"inline_keyboard": []})
                   ["inline_keyboard"] for b in row)


def test_check_errors(client, admin, env, github, monkeypatch):
    Fake.reset()
    for code, err in ((404, "not_found"), (401, "bad_token"), (403, "rate"), (500, "net")):
        github["code"] = code
        d = client.post("/api/admin/updates/check", headers=admin).json()
        assert d["error"] == err and not d["available"], code
    github["code"] = 200
    github["files"]["VERSION"] = "<html>not a version</html>"
    assert client.post("/api/admin/updates/check", headers=admin).json()["error"] == "bad_version"
    monkeypatch.setenv("UPDATE_REPO", "OWNER/vpn-seller")
    assert client.post("/api/admin/updates/check", headers=admin).json()["error"] == "no_repo"
    assert not _owner_msgs()
    # the same version as installed: nothing to do
    monkeypatch.setenv("UPDATE_REPO", "ivan/vpn-seller")
    from app import release
    github["files"]["VERSION"] = release.version()
    d = client.post("/api/admin/updates/check", headers=admin).json()
    assert d["latest"] == release.version() and not d["available"] and not d["error"]


# ---------------------------------------------------------------- the request
def test_update_request_for_the_server(client, admin, env, github):
    client.post("/api/admin/updates/check", headers=admin)
    r = client.post("/api/admin/updates/run", headers=admin)
    assert r.status_code == 409 and r.json()["detail"] == "no_updater"
    _ready(env)
    d = client.post("/api/admin/updates/run", headers=admin).json()
    req = json.loads((env.data_dir / "update.request").read_text())
    assert req["to"] == "1.2.0" and req["by"] and d["requested_at"] > 0
    # while the server is updating, a second press is refused
    (env.data_dir / "update.request").unlink()
    _status(env, state="running", **{"from": "1.1.0", "to": "1.2.0"})
    assert client.post("/api/admin/updates/run", headers=admin).json()["detail"] == "running"
    v = client.get("/api/admin/updates", headers=admin).json()
    assert v["status"]["state"] == "running" and v["updater"] is True
    # a status file that is not ours is ignored
    (env.data_dir / "update.status").write_text("garbage")
    assert client.get("/api/admin/updates", headers=admin).json()["status"] is None


def test_updates_are_the_owners_only(client, admin):
    r = client.post("/api/admin/team", headers=admin, json={"tg_id": 7600002, "name": "Оля",
                                                            "sections": ["stats", "sales", "people", "support", "system"]})
    assert r.status_code == 200
    tok = client.post("/api/admin/tg-login", headers={"X-Init-Data": init_data(7600002)}).json()["token"]
    h = {"Authorization": "Bearer " + tok}
    assert client.get("/api/admin/updates", headers=h).status_code == 403
    assert client.post("/api/admin/updates/run", headers=h).status_code == 403
    assert client.post("/api/admin/updates/auto", headers=h, json={"auto": True}).status_code == 403
    # and not through the settings either
    client.post("/api/admin/settings", headers=h, json={"auto_update": "1"})
    assert client.get("/api/admin/updates", headers=admin).json()["auto"] is False


def test_bot_button_asks_the_server(client, env, github):
    from app import updates
    from test_bot_logic import _press
    Fake.reset()
    _press(client, 9501, "a:upd")
    ans = Fake.sent("answerCallbackQuery")[-1]
    assert "vpn-seller update" in ans["text"] and not (env.data_dir / "update.request").exists()
    _ready(env)
    _press(client, 9502, "a:upd")
    assert "запущено" in Fake.sent("answerCallbackQuery")[-1]["text"]
    assert (env.data_dir / "update.request").exists() and updates.requested_at() > 0
    # someone else's press does nothing
    (env.data_dir / "update.request").unlink()
    _press(client, 9503, "a:upd", admin_id=4242)
    assert not (env.data_dir / "update.request").exists()


# ---------------------------------------------------------------- night and the end of an update
def test_night_auto_update_once_per_version(client, admin, env, github, db):
    import datetime as dt
    from app import updates
    _ready(env)
    client.post("/api/admin/updates/check", headers=admin)
    hour = dt.datetime.now().hour
    d = client.post("/api/admin/updates/auto", headers=admin, json={"auto": True, "hour": (hour + 1) % 24}).json()
    assert d["auto"] and d["hour"] == (hour + 1) % 24
    updates.tick(db)
    assert not (env.data_dir / "update.request").exists()          # not the hour yet
    client.post("/api/admin/updates/auto", headers=admin, json={"auto": True, "hour": hour})
    updates.tick(db)
    assert json.loads((env.data_dir / "update.request").read_text())["by"] == "auto"
    # it failed: no second try for the same version
    (env.data_dir / "update.request").unlink()
    updates.tick(db)
    assert not (env.data_dir / "update.request").exists()
    # off: nothing
    client.post("/api/admin/updates/auto", headers=admin, json={"auto": False, "hour": hour})
    assert client.get("/api/admin/updates", headers=admin).json()["auto"] is False


def test_owners_hear_how_it_ended_once(client, admin, env, db):
    from app import release, updates
    cur = release.version()
    Fake.reset()
    _status(env, state="ok", **{"from": "1.0.0", "to": cur})
    updates.announce(db)
    updates.announce(db)
    msgs = _owner_msgs()
    assert len(msgs) == 1 and f"обновлён до {cur}" in msgs[0]["text"]
    assert "Обновления из GitHub" in msgs[0]["text"]               # what this version brought, from CHANGELOG.md
    v = client.get("/api/admin/updates", headers=admin).json()
    assert v["fresh"] and v["fresh"][0]["version"] == cur            # the panel shows it too
    # a failed one: the reason, and that the shop is back
    _status(env, state="failed", reason="новая версия не отвечает", **{"from": cur, "to": "9.9.9"})
    updates.announce(db)
    m = _owner_msgs()[-1]
    assert "9.9.9" in m["text"] and "не отвечает" in m["text"] and cur in m["text"]
    assert len(_owner_msgs()) == 2
    # the same version rebuilt (vpn-seller update with nothing new): no message
    _status(env, state="ok", **{"from": cur, "to": cur})
    updates.announce(db)
    assert len(_owner_msgs()) == 2
    # a running one is not announced
    _status(env, state="running", **{"from": cur, "to": "9.9.10"})
    updates.announce(db)
    assert len(_owner_msgs()) == 2


def test_panel_has_the_updates_page(client):
    panel = client.get("/admin").text
    for part in ('["updates", "i-refresh"]', "async function pUpdates(", "function updPoll(", "updates:pUpdates",
                 'updates:"Обновления"', 'updates:"Updates"', "dashUpdate()", 'id="dashUpd"',
                 "updErr_no_repo:", "updErr_not_found:"):
        assert part in panel, part
