from __future__ import annotations

import argparse

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
    )

    exit_code = run_once_command(args)

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"name": "XSellTail"' in output
    assert '"head_price_spec"' in output
    assert '"tail_price_spec": 0.5' in output
    assert '"pairs"' in output


def test_run_and_run_once_share_bot_service_path(monkeypatch) -> None:
    class RecordingService:
        def __init__(self) -> None:
            self.calls: list[tuple[StrategySpec, bool]] = []

        def run_strategy(self, strategy: StrategySpec, *, asynchronous: bool) -> None:
            self.calls.append((strategy, asynchronous))

    service = RecordingService()
    strategy = read_strategy_file("orders/pi_xbtusd_sell_plus1_tail_0p5.tsv")

    monkeypatch.setattr("kolabi.bot.__main__.build_service", lambda _args: service)
    monkeypatch.setattr("kolabi.bot.__main__.read_strategy_file", lambda _path: strategy)

    run_args = argparse.Namespace(
        strategy="orders/demo_ada.tsv",
        dry_run=False,
        sync=True,
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
    )

    assert run_command(run_args) == 0
    assert run_once_command(run_once_args) == 0
    assert len(service.calls) == 2
    assert len(service.calls[0][0].pairs) == 1
    assert len(service.calls[1][0].pairs) == 1
    assert service.calls[0][1] is False
    assert service.calls[1][1] is False
