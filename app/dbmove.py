"""
Moving the shop from SQLite to PostgreSQL.

install.sh runs this inside a one-off container while the shop is stopped:

    python -m app.dbmove copy --to postgresql+psycopg://…   (from DATABASE_URL)
    python -m app.dbmove ping                               is DATABASE_URL reachable
    python -m app.dbmove kind                               sqlite | postgresql

`copy` puts every row of every table into the new database and then counts
them on both sides: a table that came out different stops the move, and the
shop goes back to SQLite (the file is never touched). It prints one JSON
line: {"ok": true, "tables": {...}, "users": N} or {"ok": false, "error": …}.

SQLite is lax where PostgreSQL is not, so before copying:
  * a string longer than its declared length widens that column,
  * an integer beyond 32 bits makes an INTEGER column BIGINT,
  * a NUL character (PostgreSQL refuses it in text) is dropped.
"""

from __future__ import annotations

import json
import os
import sys
import time

from sqlalchemy import (BigInteger, Integer, MetaData, String, create_engine, delete, func, insert, select,
                        text)
from sqlalchemy.engine import Engine

INT32 = 2_147_483_647
BATCH = 1000


class MoveError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def kind(url: str) -> str:
    return "sqlite" if url.startswith("sqlite") else "postgresql" if url.startswith("postgres") else "other"


def _tables():
    from . import models  # noqa: F401 — registers tables
    from .db import Base
    return Base.metadata.sorted_tables


def _engine(url: str) -> Engine:
    args = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {"connect_timeout": 10}
    return create_engine(url, connect_args=args, pool_pre_ping=True)


def ping(url: str, wait: float = 0) -> bool:
    """True when the database answers; waits up to `wait` seconds for it."""
    end = time.monotonic() + wait
    eng = _engine(url)
    try:
        while True:
            try:
                with eng.connect() as c:
                    c.execute(text("SELECT 1"))
                return True
            except Exception:  # noqa: BLE001 — not up yet
                if time.monotonic() >= end:
                    return False
                time.sleep(2)
    finally:
        eng.dispose()


def _counts(eng: Engine) -> dict[str, int]:
    with eng.connect() as c:
        return {t.name: c.execute(select(func.count()).select_from(t)).scalar() or 0 for t in _tables()}


def _fit_target(src: Engine, dst: Engine) -> list[str]:
    """Widen target columns that the SQLite data would not fit into."""
    changed = []
    if dst.dialect.name != "postgresql":
        return changed
    with src.connect() as s, dst.begin() as d:
        for t in _tables():
            for col in t.columns:
                typ = col.type
                if isinstance(typ, String) and getattr(typ, "length", None):
                    longest = s.execute(select(func.max(func.length(col)))).scalar() or 0
                    if longest > typ.length:
                        d.execute(text(f'ALTER TABLE "{t.name}" ALTER COLUMN "{col.name}" TYPE VARCHAR({int(longest)})'))
                        changed.append(f"{t.name}.{col.name} → VARCHAR({longest})")
                elif isinstance(typ, Integer) and not isinstance(typ, BigInteger):
                    hi = s.execute(select(func.max(col))).scalar()
                    lo = s.execute(select(func.min(col))).scalar()
                    try:
                        big = (hi is not None and int(hi) > INT32) or (lo is not None and int(lo) < -INT32 - 1)
                    except (TypeError, ValueError):
                        big = False
                    if big:
                        d.execute(text(f'ALTER TABLE "{t.name}" ALTER COLUMN "{col.name}" TYPE BIGINT'))
                        changed.append(f"{t.name}.{col.name} → BIGINT")
    return changed


def _clean(row: dict, cols: set[str]) -> dict:
    out = {}
    for k, v in row.items():
        if k not in cols:
            continue
        if isinstance(v, str) and "\x00" in v:
            v = v.replace("\x00", "")
        out[k] = v
    return out


