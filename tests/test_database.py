"""
The database: the copy from SQLite to PostgreSQL (app/dbmove.py) and the
panel's page that asks the server for the move (app/dbswitch.py).

The copy runs against a real PostgreSQL when TEST_MOVE_URL points to an
empty database (it is wiped):

    TEST_MOVE_URL=postgresql+psycopg://user:pass@host/db python -m pytest tests/test_database.py

Without it the target is a second SQLite file: the counting, the batches and
the refusals are the same, only the PostgreSQL-specific fixes are skipped.
"""
from __future__ import annotations

import json
import os
import time

import pytest
from sqlalchemy import create_engine, func, select, text

from conftest import TEST_DB, Fake, init_data

MOVE_URL = os.getenv("TEST_MOVE_URL", "")


def _target(tmp_path) -> str:
    if MOVE_URL:
        from app import models  # noqa: F401
        from app.db import Base
        eng = create_engine(MOVE_URL)
        Base.metadata.drop_all(eng)
        eng.dispose()
        return MOVE_URL
    return f"sqlite:///{tmp_path}/target.db"


def _fill(db, n: int = 2500) -> None:
    """A shop with more customers than one batch, and the data SQLite
    lets through but PostgreSQL would not."""
    from app.models import Transaction, User
    t = int(time.time())
    db.add_all([User(tg_id=8_000_000_000 + i, first_name=f"Клиент {i}", expires_at=t + 86400 * (i % 3),
                     trial_used=bool(i % 2)) for i in range(n)])
    db.commit()
    first = db.query(User).order_by(User.id).first()
    # SQLite keeps a string longer than its column and a number beyond 32 bits
    db.execute(text("UPDATE users SET first_name = :v, sync_error = :e, reminded_for = :big WHERE id = :id"),
               {"v": "Ж" * 300, "e": "a\x00b", "big": 5_000_000_000, "id": first.id})
    db.add(Transaction(user_id=first.id, amount=199.0, status="success", gateway="yookassa", external_id="x1"))
    db.commit()


@pytest.mark.skipif(bool(TEST_DB), reason="the source must be SQLite")
def test_copy_moves_everything_and_counts_it(client, db, env, tmp_path):
    from app import dbmove
    from app.models import User
    _fill(db)
    want = {"users": db.query(User).count()}
    dst = _target(tmp_path)
    res = dbmove.copy(env.database_url, dst)
    assert res["ok"] and res["users"] == want["users"] >= 2500
    assert res["tables"]["transactions"] >= 1 and res["tables"]["plans"] >= 1
    eng = create_engine(dst)
    with eng.connect() as c:
        u = c.execute(text("SELECT first_name, sync_error, reminded_for, trial_used FROM users "
                           "ORDER BY id LIMIT 1")).one()
        assert u.first_name == "Ж" * 300 and u.reminded_for == 5_000_000_000
        assert u.sync_error == "ab"                                   # the NUL is dropped
        assert c.execute(select(func.count()).select_from(text("users")).where(text("trial_used"))).scalar() > 0
    if MOVE_URL:
        assert any("users.first_name" in w for w in res["widened"])
        assert any("users.reminded_for" in w for w in res["widened"])
    # new rows get ids after the copied ones (the sequences moved along)
    with eng.begin() as c:
        c.execute(text("INSERT INTO users (tg_id, username, first_name, lang, sub_token, xui_uuid, xui_sub_id) "
                       "VALUES (1, '', 'new', '', 'tok-new', 'u', 's')"))
        assert c.execute(text("SELECT id FROM users WHERE tg_id = 1")).scalar() > res["users"]
    eng.dispose()
    # a database that already has customers is not overwritten by mistake
    with pytest.raises(dbmove.MoveError) as e:
        dbmove.copy(env.database_url, dst)
    assert e.value.code == "target_not_empty"
    # …only when asked to
    assert dbmove.copy(env.database_url, dst, force=True)["users"] == want["users"]


@pytest.mark.skipif(bool(TEST_DB), reason="the source must be SQLite")
def test_copy_refuses_bad_targets(client, env, tmp_path):
    from app import dbmove
    with pytest.raises(dbmove.MoveError) as e:
        dbmove.copy(env.database_url, env.database_url)
    assert e.value.code == "same_database"
    with pytest.raises(dbmove.MoveError) as e:
        dbmove.copy(env.database_url, "mysql://x@y/z")
    assert e.value.code == "bad_url"


