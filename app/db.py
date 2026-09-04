"""SQLAlchemy engine and session plumbing."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import config

log = logging.getLogger(__name__)


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
#: design; these two lists are the whole of it.
_RETROFIT_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_open_draw_session "
    "ON draw_sessions (user_id) WHERE resolved_at IS NULL",
)

#: (table, column, column definition). SQLite has no ADD COLUMN IF NOT EXISTS,
#: so each is applied only when the column is genuinely absent. Additive and
#: non-destructive by construction: new columns take a default, nothing is
#: dropped or retyped. Anything beyond that needs a real migration tool.
_RETROFIT_COLUMNS = (
    ("submissions", "chosen", "BOOLEAN NOT NULL DEFAULT 0"),
    ("draw_sessions", "chosen", "BOOLEAN NOT NULL DEFAULT 0"),
    ("settings", "free_choice_quota", "INTEGER NOT NULL DEFAULT 5"),
)


def _existing_columns(conn, table: str) -> set[str]:
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _apply_retrofits(conn) -> None:
    for statement in _RETROFIT_DDL:
        conn.exec_driver_sql(statement)

    for table, column, definition in _RETROFIT_COLUMNS:
        present = _existing_columns(conn, table)
        if not present:
            continue  # table does not exist yet; create_all will build it correctly
        if column not in present:
            log.info("adding column %s.%s", table, column)
            conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    from app import models  # noqa: F401  (registers mappers)

    config.ensure_dirs()
    engine = get_engine()
    Base.metadata.create_all(engine)

    if engine.dialect.name == "sqlite":
        with engine.begin() as conn:
            _apply_retrofits(conn)
