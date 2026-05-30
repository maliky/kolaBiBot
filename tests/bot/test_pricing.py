from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal

from kolabi.bot.domain import (
    HeadSpec,
    OrderPairSpec,
    PairCycleState,
    Side,
    TailSpec,
    TimeWindow,
)
from kolabi.bot.dragon import MarketSnapshotFact, head_hooked_from_market_snapshot
from kolabi.bot.pricing import (
    executable_head_reference_price,
    pair_window_has_ended,
    pair_window_is_open,
)


def _pair(order_type: str) -> OrderPairSpec:
    return OrderPairSpec(
        name="pair-a",
        window=TimeWindow(start_minutes=0, end_minutes=60),
        try_num=1,
        dr_pause=None,
        timeout=4,
        head=HeadSpec(side=Side.BUY, order_type=order_type),
        head_price=(-5.0, -3.0),
        head_price_type="pD",
        head_quantity=3,
        head_quantity_type="qA",
        tail=TailSpec(side=Side.SELL, order_type="S-"),
        tail_price_spec=8,
        tail_price_spec_type="tD",
        amount_type="qAtDpD",
    )


@dataclass(frozen=True)
class _Market:
    best_bid: float | None
    best_ask: float | None
    mid_price: float | None
    last_price: float | None = None
    mark_price: float | None = None
    index_price: float | None = None


def test_head_limit_mark_suffix_uses_mark_reference() -> None:
    source, reference = executable_head_reference_price(
        _pair("Lm"),
        _Market(
            best_bid=99.0,
            best_ask=101.0,
            mid_price=100.0,
            mark_price=120.0,
        ),
    )

    assert source == "mark"
    assert reference == 120.0


def test_head_limit_suffix_drives_price_condition() -> None:
    pair = _pair("Lm")
    now = datetime.now(timezone.utc)
    move = head_hooked_from_market_snapshot(
        pair_state=PairCycleState(
            pair=pair,
            head_trigger_reference_price=Decimal("120"),
        ),
        launched_at=now,
        snapshot=MarketSnapshotFact(
            symbol="PI_XBTUSD",
            best_bid=90.0,
            best_ask=91.0,
            mid_price=90.5,
            mark_price=116.0,
            occurred_at=now,
        ),
    )

    assert move is not None
    assert move.reply is not None
    assert move.reply["reference_source"] == "mark"


def test_head_limit_hook_carries_materialised_order_price() -> None:
    pair = _pair("L")
    now = datetime.now(timezone.utc)
    move = head_hooked_from_market_snapshot(
        pair_state=PairCycleState(
            pair=pair,
            head_trigger_reference_price=Decimal("100"),
        ),
        launched_at=now,
        snapshot=MarketSnapshotFact(
            symbol="PI_XBTUSD",
            best_bid=95.0,
            best_ask=96.0,
            mid_price=95.5,
            occurred_at=now,
        ),
    )

    assert move is not None
    assert move.reply is not None
    assert move.reply["head_order_price"] == 95.0


def test_head_stop_hook_carries_materialised_stop_price() -> None:
    pair = replace(
        _pair("Sm"),
        head=HeadSpec(side=Side.SELL, order_type="Sm"),
        head_price=(3.0, 5.0),
    )
    now = datetime.now(timezone.utc)
    move = head_hooked_from_market_snapshot(
        pair_state=PairCycleState(
            pair=pair,
            head_trigger_reference_price=Decimal("100"),
        ),
        launched_at=now,
        snapshot=MarketSnapshotFact(
            symbol="PI_XBTUSD",
            best_bid=103.0,
            best_ask=104.0,
            mid_price=103.5,
            mark_price=104.0,
            occurred_at=now,
        ),
    )

    assert move is not None
    assert move.reply is not None
    assert move.reply["head_order_stop_price"] == 104.0


def test_pair_window_accepts_mixed_naive_and_aware_datetimes() -> None:
    launched_at = datetime(2026, 5, 30, 21, 0, tzinfo=timezone.utc)
    naive_now = datetime(2026, 5, 30, 21, 30)

    assert pair_window_is_open(
        _pair("M"),
        launched_at=launched_at,
        now=naive_now,
    )


def test_pair_window_end_accepts_mixed_naive_and_aware_datetimes() -> None:
    launched_at = datetime(2026, 5, 30, 21, 0, tzinfo=timezone.utc)
    naive_now = datetime(2026, 5, 30, 22, 1)

    assert pair_window_has_ended(
        _pair("M"),
        launched_at=launched_at,
        now=naive_now,
    )
