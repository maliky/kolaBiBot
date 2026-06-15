from __future__ import annotations

import subprocess
from pathlib import Path

from kolabi.bot.fresh_run import feeder_plan_for_strategy, route_lines, strategy_pair_count

SCRIPT = Path("scripts/kolabi-fresh-run")


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        check=False,
        text=True,
        capture_output=True,
    )


def test_cross_exchange_strategy_resolves_feed_routes() -> None:
    plan = feeder_plan_for_strategy(
        "orders/demo_cross_exchange_chain.tsv",
        default_exchange="kraken",
        default_market_type="futures",
        default_symbol="PI_XBTUSD",
        environment="demo",
    )

    assert route_lines(plan) == (
        "binance\tspot\tADAUSDT\tBINS_DEMO_API_KEY\tBINS_DEMO_API_SECRET",
        "bitmex\tfutures\tXBTUSD\tBTX_DEMO_API_KEY\tBTX_DEMO_API_SECRET",
        "kraken\tfutures\tPI_ADAUSD\tKRKF_DEMO_API_KEY\tKRKF_DEMO_API_SECRET",
    )


def test_strategy_pair_count_reads_all_strategy_pairs() -> None:
    assert strategy_pair_count("orders/demo_cross_exchange_chain.tsv") == 3


def test_fresh_run_dry_run_restarts_all_strategy_feed_routes() -> None:
    result = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "--strategy",
        "orders/demo_cross_exchange_chain.tsv",
        "--environment",
        "demo",
        "--symbol",
        "PI_XBTUSD",
    )

    assert result.returncode == 0
    output = result.stdout + result.stderr
    assert "kolabi-purge: would purge KOLABI_MARKET_DB_URL" in result.stdout
    assert "kolabi:***@" in output
    assert "kolabi:kolabi@" not in output
    assert "exchange=binance market_type=spot" in output
    assert "api_key_env=BINS_DEMO_API_KEY" in output
    assert "exchange=bitmex market_type=futures" in output
    assert "api_key_env=BTX_DEMO_API_KEY" in output
    assert "exchange=kraken market_type=futures" in output
    assert "api_key_env=KRKF_DEMO_API_KEY" in output
    assert "python -m kolabi.bot run" in result.stdout
    assert "--max-active-pairs 3" in result.stdout


def test_fresh_run_max_active_pairs_override_wins() -> None:
    result = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "--strategy",
        "orders/demo_cross_exchange_chain.tsv",
        "--environment",
        "demo",
        "--symbol",
        "PI_XBTUSD",
        "--max-active-pairs",
        "1",
    )

    assert result.returncode == 0
    assert "--max-active-pairs 1" in result.stdout
