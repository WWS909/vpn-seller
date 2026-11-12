"""Database engine and session. SQLite by default, PostgreSQL via DATABASE_URL."""

from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

engine = None
SessionLocal = sessionmaker(autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_engine(url: str | None = None):
    """(Re)create the engine. Called on start and by tests with a temp file."""
    global engine
    url = url or settings.database_url
    # Requests run in up to 40 worker threads; the pool should not be the
    # narrow place (panel calls give their connection back while waiting).
    kwargs: dict = {"pool_pre_ping": True, "pool_size": 10, "max_overflow": 30, "pool_timeout": 30}
    if url.startswith("sqlite"):
        if ":///./" in url:
            settings.data_dir.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _pragmas(conn, _):          # noqa: ANN001
            cur = conn.cursor()
            # WAL lets the scheduler write while requests read.
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    SessionLocal.configure(bind=engine)
    return engine


def wait_ready(seconds: float = 90) -> None:
    """PostgreSQL starts next to the shop and may answer a little later:
    wait for it instead of failing the start. SQLite is always ready."""
    import time

    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    if engine.dialect.name == "sqlite":
        return
    end = time.monotonic() + seconds
    while True:
        try:
            with engine.connect() as c:
                c.execute(text("SELECT 1"))
            return
        except OperationalError:
            if time.monotonic() >= end:
                raise
            time.sleep(2)


def create_all() -> None:
    from . import models  # noqa: F401  — registers tables
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
