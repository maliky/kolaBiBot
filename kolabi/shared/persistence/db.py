from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def _reject_sqlite_url(db_url: str) -> None:
    if db_url.startswith("sqlite"):
        raise ValueError("SQLite is no longer supported; use a PostgreSQL database URL.")


def create_persistence_engine(
    db_url: str,
) -> Engine:
    _reject_sqlite_url(db_url)
    return create_engine(db_url, echo=False, future=True)


def init_engine(
    db_url: str,
):
    engine = create_persistence_engine(db_url)
    Base.metadata.create_all(engine)
    return engine


def get_sessionmaker(
    db_url: str,
):
    engine = init_engine(db_url)
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
