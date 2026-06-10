"""Pytest configuration ensuring in-place imports and quiet deps."""
from __future__ import annotations

import os
import re
import sys
import uuid
import warnings
from pathlib import Path
from typing import Callable

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Silence noisy third-party deprecation warnings (websockets/binance).
warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message=r".*websockets\.WebSocketClientProtocol.*",
)
warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message=r".*websockets\.legacy.*",
)
warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message=r"There is no current event loop",
    module=r"binance\.helpers",
)


_ENV_REF = re.compile(r"\$\{([^}]+)\}")


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        def expand(match: re.Match[str]) -> str:
            name = match.group(1)
            return values.get(name, os.environ.get(name, ""))

        values[key] = _ENV_REF.sub(expand, value)
    return values


def _postgres_base_url() -> str:
    if os.environ.get("KOLABI_TEST_PG_URL"):
        return os.environ["KOLABI_TEST_PG_URL"]
    env_values = _load_env_file(REPO_ROOT / ".env.postgres")
    return (
        env_values.get("KOLABI_TEST_PG_URL")
        or os.environ.get("KOLABI_MARKET_DB_URL")
        or env_values.get("KOLABI_MARKET_DB_URL")
        or os.environ.get("KOLABI_PG")
        or env_values.get("KOLABI_PG")
        or "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi"
    )


@pytest.fixture
def postgres_url_factory() -> Callable[[str], str]:
    base_url = make_url(_postgres_base_url())
    admin_url = base_url.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    created: list[str] = []

    try:
        with admin_engine.connect() as connection:
            connection.execute(text("select 1"))
    except Exception as exc:
        admin_engine.dispose()
        pytest.skip(
            "PostgreSQL test database is unavailable; start scripts/kolabidb postgres "
            f"or set KOLABI_TEST_PG_URL ({exc})"
        )

    def make_database(label: str = "db") -> str:
        safe_label = re.sub(r"[^a-z0-9_]+", "_", label.lower()).strip("_") or "db"
        db_name = f"kolabi_test_{safe_label}_{uuid.uuid4().hex[:10]}"[:63]
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{db_name}"'))
        created.append(db_name)
        db_url = base_url.set(database=db_name).render_as_string(hide_password=False)
        test_engine = create_engine(db_url)
        try:
            with test_engine.connect() as connection:
                connection.execute(text("select 1"))
        except Exception as exc:
            test_engine.dispose()
            pytest.skip(
                "PostgreSQL test database is unavailable after creation; "
                f"check KOLABI_TEST_PG_URL or .env.postgres ({exc})"
            )
        test_engine.dispose()
        return db_url

    yield make_database

    with admin_engine.connect() as connection:
        for db_name in reversed(created):
            connection.execute(
                text(
                    "select pg_terminate_backend(pid) "
                    "from pg_stat_activity "
                    "where datname = :db_name and pid <> pg_backend_pid()"
                ),
                {"db_name": db_name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    admin_engine.dispose()