@pytest.mark.skipif(bool(TEST_DB), reason="the source must be SQLite")
def test_copy_command_prints_one_json_line(client, env, tmp_path, capsys):
    from app import dbmove
    dst = f"sqlite:///{tmp_path}/cli.db"
    assert dbmove._main(["copy", "--to", dst]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["ok"] is True and "users" in out
    assert dbmove._main(["copy", "--to", env.database_url]) == 1
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out == {"ok": False, "error": "same_database", "detail": ""}
    assert dbmove._main(["kind"]) == 0 and capsys.readouterr().out.strip() == "sqlite"


# ---------------------------------------------------------------- the panel
def _ready(env):
    (env.data_dir / "updater.json").write_text('{"installed": 1}')


def _status(env, **kw):
    (env.data_dir / "db.status").write_text(json.dumps({"at": int(time.time()), "log": "", "reason": "", **kw}))


def test_page_shows_the_database(client, admin, env):
    d = client.get("/api/admin/database", headers=admin).json()
    assert d["kind"] == ("postgresql" if TEST_DB else "sqlite")
    assert d["users"] >= 0 and d["size"] >= 0 and "ram_mb" in d and d["recommend"] is False
    assert d["status"] is None and d["requested_at"] == 0 and d["updater"] is False


@pytest.mark.skipif(bool(TEST_DB), reason="asks from SQLite")
def test_move_request_for_the_server(client, admin, env):
    r = client.post("/api/admin/database/postgres", headers=admin)
    assert r.status_code == 409 and r.json()["detail"] == "no_updater"
    _ready(env)
    d = client.post("/api/admin/database/postgres", headers=admin).json()
    req = json.loads((env.data_dir / "update.request").read_text())
    assert req["action"] == "postgres" and req["by"] and d["requested_at"] > 0
    # the update page does not take it for a waiting update
    assert client.get("/api/admin/updates", headers=admin).json()["requested_at"] == 0
    # pressed twice, or while an update waits — refused
    assert client.post("/api/admin/database/postgres", headers=admin).json()["detail"] == "running"
    assert client.post("/api/admin/updates/run", headers=admin).json()["detail"] == "running"
    (env.data_dir / "update.request").unlink()
    _status(env, state="running")
    assert client.post("/api/admin/database/postgres", headers=admin).json()["detail"] == "running"
    v = client.get("/api/admin/database", headers=admin).json()
    assert v["status"]["state"] == "running"
    (env.data_dir / "db.status").write_text("garbage")
    assert client.get("/api/admin/database", headers=admin).json()["status"] is None


def test_already_on_postgres_is_refused(client, admin, env, monkeypatch):
    from app import dbswitch
    monkeypatch.setattr(dbswitch, "kind", lambda: "postgresql")
    _ready(env)
    r = client.post("/api/admin/database/postgres", headers=admin)
    assert r.status_code == 409 and r.json()["detail"] == "already"
    assert not (env.data_dir / "update.request").exists()


def test_recommended_when_the_shop_grows(client, admin, db, env, monkeypatch):
    from app import dbswitch
    from app.models import User
    monkeypatch.setattr(dbswitch, "RECOMMEND_ACTIVE", 3)
    t = int(time.time())
    db.add_all([User(tg_id=7_700_000 + i, expires_at=t + 86400) for i in range(3)])
    db.commit()
    assert client.get("/api/admin/database", headers=admin).json()["recommend"] is (not TEST_DB)


def test_database_is_the_owners_only(client, admin):
    r = client.post("/api/admin/team", headers=admin, json={"tg_id": 7600003, "name": "Оля",
                                                            "sections": ["stats", "sales", "people", "support", "system"]})
    assert r.status_code == 200
    tok = client.post("/api/admin/tg-login", headers={"X-Init-Data": init_data(7600003)}).json()["token"]
    h = {"Authorization": "Bearer " + tok}
    assert client.get("/api/admin/database", headers=h).status_code == 403
    assert client.post("/api/admin/database/postgres", headers=h).status_code == 403


def test_owners_told_once_how_it_ended(client, db, env, monkeypatch):
    from app import dbswitch
    _status(env, state="failed", reason="PostgreSQL не отвечает")
    dbswitch.announce(db)
    dbswitch.announce(db)
    msgs = [b for b in Fake.sent("sendMessage") if b.get("chat_id") == 999]
    assert len(msgs) == 1 and "остался на SQLite" in msgs[0]["text"] and "не отвечает" in msgs[0]["text"]
    monkeypatch.setattr(dbswitch, "kind", lambda: "postgresql")
    _status(env, state="ok", users=42)
    dbswitch.announce(db)
    msgs = [b for b in Fake.sent("sendMessage") if b.get("chat_id") == 999]
    assert len(msgs) == 2 and "перешёл на PostgreSQL" in msgs[1]["text"] and "42" in msgs[1]["text"]
    assert "admin#database" in json.dumps(msgs[1]["reply_markup"])


def test_shop_waits_for_postgres_to_start(monkeypatch):
    from sqlalchemy.exc import OperationalError

    from app import db as dbmod

    class Eng:
        class dialect:
            name = "postgresql"
        tries = 0

        def connect(self):
            Eng.tries += 1
            if Eng.tries < 3:
                raise OperationalError("SELECT 1", {}, Exception("starting up"))
            raise _Done

    class _Done(Exception):
        pass

    monkeypatch.setattr(dbmod, "engine", Eng())
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with pytest.raises(_Done):
        dbmod.wait_ready(30)
    assert Eng.tries == 3
