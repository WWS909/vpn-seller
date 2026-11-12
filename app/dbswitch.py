"""
The database page of the panel: which database the shop runs on, and the
move from SQLite to PostgreSQL at any time.

The move itself runs on the server, outside the container, the same way as
an update: the panel leaves data/update.request with "action": "postgres";
the systemd unit that install.sh set up for the Update button runs
install.sh, which sees the action and moves the database instead of
updating (install.sh → cmd_db_postgres). It writes its progress to
data/db.status, which this page follows and from which the owners are told
how it went.

Nothing can be lost on the way: a backup comes first, the SQLite file is
only read, the copy is counted table by table, and on any failure the shop
starts again on SQLite.
"""

from __future__ import annotations

import json
import os
import shutil

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import core, updates
from .config import settings as env
from .models import User, now

STATUS = "db.status"              # written by install.sh: {"state", "at", "reason", "users", "log"}
STATES = ("running", "ok", "failed")
ACTION = "postgres"
RECOMMEND_ACTIVE = 400            # paying customers from which PostgreSQL is worth it
RECOMMEND_USERS = 2000
RAM_COMFORT_MB = 1900             # below this PostgreSQL still works, but squeezes the shop


def kind() -> str:
    url = env.database_url
    return "sqlite" if url.startswith("sqlite") else "postgresql" if url.startswith("postgres") else "other"


def ram_mb() -> int:
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return 0


def _sqlite_file():
    url = env.database_url
    if not url.startswith("sqlite:///"):
        return None
    from pathlib import Path
    return Path(url[len("sqlite:///"):])


def size_bytes(db: Session) -> int:
    try:
        if kind() == "postgresql":
            return int(db.execute(text("SELECT pg_database_size(current_database())")).scalar() or 0)
        f = _sqlite_file()
        if f and f.is_file():
            total = f.stat().st_size
            wal = f.with_name(f.name + "-wal")
            return total + (wal.stat().st_size if wal.is_file() else 0)
    except Exception:  # noqa: BLE001 — a size is a nicety
        pass
    return 0


def read_status() -> dict | None:
    try:
        d = json.loads((env.data_dir / STATUS).read_text(encoding="utf-8")[:20_000])
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("state") not in STATES:
        return None
    at = str(d.get("at") or "0")
    users = str(d.get("users") or "0")
    return {"state": d["state"], "at": int(at) if at.isdigit() else 0,
            "users": int(users) if users.isdigit() else 0,
            "reason": str(d.get("reason") or "")[:300], "log": str(d.get("log") or "")[-3000:]}


def running(st: dict | None = None) -> bool:
    st = read_status() if st is None else st
    return bool(st) and st["state"] == "running" and now() - st["at"] < 1800


def requested() -> int:
    """When the panel asked for the move (0 — not asked or already taken)."""
    return updates.requested_at() if updates.request_action() == ACTION else 0


def view(db: Session) -> dict:
    users = db.query(User).count()
    active = db.query(User).filter(User.expires_at > now(), User.is_blocked.is_(False)).count()
    k = kind()
    try:
        free = shutil.disk_usage(env.data_dir).free
    except OSError:
        free = 0
    ram = ram_mb()
    return {
        "kind": k, "users": users, "active": active, "size": size_bytes(db),
        "ram_mb": ram, "ram_ok": ram == 0 or ram >= RAM_COMFORT_MB, "disk_free": free,
        "recommend": k == "sqlite" and (active >= RECOMMEND_ACTIVE or users >= RECOMMEND_USERS),
        "thresholds": {"active": RECOMMEND_ACTIVE, "users": RECOMMEND_USERS, "ram_mb": RAM_COMFORT_MB},
        "updater": updates.updater_ready(), "status": read_status(), "requested_at": requested(),
        "update_busy": updates.running() or (bool(updates.requested_at()) and updates.request_action() != ACTION),
    }


def request(db: Session, who: str) -> dict:
    """Ask the server to move to PostgreSQL. Raises ValueError with a short reason."""
    if kind() != "sqlite":
        raise ValueError("already")
    if not updates.updater_ready():
        raise ValueError("no_updater")
    if running() or updates.running() or updates.requested_at():
        raise ValueError("running")
    payload = {"action": ACTION, "at": now(), "by": str(who)[:64]}
    tmp = env.data_dir / (updates.REQUEST + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, env.data_dir / updates.REQUEST)
    core.audit(db, "Move to PostgreSQL requested", who, f"customers {db.query(User).count()}", level="warn")
    return view(db)


def announce(db: Session) -> None:
    """Tell the owners once how the move ended (it ran outside the shop)."""
    st = read_status()
    if not st or st["state"] not in ("ok", "failed"):
        return
    key = f"{st['state']}:{st['at']}"
    if core.get(db, "db_move_announced") == key:
        return
    core.put(db, "db_move_announced", key)
    lang = env.default_lang
    if st["state"] == "ok":
        if kind() != "postgresql":
            return
        text_ = core.t(lang, "db_done", users=st["users"])
        core.audit(db, "Moved to PostgreSQL", "system", f"customers {st['users']}")
    else:
        text_ = core.t(lang, "db_failed", reason=updates.E(st["reason"] or "—"))
        core.audit(db, "Move to PostgreSQL failed", "system", st["reason"][:250], level="error")
    row = []
    if env.public_url.startswith("https://"):
        row = [{"text": core.t(lang, "upd_panel"), "web_app": {"url": f"{env.public_url}/admin#database"}}]
    updates._owners(text_, [row] if row else None)
