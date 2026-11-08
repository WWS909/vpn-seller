"""
Backups.

Once a day (and on demand from the panel or with /backup in the bot) the
whole database goes into a zip and is sent to the admins in Telegram —
if the server dies, the shop comes back from the last message.

The archive holds:
  db.json      every table as rows — portable, restores into SQLite or
               PostgreSQL alike
  vpn.db       a consistent copy of the SQLite file (only with SQLite)
  brand/       the uploaded logo
  meta.json    when, which version, how many customers

Restore on a fresh install (the service stopped):

    docker compose run --rm app python -m app.backup restore /app/data/backup-….zip

The archive contains everything the shop knows, panel passwords included:
it goes only to the admins from ADMIN_TELEGRAM_IDS.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import sqlite3
import sys
import zipfile
from pathlib import Path

import httpx
from sqlalchemy import delete, insert, select, text
from sqlalchemy.orm import Session

from . import core
from .config import settings as env

KEEP = 7                          # archives kept on the server
TELEGRAM_LIMIT = 49 * 1024 * 1024  # bots may send files up to 50 MB


def backup_dir() -> Path:
    d = env.data_dir / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _tables():
    from . import models  # noqa: F401 — registers tables
    from .db import Base
    return Base.metadata.sorted_tables


def dump(db: Session) -> dict:
    out = {}
    for table in _tables():
        rows = db.execute(select(table)).mappings().all()
        out[table.name] = [dict(r) for r in rows]
    return out


def make(db: Session) -> Path:
    """Write a new archive and return its path. Old ones beyond KEEP go."""
    from .main import VERSION
    from .models import User, now
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = backup_dir() / f"backup-{stamp}.zip"
    tables = dump(db)
    meta = {"created_at": now(), "version": VERSION, "brand": core.get(db, "brand_name"),
            "users": db.query(User).count(),
            "active": db.query(User).filter(User.expires_at > now()).count(),
            "database": "sqlite" if env.database_url.startswith("sqlite") else "postgresql"}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=1))
        z.writestr("db.json", json.dumps({"tables": tables}, ensure_ascii=False, default=str))
        sqlite_file = _sqlite_path()
        if sqlite_file and sqlite_file.is_file():
            z.writestr("vpn.db", _sqlite_copy(sqlite_file))
        brand = env.data_dir / "brand"
        if brand.is_dir():
            for f in brand.iterdir():
                if f.is_file():
                    z.write(f, f"brand/{f.name}")
    for old in sorted(backup_dir().glob("backup-*.zip"))[:-KEEP]:
        old.unlink(missing_ok=True)
    core.put(db, "backup_last", str(meta["created_at"]))
    core.audit(db, "Backup made", "system", f"{path.name}, {path.stat().st_size // 1024} KB")
    return path


def _sqlite_path() -> Path | None:
    url = env.database_url
    if not url.startswith("sqlite:///"):
        return None
    return Path(url[len("sqlite:///"):])


def _sqlite_copy(src: Path) -> bytes:
    """A consistent copy even while the service writes (the backup API of
    SQLite, not a file copy that could catch half a transaction)."""
    tmp = backup_dir() / ".copy.db"
    tmp.unlink(missing_ok=True)
    with sqlite3.connect(str(src)) as a, sqlite3.connect(str(tmp)) as b:
        a.backup(b)
    data = tmp.read_bytes()
    tmp.unlink(missing_ok=True)
    return data


def listing() -> list[dict]:
    return [{"name": p.name, "size": p.stat().st_size, "at": int(p.stat().st_mtime)}
            for p in sorted(backup_dir().glob("backup-*.zip"), reverse=True)]


def file_of(name: str) -> Path | None:
    p = backup_dir() / name
    return p if name.startswith("backup-") and name.endswith(".zip") and "/" not in name \
        and p.is_file() else None


# ---------------------------------------------------------------------------
#  To Telegram
# ---------------------------------------------------------------------------
def send(db: Session, path: Path, chat_ids: list[int] | None = None) -> int:
    """Send the archive to the admins. Returns how many got it."""
    from . import notify
    with zipfile.ZipFile(path) as z:
        meta = json.loads(z.read("meta.json"))
    lang = env.default_lang
    caption = core.t(lang, "backup_caption", brand=meta.get("brand", ""), users=meta.get("users", 0),
                     active=meta.get("active", 0),
                     date=dt.datetime.fromtimestamp(meta["created_at"]).strftime("%d.%m.%Y %H:%M"))
    targets = chat_ids if chat_ids is not None else sorted(env.admin_telegram_ids)
    size = path.stat().st_size
    sent = 0
    for chat in targets:
        if size > TELEGRAM_LIMIT:
            try:
                notify.send(chat, core.t(lang, "backup_too_big", name=path.name,
                                         mb=round(size / 1024 / 1024)))
            except notify.Blocked:
                pass
            continue
        try:
            r = httpx.post(f"{notify.API_BASE}/bot{env.bot_token}/sendDocument", timeout=120,
                           data={"chat_id": str(chat), "caption": caption},
                           files={"document": (path.name, path.read_bytes(), "application/zip")})
            sent += 1 if r.json().get("ok") else 0
        except (httpx.HTTPError, ValueError):
            continue
    return sent


def run_daily(db: Session) -> bool:
    """Called by the scheduler every few minutes; makes and sends the
    archive once a day at the chosen hour."""
    if core.get(db, "backup_enabled") != "1":
        return False
    hour = core.get_int(db, "backup_hour", 4)
    now_local = dt.datetime.now()
    last = core.get_int(db, "backup_last", 0)
    if now_local.hour != hour:
        return False
    if last and dt.datetime.fromtimestamp(last).date() == now_local.date():
        return False
    path = make(db)
    send(db, path)
    return True


# ---------------------------------------------------------------------------
#  Restore
# ---------------------------------------------------------------------------
def restore(archive: str, force: bool = False) -> dict:
    """Load an archive into the database of this install (DATABASE_URL).
    Refuses to overwrite a database that already has customers unless
    force is given."""
    from . import db as dbmod
    from . import migrate
    from .models import User
    dbmod.init_engine()
    dbmod.create_all()
    migrate.run(dbmod.engine)
    with zipfile.ZipFile(archive) as z:
        data = json.loads(z.read("db.json"))["tables"]
        brand = [n for n in z.namelist() if n.startswith("brand/") and not n.endswith("/")]
        files = {n: z.read(n) for n in brand}
    session = dbmod.SessionLocal()
    try:
        if session.query(User).count() and not force:
            raise SystemExit("this database already has customers; add --force to replace them")
        tables = _tables()
        with dbmod.engine.begin() as conn:
            for table in reversed(tables):
                conn.execute(delete(table))
            counts = {}
            for table in tables:
                rows = data.get(table.name) or []
                cols = {c.name for c in table.columns}
                rows = [{k: v for k, v in r.items() if k in cols} for r in rows]
                if rows:
                    conn.execute(insert(table), rows)
                counts[table.name] = len(rows)
            if dbmod.engine.dialect.name == "postgresql":
                for table in tables:
                    pk = [c for c in table.primary_key.columns if c.autoincrement is not False
                          and str(c.type).upper().startswith(("INTEGER", "BIGINT"))]
                    if len(pk) == 1:
                        conn.execute(text(
                            f"SELECT setval(pg_get_serial_sequence('{table.name}', '{pk[0].name}'), "
                            f"COALESCE((SELECT MAX({pk[0].name}) FROM \"{table.name}\"), 0) + 1, false)"))
        from . import nalog
        held = nalog.recover_after_restore(session)
        if held:
            print(f"receipts to check by hand in «Мой налог»: {held}")
    finally:
        session.close()
    brand_dir = env.data_dir / "brand"
    brand_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (brand_dir / Path(name).name).write_bytes(content)
    return counts


def _main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "restore":
        counts = restore(argv[1], force="--force" in argv)
        print("restored:", ", ".join(f"{k} {v}" for k, v in counts.items() if v))
        return 0
    if argv and argv[0] == "make":
        from . import db as dbmod
        dbmod.init_engine()
        s = dbmod.SessionLocal()
        try:
            print(make(s))
        finally:
            s.close()
        return 0
    print("usage: python -m app.backup make | restore <archive.zip> [--force]")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))


__all__ = ["make", "send", "listing", "file_of", "run_daily", "restore", "io"]
