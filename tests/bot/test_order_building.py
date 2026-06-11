from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from kolabi.bot.domain import PairCycleState
from kolabi.bot.order_building import head_command, tail_command
from kolabi.bot.tail_tracking import initial_tail_trail
from kolabi.bot.tsv.parser import read_strategy_file
from kolabi.shared.core.runtime_types import RuntimeCommandKind, Symbol


def _write_strategy(path: Path, row: str) -> Path:
    path.write_text(
        "\n".join(
            [
                (
                    "name\tsymbol\ttps_run\tessais\tpause\ttOut\tside\toType\toDelta"
                    "\ttType\ttDelta\tatype\tqty\ttp\tprix\thook\texchg"
                ),
                row,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_tsv_exchange_route_reaches_head_and_tail_commands(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "route.tsv",
            (
                "BTX_SPOT\tXBT_USDT\t0 60\t1\t\t4\tbuy\tL\t\tS\t\tqAtApD\t3"
                "\t8\t- -5\t\tBTXS"
            ),
        )
    )
    pair = strategy.pairs[0]
    state = PairCycleState(pair=pair)

    head = head_command(
        state,
        symbol=Symbol(pair.symbol or ""),
        kind=RuntimeCommandKind.PLACE,
    )
    tail = tail_command(
        state,
        symbol=Symbol(pair.symbol or ""),
        kind=RuntimeCommandKind.PLACE,
    )

    assert (head.exchange, head.market_type, head.symbol) == (
        "bitmex",
        "spot",
        "XBT_USDT",
    )
    assert (tail.exchange, tail.market_type, tail.symbol) == (
        "bitmex",
        "spot",
        "XBT_USDT",
    )


def test_post_only_zero_distance_limit_tail_materialises_marketable_price(
    tmp_path: Path,
) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "terminal_tail.tsv",
            (
                "BIN_TERM\tADAUSDT\t0 60\t1\t\t4\tbuy\tM\t\tL!\t\t"
                "qAtDpD\t3\t0\t- +\tPARENT-tail-closed\tBINS"
            ),
        )
    )
    pair = strategy.pairs[0]
    state = PairCycleState(
        pair=pair,
        played_quantity=Decimal("3"),
        tail_trail=initial_tail_trail(
            pair,
            Decimal("100.00"),
            datetime(2026, 6, 10, tzinfo=timezone.utc),
        ),
        instrument_tick_size=Decimal("0.01"),
    )

    command = tail_command(
        state,
        symbol=Symbol("ADAUSDT"),
        kind=RuntimeCommandKind.PLACE,
    )

    assert command.request.ordType == "L"
    assert command.request.execInst == "ParticipateDoNotInitiate"
    assert command.request.price == Decimal("99.99")
    assert command.request.stopPx is None
    assert command.legacy_order is not None
    assert command.legacy_order["price"] == Decimal("99.99")
