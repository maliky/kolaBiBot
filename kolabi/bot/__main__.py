from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kolabi.bot.domain import StrategySpec
from kolabi.bot.service import BotConfig, BotService
from kolabi.bot.tsv import (
    read_strategy_file,
    strategy_from_run_once_args,
    strategy_to_pretty_dict,
)


def add_runtime_options(parser: argparse.ArgumentParser) -> None:
    """Attach common runtime options shared by run and run-once."""
    parser.add_argument("--exchange", default="kraken")
    parser.add_argument("--symbol", default="PI_XBTUSD")
    parser.add_argument("--environment", choices=("demo", "live"), default="demo")
    parser.add_argument("--db-url", help="Optional database URL for run persistence")
    parser.add_argument(
        "--market-db-url",
        help="Optional public market data DB URL. Defaults from Kraken Futures environment.",
    )
    parser.add_argument(
        "--account-db-url",
        help="Optional private account DB URL. Defaults from Kraken Futures environment.",
    )
    parser.add_argument(
        "--ready-timeout-seconds",
        type=float,
        default=45.0,
        help="Maximum wait for fresh Kraken public/private state before the TSV loop starts.",
    )
    parser.add_argument(
        "--ready-poll-seconds",
        type=float,
        default=1.0,
        help="Polling cadence used while waiting for Kraken runtime readiness.",
    )
    parser.add_argument(
        "--max-public-age-seconds",
        type=float,
        default=15.0,
        help="Maximum acceptable age for the public market snapshot.",
    )
    parser.add_argument(
        "--max-private-age-seconds",
        type=float,
        default=30.0,
        help="Maximum acceptable age for the private websocket heartbeat.",
    )
    parser.add_argument(
        "--max-reconcile-age-seconds",
        type=float,
        default=300.0,
        help="Maximum acceptable age for the startup REST reconcile.",
    )
    parser.add_argument(
        "--skip-ready-check",
        action="store_true",
        help="Skip Kraken runtime freshness checks before starting the strategy.",
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--update-pause", type=int, default=10)
    parser.add_argument("--log-pause", type=int, default=60)
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Run orders synchronously (blocking) instead of spawning threads",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse the strategy file and print orders without submitting them",
    )


def add_single_order_options(parser: argparse.ArgumentParser) -> None:
    """Expose the compatibility one-order vocabulary on the active kolabi.bot CLI."""
    parser.add_argument(
        "--tps_run",
        "-t",
        type=float,
        nargs=2,
        default=[-1, 800],
        help=(
            "Strategy validity window in minutes from launch, as start end. "
            "Example: -t 0 60 means active now for one hour."
        ),
    )
    parser.add_argument(
        "--name",
        "-m",
        default="NaDef",
        help="Internal order name shown in logs.",
    )
    parser.add_argument(
        "--tOut",
        "-O",
        type=int,
        default=None,
        help="Validation timeout in minutes for the main order lifecycle.",
    )
    parser.add_argument(
        "--drPause",
        "-p",
        type=float,
        default=None,
        help="Pause between repeated attempts, in minutes.",
    )
    parser.add_argument(
        "--prix",
        "-x",
        type=float,
        nargs=2,
        default=[-1, 1],
        help=(
            "Price interval as two ordered bounds. This is a strict range, not a single point. "
            "Meaning depends on aType: pD = differential from reference price, "
            "p%% = percent from reference price, pA = absolute prices. "
            "Examples: -a qAt%%pD -x 1 2 means from ref+1 to ref+2; "
            "-a qAt%%p%% -x -1 1 means from -1%% to +1%% around ref; "
            "-a qAt%%pA -x 79320 79340 means an absolute interval. "
            "Do not use equal bounds like -x 1 1."
        ),
    )
    parser.add_argument(
        "--quantity",
        "-q",
        type=int,
        default=75,
        help="Absolute size or percentage, depending on aType.",
    )
    parser.add_argument(
        "--tailPrice",
        "-T",
        type=float,
        default=2.0,
        help=(
            "Tail thickness using the compatibility aType semantics. "
            "Default interpretation is percent unless aType contains tA or tD."
        ),
    )
    parser.add_argument(
        "--oDelta",
        "-d",
        type=float,
        default=None,
        help="Difference between trigger price and order price.",
    )
    parser.add_argument(
        "--tDelta",
        "-s",
        type=float,
        default=None,
        help="Difference between trigger price and tail stop price.",
    )
    parser.add_argument(
        "--nbEssais",
        "-n",
        type=int,
        default=1,
        help="Number of attempts.",
    )
    parser.add_argument(
        "--oType",
        "-o",
        type=str,
        default="M",
        help=(
            "Main order type in the compatibility vocabulary. "
            "Examples: M = Market, L = Limit, S = Stop, SL = StopLimit, "
            "MT = MarketIfTouched, LT = LimitIfTouched."
        ),
    )
    parser.add_argument(
        "--tType",
        "-y",
        type=str,
        default="Si-",
        help=(
            "Tail order type in the compatibility vocabulary. "
            "Examples: S- = reduce-only Stop on lastMidPrice, "
            "Sf- = reduce-only Stop on fairPrice, "
            "SL- = reduce-only StopLimit."
        ),
    )
    parser.add_argument(
        "--side",
        "-c",
        type=str,
        default="buy",
        help="Order side.",
    )
    parser.add_argument(
        "--aType",
        "-a",
        type=str,
        default="p%q%t%",
        help=(
            "Compatibility interpretation of prix, quantity, and tailPrice. "
            "Use pD/p%%/pA for price, q%%/qA for quantity, t%%/tD/tA for tail. "
            "Example: qAt%%pD means absolute quantity, percent tail, differential prices."
        ),
    )
    parser.add_argument(
        "--Hook",
        "-H",
        type=str,
        default="",
        help="Optional hook dependency, for example XBrx-S_T.",
    )


