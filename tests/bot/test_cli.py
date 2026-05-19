from __future__ import annotations

import argparse

from kolabi.bot.__main__ import (
    build_parser,
    build_single_order_spec,
    run_once_command,
)


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