def copy(src_url: str, dst_url: str, force: bool = False) -> dict:
    """Copy every table from src to dst and check the counts match."""
    from . import migrate
    from .db import Base
    if kind(src_url) == "other" or kind(dst_url) == "other":
        raise MoveError("bad_url")
    if src_url == dst_url:
        raise MoveError("same_database")
    src, dst = _engine(src_url), _engine(dst_url)
    try:
        if not ping(dst_url, wait=float(os.getenv("DBMOVE_WAIT", "60"))):
            raise MoveError("target_unreachable")
        # the source is the live shop's database: bring its schema to this
        # version too, so both sides have the same columns
        Base.metadata.create_all(bind=src)
        migrate.run(src)
        Base.metadata.create_all(bind=dst)
        migrate.run(dst)
        before = _counts(dst)
        if not force and (before.get("users") or before.get("transactions")):
            raise MoveError("target_not_empty", f"users {before.get('users', 0)}")
        widened = _fit_target(src, dst)
        tables = _tables()
        # Rows go in through the target's own column types: a widened column
        # must not be cast back to the model's narrower type on the way.
        real = MetaData()
        real.reflect(bind=dst, only=[t.name for t in tables])
        with dst.begin() as d:
            for t in reversed(tables):
                d.execute(delete(real.tables[t.name]))
            with src.connect() as s:
                for t in tables:
                    target = real.tables[t.name]
                    cols = {c.name for c in target.columns}
                    batch = []
                    for row in s.execute(select(t)).mappings():
                        batch.append(_clean(dict(row), cols))
                        if len(batch) >= BATCH:
                            d.execute(insert(target), batch)
                            batch = []
                    if batch:
                        d.execute(insert(target), batch)
            if dst.dialect.name == "postgresql":
                # new rows must get ids after the copied ones
                for t in tables:
                    pk = [c for c in t.primary_key.columns if c.autoincrement is not False
                          and isinstance(c.type, Integer)]
                    if len(pk) == 1:
                        d.execute(text(
                            f"SELECT setval(pg_get_serial_sequence('\"{t.name}\"', '{pk[0].name}'), "
                            f"COALESCE((SELECT MAX(\"{pk[0].name}\") FROM \"{t.name}\"), 0) + 1, false)"))
        want, got = _counts(src), _counts(dst)
        diff = {n: (want[n], got.get(n)) for n in want if want[n] != got.get(n)}
        if diff:
            raise MoveError("count_mismatch", ", ".join(f"{n} {a}≠{b}" for n, (a, b) in diff.items()))
        return {"ok": True, "tables": {n: c for n, c in got.items() if c}, "users": got.get("users", 0),
                "widened": widened}
    finally:
        src.dispose()
        dst.dispose()


def _main(argv: list[str]) -> int:
    from .config import settings
    if argv[:1] == ["kind"]:
        print(kind(settings.database_url))
        return 0
    if argv[:1] == ["ping"]:
        ok = ping(settings.database_url, wait=float(argv[1]) if len(argv) > 1 else 0)
        print("ok" if ok else "no answer")
        return 0 if ok else 1
    if argv[:1] == ["copy"] and "--to" in argv:
        dst = argv[argv.index("--to") + 1]
        src = argv[argv.index("--from") + 1] if "--from" in argv else settings.database_url
        try:
            res = copy(src, dst, force="--force" in argv)
        except MoveError as e:
            print(json.dumps({"ok": False, "error": e.code, "detail": e.detail}, ensure_ascii=False))
            return 1
        except Exception as e:  # noqa: BLE001 — reported, the move is undone by install.sh
            print(json.dumps({"ok": False, "error": "failed", "detail": str(e)[:300]}, ensure_ascii=False))
            return 1
        print(json.dumps(res, ensure_ascii=False))
        return 0
    print("usage: python -m app.dbmove kind | ping [seconds] | copy --to URL [--from URL] [--force]")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