def build_service(args: argparse.Namespace) -> BotService:
    """Build a bot service from parsed CLI args."""
    return BotService(
        BotConfig(
            exchange=args.exchange,
            symbol=args.symbol,
            environment=args.environment,
            updatepause=args.update_pause,
            logpause=args.log_pause,
            log_level=args.log_level,
            db_url=args.db_url,
            market_db_url=args.market_db_url,
            account_db_url=args.account_db_url,
            require_ready=not args.skip_ready_check,
            ready_timeout_seconds=args.ready_timeout_seconds,
            ready_poll_seconds=args.ready_poll_seconds,
            max_public_age_seconds=args.max_public_age_seconds,
            max_private_age_seconds=args.max_private_age_seconds,
            max_reconcile_age_seconds=args.max_reconcile_age_seconds,
        )
    )


def build_single_strategy(args: argparse.Namespace) -> StrategySpec:
    """Traduit la CLI legacy vers une StrategySpec canonique."""
    return strategy_from_run_once_args(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kolabi.bot", description="kolaBiBot execution CLI"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", help="Run strategies defined in a TSV file"
    )
    run_parser.add_argument(
        "--strategy",
        "-s",
        required=True,
        help="Path to TSV strategy file (e.g. orders/demo_ada.tsv)",
    )
    add_runtime_options(run_parser)

    run_once_parser = subparsers.add_parser(
        "run-once",
        help="Run one strategy-defined order pair from the command line using compatibility vocabulary",
        description=(
            "Run one order pair through kolabi.bot while keeping the compatibility "
            "multi_kola vocabulary. The key point is that --prix is always a "
            "two-bound ordered interval, interpreted by --aType."
        ),
        epilog=(
            "Examples:\n"
            "  Differential around current reference:\n"
            "    python -m kolabi.bot run-once --symbol PI_XBTUSD --environment demo "
            "-m XSellTail -t 0 1440 -O 60 -x 1 2 -q 1 -T 0.5 -o L -y S- -c sell -a qAt%pD --dry-run\n"
            "  Percent around current reference:\n"
            "    python -m kolabi.bot run-once --symbol PI_XBTUSD --environment demo "
            "-m XPct -t 0 60 -x -1 1 -q 1 -T 0.5 -o L -y S- -c buy -a qAt%p% --dry-run\n"
            "  Absolute interval:\n"
            "    python -m kolabi.bot run-once --symbol PI_XBTUSD --environment demo "
            "-m XAbs -t 0 60 -x 79320 79340 -q 1 -T 0.5 -o L -y S- -c sell -a qAt%pA --dry-run"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    add_runtime_options(run_once_parser)
    add_single_order_options(run_once_parser)

    preflight_parser = subparsers.add_parser(
        "preflight",
        help="Check whether Kraken public/private DB state is fresh enough to start a strategy",
    )
    preflight_parser.add_argument("--exchange", default="kraken")
    preflight_parser.add_argument("--symbol", default="PI_XBTUSD")
    preflight_parser.add_argument(
        "--environment", choices=("demo", "live"), default="demo"
    )
    preflight_parser.add_argument(
        "--market-db-url",
        help="Optional public market data DB URL. Defaults from Kraken Futures environment.",
    )
    preflight_parser.add_argument(
        "--account-db-url",
        help="Optional private account DB URL. Defaults from Kraken Futures environment.",
    )
    preflight_parser.add_argument(
        "--ready-timeout-seconds",
        type=float,
        default=45.0,
    )
    preflight_parser.add_argument(
        "--ready-poll-seconds",
        type=float,
        default=1.0,
    )
    preflight_parser.add_argument(
        "--max-public-age-seconds",
        type=float,
        default=15.0,
    )
    preflight_parser.add_argument(
        "--max-private-age-seconds",
        type=float,
        default=30.0,
    )
    preflight_parser.add_argument(
        "--max-reconcile-age-seconds",
        type=float,
        default=300.0,
    )
    return parser


def run_command(args: argparse.Namespace) -> int:
    strategy_path = Path(args.strategy)
    if not strategy_path.exists():
        raise FileNotFoundError(f"Strategy file not found: {strategy_path}")

    strategy = read_strategy_file(strategy_path)
    if not strategy.pairs:
        print("No strategies found in file.", file=sys.stderr)
        return 1

    if args.dry_run:
        print(json.dumps(strategy_to_pretty_dict(strategy), indent=2, sort_keys=True, default=str))
        return 0

    service = build_service(args)
    service.run_strategy(strategy, asynchronous=not args.sync)
    if args.sync:
        print(f"Executed {len(strategy.pairs)} order(s) synchronously.")
    else:
        print(
            f"Submitted {len(strategy.pairs)} order(s). "
            "Threads running in background; monitor logs for progress."
        )
    return 0


def run_once_command(args: argparse.Namespace) -> int:
    """Run one command-line strategy row using the compatibility parameter names."""
    strategy = build_single_strategy(args)

    if args.dry_run:
        print(json.dumps(strategy_to_pretty_dict(strategy), indent=2, sort_keys=True, default=str))
        return 0

    service = build_service(args)
    service.run_strategy(strategy, asynchronous=not args.sync)
    if args.sync:
        print("Executed 1 order pair synchronously.")
    else:
        print("Submitted 1 order pair. Threads running in background; monitor logs for progress.")
    return 0


def preflight_command(args: argparse.Namespace) -> int:
    """Print a readiness snapshot for the Kraken TSV route."""
    service = BotService(
        BotConfig(
            exchange=args.exchange,
            symbol=args.symbol,
            environment=args.environment,
            market_db_url=args.market_db_url,
            account_db_url=args.account_db_url,
            require_ready=True,
            ready_timeout_seconds=args.ready_timeout_seconds,
            ready_poll_seconds=args.ready_poll_seconds,
            max_public_age_seconds=args.max_public_age_seconds,
            max_private_age_seconds=args.max_private_age_seconds,
            max_reconcile_age_seconds=args.max_reconcile_age_seconds,
        )
    )
    payload = service.preflight()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if bool(payload.get("ready")) else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return run_command(args)
    if args.command == "run-once":
        return run_once_command(args)
    if args.command == "preflight":
        return preflight_command(args)
    raise ValueError(f"Unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
