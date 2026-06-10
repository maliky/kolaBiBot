from __future__ import annotations

import pytest

from kolabi.shared.persistence.db import create_persistence_engine


def test_create_persistence_engine_rejects_sqlite_url(tmp_path) -> None:
    with pytest.raises(ValueError, match="SQLite is no longer supported"):
        create_persistence_engine(f"sqlite:///{tmp_path / 'removed.sqlite'}")


def test_create_persistence_engine_accepts_postgres_url(postgres_url_factory) -> None:
    engine = create_persistence_engine(postgres_url_factory("persistence"))

    with engine.connect() as connection:
        assert connection.exec_driver_sql("select 1").scalar_one() == 1
