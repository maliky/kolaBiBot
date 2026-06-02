from __future__ import annotations

import argparse

import pytest
from kolabi.bot.__main__ import (
    build_parser,
    build_single_strategy,
    run_command,
    run_once_command,
)
from kolabi.bot.domain import StrategySpec
from kolabi.bot.tsv import read_strategy_file


def test_run_once_parser_accepts_legacy_short_flags() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "run-once",
            "--symbol",
            "PI_XBTUSD",
            "-m",
            "XSellTail",
            "-t",
            "0",
            "1440",
            "-O",
            "60",
            "-x",
            "1",
            "2",
            "-q",
            "1",
            "-T",
            "0.5",
            "-o",
            "L",
            "-y",
            "S-",
            "-c",
            "sell",
            "-a",
            "qAt%pD",
            "--dry-run",
        ]
    )

    strategy = build_single_strategy(args)
    assert isinstance(strategy, StrategySpec)
    assert len(strategy.pairs) == 1
    pair = strategy.pairs[0]

    assert pair.name == "XSellTail"
    assert pair.window.start_minutes == 0.0
    assert pair.window.end_minutes == 1440.0
    assert pair.timeout == 60
    assert pair.head_price == (1.0, 2.0)
    assert pair.head_quantity == 1
    assert pair.tail_price_spec == 0.5
    assert pair.head.order_type == "L"
    assert pair.tail.order_type == "S-"
    assert pair.head.side.value == "sell"
    assert pair.amount_type == "qAt%pD"


def test_run_once_parser_keeps_percent_tail_percent_head_grammar() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "run-once",
            "-m",
            "XSellPercentTail",
            "-x",
            "-1",
            "1",
            "-q",
            "1",
            "-T",
            "0.5",
            "-o",
            "M",
            "-y",
            "S-",
            "-c",
            "sell",
            "-a",
            "qAt%p%",
            "--dry-run",
        ]
    )

    pair = build_single_strategy(args).pairs[0]

    assert pair.head_quantity_type == "qA"
    assert pair.tail_price_spec_type == "t%"
    assert pair.head_price_type == "p%"
    assert pair.tail_price_spec == 0.5


def test_bot_parser_uses_critical_db_url_only() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "run",
            "--strategy",
            "orders/advers.tsv",
            "--critical-db-url",
            "sqlite:///critical.sqlite",
            "--audit-db-url",
            "sqlite:///audit.sqlite",
            "--telemetry-db-url",
            "sqlite:///telemetry.sqlite",
            "--rest-audit-retention-minutes",
            "60",
            "--rest-audit-retention-limit",
            "10",
            "--tail-telemetry-retention-minutes",
            "30",
            "--tail-telemetry-retention-limit",
            "5",
            "--account-scope",
            "advers",
            "--dry-run",
        ]
    )

    assert args.critical_account_db_url == "sqlite:///critical.sqlite"
    assert args.audit_db_url == "sqlite:///audit.sqlite"
    assert args.telemetry_db_url == "sqlite:///telemetry.sqlite"
    assert args.rest_audit_retention_minutes == 60
    assert args.rest_audit_retention_limit == 10
    assert args.tail_telemetry_retention_minutes == 30
    assert args.tail_telemetry_retention_limit == 5
    assert args.account_scope == "advers"
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "run",
                "--strategy",
                "orders/advers.tsv",
                "--critical-account-db-url",
                "sqlite:///critical.sqlite",
            ]
        )


def test_preflight_parser_accepts_account_scope() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "preflight",
            "--account-scope",
            "advers",
            "--critical-db-url",
            "postgresql+psycopg://x/critical",
        ]
    )

    assert args.account_scope == "advers"
    assert args.critical_account_db_url == "postgresql+psycopg://x/critical"


