"""SQLAlchemy engine and session plumbing."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import config


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    url = url or config.DATABASE_URL
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    kwargs: dict = {}

    if url.startswith("sqlite") and ":memory:" in url:
        # An in-memory SQLite database belongs to its *connection*, and the
        # default pool hands out a connection per thread -- so the app thread
        # would otherwise see a different, empty database from the one the test
        # populated. StaticPool keeps everyone on one connection.
        kwargs["poolclass"] = StaticPool

    engine = create_engine(url, connect_args=connect_args, future=True, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver glue
            cur = dbapi_conn.cursor()
            # Foreign keys are off by default in SQLite; the counting rules in
            # scope 6 depend on submission -> pokemon integrity holding.
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    return engine


_engine = None
_SessionFactory = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


#: Indexes added after a table already existed somewhere. `create_all` skips
#: existing tables entirely, so a plain declarative Index would never reach a
#: database created by an earlier slice. There is no migration framework here by
#: design; this list is the whole of it.
_RETROFIT_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_open_draw_session "
    "ON draw_sessions (user_id) WHERE resolved_at IS NULL",
)


def init_db() -> None:
    from app import models  # noqa: F401  (registers mappers)

    config.ensure_dirs()
    engine = get_engine()
    Base.metadata.create_all(engine)

    if engine.dialect.name == "sqlite":
        with engine.begin() as conn:
            for statement in _RETROFIT_DDL:
                conn.exec_driver_sql(statement)
