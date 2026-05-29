from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

_SQLITE_BUSY_TIMEOUT_MS = 30_000


def _is_sqlite_url(db_url: str) -> bool:
    return db_url.startswith("sqlite")


def create_persistence_engine(db_url: str) -> Engine:
    connect_args: dict[str, Any] = {}
    if _is_sqlite_url(db_url):
        connect_args["timeout"] = _SQLITE_BUSY_TIMEOUT_MS / 1000
    engine = create_engine(db_url, echo=False, future=True, connect_args=connect_args)
    if _is_sqlite_url(db_url):
        _configure_sqlite_engine(engine)
    return engine


def _configure_sqlite_engine(engine: Engine) -> None:
    if getattr(engine, "_kola_sqlite_pragmas_installed", False):
        return

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: object, _connection_record: object) -> None:
        connection = cast_dbapi_connection(dbapi_connection)
        cursor = connection.cursor()
        try:
            # WAL + busy timeout reduce lock contention between bot processes.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    setattr(engine, "_kola_sqlite_pragmas_installed", True)


def cast_dbapi_connection(connection: object) -> Any:
    return connection


def init_engine(db_url: str):
    engine = create_persistence_engine(db_url)
    Base.metadata.create_all(engine)
    return engine


def get_sessionmaker(db_url: str):
    engine = init_engine(db_url)
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
