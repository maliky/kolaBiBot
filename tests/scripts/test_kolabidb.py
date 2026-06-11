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
    assert "--backend postgres" in result.stdout
    assert "Default: postgres" in result.stdout
    assert "--rest-url URL" in result.stdout
    assert "--private-rest-url URL" in result.stdout
    assert "--public-rest-url URL" in result.stdout
    assert "Kraken supports futures, spot, and margin." in result.stdout
    assert "BitMEX supports futures and spot." in result.stdout


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
    assert "logs/kolabidb-public-kraken-futures-postgres-demo-PI_ADAUSD.log" in result.stdout


def test_kolabidb_rejects_sqlite_backend() -> None:
    result = run_script(
        "--dry-run",
        "--backend",
        "sqlite",
        "public",
        "start",
        "--pair",
        "PI_ADAUSD",
    )

    assert result.returncode == 2
    assert "SQLite backend is no longer supported" in result.stderr


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


def test_kolabidb_private_spawn_public_dry_run_starts_both_with_postgres() -> None:
    result = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "private",
        "start",
        "--public",
        "spawn",
        "--pair",
        "PI_XBTUSD",
        "--account-scope",
        "advers",
        "--api-key-env",
        "KRKF_DEMO2_API_KEY",
        "--api-secret-env",
        "KRKF_DEMO2_API_SECRET",
    )

    assert result.returncode == 0
    assert "python\\ -m\\ kolabi.tree.kraken\\ run" in result.stdout
    assert "python\\ -m\\ kolabi.tree.account\\ run" in result.stdout
    assert "--account-scope\\ advers" in result.stdout
    assert "--api-key-env\\ KRKF_DEMO2_API_KEY" in result.stdout
    assert "--api-secret-env\\ KRKF_DEMO2_API_SECRET" in result.stdout
    assert "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account_advers" in result.stdout
    assert "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_critical_advers" in result.stdout


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
    assert "logs/kolabidb-public-kraken-futures-postgres-demo-PI_XBTUSD.log" in result.stdout


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
    assert "logs/kolabidb-private-kraken-futures-postgres-demo-default.log" in result.stdout


def test_kolabidb_binance_public_private_dry_run_uses_binance_modules() -> None:
    result = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "--exchange",
        "binance",
        "private",
        "start",
        "--public",
        "spawn",
        "--account-scope",
        "advers",
    )

    assert result.returncode == 0
    assert "python\\ -m\\ kolabi.tree.binance\\ run" in result.stdout
    assert "python\\ -m\\ kolabi.tree.binance_account\\ run" in result.stdout
    assert "--pair\\ BTCUSDT" in result.stdout
    assert "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market" in result.stdout
    assert "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account_advers" in result.stdout
    assert "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_critical_advers" in result.stdout


def test_kolabidb_binance_isolated_margin_spawn_public_keeps_route_lane() -> None:
    result = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "--exchange",
        "binance",
        "--market-type",
        "isolated_margin",
        "--environment",
        "live",
        "private",
        "start",
        "--public",
        "spawn",
        "--pair",
        "BTCUSDT",
        "--account-scope",
        "advers",
        "--api-key-env",
        "BINM_API_KEY",
        "--api-secret-env",
        "BINM_API_SECRET",
    )

    assert result.returncode == 0
    assert "python\\ -m\\ kolabi.tree.binance\\ run" in result.stdout
    assert "python\\ -m\\ kolabi.tree.binance_account\\ run" in result.stdout
    assert result.stdout.count("--market-type\\ isolated_margin") == 2
    assert "--pair\\ BTCUSDT" in result.stdout
    assert "--symbol\\ BTCUSDT" in result.stdout
    assert "--api-key-env\\ BINM_API_KEY" in result.stdout
    assert "--api-secret-env\\ BINM_API_SECRET" in result.stdout
    assert "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account_advers" in result.stdout
    assert "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_critical_advers" in result.stdout
    assert "logs/kolabidb-public-binance-isolated_margin-postgres-live-BTCUSDT.log" in result.stdout
    assert "logs/kolabidb-private-binance-isolated_margin-postgres-live-advers.log" in result.stdout


def test_kolabidb_binance_margin_demo_private_passes_rest_override() -> None:
    result = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "--exchange",
        "binance",
        "--market-type",
        "margin",
        "--environment",
        "demo",
        "private",
        "start",
        "--pair",
        "BTCUSDT",
        "--account-scope",
        "advers",
        "--private-rest-url",
        "https://margin-demo.example.test",
    )

    assert result.returncode == 0
    assert "python\\ -m\\ kolabi.tree.binance_account\\ run" in result.stdout
    assert "--market-type\\ margin" in result.stdout
    assert "--symbol\\ BTCUSDT" in result.stdout
    assert "--rest-url\\ https://margin-demo.example.test" in result.stdout
    assert "python\\ -m\\ kolabi.tree.binance\\ run" not in result.stdout


