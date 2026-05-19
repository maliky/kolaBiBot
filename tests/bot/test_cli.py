from __future__ import annotations

import argparse

from kolabi.bot.__main__ import (
    build_parser,
    build_single_order_spec,
    run_command,
    run_once_command,
)
from kolabi.bot.tsv import OrderSpec


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
            "1",
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

    spec = build_single_order_spec(args)

    assert spec.name == "XSellTail"
    assert spec.tps_run == (0.0, 1440.0)
    assert spec.timeout == 60
    assert spec.prix == (1.0, 1.0)
    assert spec.q == 1
    assert spec.tp == 0.5
    assert spec.oType == "L"
    assert spec.tType == "S-"
    assert spec.side == "sell"
    assert spec.atype == "qAt%pD"


def test_run_once_command_dry_run_prints_summary(capsys) -> None:
    args = argparse.Namespace(
        name="XSellTail",
        tps_run=[0.0, 1440.0],
        nbEssais=1,
        drPause=None,
        tOut=60,
        side="sell",
        prix=[1.0, 1.0],
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
    assert "[DRY-RUN] XSellTail: sell 1" in output
    assert "type=L tail=S-" in output
    assert "atype=qAt%pD tp=0.5" in output


def test_run_and_run_once_share_bot_service_path(monkeypatch) -> None:
    class RecordingService:
        def __init__(self) -> None:
            self.calls: list[tuple[list[OrderSpec], bool]] = []

        def run_orders(self, specs: list[OrderSpec], *, asynchronous: bool) -> None:
            self.calls.append((specs, asynchronous))

    service = RecordingService()
    spec = OrderSpec(
        name="XSellTail",
        tps_run=(0.0, 60.0),
        essais=1,
        dr_pause=None,
        timeout=60,
        side="sell",
        prix=(1.0, 2.0),
        q=1,
        tp=0.5,
        atype="qAt%pD",
        oType="L",
        oDelta=None,
        tDelta=None,
        tType="S-",
        hook="",
    )

    monkeypatch.setattr("kolabi.bot.__main__.build_service", lambda _args: service)
    monkeypatch.setattr("kolabi.bot.__main__.read_strategy_file", lambda _path: [spec])

    run_args = argparse.Namespace(
        strategy="Orders/demo_ada.tsv",
        dry_run=False,
        sync=True,
    )
    run_once_args = argparse.Namespace(
        name=spec.name,
        tps_run=list(spec.tps_run),
        nbEssais=spec.essais,
        drPause=spec.dr_pause,
        tOut=spec.timeout,
        side=spec.side,
        prix=list(spec.prix),
        quantity=spec.q,
        tailPrice=spec.tp,
        aType=spec.atype,
        oType=spec.oType,
        oDelta=spec.oDelta,
        tDelta=spec.tDelta,
        tType=spec.tType,
        Hook=spec.hook,
        dry_run=False,
        sync=True,
    )

    assert run_command(run_args) == 0
    assert run_once_command(run_once_args) == 0
    assert len(service.calls) == 2
    assert len(service.calls[0][0]) == 1
    assert len(service.calls[1][0]) == 1
    assert service.calls[0][1] is False
    assert service.calls[1][1] is False
