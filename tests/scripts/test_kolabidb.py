from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path("scripts/kolabidb")


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        check=False,
        text=True,
        capture_output=True,
    )


def test_kolabidb_help_documents_account_scope_and_logs() -> None:
    result = run_script("--help")

    assert result.returncode == 0
    assert "--account-scope NAME" in result.stdout
    assert "./logs" in result.stdout
    assert "--backend postgres|sqlite" in result.stdout
    assert "Default: postgres" in result.stdout


def test_kolabidb_public_start_dry_run_defaults_to_postgres_lane() -> None:
    result = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "public",
        "start",
        "--pair",
        "PI_ADAUSD",
    )

    assert result.returncode == 0
    assert "python\\ -m\\ kolabi.tree.kraken\\ run" in result.stdout
    assert "--pair\\ PI_ADAUSD" in result.stdout
    assert "--db-url\\ postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market" in result.stdout
    assert "logs/kolabidb-public-postgres-demo-PI_ADAUSD.log" in result.stdout


def test_kolabidb_public_start_dry_run_uses_sqlite_instrument_db_when_requested() -> None:
    result = run_script(
        "--dry-run",
        "--backend",
        "sqlite",
        "public",
        "start",
        "--pair",
        "PI_ADAUSD",
    )

    assert result.returncode == 0
    assert "python\\ -m\\ kolabi.tree.kraken\\ run" in result.stdout
    assert "--pair\\ PI_ADAUSD" in result.stdout
    assert "sqlite:///dbs/pub-futures-demo-PI_ADAUSD.sqlite" in result.stdout
    assert "logs/kolabidb-public-demo-PI_ADAUSD.log" in result.stdout


def test_kolabidb_private_start_dry_run_defaults_to_postgres_reuse_public() -> None:
    result = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "private",
        "start",
    )

    assert result.returncode == 0
    assert "python\\ -m\\ kolabi.tree.account\\ run" in result.stdout
    assert "python\\ -m\\ kolabi.tree.kraken\\ run" not in result.stdout
    assert (
        "--account-db-url\\ postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account"
        in result.stdout
    )
    assert (
        "--critical-db-url\\ postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_critical"
        in result.stdout
    )


def test_kolabidb_private_spawn_public_dry_run_starts_both_with_sqlite() -> None:
    result = run_script(
        "--dry-run",
        "--backend",
        "sqlite",
        "private",
        "start",
        "--public",
        "spawn",
        "--pair",
        "PI_XBTUSD",
        "--account-scope",
        "advers",
        "--api-key-env",
        "KRAKEN_FUTURE_DEMO2_API_KEY",
        "--api-secret-env",
        "KRAKEN_FUTURE_DEMO2_API_SECRET",
    )

    assert result.returncode == 0
    assert "python\\ -m\\ kolabi.tree.kraken\\ run" in result.stdout
    assert "python\\ -m\\ kolabi.tree.account\\ run" in result.stdout
    assert "--account-scope\\ advers" in result.stdout
    assert "--api-key-env\\ KRAKEN_FUTURE_DEMO2_API_KEY" in result.stdout
    assert "--api-secret-env\\ KRAKEN_FUTURE_DEMO2_API_SECRET" in result.stdout
    assert "sqlite:///dbs/prv-futures-demo-advers.sqlite" in result.stdout
    assert "sqlite:///dbs/prv-futures-demo-advers-critical.sqlite" in result.stdout


def test_kolabidb_postgres_public_dry_run_uses_env_lane_and_distinct_log() -> None:
    result = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "public",
        "start",
    )

    assert result.returncode == 0
    assert "--db-url\\ postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market" in result.stdout
    assert (
        "--private-db-url\\ postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account"
        in result.stdout
    )
    assert "logs/kolabidb-public-postgres-demo-PI_XBTUSD.log" in result.stdout


def test_kolabidb_postgres_private_logs_uses_distinct_log() -> None:
    result = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "private",
        "logs",
        "--tail-lines",
        "20",
    )

    assert result.returncode == 0
    assert "tail -n 20 -f" in result.stdout
    assert "logs/kolabidb-private-postgres-demo-default.log" in result.stdout


def test_kolabidb_postgres_container_logs_use_compose() -> None:
    result = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "postgres",
        "logs",
        "--tail-lines",
        "20",
    )

    assert result.returncode == 0
    assert "docker-compose --env-file docker/postgres/kolabi-postgres.env.example -f docker-compose.postgres.yml logs --tail=20 -f" in result.stdout


def test_kolabidb_rejects_bad_public_mode() -> None:
    result = run_script("--dry-run", "private", "start", "--public", "maybe")

    assert result.returncode == 2
    assert "--public must be reuse or spawn" in result.stderr
