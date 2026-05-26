from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from kolabi.bot.domain import HeadSpec, OrderPairSpec, Side, TailSpec, TimeWindow
from kolabi.bot.tail_tracking import initial_tail_trail, step_tail_trail


def sample_pair(*, side: Side, tail: float = 1.0, tail_type: str = "t%") -> OrderPairSpec:
    return OrderPairSpec(
        name="pair-a",
        window=TimeWindow(start_minutes=-1.0, end_minutes=10.0),
        try_num=1,
        dr_pause=None,
        timeout=60,
        head=HeadSpec(side=side, order_type="Limit"),
        head_price=(100.0, 101.0),
        head_price_type="pA",
        head_quantity=2,
        head_quantity_type="qA",
        tail=TailSpec(side=Side.SELL if side == Side.BUY else Side.BUY, order_type="Stop"),
        tail_price_spec=tail,
        tail_price_spec_type=tail_type,
        amount_type=f"qA{tail_type}pD",
    )


def test_buy_head_unfavourable_or_too_small_move_keeps_stop() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.BUY)
    trail = initial_tail_trail(pair, Decimal("100"), now)

    down = step_tail_trail(pair, trail, Decimal("99"), now + timedelta(seconds=1))
    small_up = step_tail_trail(pair, trail, Decimal("100.5"), now + timedelta(seconds=2))

    assert down.current_stop_price == Decimal("99")
    assert small_up.current_stop_price == Decimal("99")


def test_buy_head_favourable_move_past_entry_moves_stop_up() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.BUY)
    trail = initial_tail_trail(pair, Decimal("100"), now)

    moved = step_tail_trail(pair, trail, Decimal("102"), now + timedelta(seconds=1))

    assert moved.previous_stop_price == Decimal("99")
    assert moved.current_stop_price > Decimal("100")


def test_sell_head_unfavourable_or_too_small_move_keeps_stop() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.SELL)
    trail = initial_tail_trail(pair, Decimal("100"), now)

    up = step_tail_trail(pair, trail, Decimal("101"), now + timedelta(seconds=1))
    small_down = step_tail_trail(pair, trail, Decimal("99.5"), now + timedelta(seconds=2))

    assert up.current_stop_price == Decimal("101")
    assert small_down.current_stop_price == Decimal("101")


def test_sell_head_favourable_move_past_entry_moves_stop_down() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.SELL)
    trail = initial_tail_trail(pair, Decimal("100"), now)

    moved = step_tail_trail(pair, trail, Decimal("98"), now + timedelta(seconds=1))

    assert moved.previous_stop_price == Decimal("101")
    assert moved.current_stop_price < Decimal("100")


def test_fast_favourable_move_shortens_width_without_widening() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.BUY)
    trail = initial_tail_trail(pair, Decimal("100"), now)
    with_history = step_tail_trail(pair, trail, Decimal("100"), now + timedelta(seconds=1))

    moved = step_tail_trail(pair, with_history, Decimal("110"), now + timedelta(seconds=70))

    assert Decimal("0") < moved.current_stop_price - Decimal("100")
    assert Decimal("110") - moved.current_stop_price <= trail.baseline_width


def test_tail_tracking_handles_mixed_naive_and_aware_timestamps() -> None:
    aware = datetime.now(timezone.utc)
    naive = aware.replace(tzinfo=None)
    pair = sample_pair(side=Side.BUY)
    trail = initial_tail_trail(pair, Decimal("100"), aware)

    moved = step_tail_trail(pair, trail, Decimal("102"), naive + timedelta(seconds=1))
    moved_again = step_tail_trail(
        pair,
        moved,
        Decimal("103"),
        aware + timedelta(seconds=61),
    )

    assert moved_again.current_stop_price >= moved.current_stop_price