def test_kolabidb_spawn_can_split_public_and_private_endpoint_overrides() -> None:
    result = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "--exchange",
        "binance",
        "--market-type",
        "margin",
        "--environment",
        "demo",
        "private",
        "start",
        "--public",
        "spawn",
        "--pair",
        "BTCUSDT",
        "--public-ws-url",
        "wss://public.example.test/stream",
        "--public-rest-url",
        "https://public.example.test",
        "--private-ws-url",
        "wss://private.example.test/ws",
        "--private-rest-url",
        "https://private.example.test",
    )

    assert result.returncode == 0
    assert "python\\ -m\\ kolabi.tree.binance\\ run" in result.stdout
    assert "python\\ -m\\ kolabi.tree.binance_account\\ run" in result.stdout
    assert "--ws-url\\ wss://public.example.test/stream" in result.stdout
    assert "--rest-url\\ https://public.example.test" in result.stdout
    assert "--ws-url\\ wss://private.example.test/ws" in result.stdout
    assert "--rest-url\\ https://private.example.test" in result.stdout


def test_kolabidb_kraken_spot_and_margin_dry_runs_use_kraken_modules() -> None:
    public = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "--exchange",
        "kraken",
        "--market-type",
        "spot",
        "public",
        "start",
        "--pair",
        "XBT/USD",
    )
    private = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "--exchange",
        "kraken",
        "--market-type",
        "margin",
        "private",
        "start",
    )

    assert public.returncode == 0
    assert "python\\ -m\\ kolabi.tree.kraken\\ run" in public.stdout
    assert "--market-type\\ spot" in public.stdout
    assert "--pair\\ XBT/USD" in public.stdout
    assert private.returncode == 0
    assert "python\\ -m\\ kolabi.tree.account\\ run" in private.stdout
    assert "--market-type\\ margin" in private.stdout


def test_kolabidb_kraken_spot_margin_default_to_spot_pair() -> None:
    spot = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "--exchange",
        "kraken",
        "--market-type",
        "spot",
        "public",
        "start",
    )
    margin = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "--exchange",
        "kraken",
        "--market-type",
        "margin",
        "public",
        "start",
    )

    assert spot.returncode == 0
    assert "--pair\\ XBT/USD" in spot.stdout
    assert "PI_XBTUSD" not in spot.stdout
    assert margin.returncode == 0
    assert "--pair\\ XBT/USD" in margin.stdout
    assert "PI_XBTUSD" not in margin.stdout


def test_kolabidb_bitmex_spot_public_private_dry_runs_use_bitmex_modules() -> None:
    public = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "--exchange",
        "bitmex",
        "--market-type",
        "spot",
        "public",
        "start",
        "--pair",
        "XBT_USDT",
    )
    private = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "--exchange",
        "bitmex",
        "--market-type",
        "spot",
        "private",
        "start",
        "--pair",
        "XBT_USDT",
    )

    assert public.returncode == 0
    assert "python\\ -m\\ kolabi.tree.bitmex\\ run" in public.stdout
    assert "--market-type\\ spot" in public.stdout
    assert "--pair\\ XBT_USDT" in public.stdout
    assert private.returncode == 0
    assert "python\\ -m\\ kolabi.tree.bitmex_account\\ run" in private.stdout
    assert "--market-type\\ spot" in private.stdout
    assert "--symbol\\ XBT_USDT" in private.stdout


def test_kolabidb_bitmex_spot_defaults_to_spot_symbol() -> None:
    public = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "--exchange",
        "bitmex",
        "--market-type",
        "spot",
        "public",
        "start",
    )
    private = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "--exchange",
        "bitmex",
        "--market-type",
        "spot",
        "private",
        "start",
    )

    assert public.returncode == 0
    assert "--pair\\ XBT_USDT" in public.stdout
    assert "XBTUSD" not in public.stdout
    assert private.returncode == 0
    assert "--symbol\\ XBT_USDT" in private.stdout
    assert "XBTUSD" not in private.stdout


def test_kolabidb_bitmex_private_rejects_websocket_override() -> None:
    result = run_script(
        "--dry-run",
        "--exchange",
        "bitmex",
        "private",
        "start",
        "--private-ws-url",
        "wss://private.example.test/realtime",
    )

    assert result.returncode == 2
    assert "private REST reconciliation" in result.stderr


def test_kolabidb_bitmex_spawn_shared_ws_applies_only_to_public() -> None:
    result = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "--exchange",
        "bitmex",
        "private",
        "start",
        "--public",
        "spawn",
        "--ws-url",
        "wss://bitmex.example.test/realtime",
        "--rest-url",
        "https://bitmex.example.test/api/v1",
    )

    assert result.returncode == 0
    public_line = next(
        line for line in result.stdout.splitlines()
        if "kolabi.tree.bitmex\\ run" in line
    )
    private_line = next(
        line for line in result.stdout.splitlines()
        if "kolabi.tree.bitmex_account\\ run" in line
    )
    assert "--ws-url\\ wss://bitmex.example.test/realtime" in public_line
    assert "--ws-url" not in private_line
    assert "--rest-url\\ https://bitmex.example.test/api/v1" in public_line
    assert "--rest-url\\ https://bitmex.example.test/api/v1" in private_line


def test_kolabidb_rejects_bitmex_margin() -> None:
    result = run_script(
        "--dry-run",
        "--exchange",
        "bitmex",
        "--market-type",
        "margin",
        "private",
        "start",
    )

    assert result.returncode == 2
    assert "bitmex supports --market-type futures or spot" in result.stderr


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
