"""
New versions from GitHub.

Every few hours the shop asks GitHub for the VERSION file of its repository.
When a newer one is out, the owners get one message with what is new and an
"Update" button, and the panel shows the same.

The update itself runs on the server, outside the container: the panel (or
the bot button) leaves a request file in data/; a systemd unit that
install.sh sets up notices it and runs `vpn-seller update --from-panel`.
That makes a backup, fetches the new code, rebuilds, checks that the shop
answers and, if it does not, puts the previous code back. Its progress goes
to data/update.status, which the panel shows and from which the owners are
told how it went (with what is new).
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os

import httpx
from sqlalchemy.orm import Session

from . import core, notify, release
from .config import settings as env
from .models import now

STATUS = "update.status"        # written by install.sh: {"state", "from", "to", "at", "reason", "log"}
REQUEST = "update.request"      # written here: the server picks it up
READY = "updater.json"          # written by install.sh when the systemd unit is in place
CHECK_EVERY = 6 * 3600
API = "https://api.github.com"
STATES = ("running", "ok", "failed")
E = html.escape


class UpdateError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _file(name: str):
    return env.data_dir / name


# ---------------------------------------------------------------- GitHub
def _http_get(url: str, headers: dict) -> httpx.Response:
    return httpx.get(url, headers=headers, timeout=15, follow_redirects=True)


def _raw(path: str) -> str:
    slug = release.repo()
    if not slug:
        raise UpdateError("no_repo")
    headers = {"Accept": "application/vnd.github.raw", "User-Agent": "vpn-seller",
               "X-GitHub-Api-Version": "2022-11-28"}
    if release.token():
        headers["Authorization"] = "Bearer " + release.token()
    try:
        r = _http_get(f"{API}/repos/{slug}/contents/{path}?ref={release.branch()}", headers)
    except httpx.HTTPError as e:
        raise UpdateError("net") from e
    if r.status_code == 404:
        raise UpdateError("not_found")
    if r.status_code == 401:
        raise UpdateError("bad_token")
    if r.status_code in (403, 429):
        raise UpdateError("rate")
    if r.status_code != 200:
        raise UpdateError("net")
    return r.text[:200_000]


def fetch(current: str) -> dict:
    """The latest version on GitHub and, if it is newer, what changed."""
    latest = _raw("VERSION").strip()
    if not release.SEMVER.match(latest):
        raise UpdateError("bad_version")
    out = {"latest": latest, "notes_ru": [], "notes_en": []}
    if release.newer(latest, current):
        for lang, name in (("ru", "CHANGELOG.md"), ("en", "CHANGELOG.en.md")):
            try:
                out["notes_" + lang] = release.notes(_raw(name), current, latest)
            except UpdateError:
                pass                     # no notes is not a reason to hide the update
    return out


# ---------------------------------------------------------------- state
def info(db: Session) -> dict:
    try:
        d = json.loads(core.get(db, "update_info") or "{}")
    except ValueError:
        d = {}
    return d if isinstance(d, dict) else {}


def read_status() -> dict | None:
    try:
        d = json.loads(_file(STATUS).read_text(encoding="utf-8")[:20_000])
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("state") not in STATES:
        return None
    return {"state": d["state"], "from": str(d.get("from") or "")[:20], "to": str(d.get("to") or "")[:20],
            "at": int(d.get("at") or 0) if str(d.get("at") or "0").isdigit() else 0,
            "reason": str(d.get("reason") or "")[:300], "log": str(d.get("log") or "")[-3000:]}


def updater_ready() -> bool:
    return _file(READY).is_file()


def requested_at() -> int:
    try:
        return int(_file(REQUEST).stat().st_mtime)
    except OSError:
        return 0


def request_action() -> str:
    """What the waiting request asks for: "" — an update, "postgres" — the
    database move (app/dbswitch.py); both go through the same server unit."""
    try:
        d = json.loads(_file(REQUEST).read_text(encoding="utf-8")[:2000])
    except (OSError, ValueError):
        return ""
    return str(d.get("action") or "") if isinstance(d, dict) else ""


def running(st: dict | None = None) -> bool:
    st = read_status() if st is None else st
    return bool(st) and st["state"] == "running" and now() - st["at"] < 1800


def view(db: Session) -> dict:
    cur = release.version()
    d = info(db)
    latest = d.get("latest") or ""
    st = read_status()
    return {
        "current": cur, "latest": latest, "available": release.newer(latest, cur),
        "checked_at": int(d.get("checked_at") or 0), "error": d.get("error") or "",
        "repo": release.repo(), "branch": release.branch(), "private": bool(release.token()),
        "notes": {"ru": d.get("notes_ru") or [], "en": d.get("notes_en") or []},
        "auto": core.get(db, "auto_update") == "1",
        "hour": core.get_int(db, "auto_update_hour", 1) % 24,
        "server_time": dt.datetime.now().strftime("%H:%M"),
        "updater": updater_ready(), "status": st,
        "requested_at": requested_at() if not request_action() else 0,
        # just updated: what this version brought, shown once in the panel
        "fresh": release.notes(release.local_notes("ru"), st["from"], cur) if st and st["state"] == "ok"
        and st["to"] == cur and now() - st["at"] < 3 * 86400 else [],
        "fresh_en": release.notes(release.local_notes("en"), st["from"], cur) if st and st["state"] == "ok"
        and st["to"] == cur and now() - st["at"] < 3 * 86400 else [],
    }


def check(db: Session, tell: bool = True) -> dict:
    cur = release.version()
    d = info(db)
    d.update(checked_at=now(), current=cur)
    try:
        d.update(fetch(cur), error="")
    except UpdateError as e:
        d["error"] = e.code
    core.put(db, "update_info", json.dumps(d, ensure_ascii=False))
    latest = d.get("latest") or ""
    if tell and not d["error"] and release.newer(latest, cur) and core.get(db, "update_notified") != latest:
        core.put(db, "update_notified", latest)
        core.audit(db, "New version", "system", f"{cur} → {latest}")
        tell_new(db, cur, latest, d.get("notes_" + env.default_lang) or d.get("notes_ru") or [])
    return view(db)


def request(db: Session, who: str) -> dict:
    """Ask the server to update. Raises ValueError with a short reason."""
    if not updater_ready():
        raise ValueError("no_updater")
    from . import dbswitch
    if running() or dbswitch.running() or (requested_at() and request_action()):
        raise ValueError("running")
    v = view(db)
    payload = {"at": now(), "by": str(who)[:64], "from": v["current"], "to": v["latest"] or v["current"]}
    tmp = _file(REQUEST + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, _file(REQUEST))
    core.audit(db, "Update requested", who, f"{payload['from']} → {payload['to']}", level="warn")
    return view(db)


# ---------------------------------------------------------------- messages
def _items(sections: list[dict], cap: int = 12) -> str:
    lines, n = [], 0
    for s in sections:
        for it in s.get("items") or []:
            if n >= cap:
                lines.append("…")
                return "\n".join(lines)
            lines.append("• " + E(it))
            n += 1
    return "\n".join(lines)


def _owners(text: str, rows: list | None = None) -> None:
    payload_base = {"text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if rows:
        payload_base["reply_markup"] = {"inline_keyboard": rows}
    for tg_id in env.admin_telegram_ids:
        try:
            notify.call("sendMessage", {"chat_id": tg_id, **payload_base})
        except Exception:  # noqa: BLE001 — a message must never break the check
            pass


def _panel_button(lang: str) -> list:
    if not env.public_url.startswith("https://"):
        return []
    return [{"text": core.t(lang, "upd_panel"), "web_app": {"url": f"{env.public_url}/admin#updates"}}]


def tell_new(db: Session, cur: str, latest: str, sections: list[dict]) -> None:
    lang = env.default_lang
    text = core.t(lang, "upd_new", latest=E(latest), cur=E(cur))
    items = _items(sections)
    if items:
        text += "\n\n" + core.t(lang, "upd_whats_new") + "\n" + items
    text += "\n\n" + core.t(lang, "upd_safe")
    row = []
    if updater_ready():
        row.append({"text": core.t(lang, "upd_button"), "callback_data": "a:upd"})
    else:
        text += "\n" + core.t(lang, "upd_by_hand")
    row += _panel_button(lang)
    _owners(text, [row] if row else None)


def announce(db: Session) -> None:
    """Tell the owners once how an update ended (it was run outside the shop)."""
    st = read_status()
    if not st or st["state"] not in ("ok", "failed"):
        return
    key = f"{st['state']}:{st['to']}:{st['at']}"
    if core.get(db, "update_announced") == key:
        return
    core.put(db, "update_announced", key)
    lang = env.default_lang
    if st["state"] == "ok":
        cur = release.version()
        if st["from"] == cur:            # rebuilt the same version: nothing new to tell
            return
        text = core.t(lang, "upd_done", to=E(cur))
        items = _items(release.notes(release.local_notes(lang), st["from"], cur))
        if items:
            text += "\n\n" + core.t(lang, "upd_whats_new") + "\n" + items
        core.audit(db, "Updated", "system", f"{st['from']} → {cur}")
    else:
        text = core.t(lang, "upd_failed", to=E(st["to"] or "?"), back=E(st["from"] or release.version()),
                      reason=E(st["reason"] or "—"))
        core.audit(db, "Update failed", "system", f"{st['from']} → {st['to']}: {st['reason']}"[:250],
                   level="error")
    row = _panel_button(lang)
    _owners(text, [row] if row else None)


def tick(db: Session) -> None:
    """The job: check every few hours, tell how an update ended, and update
    by itself at night when the owner allowed it."""
    announce(db)
    from . import dbswitch
    dbswitch.announce(db)
    d = info(db)
    if now() - int(d.get("checked_at") or 0) >= CHECK_EVERY:
        check(db)
        d = info(db)
    if core.get(db, "auto_update") != "1" or not updater_ready() or requested_at() or running() \
            or dbswitch.running():
        return
    latest = d.get("latest") or ""
    if not release.newer(latest, release.version()) or core.get(db, "auto_update_tried") == latest:
        return
    if dt.datetime.now().hour != core.get_int(db, "auto_update_hour", 1) % 24:
        return
    core.put(db, "auto_update_tried", latest)     # one try per version: a failed one waits for the owner
    try:
        request(db, "auto")
    except ValueError:
        pass

