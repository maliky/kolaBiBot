from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def init_engine(db_url: str):
    engine = create_engine(db_url, echo=False, future=True)
    Base.metadata.create_all(engine)
    return engine


def get_sessionmaker(db_url: str):
    engine = init_engine(db_url)
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
