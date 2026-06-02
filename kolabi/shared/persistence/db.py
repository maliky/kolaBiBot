from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

_SQLITE_BUSY_TIMEOUT_MS = 30_000


def _is_sqlite_url(db_url: str) -> bool:
    return db_url.startswith("sqlite")


def create_persistence_engine(
    db_url: str,
    *,
    sqlite_busy_timeout_seconds: float | None = None,
) -> Engine:
    timeout_seconds = (
        _SQLITE_BUSY_TIMEOUT_MS / 1000
        if sqlite_busy_timeout_seconds is None
        else max(0.0, float(sqlite_busy_timeout_seconds))
    )
    connect_args: dict[str, Any] = {}
    if _is_sqlite_url(db_url):
        connect_args["timeout"] = timeout_seconds
    engine = create_engine(db_url, echo=False, future=True, connect_args=connect_args)
    if _is_sqlite_url(db_url):
        _configure_sqlite_engine(engine, busy_timeout_ms=int(timeout_seconds * 1000))
    return engine


def _configure_sqlite_engine(
    engine: Engine,
    *,
    busy_timeout_ms: int = _SQLITE_BUSY_TIMEOUT_MS,
) -> None:
    if getattr(engine, "_kola_sqlite_pragmas_installed", False):
        return

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: object, _connection_record: object) -> None:
        connection = cast_dbapi_connection(dbapi_connection)
        cursor = connection.cursor()
        try:
            # WAL + busy timeout reduce lock contention between bot processes.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={max(0, busy_timeout_ms)}")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    setattr(engine, "_kola_sqlite_pragmas_installed", True)


def cast_dbapi_connection(connection: object) -> Any:
    return connection


def init_engine(
    db_url: str,
    *,
    sqlite_busy_timeout_seconds: float | None = None,
):
    engine = create_persistence_engine(
        db_url,
        sqlite_busy_timeout_seconds=sqlite_busy_timeout_seconds,
    )
    Base.metadata.create_all(engine)
    return engine


def get_sessionmaker(
    db_url: str,
    *,
    sqlite_busy_timeout_seconds: float | None = None,
):
    engine = init_engine(
        db_url,
        sqlite_busy_timeout_seconds=sqlite_busy_timeout_seconds,
    )
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
