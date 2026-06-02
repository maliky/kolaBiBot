from __future__ import annotations

from kolabi.shared.persistence.db import create_persistence_engine


def test_create_persistence_engine_accepts_short_sqlite_busy_timeout(tmp_path) -> None:
    engine = create_persistence_engine(
        f"sqlite:///{tmp_path / 'short-timeout.sqlite'}",
        sqlite_busy_timeout_seconds=0.25,
    )

    with engine.connect() as connection:
        timeout_ms = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()

    assert timeout_ms == 250