def test_run_once_command_dry_run_prints_canonical_structure(capsys) -> None:
    args = argparse.Namespace(
        name="XSellTail",
        tps_run=[0.0, 1440.0],
        nbEssais=1,
        drPause=None,
        tOut=60,
        side="sell",
        prix=[1.0, 2.0],
        quantity=1,
        tailPrice=0.5,
        aType="qAt%pD",
        oType="L",
        oDelta=None,
        tDelta=None,
        tType="S-",
        Hook="",
        dry_run=True,
        sync=False,
        simulate=False,
        exchange="kraken",
        symbol="PI_XBTUSD",
        environment="demo",
        db_url=None,
        market_db_url=None,
        account_db_url=None,
        skip_ready_check=True,
        ready_timeout_seconds=45.0,
        ready_poll_seconds=1.0,
        max_public_age_seconds=15.0,
        max_private_age_seconds=30.0,
        max_reconcile_age_seconds=300.0,
        log_level="INFO",
        update_pause=10,
        log_pause=60,
    )

    exit_code = run_once_command(args)

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"name": "XSellTail"' in output
    assert '"pairs"' in output
    assert '"commands"' in output
    assert '"kind": "place"' in output


def test_run_and_run_once_share_bot_service_path(monkeypatch) -> None:
    class RecordingService:
        def __init__(self) -> None:
            self.calls: list[tuple[StrategySpec, bool, bool]] = []

        def run_strategy(self, strategy: StrategySpec, *, dry_run: bool, simulate: bool):
            from datetime import datetime, timezone

            from kolabi.bot.domain import PairCycleState, StrategyState
            from kolabi.bot.strategy_runtime import StrategyRunResult

            self.calls.append((strategy, dry_run, simulate))
            state = StrategyState(
                launched_at=datetime.now(timezone.utc),
                pairs={pair.name: PairCycleState(pair=pair) for pair in strategy.pairs},
            )
            return StrategyRunResult(state=state, commands=(), notices=())

    service = RecordingService()
    strategy = read_strategy_file("orders/pi_xbtusd_sell_plus1_tail_0p5.tsv")

    monkeypatch.setattr("kolabi.bot.__main__.build_service", lambda _args: service)
    monkeypatch.setattr("kolabi.bot.__main__.read_strategy_file", lambda _path: strategy)

    run_args = argparse.Namespace(
        strategy="orders/demo_ada.tsv",
        dry_run=False,
        sync=True,
        simulate=False,
    )
    run_once_args = argparse.Namespace(
        name="XSellTail",
        tps_run=[0.0, 1440.0],
        nbEssais=1,
        drPause=None,
        tOut=60,
        side="sell",
        prix=[1.0, 2.0],
        quantity=1,
        tailPrice=0.5,
        aType="qAt%pD",
        oType="L",
        oDelta=None,
        tDelta=None,
        tType="S-",
        Hook="",
        dry_run=False,
        sync=True,
        simulate=False,
    )

    assert run_command(run_args) == 0
    assert run_once_command(run_once_args) == 0
    assert len(service.calls) == 2
    assert len(service.calls[0][0].pairs) == 1
    assert len(service.calls[1][0].pairs) == 1
    assert service.calls[0][1] is False
    assert service.calls[1][1] is False
    assert service.calls[0][2] is False
    assert service.calls[1][2] is False


def test_run_once_returns_130_on_keyboard_interrupt(monkeypatch, capsys) -> None:
    class InterruptingService:
        def run_strategy(self, strategy: StrategySpec, *, dry_run: bool, simulate: bool):
            del strategy, dry_run, simulate
            raise KeyboardInterrupt

    monkeypatch.setattr("kolabi.bot.__main__.build_service", lambda _args: InterruptingService())
    args = argparse.Namespace(
        name="XSellTail",
        tps_run=[0.0, 1440.0],
        nbEssais=1,
        drPause=None,
        tOut=60,
        side="sell",
        prix=[1.0, 2.0],
        quantity=1,
        tailPrice=0.5,
        aType="qAt%pD",
        oType="L",
        oDelta=None,
        tDelta=None,
        tType="S-",
        Hook="",
        dry_run=False,
        sync=True,
        simulate=False,
    )
    exit_code = run_once_command(args)
    assert exit_code == 130
    assert "Interrupted by operator." in capsys.readouterr().out
