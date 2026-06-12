from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence, TextIO

from kolabi.bot.exchange_routes import (
    ExchangeRoute,
    default_symbol_for_route,
    pair_route,
)
from kolabi.bot.tsv import read_strategy_file
from kolabi.shared.config import exchange_credential_env_names


@dataclass(frozen=True, order=True)
class RouteFeederPlan:
    route: ExchangeRoute
    api_key_env: str
    api_secret_env: str


def resolve_strategy_routes(
    strategy_path: str | Path,
    *,
    default_exchange: str,
    default_market_type: str,
    default_symbol: str,
) -> tuple[ExchangeRoute, ...]:
    strategy = read_strategy_file(strategy_path)
    routes = {
        pair_route(
            pair,
            default_exchange=default_exchange,
            default_market_type=default_market_type,
            default_symbol=default_symbol,
        )
        for pair in strategy.pairs
    }
    return tuple(sorted(routes))


def feeder_plan_for_routes(
    routes: Sequence[ExchangeRoute],
    *,
    environment: str,
) -> tuple[RouteFeederPlan, ...]:
    return tuple(
        RouteFeederPlan(
            route=route,
            api_key_env=_first_env_name(
                exchange_credential_env_names(
                    route.exchange,
                    route.market_type,
                    environment,
                )
            ),
            api_secret_env=_first_env_name(
                exchange_credential_env_names(
                    route.exchange,
                    route.market_type,
                    environment,
                    secret=True,
                )
            ),
        )
        for route in sorted(routes)
    )


def feeder_plan_for_strategy(
    strategy_path: str | Path,
    *,
    default_exchange: str,
    default_market_type: str,
    default_symbol: str,
    environment: str,
) -> tuple[RouteFeederPlan, ...]:
    return feeder_plan_for_routes(
        resolve_strategy_routes(
            strategy_path,
            default_exchange=default_exchange,
            default_market_type=default_market_type,
            default_symbol=default_symbol,
        ),
        environment=environment,
    )


def route_lines(
    plan: Sequence[RouteFeederPlan],
) -> tuple[str, ...]:
    return tuple(
        "\t".join(
            (
                item.route.exchange,
                item.route.market_type,
                item.route.symbol,
                item.api_key_env,
                item.api_secret_env,
            )
        )
        for item in plan
    )


def _first_env_name(names: Sequence[str]) -> str:
    return names[0] if names else ""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve strategy routes into kolabidb feeder inputs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    routes = subparsers.add_parser("route-lines")
    routes.add_argument("--strategy", required=True)
    routes.add_argument("--exchange", default="kraken")
    routes.add_argument("--market-type", default="futures")
    routes.add_argument("--symbol")
    routes.add_argument("--environment", choices=("demo", "live"), default="demo")
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = _parser().parse_args(argv)
    out = stdout
    if out is None:
        import sys

        out = sys.stdout

    if args.command == "route-lines":
        default_symbol = args.symbol or default_symbol_for_route(
            args.exchange,
            args.market_type,
        )
        for line in route_lines(
            feeder_plan_for_strategy(
                args.strategy,
                default_exchange=args.exchange,
                default_market_type=args.market_type,
                default_symbol=default_symbol,
                environment=args.environment,
            )
        ):
            print(line, file=out)
        return 0
    raise ValueError(f"unsupported command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
